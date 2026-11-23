"""Deploy exclusion and release history use Flyte, never the old storage records."""

import hashlib
import json
import re
import time
from types import SimpleNamespace
from typing import cast

import pytest
from flyte import remote

from flyte_agent_factory import _deployer, _profile, _state_store
from flyte_agent_factory._deploy import DeployError
from flyte_agent_factory._github import GithubApp
from tests.test_deployer import NEW_SHA, OLD_SHA, SHA, _scope, both_paths
from tests.test_deployer import directed as directed
from tests.test_deployer import world as world

# These tests never reach the app: admission is decided by the stubbed listing or `_acquire_lease`.
_APP = cast("GithubApp", object())


async def test_newest_readable_deployment_skips_stand_downs(world, monkeypatch):
    def run(name, output):
        async def outputs():
            if isinstance(output, Exception):
                raise output
            return (output,)

        return SimpleNamespace(
            name=name, pb2=SimpleNamespace(labels={"deploy-sha": name}), outputs=SimpleNamespace(aio=outputs)
        )

    async def listing(**kwargs):
        assert kwargs["task_name"] == "factory-deployer.deploy_pair"
        assert kwargs["with_labels"] == _deployer.release_labels(world.profile)
        for candidate in [
            run("stand-down", {"deployed": False}),
            run("broken", OSError()),
            run("actual", {"deployed": True, "envs": ["factory"], "sha": "a" * 40, "direction_sha": ""}),
        ]:
            yield candidate

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    release = await _deployer.deployed_pair(world.profile)
    assert release is not None
    deployed, pair = release
    assert deployed.name == "actual" and pair == ("a" * 40, "")


@pytest.mark.parametrize("consistent", [True, False])
async def test_pair_exclusion_uses_recorded_probe_result(world, consistent, monkeypatch):
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    monkeypatch.setattr(_deployer, "SETTLE", 0)
    calls = []

    async def listing(**kwargs):
        for name in ["older", "younger"]:
            yield SimpleNamespace(name=name)

    def acquire(*args):
        calls.append(args)
        return False

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    monkeypatch.setattr(_deployer, "_acquire_lease", acquire)
    assert not await _deployer._admitted(world.profile, _APP, "younger")
    assert bool(calls) is not consistent


async def test_oldest_live_pair_wins_even_across_different_heads(world, monkeypatch):
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", True)
    monkeypatch.setattr(_deployer, "SETTLE", 0)

    async def listing(**kwargs):
        assert kwargs["sort_by"] == ("created_at", "asc")
        assert "deploy-sha" not in kwargs["with_labels"]
        for name in ["older", "younger"]:
            yield SimpleNamespace(name=name)

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    assert await _deployer._admitted(world.profile, _APP, "older")
    assert not await _deployer._admitted(world.profile, _APP, "younger")


@both_paths
async def test_live_pair_stops_tick(world):
    world.live_runs = ["other"]
    assert await world.tick() == "deploy in flight"
    assert not world.deployed
    assert not world.submissions


@both_paths
async def test_a_timeout_counts_without_a_failure_record(world):
    labels = _deployer._pair_labels(world.profile, "a" * 40, "")
    for _ in range(3):
        world.add_run("TIMED_OUT", labels=labels)
    assert "giving up" in await world.tick()
    assert not world.deployed and not world.state.exists()


@both_paths
async def test_attempts_before_the_latest_success_do_not_count(world):
    labels = _deployer._pair_labels(world.profile, "a" * 40, "")
    for _ in range(3):
        world.add_run("ABORTED", labels=labels)
    world.record("b" * 40)
    await world.tick()
    assert world.last["sha"] == "a" * 40


async def test_stale_repository_history_is_not_used(world):
    labels = {**_deployer._pair_labels(world.profile, "a" * 40, ""), "deploy-repo": "old-repo"}
    for _ in range(3):
        world.add_run("FAILED", labels=labels)
    world.add_run("SUCCEEDED", labels=labels, output={"deployed": True, "envs": ["factory"]})
    await world.tick()
    assert world.deployed
    assert all(request.url.path.startswith(("/repos/acme/factory", "/app/")) for request in world.gh.requests)


async def test_a_stale_queued_pair_never_fetches_the_old_commit(world, monkeypatch):
    called = []
    monkeypatch.setattr(_deployer, "_acquire_lease", lambda *args: called.append(args))
    output = await _deployer.run_pair(world.profile, world.app, "old-attempt", "b" * 40, "")
    assert output == {"deployed": False}
    assert not called and not world.fetched
    assert world.submissions[-1][0] == "factory-deployer.deploy_if_changed"


def _rival_takes_the_lease_first(monkeypatch, world, tmp_path, run: str) -> list[float]:
    """Make the next push of the lease store lose: another deployer's store (own cache) records `run` first.

    The deterministic form of two acquirers that both read the same tip. The loser's retry backoff is
    recorded, not slept: the returned list holds each delay the compare-and-swap asked for.
    """
    store = cast(_state_store.StateStore, _deployer._open_lease_store(world.profile))
    rival = _state_store.StateStore(store.remote, store.branch, tmp_path / "rival-cache.git", ttl=0.0)
    record = json.dumps({"run": run, "profile": world.profile.name, "scope": _scope()}).encode()
    real_push = _state_store.StateStore._push
    raced: list[str] = []

    def push(self, commit):
        if self is store and not raced:  # the rival's own push goes through untouched
            raced.append(commit)
            rival.write({_deployer._build_lease_key(world.profile): record}, "rival")
        return real_push(self, commit)

    monkeypatch.setattr(_state_store.StateStore, "_push", push)
    backoff: list[float] = []
    # Only the store's own clock: a global `time.sleep` spy also records every other poller in the process.
    clock = SimpleNamespace(monotonic=time.monotonic, sleep=backoff.append)
    monkeypatch.setattr("flyte_agent_factory._state_store.time", clock)
    return backoff


def _acquire(world, run):
    return _deployer._acquire_lease(world.profile, cast("GithubApp", world.app), run)


@pytest.mark.parametrize("race", [False, True])
def test_lease_create_is_exclusive(world, tmp_path, monkeypatch, race):
    backoff: list[float] = []
    if race:
        world.add_run("RUNNING", name="contender")
        backoff = _rival_takes_the_lease_first(monkeypatch, world, tmp_path, "contender")
    assert _acquire(world, "first") is not race
    assert world.lease()["run"] == ("contender" if race else "first")
    # The loser retried (one backoff, recorded instead of slept) and re-read the winner's record.
    assert len(backoff) == (1 if race else 0)


@pytest.mark.parametrize("phase", ["RUNNING", "QUEUED", "FAILED", "TIMED_OUT", "ABORTED", "SUCCEEDED"])
def test_lease_can_only_replace_an_authoritatively_terminal_holder(world, monkeypatch, phase):
    assert _acquire(world, "first")
    monkeypatch.setattr(remote.Run, "get", lambda **kw: SimpleNamespace(phase=phase))
    terminal = phase in _deployer._TERMINAL
    assert _acquire(world, "second") is terminal
    assert world.lease()["run"] == ("second" if terminal else "first")


def test_lease_reclaim_race_does_not_admit_both(world, tmp_path, monkeypatch):
    # "first" is dead and "second" and "contender" both mean to replace it; "contender" pushes first. "second"
    # re-reads the winner's record, finds it live, and stands down.
    assert _acquire(world, "first")
    phases = {"first": "ABORTED", "contender": "RUNNING"}
    monkeypatch.setattr(remote.Run, "get", lambda name: SimpleNamespace(phase=phases[name]))
    backoff = _rival_takes_the_lease_first(monkeypatch, world, tmp_path, "contender")
    assert not _acquire(world, "second")
    assert world.lease()["run"] == "contender"
    assert len(backoff) == 1


def test_unreadable_lock_holder_fails_closed(world, monkeypatch):
    assert _acquire(world, "first")

    def missing(**kwargs):
        raise OSError("Flyte unavailable")

    monkeypatch.setattr(remote.Run, "get", missing)
    with pytest.raises(OSError, match="Flyte unavailable"):
        _acquire(world, "second")
    assert world.lease()["run"] == "first"


def test_the_same_run_taking_the_lease_again_is_admitted_without_a_write(world, monkeypatch):
    assert _acquire(world, "first")
    store = cast(_state_store.StateStore, _deployer._open_lease_store(world.profile))
    pushes = store.pushes
    monkeypatch.setattr(remote.Run, "get", lambda **kw: pytest.fail("the holder is this run"))
    assert _acquire(world, "first")
    assert store.pushes == pushes


@pytest.mark.parametrize("field", ["profile", "scope"])
def test_a_lease_of_another_profile_or_scope_needs_the_operator(world, field):
    record = {"run": "x", "profile": world.profile.name, "scope": _scope(), field: "elsewhere"}
    _deployer._open_lease_store(world.profile).write(
        {_deployer._build_lease_key(world.profile): json.dumps(record).encode()}, "foreign"
    )
    with pytest.raises(DeployError, match="unrecognized deployer lock; operator resolution required"):
        _acquire(world, "first")


def test_the_lease_is_a_record_of_run_profile_and_scope_and_no_credential(world, monkeypatch):
    monkeypatch.setenv("GIT_DEPLOY_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----")  # gitleaks:allow
    assert _acquire(world, "first")
    assert world.lease() == {"run": "first", "profile": "work", "scope": _scope()}
    key = _deployer._build_lease_key(world.profile)
    digest = hashlib.sha256(f"{_scope()}/work".encode()).hexdigest()
    assert key == f"deployer-lock/{digest}"
    body = _deployer._open_lease_store(world.profile).read(key)
    assert body is not None and b"PRIVATE KEY" not in body


def test_the_lease_is_pushed_without_force(world, monkeypatch):
    pushes = []
    real_run = _state_store.StateStore._run

    def run(self, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(_state_store.StateStore, "_run", run)
    assert _acquire(world, "first")
    (push,) = pushes
    assert not any(arg.startswith(("+", "--force")) for arg in push) and ":refs/heads/" in push[-1]


@pytest.mark.parametrize("consistent", [True, False])
async def test_two_pair_attempts_produce_one_deploy(world, monkeypatch, consistent):
    import asyncio

    labels = _deployer._pair_labels(world.profile, SHA, "")
    older = world.add_run("RUNNING", labels=labels)
    younger = world.add_run("RUNNING", labels=labels)
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    results = await asyncio.gather(
        _deployer.run_pair(world.profile, world.app, older.name, SHA, ""),
        _deployer.run_pair(world.profile, world.app, younger.name, SHA, ""),
    )
    assert sum(result["deployed"] for result in results) == 1
    assert len(world.factory_calls) == 1


@both_paths
async def test_a_younger_pair_placed_after_the_older_one_deployed_stands_down(world):
    # Two ticks each submitted the same pair; the older deployed and finished before the younger
    # was placed, so the younger is admitted, and must find the pair already deployed.
    labels = _deployer._pair_labels(world.profile, SHA, "")
    older = world.add_run("RUNNING", labels=labels, inputs={"sha": SHA, "direction_sha": ""})
    younger = world.add_run("QUEUED", labels=labels, inputs={"sha": SHA, "direction_sha": ""})
    assert (await world.attempt(older)).startswith(f"deployed {SHA}")
    younger.phase = "RUNNING"
    assert await world.attempt(younger) == f"up to date at {SHA}"
    assert younger.output == {"deployed": False}
    assert len(world.factory_calls) == 1


async def test_more_stand_downs_than_a_listing_page_do_not_hide_the_release(world):
    world.record(SHA)
    stand_down = _deployer._pair_labels(world.profile, SHA, "")
    for _ in range(150):  # more than the SDK's default page of 100
        world.add_run("SUCCEEDED", labels=stand_down, output={"deployed": False})
    assert await world.tick() == f"up to date at {SHA}"
    assert not world.deployed


async def test_the_release_is_the_pair_the_run_says_it_deployed(world):
    # A run that followed a moved head is labelled with the pair it was submitted for.
    world.add_run(
        "SUCCEEDED",
        labels=_deployer._pair_labels(world.profile, OLD_SHA, ""),
        output={"deployed": True, "envs": ["factory"], "sha": SHA, "direction_sha": ""},
    )
    assert await world.tick() == f"up to date at {SHA}"


@both_paths
async def test_a_pair_that_never_left_the_spot_queue_is_not_an_attempt(world):
    labels = _deployer._pair_labels(world.profile, SHA, "")
    for _ in range(_deployer.MAX_ATTEMPTS):
        world.add_run("TIMED_OUT", labels=labels, started=False)
    assert (await world.tick()).startswith(f"deployed {SHA}")
    assert world.last["sha"] == SHA


@both_paths
async def test_a_pair_killed_after_it_started_is_an_attempt(world):
    labels = _deployer._pair_labels(world.profile, SHA, "")
    world.add_run("TIMED_OUT", labels=labels, started=False)
    for _ in range(_deployer.MAX_ATTEMPTS):
        world.add_run("TIMED_OUT", labels=labels)
    assert "giving up" in await world.tick()
    assert not world.deployed


_LABEL_VALUE = re.compile(r"([A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?)?")


@pytest.mark.parametrize("consistent", [False, True], ids=["git-ref", "listing"])
async def test_every_label_value_the_deployer_sets_is_a_kubernetes_label_value(
    directed, monkeypatch, consistent
):
    # Run labels may become pod labels: at most 63 characters, alphanumeric at both ends.
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    # A profile name may be any string, and is not itself a label value.
    text = directed.profile.text.replace('name = "work"', 'name = "Work + Ops/EU"', 1)
    directed.profile = _profile.parse(text, "work+direction")
    directed.ship_direction_profile()
    await directed.tick()
    directed.gh.head_sha = NEW_SHA
    await directed.tick()
    values = [v for labels in directed.labels for v in labels.values()]
    values += _deployer.release_labels(directed.profile).values()
    values += _deployer._pair_labels(directed.profile, SHA, "").values()
    assert len(directed.labels) == 2 and values
    for value in values:
        assert len(value) <= 63 and _LABEL_VALUE.fullmatch(value), value


def test_the_repository_label_is_a_32_character_prefix(world):
    repo = _deployer.release_labels(world.profile)["deploy-repo"]
    assert len(repo) == 32 and hashlib.sha256(b"acme/factory").hexdigest().startswith(repo)
