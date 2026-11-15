"""The factory's state is on the git branch alone: no `[state]`, concurrent pods, resume, cutover."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _factory,
    _factory_base,
    _factory_recreate,
    _git,
    _profile,
    _queue,
    _state,
    _state_store,
    _timeline,
)
from tests.conftest import TEST_PROFILE
from tests.fake_flyte import status_report
from tests.state_git import make_remote, make_store

OWN_KEY = "pods-key-id"


def profile_text(state: str = "") -> str:
    return TEST_PROFILE.read_text() + state


def request(name: str, at: float = 1.0, gen: int = 1, **kw: Any) -> _queue.Request:
    return _queue.Request(name, gen, "new", "brief", "opus", "r", "", {}, at, **kw)


# --- profiles: `[state] uri` is optional and ignored, `[state] branch` names the branch ---------------------


@pytest.fixture
def warnings(monkeypatch, capsys):
    """The profile's deprecation notes are printed once per process; start each test with none printed."""
    monkeypatch.setattr(_profile, "_warned", set())
    return capsys


def test_a_profile_without_state_loads_and_has_no_uri_or_branch():
    profile = _profile.parse(profile_text())
    assert (profile.state.uri, profile.state.branch) == ("", "")
    _profile.planner_ready(profile)  # no `[state]` is needed to be planner-ready


def test_an_old_profile_with_a_state_uri_loads_and_warns_once(warnings):
    text = profile_text('\n[state]\nuri = "s3://b/p"\n')
    for _ in range(3):  # a process parses its profile several times
        assert _profile.parse(text).state.uri == "s3://b/p"
    err = warnings.readouterr().err
    assert err.count("[state] uri is no longer read and is ignored") == 1
    assert "docs/state-store.md" in err


@pytest.mark.parametrize("uri", ["s3://b/p", "gs://b/p", "file:///tmp/x", "no-scheme/at/all", "http://h/p"])
def test_a_state_uri_of_any_shape_loads_because_nothing_reads_it(uri, warnings):
    assert _profile.parse(profile_text(f'\n[state]\nuri = "{uri}"\n')).state.uri == uri


def test_a_state_uri_that_is_not_a_string_is_still_refused():
    with pytest.raises(_profile.ProfileError, match=r"\[state\] uri must be a non-empty string"):
        _profile.parse(profile_text("\n[state]\nuri = 3\n"))


def test_nothing_reads_the_old_uri_whatever_the_profile_says(flyte_world, monkeypatch):
    import flyte.storage

    def no_bucket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the bucket was reached")

    monkeypatch.setattr(flyte.storage, "get_underlying_filesystem", no_bucket)
    profile = dataclasses.replace(_factory_base.PROFILE, state=_profile.State(uri="s3://b/p"))
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    for name, slot in zip(("x", "y"), _factory_base.SLOTS, strict=False):
        flyte_world.add(name, slot)
    assert _factory.launch_project("brief", "alpha")["status"] == "queued"
    assert _queue.read_reserves() == {}
    assert [r["name"] for r in _factory.queue_view()["queue"]] == ["alpha"]


def test_a_state_branch_in_the_profile_selects_the_branch(monkeypatch, tmp_path):
    assert _profile.parse(profile_text('\n[state]\nbranch = "x/y"\n')).state.branch == "x/y"
    remote = make_remote(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_STATE_REMOTE", str(remote))
    monkeypatch.delenv("AGENT_FACTORY_STATE_BRANCH", raising=False)
    profile = dataclasses.replace(_factory_base.PROFILE, state=_profile.State(branch="x/y"))
    monkeypatch.setattr(_profile, "current", lambda: profile)
    _state_store.configure(None)  # the suite's own store would answer otherwise
    assert _state_store.current().branch == "x/y"
    _state_store.configure(None)
    monkeypatch.setattr(_profile, "current", lambda: dataclasses.replace(profile, state=_profile.State()))
    assert _state_store.current().branch == _state_store.default_branch(
        profile.backend.project, profile.backend.domain
    )


@pytest.mark.parametrize(
    "branch", ["a..b", "/a", "a/", "a//b", "a b", "x.lock", "a:b", "a~1", "HEAD"], ids=repr
)
def test_an_unsafe_state_branch_is_a_profile_error(branch):
    with pytest.raises(_profile.ProfileError, match=r"\[state\] branch must be"):
        _profile.parse(profile_text(f'\n[state]\nbranch = "{branch}"\n'))


@pytest.mark.parametrize("name", ["ü", "a@b", "state+x", "a,b", "x#y", "a;b"], ids=repr)
def test_a_state_branch_git_accepts_loads(name):
    assert _profile.parse(profile_text(f'\n[state]\nbranch = "{name}"\n')).state.branch == name


# --- two pods dispatching at once ---------------------------------------------------------------------------


def test_two_dispatchers_on_one_remote_start_a_request_exactly_once(
    flyte_world, tmp_path, monkeypatch, capsys
):
    """Each thread is a pod: its own StateStore and cache directory, one bare remote between them."""
    remote = make_remote(tmp_path)
    local = threading.local()
    monkeypatch.setattr(_state_store, "current", lambda **_: local.store)
    for round_ in range(3):
        name = f"proj{round_}"
        local.store = make_store(tmp_path, remote=remote, name="seed", ttl=30.0)
        _queue.enqueue(flyte_world.queue, request(name))
        flyte_world.runs.clear()
        flyte_world.created.clear()
        barrier = threading.Barrier(2)
        # The winner holds the lease inside create until the loser has met it and given up.
        done = threading.Event()
        flyte_world.before_create = lambda done=done: done.wait(timeout=30)
        capsys.readouterr()

        def dispatcher(pod: str, round_=round_, barrier=barrier, done=done) -> None:
            local.store = make_store(tmp_path, remote=remote, name=f"{pod}-{round_}", ttl=30.0)
            barrier.wait(timeout=30)
            _factory.dispatch()
            done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatcher, pod) for pod in ("pod-a", "pod-b")]
            for future in futures:
                future.result(timeout=120)  # a dispatcher's exception is raised here
        assert capsys.readouterr().out.count("[queue] dispatch skipped: the lease is held by") == 1
        local.store = make_store(tmp_path, remote=remote, name="reader", ttl=0.0)
        keys = _queue._fs(flyte_world.queue).find(f"{flyte_world.queue}/{name}")
        assert [k.rsplit("/", 1)[1] for k in keys if k.endswith(".started")] == ["0001.started"]
        assert len(flyte_world.created) == 1
        assert [e.closed for e in _queue.entries(flyte_world.queue, name)] == ["started"]


# --- the pod side of the checkpoint pointer, and the factory side of it -------------------------------------


def test_every_report_flush_carries_the_checkpoint_location_and_the_key_id(monkeypatch):
    monkeypatch.setattr(_state, "checkpoint_location", lambda: "s3://b/run/checkpoint")
    monkeypatch.setattr("flyte_agent_factory._timeline._auth.own_client_id", lambda: OWN_KEY)
    data = _timeline._Timeline().snapshot()
    assert (data["checkpoint"], data["app"]) == ("s3://b/run/checkpoint", OWN_KEY)
    from flyte_agent_factory import _dashboard

    page = _timeline._Timeline().render()
    assert _dashboard.from_report(page) is not None  # the blob a reader finds in the report is the snapshot


def _abort_own_run(world: Any, report: Any) -> None:
    _queue.enqueue(world.queue, request("alpha"))
    _queue.mark(world.queue, "alpha", 1, "started", run="alpha-run")
    world.add("other", _factory_base.SLOTS[1])
    world.add(
        "alpha-run",
        _factory_base.SLOTS[0],
        labels={_factory_base.LABEL_PROJECT: "alpha", _factory_base.LABEL_GEN: "1"},
        phase="ABORTED",
        aborted_by=OWN_KEY,
        report=report,
    )


def test_a_run_aborted_by_its_own_key_is_reopened_as_the_next_attempt_and_loads_its_checkpoint(
    flyte_world, monkeypatch
):
    asked: list[str] = []

    def load(location: str) -> str:
        asked.append(location)
        return "ws"

    monkeypatch.setattr(_factory_recreate, "_load_checkpoint_workspace", load)
    _abort_own_run(flyte_world, status_report(checkpoint="s3://b/cp", app=OWN_KEY))
    [started] = [*_factory.dispatch()] or _sweep_once()
    assert re.fullmatch(r"alpha-1-1-[0-9a-z]{6}", started)
    assert asked == ["s3://b/cp"]
    assert _queue.marker(flyte_world.queue, "alpha", 1, "attempt-1")["recreated_from"] == "alpha-run"
    assert flyte_world.created[0]["inputs"]["workspace_in"] == "ws"


def _sweep_once() -> list[str]:
    _factory_recreate.recreate_aborted()
    return list(_factory.dispatch())


@pytest.mark.parametrize(
    "report",
    [None, "", "<div>no snapshot</div>", status_report(), status_report(checkpoint="s3://b/cp")],
    ids=["unreadable", "empty", "no-blob", "no-fields", "no-app"],
)
def test_without_the_report_fields_an_abort_by_the_pods_key_is_final(flyte_world, monkeypatch, report):
    monkeypatch.setattr(_factory_recreate, "_load_checkpoint_workspace", lambda location: "ws")
    _abort_own_run(flyte_world, report)
    assert _sweep_once() == []
    assert flyte_world.created == []


# --- chat's views read what the pods wrote ------------------------------------------------------------------


def test_queue_list_and_status_show_state_written_by_another_pod(flyte_world, tmp_path):
    other = make_store(tmp_path, remote=Path(flyte_world_remote(flyte_world)), name="other-pod", ttl=0.0)
    flyte_world.add(
        "alpha-run",
        _factory_base.SLOTS[0],
        labels={_factory_base.LABEL_PROJECT: "alpha", _factory_base.LABEL_GEN: "1"},
        report=status_report(phase={"kind": "building"}, project="alpha", generated_at=60),
    )
    flyte_world.add("busy", _factory_base.SLOTS[1])
    # Written through the other pod's store only; this pod's cache has never seen these files.
    other.write(
        {
            "queue/alpha/0002.json": request("alpha", at=5.0, gen=2, reason="token rotated").to_json(),
            "queue/bravo/0001.json": request("bravo", at=6.0).to_json(),
            "waits.json": json.dumps(
                {"bravo": _queue.describe_wait(1, "no_free_slot", "every slot is busy")}
            ).encode(),
        },
        "pod writes",
    )
    listed = json.loads(asyncio.run(_factory.queue_list.func()))
    assert [(r["name"], r.get("wait_reason")) for r in listed["queue"]] == [
        ("alpha", "waiting for alpha's current run to finish"),
        ("bravo", "no_free_slot: every slot is busy"),
    ]
    status = json.loads(asyncio.run(_factory.status.func("alpha")))
    assert status["queued"] == {"reason": "token rotated", "waiting_for": status["queued"]["waiting_for"]}
    asyncio.run(_factory.set_reserve.func("claude", 5.0))
    other_read = make_store(tmp_path, remote=Path(flyte_world_remote(flyte_world)), name="reader", ttl=0.0)
    assert json.loads(other_read.read("reserve.json") or b"{}") == {"claude": 5.0}
    assert json.loads(asyncio.run(_factory.get_reserve.func())) == {"claude": 5.0}


def flyte_world_remote(_world: Any) -> str:
    """The bare repository behind the suite's store."""
    return str(_state_store.current().remote)


# --- a project pod's deploy key is chosen by the project's clone, never by the state store ------------------


def test_the_state_store_leaves_the_project_pods_key_alone(tmp_path, monkeypatch):
    """A pod on a `[repo_keys]` repository mounts its own key beside the default. Whichever
    `install_deploy_key` call comes first picks the pod's only key, so the store must never be first: the
    project's clone (`_session_setup._export_runtime_env`) goes before any store call (`agent.run`), and
    the store's own call then finds a key installed and changes nothing."""
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(base))
    monkeypatch.setenv("GIT_DEPLOY_KEY", "DEFAULT-KEY")
    monkeypatch.setenv("GIT_DEPLOY_KEY_FACTORY", "FACTORY-KEY")
    project_repo = "git@example.org:team/engine.git"
    monkeypatch.setenv(_git.REPO_KEYS_ENV_VAR, json.dumps({project_repo: "GIT_DEPLOY_KEY_FACTORY"}))
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)

    _git.install_deploy_key(project_repo)  # the project's clone
    key_file = base / ".ssh" / "deploy_key"
    ssh_command = os.environ["GIT_SSH_COMMAND"]
    assert key_file.read_text().strip() == "FACTORY-KEY"

    store = make_store(tmp_path, name="pod")  # a fetch and a push, each calling install_deploy_key(remote)
    store.write({"queue/a/0001.json": b"{}"}, "write")
    assert store.read("queue/a/0001.json", fresh=True) == b"{}"
    assert key_file.read_text().strip() == "FACTORY-KEY"
    assert os.environ["GIT_SSH_COMMAND"] == ssh_command


def test_a_state_error_is_one_of_the_errors_best_effort_paths_survive():
    from flyte_agent_factory import _errors

    assert issubclass(_state_store.StateError, _errors.REMOTE_ERRORS)


def test_the_state_environment_names_are_registered_and_documented():
    from flyte_agent_factory import _names

    doc = (Path(__file__).resolve().parents[1] / "docs" / "state-store.md").read_text()
    for name in ("AGENT_FACTORY_STATE_REMOTE", "AGENT_FACTORY_STATE_BRANCH"):
        assert name in _names.POD_FIXED and name in doc


# --- generations after a cutover: the bucket-era code and the new code never label a run alike ---------


def _migrated(gens: dict[str, int]) -> None:
    _state_store.current().write({"migration.json": json.dumps({"gens": gens}).encode()}, "migrate")


def test_the_cutover_floor_is_set_only_by_a_migration():
    assert _queue.cutover_floor("gamma") == 0
    _migrated({"gamma": 2})
    assert _queue.cutover_floor("gamma") == 2 + _queue.CUTOVER_GAP
    assert _queue.cutover_floor("never-in-the-bucket") == _queue.CUTOVER_GAP


def test_a_launch_after_the_migration_is_numbered_above_the_bucket_era(flyte_world):
    """The old code queued and ran gamma gen 2 after the first pass; a new launch must not take its run."""
    _migrated({"gamma": 1})
    flyte_world.add(
        "gamma-2-0-bbbbbb",
        _factory_base.SLOTS[0],
        phase="SUCCEEDED",
        labels={_factory_base.LABEL_PROJECT: "gamma", _factory_base.LABEL_GEN: "2"},
    )
    result = _factory.launch_project("new brief", "gamma")
    assert result["gen"] == 1 + _queue.CUTOVER_GAP + 1
    assert result["status"] == "started" and result["run"] != "gamma-2-0-bbbbbb"
    assert len(flyte_world.created) == 1


def test_a_generation_a_server_run_carries_is_not_reused(flyte_world):
    """Without a queue entry for it (not migrated yet), a labelled run still reserves its generation."""
    flyte_world.add(
        "gamma-4-0-cccccc",
        _factory_base.SLOTS[0],
        phase="SUCCEEDED",
        labels={_factory_base.LABEL_PROJECT: "gamma", _factory_base.LABEL_GEN: "4"},
    )
    result = _factory.launch_project("new brief", "gamma")
    assert result["gen"] == 5
    assert result["run"] != "gamma-4-0-cccccc" and len(flyte_world.created) == 1
