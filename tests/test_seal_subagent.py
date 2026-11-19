"""Sealed sub-agents: `_call_sealed` registers and merges, `subagent` and `sealed_plain` run sealed.

The Flyte task boundary is the outer edge: `subagent_task` is faked where the call is made, and
`agent.subagent.func` is run directly, with a fake `claude` on PATH, where the engine is started.
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import flyte.errors
import flyte.report
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _auth,
    _reviewer,
    _seal_audit,
    _seal_guard,
    _seal_record,
    _seal_role,
    _seal_subagent,
    _session_config,
    _session_setup,
    _subagent_run,
    _unprivileged,
    agent,
)
from tests._seal_fakes import JAIL_CACHE_VARS, SPEC, assert_inside_the_jail, install_fake_claude, read_jsonl

needs_netns = pytest.mark.usefixtures("seal_namespaces")


@pytest.fixture
def record_root(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "SEAL_RECORD", tmp_path / "record")
    return tmp_path / "record"


@pytest.fixture
def sealed_process(record_root, monkeypatch):
    monkeypatch.setattr(_session_config, "_seal", SPEC)
    return record_root


class FakeFile:
    def __init__(self, path: Path, source: Path) -> None:
        self.path, self._source = path, source

    async def download(self, dest: str) -> None:
        shutil.copy2(self._source, dest)


class FakeDir:
    """A returned `Dir` backed by a local directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def walk(self, recursive: bool = False):
        for path in sorted(self._root.rglob("*")):
            if path.is_file():
                yield FakeFile(Path("s3://bucket/out") / path.relative_to(self._root), path)


def _finished_bundle(tmp_path: Path, role_id: str) -> FakeDir:
    """A sub-agent's returned workspace: some work, and the bundle of one finished role."""
    out, root = tmp_path / "returned", tmp_path / "subagent-record"
    out.mkdir()
    (out / "work.txt").write_text("work")
    _seal_record.register(
        root, role_id, "reviewer", engine="claude", seal=_seal_role.seal_flags(SPEC, "claude")
    )
    _seal_record.finish(root, role_id, 0)
    _seal_record.bundle(root, role_id, out)
    return FakeDir(out)


def _fake_task(monkeypatch, behave):
    calls = []

    class Task:
        def override(self, **kwargs):
            return self

        async def aio(self, **kwargs):
            calls.append(kwargs)
            return await behave(kwargs)

    monkeypatch.setattr(_reviewer, "subagent_task", Task())
    return calls


async def test_a_sealed_call_registers_first_passes_the_seal_and_merges_the_record(
    sealed_process, tmp_path, monkeypatch
):
    seen: dict[str, Any] = {}

    async def behave(kwargs):
        # Registered before the call is made, and without a dir: nothing has come back yet.
        role_id = kwargs["seal_role"]
        seen["registered"] = [
            r["role_id"] for r in json.loads((sealed_process / "manifest.json").read_text())["roles"]
        ]
        seen["dir_before"] = _seal_record.role_paths(sealed_process, role_id).dir.exists()
        return "verdict", _finished_bundle(tmp_path, role_id)

    calls = _fake_task(monkeypatch, behave)
    text, _ = await _reviewer._call_sealed("correctness", "claude", brief="b")

    assert text == "verdict"
    (kwargs,) = calls
    assert kwargs["seal"] == SPEC.to_json() and kwargs["routed"] is True and kwargs["engine"] == "claude"
    assert seen["registered"] == [kwargs["seal_role"]] and seen["dir_before"] is False
    assert kwargs["seal_role"].startswith("reviewer-")
    paths = _seal_record.role_paths(sealed_process, kwargs["seal_role"])
    assert paths.end.is_file(), "the returned record was merged"
    assert not (sealed_process / "work.txt").exists(), "only the record is taken from the result"


@pytest.mark.parametrize(
    ("purpose", "kind"),
    [("delegate", "delegate"), ("judgement", "judge"), ("correctness", "reviewer"), ("planner", "subagent")],
)
async def test_the_record_kind_follows_the_purpose(sealed_process, tmp_path, monkeypatch, purpose, kind):
    async def behave(kwargs):
        return "x", _finished_bundle(tmp_path, kwargs["seal_role"])

    calls = _fake_task(monkeypatch, behave)
    await _reviewer._call_sealed(purpose, "codex", brief="b")
    assert calls[0]["seal_role"].startswith(f"{kind}-")
    roles = json.loads((sealed_process / "manifest.json").read_text())["roles"]
    assert [r["kind"] for r in roles] == [kind]


async def test_a_failing_call_leaves_the_role_registered_without_a_dir(sealed_process, monkeypatch):
    async def behave(kwargs):
        raise RuntimeError("the action was lost")

    calls = _fake_task(monkeypatch, behave)
    with pytest.raises(RuntimeError, match="lost"):
        await _reviewer._call_sealed("correctness", "claude", brief="b")
    role_id = calls[0]["seal_role"]
    manifest = json.loads((sealed_process / "manifest.json").read_text())
    assert [r["role_id"] for r in manifest["roles"]] == [role_id]
    assert not _seal_record.role_paths(sealed_process, role_id).dir.exists()
    verdict = _seal_audit.audit(sealed_process)
    assert verdict.verdict == "void"
    assert [(f.kind, f.role_id) for f in verdict.reasons] == [("incomplete", role_id)]


async def test_an_unsealed_project_calls_exactly_as_before(record_root, monkeypatch):
    async def behave(kwargs):
        return "x", None

    calls = _fake_task(monkeypatch, behave)
    await _reviewer._call_sealed("correctness", "claude", brief="b")
    assert calls == [{"engine": "claude", "routed": True, "brief": "b"}]
    assert not record_root.exists()


def test_an_unsealed_sub_agent_call_from_a_sealed_project_is_refused(sealed_process):
    with pytest.raises(RuntimeError, match="unsealed sub-agent"):
        _seal_subagent.check_call("")
    assert _seal_subagent.check_call(SPEC.to_json()) == SPEC


def test_an_unsealed_process_may_make_an_unsealed_call(monkeypatch):
    monkeypatch.setattr(_session_config, "_seal", None)
    assert _seal_subagent.check_call("") is None


async def test_the_subagent_task_refuses_before_starting_anything(sealed_process):
    with pytest.raises(RuntimeError, match="unsealed sub-agent"):
        await agent.subagent.func(brief="b", engine="claude", routed=True)


async def test_the_reattach_path_merges_the_workers_record(sealed_process, tmp_path, monkeypatch):
    """A worker collected after a restart was never merged by `_call_sealed`; the reattach does it."""
    bundle = _finished_bundle(tmp_path, "delegate-1")
    await _reviewer.merge_record(cast(Dir, bundle), sealed_process)
    assert _seal_record.role_paths(sealed_process, "delegate-1").end.is_file()
    await _reviewer.merge_record(
        cast(Dir, bundle), sealed_process
    )  # again: the same record is not a conflict


# --- the engine, run sealed, with a fake `claude` ------------------------------------------------------


@pytest.fixture
def engine_env(tmp_path, monkeypatch):
    install_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")

    async def from_local(path):
        return SimpleNamespace(path=path)

    async def report(text, **kwargs):
        return None

    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_session_setup, "_seed_team", lambda work: {"dev"})
    monkeypatch.setattr(_session_setup, "_export_runtime_env", lambda: dict(os.environ))
    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _check_sealed_result(output, role_id):
    record = Path(output.path) / _seal_record.BUNDLE_DIR
    paths = _seal_record.role_paths(record, role_id)
    end = json.loads(paths.end.read_text())
    assert end["exit_code"] == 0
    assert {"role.json", "proxy.jsonl", "guard.jsonl", "transcript/projects/-work/session-1.jsonl"} <= set(
        end["files"]
    )
    proxy = read_jsonl(paths.proxy_log)
    connect = {line["target"]: line["decision"] for line in proxy if line["method"] == "CONNECT"}
    assert connect == {"api.github.com:443": "deny", "github.com:443": "deny"}
    return record


@needs_netns
async def test_a_sealed_subagent_runs_inside_the_seal_and_returns_its_record(
    sealed_process, engine_env, monkeypatch
):
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "reviewer", "claude")
    summary, output = await agent.subagent.func(
        brief="b",
        engine="claude",
        routed=True,
        answer_only=True,
        seal=SPEC.to_json(),
        seal_role=role_id,
    )
    assert "finished: no route out" in summary and "curl_rc=0" not in summary
    record = _check_sealed_result(output, role_id)
    # The caller merges it into its own root, replacing the registered placeholder.
    assert _seal_record.merge(Path(output.path), sealed_process) == [role_id]
    assert (_seal_record.role_paths(sealed_process, role_id).end).is_file()
    assert (record / "roles" / role_id / "role.json").is_file()
    assert _seal_audit.audit(sealed_process).verdict == "clean"


@needs_netns
async def test_the_guard_settings_keep_the_harness_deny_rules(sealed_process, engine_env, tmp_path):
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "reviewer", "claude")
    _, output = await agent.subagent.func(
        brief="b", engine="claude", routed=True, seal=SPEC.to_json(), seal_role=role_id
    )
    settings = next(Path(output.path).parent.glob("claude/settings.json"))
    deny = json.loads(settings.read_text())["permissions"]["deny"]
    assert "Bash(flyte run:*)" in deny and "WebSearch" in deny
    assert json.loads(settings.read_text())["hooks"]["PreToolUse"]


@needs_netns
async def test_a_sealed_subagent_that_fails_raises_and_merges_nothing(sealed_process, engine_env, tmp_path):
    (tmp_path / "bin" / "claude").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "reviewer", "claude")
    with pytest.raises(RuntimeError, match="exited 3"):
        await agent.subagent.func(
            brief="b", engine="claude", routed=True, seal=SPEC.to_json(), seal_role=role_id
        )
    assert not _seal_record.role_paths(sealed_process, role_id).dir.exists()


@needs_netns
def test_an_attempt_that_ends_without_a_result_is_finished_as_failed(tmp_path):
    sealed = _seal_subagent.SubagentSeal(SPEC, "reviewer-1", tmp_path, tmp_path / "w")
    (tmp_path / "w").mkdir()
    sealed.open("claude", sealed.config_dir("claude"))
    sealed.open("codex", sealed.config_dir("codex"))  # the engine moved: the first attempt failed
    sealed.close(failed=True)
    record = tmp_path / "seal-record"
    exits = {
        role: json.loads(_seal_record.role_paths(record, role).end.read_text())["exit_code"]
        for role in ("reviewer-1", "reviewer-1-a1")
    }
    assert exits == {"reviewer-1": 1, "reviewer-1-a1": 1}


@needs_netns
async def test_a_sealed_codex_subagent_has_the_launcher_and_web_search_off(
    sealed_process, engine_env, tmp_path, monkeypatch
):
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0

        async def communicate(self):
            seen["last"].write_text("codex answer")
            return b"", b""

    async def fake_exec(*args, **kwargs):
        seen["argv"], seen["env"] = list(args), kwargs["env"]
        seen["last"] = Path(args[args.index("-o") + 1])
        return Done()

    async def home(base):
        path = tmp_path / "codex-home"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(_subagent_run, "_codex_home", home)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "delegate", "codex")
    summary, output = await agent.subagent.func(
        brief="b", engine="codex", routed=True, answer_only=True, seal=SPEC.to_json(), seal_role=role_id
    )
    argv = seen["argv"]
    assert argv.index("codex") > 0 and argv[argv.index("codex") - 1] == "--", "the launcher comes first"
    for flag in _seal_role.CODEX_TOOLS_OFF_ARGS:
        assert flag in argv
    assert "PYTHONPATH" in seen["env"]
    role = json.loads(
        _seal_record.role_paths(Path(output.path) / _seal_record.BUNDLE_DIR, role_id)
        .dir.joinpath("role.json")
        .read_text()
    )
    assert role["engine"] == "codex" and role["seal"]["guard"] is False


async def test_the_plan_follow_up_runs_through_the_launcher(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "1. the rest"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _subagent_run._ask_remaining_work(
        "claude", tmp_path, {}, {"session_id": "s"}, "m", "e", launcher=["launch", "--"]
    )
    assert seen["argv"][:3] == ["launch", "--", "claude"]


def test_the_work_snapshot_is_not_needed_to_name_the_attempt_dirs(tmp_path):
    sealed = _seal_subagent.SubagentSeal(SPEC, "reviewer-1", tmp_path, tmp_path / "w")
    assert sealed.config_dir("claude") == tmp_path / "claude"
    sealed._ids.append("reviewer-1")
    assert sealed.config_dir("claude") == tmp_path / "claude-a1"


# --- sealed and never root: every sealed start goes through `_unprivileged.spawn` -------------------------


@pytest.fixture
def spawns(monkeypatch):
    """Record each start made through `_unprivileged.spawn` and each sealed role the code opened."""
    seen = SimpleNamespace(starts=[], roles=[])
    real_spawn, real_open = _unprivileged.spawn, _seal_subagent.SubagentSeal.open

    async def record(what, *argv, **kwargs):
        seen.starts.append(SimpleNamespace(what=what, argv=list(argv), env=kwargs.get("env") or {}))
        return await real_spawn(what, *argv, **kwargs)

    def opened(self, engine, config_dir):
        role = real_open(self, engine, config_dir)
        seen.roles.append(role)
        return role

    monkeypatch.setattr(_unprivileged, "spawn", record)
    monkeypatch.setattr(_seal_subagent.SubagentSeal, "open", opened)
    return seen


def _through_the_launcher(argv: list[str], engine: str) -> None:
    assert argv[0] != engine and argv[argv.index(engine) - 1] == "--", (
        "the jail's wrapped argv, not the bare CLI"
    )


@needs_netns
async def test_a_sealed_claude_subagent_is_spawned_unprivileged_with_the_jails_argv_and_env(
    sealed_process, engine_env, spawns
):
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "reviewer", "claude")
    _, output = await agent.subagent.func(
        brief="b", engine="claude", routed=True, answer_only=True, seal=SPEC.to_json(), seal_role=role_id
    )
    [start], [role] = spawns.starts, spawns.roles
    assert start.what == "a Claude Code sub-agent"
    _through_the_launcher(start.argv, "claude")
    assert_inside_the_jail(start.env, role.env)
    _check_sealed_result(output, role_id)


@needs_netns
async def test_a_sealed_codex_subagent_is_spawned_unprivileged_with_the_jails_argv_and_env(
    sealed_process, engine_env, tmp_path, monkeypatch, spawns
):
    last = Path()

    class Done:
        returncode = 0

        async def communicate(self):
            last.write_text("codex answer")
            return b"", b""

    async def fake_exec(*args, **kwargs):
        nonlocal last
        last = Path(args[args.index("-o") + 1])
        return Done()

    async def home(base):
        path = tmp_path / "codex-home"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(_subagent_run, "_codex_home", home)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    role_id = _seal_role.register_subagent(SPEC, sealed_process, "delegate", "codex")
    summary, _ = await agent.subagent.func(
        brief="b", engine="codex", routed=True, answer_only=True, seal=SPEC.to_json(), seal_role=role_id
    )
    [start], [role] = spawns.starts, spawns.roles
    assert start.what == "a Codex sub-agent"
    _through_the_launcher(start.argv, "codex")
    assert_inside_the_jail(start.env, role.env)
    assert "CODEX_HOME" in start.env
    assert "codex answer" in summary


async def test_the_sealed_plan_follow_up_is_spawned_unprivileged_through_the_launcher(
    tmp_path, monkeypatch, spawns
):
    class Done:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "1. the rest"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _subagent_run._ask_remaining_work(
        "claude", tmp_path, {}, {"session_id": "s"}, "m", "e", launcher=["launch", "--"]
    )
    [start] = spawns.starts
    assert start.what == "a Claude Code sub-agent"
    assert start.argv[:3] == ["launch", "--", "claude"]


@pytest.fixture
def nothing_starts(monkeypatch):
    """Fail the first step after the task's refusals, and any process start."""

    def started(*args, **kwargs):
        raise AssertionError("the sub-agent started work")

    async def spawned(*args, **kwargs):
        raise AssertionError(f"spawned {args!r}")

    monkeypatch.setattr(_auth, "set_non_dumpable", started)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawned)


@pytest.mark.parametrize("sealed_call", [True, False], ids=["with-seal", "without-seal"])
async def test_a_root_sub_agent_is_refused_once_before_anything_starts(
    sealed_process, nothing_starts, monkeypatch, sealed_call
):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    seal = SPEC.to_json() if sealed_call else ""
    with pytest.raises(flyte.errors.NonRecoverableError, match="Refusing to start a sub-agent as root"):
        await agent.subagent.func(brief="b", engine="claude", routed=True, seal=seal)


async def test_an_unsealed_call_from_a_sealed_project_is_still_refused_before_anything_starts(
    sealed_process, nothing_starts, monkeypatch
):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="unsealed sub-agent") as refused:
        await agent.subagent.func(brief="b", engine="claude", routed=True, seal="")
    assert not isinstance(refused.value, flyte.errors.NonRecoverableError)


async def test_a_sealed_sub_agent_whose_cli_never_started_is_not_retried(tmp_path):
    """Upstream's "never started" error survives the move of the result handling into `_claude_summary`."""
    kwargs: dict[str, Any] = dict(
        brief="b", role="", model="", effort="", max_turns=1, answer_only=True, partial_base="", jail=object()
    )
    with pytest.raises(flyte.errors.NonRecoverableError, match="exited 1 without a result") as never:
        await _subagent_run._claude_summary(
            b"error: unknown option", b"boom", 1, tmp_path, ["claude"], {}, **kwargs
        )
    assert "boom" in str(never.value) and "will not be retried" in str(never.value)
    # A signal is a crash mid-conversation, and one that left a result has its own handling: both retried.
    for raw, status in ((b"", -9), (json.dumps({"subtype": "error_during_execution"}).encode(), 1)):
        with pytest.raises(RuntimeError, match=f"exited {status}") as crash:
            await _subagent_run._claude_summary(raw, b"", status, tmp_path, ["claude"], {}, **kwargs)
        assert not isinstance(crash.value, flyte.errors.NonRecoverableError)


async def test_a_sealed_workers_turn_limit_follow_up_runs_inside_its_jail(tmp_path, monkeypatch, spawns):
    """`_claude_summary` hands the follow-up the worker's jail: its launcher, its env, the web tools off."""
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "tracked").write_text("before\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "Delegation base"], check=True)
    base = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "tracked").write_text("after\n")
    jail = SimpleNamespace(
        argv_prefix=["seal-launch", "--"],
        env={var: str(tmp_path / "jail-cache" / var) for var in JAIL_CACHE_VARS}
        | {"PYTHONPATH": "jail-path"},
    )

    class Done:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "1. the rest"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    worker_env = {**os.environ, **jail.env}  # the env the worker itself was started with
    raw = json.dumps({"subtype": "error_max_turns", "session_id": "s"}).encode()
    kwargs: dict[str, Any] = dict(
        brief="b",
        role="",
        model="m",
        effort="e",
        max_turns=3,
        answer_only=False,
        partial_base=base,
        jail=jail,
    )
    summary = await _subagent_run._claude_summary(
        raw, b"", 1, tmp_path / "work", ["claude"], worker_env, **kwargs
    )
    assert "PARTIAL" in summary
    [start] = spawns.starts
    assert start.what == "a Claude Code sub-agent"
    assert start.argv[:3] == ["seal-launch", "--", "claude"]
    deny = _seal_guard.cli_deny_args()
    assert start.argv[-len(deny) :] == deny, "the web tools are off on the follow-up too"
    assert jail.env.items() <= start.env.items()
