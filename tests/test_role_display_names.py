"""Every routed sub-agent call is named in the console by the role it played (#91).

The name is the display-only `short_name` of the one `subagent` task: `_reviewer._call_reviewer_raw`
passes it to `subagent_task.override`, which is faked here where the call is made. The real task is
used, without a cluster, to show the override changes nothing a backend identifies the task by.
"""

from __future__ import annotations

import tarfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import flyte.errors
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _contract,
    _meter,
    _review,
    _reviewer,
    _roles,
    _session_config,
    _session_setup,
    _timeline,
    _usage,
    agent,
)
from tests._seal_fakes import SPEC


class _Output:
    """A returned workspace: what a delegate's caller downloads."""

    async def download(self, path: str) -> None:
        Path(path).mkdir()


class _RecordingTask:
    """Stands in for `subagent_task`: records each override and the inputs each submission was given."""

    def __init__(self, answer: Callable[[dict[str, Any]], str] = lambda _inputs: "ok\nVERDICT: PASS"):
        self.answer = answer
        self.overrides: list[dict[str, Any]] = []
        self.inputs: list[dict[str, Any]] = []
        self.miss_on_spot = False

    def override(self, **options: Any) -> Any:
        outer = self
        self.overrides.append(options)

        class _Bound:
            async def aio(self, **inputs: Any) -> tuple[str, Any]:
                outer.inputs.append(inputs)
                if outer.miss_on_spot and options["interruptible"]:
                    raise flyte.errors.MaxQueuedTimeExceededError("never placed")
                return outer.answer(inputs), _Output()

        return _Bound()

    @property
    def short_names(self) -> list[str]:
        return [o["short_name"] for o in self.overrides]


@pytest.fixture
def task(tmp_path, monkeypatch):
    """The router's inputs as in `test_router.wired`, and the `subagent` task replaced by a recorder."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    store = _meter.MeterStore(tmp_path / "meters")
    now = time.time()
    store.update(_meter.Meter(_session_config._claude_account(), "oauth-usage", now, 50, now + 3600))
    store.update(_meter.Meter("codex", "app-server", now, 50, now + 3600))
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "_routes", [])
    for key in (
        "PLANNER",
        "DELEGATE",
        "CORRECTNESS",
        "CRAFT",
        "COMPLETENESS",
        "QA_DIFFERENT",
        "CHOSEN_AUTHOR",
    ):
        monkeypatch.delenv(f"AGENT_FACTORY_{key}_ENGINE", raising=False)
    fake = _RecordingTask()
    monkeypatch.setattr(_reviewer, "subagent_task", fake)
    return SimpleNamespace(fake=fake, store=store)


def _no_name_reaches_the_task(fake: _RecordingTask) -> None:
    """The name is the action's, not the task's: it never becomes an input of the `subagent` task."""
    assert fake.inputs, "the task was submitted"
    for inputs in fake.inputs:
        assert "action" not in inputs and "short_name" not in inputs, inputs


@pytest.mark.parametrize(
    ("purpose", "name"),
    [
        ("correctness", "judge-correctness"),
        ("completeness", "judge-completeness"),
        ("craft", "judge-craft"),
        ("planner", "plan"),
        ("arbiter", "arbitrate"),
        ("delegate", "delegate"),
        ("contract", "contract"),
        ("judgement", "judgement"),
        ("correctness-check", "correctness-check"),
        ("", "subagent"),
    ],
)
async def test_a_routed_call_is_submitted_under_its_role_name(task, purpose, name):
    """#91: every judge, planner, arbiter, check and delegate showed as `subagent` in the run's graph."""
    await _reviewer._call_reviewer(purpose=purpose, brief="b")
    assert task.fake.short_names == [name]
    _no_name_reaches_the_task(task.fake)


async def test_the_on_demand_fallback_keeps_the_role_name(task):
    """#91: a judge that misses spot placement is resubmitted on-demand under the same name."""
    task.fake.miss_on_spot = True
    await _reviewer._call_reviewer(purpose="craft", brief="b")
    assert [o["interruptible"] for o in task.fake.overrides] == [True, False]
    assert task.fake.short_names == ["judge-craft", "judge-craft"]
    _no_name_reaches_the_task(task.fake)


async def test_a_call_moved_to_the_other_engine_by_a_limit_keeps_the_role_name(task, monkeypatch):
    """#91: the retry is metered as `planner+codex`, but the console still names it by its role."""
    # As in `test_router.unmetered_claude`: no Claude meter, so the planner goes to Claude first.
    del task.store.latest["claude"]
    del task.store.readings[_session_config._claude_account()]

    def answer(inputs: dict[str, Any]) -> str:
        if inputs["engine"] == "claude":
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} claude usage limit")
        return "the plan"

    task.fake.answer = answer
    metered: list[str] = []
    real_ingest = _usage._ingest_subagent_usage

    def ingest(text: str, purpose: str, *args: Any, **kwargs: Any) -> str:
        metered.append(purpose)
        return real_ingest(text, purpose, *args, **kwargs)

    monkeypatch.setattr(_usage, "_ingest_subagent_usage", ingest)
    text, _ = await _reviewer._call_reviewer(purpose="planner", brief="b")
    assert text == "the plan"
    assert [i["engine"] for i in task.fake.inputs] == ["claude", "codex"]
    assert metered == ["planner+codex"], "the retry is metered under the suffixed purpose"
    assert task.fake.short_names == ["plan", "plan"], "the console name carries no engine suffix"
    _no_name_reaches_the_task(task.fake)


async def test_a_sealed_call_is_submitted_under_its_role_name(task, tmp_path, monkeypatch):
    """#91: a sealed project's sub-agents are named by role too, with the seal still passed."""
    monkeypatch.setattr(_session_config, "SEAL_RECORD", tmp_path / "record")
    monkeypatch.setattr(_session_config, "_seal", SPEC)
    merged: list[Any] = []

    async def merge(out: Dir, root: Path) -> None:
        merged.append(out)

    # The record merge is `test_seal_subagent`'s subject; here it only has to be reached.
    monkeypatch.setattr(_reviewer, "merge_record", merge)
    await _reviewer._call_reviewer(purpose="arbiter", brief="b")
    assert task.fake.short_names == ["arbitrate"]
    (inputs,) = task.fake.inputs
    assert inputs["seal"] == SPEC.to_json() and inputs["seal_role"]
    assert len(merged) == 1
    _no_name_reaches_the_task(task.fake)


@pytest.mark.parametrize("purpose", ["x" * 200, "correctness-" + "y" * 100, "", "craft"])
def test_an_action_name_is_never_empty_and_fits_flytes_limit(purpose):
    """#91: Flyte keeps 63 characters of a short name; an empty one would leave the action unnamed."""
    name = _roles.action_name(purpose)
    assert 0 < len(name) <= 63
    assert name == (_roles.display_role(purpose) or "subagent")[:63]


def test_the_role_name_overrides_only_the_display_name_of_the_real_task():
    """#91: one `subagent` task, whatever the role: the override must not make a different task."""
    original = agent.subagent
    bound = flyte.Timeout(max_queued_time=timedelta(minutes=5))
    named = original.override(short_name="judge-craft", interruptible=True, timeout=bound)

    assert named.short_name == "judge-craft" and original.short_name == "subagent"
    assert named.name == original.name
    assert named.image == original.image
    assert named.resources == original.resources
    assert named.secrets == original.secrets
    assert named.env_vars == original.env_vars
    assert named.parent_env == original.parent_env
    assert named.interface == original.interface


def test_a_backend_ignoring_the_short_name_sees_the_identical_task():
    """#91: serialized offline, the named task differs from the original only in `short_name`."""
    from flyte._internal.runtime.convert import generate_task_identity_hash
    from flyte._internal.runtime.task_serde import translate_task_to_wire
    from flyte.models import CodeBundle, SerializationContext

    context = SerializationContext(
        project="p",
        domain="d",
        org="o",
        version="v1",
        code_bundle=CodeBundle(tgz="s3://bucket/code.tgz", destination=".", computed_version="v1"),
        root_dir=Path(agent.__file__).resolve().parents[1],
    )
    original = agent.subagent
    named = original.override(short_name="judge-craft")
    plain_spec = translate_task_to_wire(original, context)
    named_spec = translate_task_to_wire(named, context)

    assert named_spec.short_name == "judge-craft" and plain_spec.short_name == "subagent"
    assert named_spec.task_template.id.name == plain_spec.task_template.id.name == original.name
    assert generate_task_identity_hash(named_spec.task_template) == generate_task_identity_hash(
        plain_spec.task_template
    )
    assert named_spec.task_template == plain_spec.task_template
    assert named_spec.environment == plain_spec.environment


def test_no_task_or_environment_is_registered_per_role():
    """#91: naming is display-only, so the registered tasks are exactly the ones there were before."""
    # The environment's name comes from the deployment profile (`claude-subagent-2` in a slot pod,
    # `agent-factory-subagent` under the test profile); the tasks in it are what this pins.
    env = agent.subagent_env.name
    assert set(agent.subagent_env.tasks) == {f"{env}.subagent", f"{env}.sealed_plain"}
    roles = ("judge", "plan", "arbitrate", "delegate")
    assert not any(role in name for name in agent.subagent_env.tasks for role in roles)
    assert not any(role in agent.subagent_env.name for role in roles)


# --- through the review loop's own call sites ---


#: The uploaded workspace a call site passes on: only handed to the faked task, never read.
_SNAPSHOT = cast(Dir, object())


async def _snapshot(_path: Any) -> Dir:
    return _SNAPSHOT


async def test_a_judge_is_named_by_its_judge(task):
    """#91: `_review._one_judge`'s call is the judge's, named `judge-<name>`."""
    _, passed, _ = await _review._one_judge("correctness", "i", _SNAPSHOT, "b", "", 1, _timeline._Timeline())
    assert passed
    assert set(task.fake.short_names) == {"judge-correctness"}
    _no_name_reaches_the_task(task.fake)


async def test_the_arbiter_is_named_arbitrate(task):
    """#91: `_review._arbitrate`'s call is named `arbitrate`."""
    task.fake.answer = lambda _inputs: "RULING [1]: KEEP -- the brief requires it"
    rulings, _ = await _review._arbitrate(_SNAPSHOT, "b", "a", ["conflict"], "", _timeline._Timeline())
    assert rulings == {1: "KEEP -- the brief requires it"}
    assert task.fake.short_names == ["arbitrate"]
    _no_name_reaches_the_task(task.fake)


@pytest.mark.real_planner
async def test_the_planner_is_named_plan(task, tmp_path, monkeypatch):
    """#91: `_contract._plan_next_round`'s call is named `plan`."""
    task.fake.answer = lambda _inputs: "1. fix the lock\nCONVERGING: YES"
    monkeypatch.setattr(Dir, "from_local", _snapshot)
    plan, converging, _ = await _contract._plan_next_round(tmp_path, "b", "", "- f", 1, _timeline._Timeline())
    assert plan == "1. fix the lock" and converging is True
    assert task.fake.short_names == ["plan"]
    _no_name_reaches_the_task(task.fake)


async def test_the_contract_is_named_contract(task):
    """#91: `_contract._freeze_contract`'s call keeps its purpose, `contract`, as its name."""
    task.fake.answer = lambda _inputs: "C1. The tracked file is repaired."
    assert await _contract._freeze_contract(_SNAPSHOT, "b", "a", _timeline._Timeline()) == (
        "C1. The tracked file is repaired."
    )
    assert task.fake.short_names == ["contract"]
    _no_name_reaches_the_task(task.fake)


async def test_a_delegation_is_named_delegate(task, tmp_path, monkeypatch):
    """#91: the `delegate` worker's call is named `delegate`."""
    target = tmp_path / "job"
    target.mkdir()
    with tarfile.open(target / "repo.tar.gz", "w:gz"):
        pass
    task.fake.answer = lambda _inputs: "patch ready"

    async def no_meters(_timeline: Any) -> None:
        return None

    monkeypatch.setattr(_usage, "_read_meters", no_meters)
    monkeypatch.setattr(Dir, "from_local", _snapshot)
    worker = _session_setup._delegate_worker(_timeline._Timeline())
    assert await worker(target, "do the work", "claude") == "patch ready"
    assert task.fake.short_names == ["delegate"]
    assert (target / "result").is_dir()
    _no_name_reaches_the_task(task.fake)
