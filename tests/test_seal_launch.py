"""A sealed launch: spec on the command line -> Request -> run input, and nowhere else."""

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flyte.io import Dir

from flyte_agent_factory import _factory, _factory_base, _profile, _queue
from flyte_agent_factory._profile_policy import Seal
from tests._seal_fakes import SPEC
from tests.test_queue import request

OPS = Path(__file__).resolve().parents[1] / "ops"
WORK = Path(__file__).resolve().parent / "profiles" / "work.toml"


def _load_operate():
    spec = importlib.util.spec_from_file_location("operate", OPS / "operate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operate = _load_operate()

SPEC_TOML = """
task_repo = "https://github.com/example/task.git"
base_sha = "{sha}"
index_cutoff = "2025-03-01"
task_project = "taskproj"
""".format(sha="a" * 40)


@pytest.fixture(autouse=True)
def watcher(monkeypatch):
    stub = SimpleNamespace(LOG="log", ensure_running=lambda: 42)
    monkeypatch.setitem(sys.modules, "watch", stub)


@pytest.fixture
def seal_file(tmp_path):
    path = tmp_path / "seal.toml"
    path.write_text(SPEC_TOML)
    return path


@pytest.fixture
def brief_file(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text("fix the bug in {name}")
    return path


@pytest.fixture
def bench_profile(monkeypatch):
    profile = dataclasses.replace(_factory_base.PROFILE, seal=Seal(workspace_repo="owner/bench-ws"))
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    return profile


def _args(brief, seal=""):
    return SimpleNamespace(brief=str(brief), name="proj", demo="", seal=str(seal))


def test_request_round_trips_with_and_without_a_seal():
    plain = request("alpha")
    assert plain.seal == "" and _queue.Request.from_json(plain.to_json()) == plain
    sealed = request("beta", seal=SPEC.to_json())
    assert _queue.Request.from_json(sealed.to_json()) == sealed


def test_a_request_written_before_seals_existed_still_loads():
    body = json.loads(request("alpha").to_json())
    del body["seal"]
    assert _queue.Request.from_json(json.dumps(body).encode()).seal == ""


@pytest.mark.parametrize("seal", [3, "not json", '{"task_repo": "x"}'])
def test_a_request_with_a_bad_seal_is_not_a_request(seal):
    body = json.loads(request("alpha").to_json())
    body["seal"] = seal
    with pytest.raises(ValueError, match="not a launch request"):
        _queue.Request.from_json(json.dumps(body).encode())


def test_operate_launch_fails_fast_on_a_bad_seal_file(flyte_world, brief_file, tmp_path, bench_profile):
    bad = tmp_path / "bad.toml"
    bad.write_text('task_repo = "ftp://x"\nbase_sha = "abc"\nindex_cutoff = "2025-03-01"\n')
    with pytest.raises(ValueError, match="base_sha"):
        operate.launch(_args(brief_file, bad))
    assert flyte_world.created == []
    assert _queue.pending(flyte_world.queue) == []


def test_a_sealed_launch_without_workspace_repo_refuses_naming_the_key(flyte_world, brief_file, seal_file):
    assert _factory_base.PROFILE.seal.workspace_repo == ""
    with pytest.raises(ValueError, match=r"\[seal\] workspace_repo"):
        operate.launch(_args(brief_file, seal_file))
    assert flyte_world.created == []
    assert _queue.pending(flyte_world.queue) == []


def test_a_sealed_launch_runs_in_the_bench_repo_and_carries_the_spec(
    flyte_world, brief_file, seal_file, bench_profile
):
    operate.launch(_args(brief_file, seal_file))
    inputs = flyte_world.created[0]["inputs"]
    spec = json.loads(inputs["seal"])
    assert spec["push_remote"] == "https://github.com/owner/bench-ws.git"
    assert spec["task_dir"] == "repo" and spec["base_sha"] == "a" * 40
    assert inputs["repo"] == "https://github.com/owner/bench-ws.git"
    started = _queue.entries(flyte_world.queue, "proj")[0]
    loaded = _queue.load(flyte_world.queue, started)
    assert loaded.request is not None
    assert loaded.request.seal == inputs["seal"]


def test_an_unsealed_launch_passes_no_seal_input(flyte_world, brief_file):
    operate.launch(_args(brief_file))
    assert "seal" not in flyte_world.created[0]["inputs"]


def test_the_spec_must_agree_with_the_bench_repo(flyte_world, bench_profile):
    other = json.loads(SPEC.to_json())
    other["push_remote"] = "https://github.com/owner/regular-workspace.git"
    with pytest.raises(ValueError, match="push_remote"):
        _factory.launch_project("b", "proj", seal=json.dumps(other))


def test_the_intake_launch_task_has_no_seal_parameter():
    import inspect

    parameters = inspect.signature(_factory.launch.func).parameters
    assert "seal" not in parameters


def test_the_profile_reads_the_seal_table():
    text = WORK.read_text() + '\n[seal]\nworkspace_repo = "owner/bench-ws"\n'
    assert _profile.parse(text, "x.toml").seal.workspace_repo == "owner/bench-ws"
    assert _profile.parse(WORK.read_text(), "x.toml").seal.workspace_repo == ""
    for bad in ('workspace_repo = "nope"', 'workspace_repo = "a/b"\nextra = 1'):
        with pytest.raises(_profile.ProfileError, match="seal"):
            _profile.parse(WORK.read_text() + f"\n[seal]\n{bad}\n", "x.toml")


# --- a sealed rerun is a fresh project: nothing inherited -------------------------------------------------


@pytest.fixture
def spec_json(monkeypatch):
    """SPEC (the shared fake) pushes to example/bench-ws; point the profile at that repo."""
    profile = dataclasses.replace(_factory_base.PROFILE, seal=Seal(workspace_repo="example/bench-ws"))
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    return SPEC.to_json()


def _finished_run(world, name):
    _factory.launch_project("first", name)
    run = world.runs[_queue.marker(world.queue, name, 1, "started")["run"]]
    run.phase, run.outputs_ = "SUCCEEDED", ("done", Dir(path="s3://bucket/ws"))


def test_a_sealed_launch_under_a_name_with_earlier_runs_is_refused_before_queueing(flyte_world, spec_json):
    _finished_run(flyte_world, "proj")
    queued = _queue.entries(flyte_world.queue, "proj")
    with pytest.raises(ValueError, match=r"fresh project.*next_run_name"):
        _factory.launch_project("again", "proj", seal=spec_json)
    assert _queue.entries(flyte_world.queue, "proj") == queued
    assert len(flyte_world.created) == 1


def test_an_unsealed_launch_under_the_same_name_still_resumes(flyte_world, spec_json):
    _finished_run(flyte_world, "proj")
    result = _factory.launch_project("again", "proj")
    assert result["kind"] == "resume"
    assert flyte_world.created[1]["inputs"]["workspace_in"].path == "s3://bucket/ws"


def test_a_sealed_launch_under_a_new_name_is_fresh(flyte_world, spec_json):
    _finished_run(flyte_world, "proj")
    result = _factory.launch_project("again", "proj-r2", seal=spec_json)
    assert result["kind"] == "new"
    assert "workspace_in" not in flyte_world.created[1]["inputs"]


def test_a_sealed_request_never_dispatches_with_a_workspace_in(flyte_world):
    with pytest.raises(ValueError, match="inherits nothing"):
        _factory.launch_on_slot(
            _factory_base.SLOTS[0], "b", "proj", seal=SPEC.to_json(), workspace_in=Dir(path="s3://x")
        )
    assert flyte_world.created == []
