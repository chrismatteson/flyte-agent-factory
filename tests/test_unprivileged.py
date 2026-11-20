"""A model session is never started as root, and a startup failure that repeats is not retried."""

import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import flyte.errors
import flyte.report
import pytest
from flyte._internal.runtime.convert import convert_from_native_to_error
from flyte.io import Dir

from flyte_agent_factory import _session_setup, _timeline, _unprivileged, _usage, agent
from flyte_agent_factory._process import ClaudeProcess

ROOT_REFUSAL = "--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons"


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def test_root_is_refused_without_a_retry(as_root):
    with pytest.raises(flyte.errors.NonRecoverableError, match="root") as refused:
        _unprivileged.refuse_root("Claude Code")
    assert "rebuild the factory images" in str(refused.value) and "USER flyte" in str(refused.value)
    assert convert_from_native_to_error(refused.value).recoverable is False


def test_an_unprivileged_user_is_let_through(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    _unprivileged.refuse_root("Claude Code")  # returns, no refusal


def test_the_first_start_logs_who_the_task_runs_as_once(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("HOME", "/home/flyte")
    monkeypatch.setattr(_unprivileged, "_announced", False)
    _unprivileged.refuse_root("the project session")
    _unprivileged.refuse_root("Claude Code")
    assert capsys.readouterr().out == "Starting the project session as uid 1000, HOME=/home/flyte\n"


def test_the_refusal_does_not_claim_a_sandbox(as_root, monkeypatch):
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    with pytest.raises(flyte.errors.NonRecoverableError):
        _unprivileged.refuse_root("Claude Code")
    assert "IS_SANDBOX" not in os.environ


@pytest.fixture
def no_spawn(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError(f"spawned {args!r} as root")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)


async def test_the_session_launcher_spawns_nothing_as_root(as_root, no_spawn, tmp_path):
    proc = ClaudeProcess(["claude"], cwd=tmp_path, env={})
    with pytest.raises(flyte.errors.NonRecoverableError, match="root"):
        await proc.start()
    assert proc._master is None  # the pty was closed again


async def test_the_project_task_refuses_before_any_work(as_root, no_spawn):
    with pytest.raises(flyte.errors.NonRecoverableError, match="project session"):
        await agent.project.func(brief="b")


async def test_the_sub_agent_task_refuses_before_any_work(as_root, no_spawn):
    with pytest.raises(flyte.errors.NonRecoverableError, match="sub-agent"):
        await agent.subagent.func(brief="b")


async def test_a_missing_binary_is_not_retried(tmp_path):
    proc = ClaudeProcess(["no-such-claude-binary"], cwd=tmp_path, env={})
    with pytest.raises(flyte.errors.NonRecoverableError, match="no-such-claude-binary") as missing:
        await proc.start()
    assert convert_from_native_to_error(missing.value).recoverable is False
    assert proc._master is None


async def test_a_missing_working_directory_is_not_blamed_on_the_binary(tmp_path):
    proc = ClaudeProcess([sys.executable, "-c", "pass"], cwd=tmp_path / "gone", env={})
    with pytest.raises(FileNotFoundError) as missing:
        await proc.start()
    assert not isinstance(missing.value, flyte.errors.NonRecoverableError)


async def test_a_binary_that_is_not_executable_is_not_retried(tmp_path):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n")
    script.chmod(stat.S_IRUSR)
    with pytest.raises(flyte.errors.NonRecoverableError, match="claude"):
        await _unprivileged.spawn("Claude Code", str(script))


def _fake_claude(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


async def _run_to_exit(script: Path, tmp_path: Path) -> tuple[ClaudeProcess, int]:
    proc = ClaudeProcess([str(script)], cwd=tmp_path, env={**os.environ})
    await proc.start()
    async with asyncio.timeout(30):
        return proc, await proc.wait()


async def test_a_session_refused_before_its_prompt_is_not_retried(tmp_path):
    script = _fake_claude(tmp_path, f"import sys\nprint({ROOT_REFUSAL!r})\nsys.exit(1)\n")
    proc, status = await _run_to_exit(script, tmp_path)
    assert status == 1 and not proc.reached_prompt
    error = _unprivileged.exit_error("claude", status, "\n".join(proc.tail), proc)
    assert isinstance(error, flyte.errors.NonRecoverableError)
    assert str(error).startswith(ROOT_REFUSAL) and "will not be retried" in str(error)
    assert convert_from_native_to_error(error).recoverable is False


async def test_a_session_that_reached_its_prompt_and_exits_is_retried(tmp_path):
    script = _fake_claude(tmp_path, "import sys\nprint('\\n❯ ')\nprint('crashed')\nsys.exit(1)\n")
    proc, status = await _run_to_exit(script, tmp_path)
    assert status == 1 and proc.reached_prompt
    error = _unprivileged.exit_error("claude", status, "\n".join(proc.tail), proc)
    assert type(error) is RuntimeError and "retrying from the last checkpoint" in str(error)
    assert convert_from_native_to_error(error).recoverable is True


@pytest.mark.parametrize("status", [-9, 137, 143])
def test_a_killed_session_is_retried(status):
    error = _unprivileged.exit_error("claude", status, "", SimpleNamespace(reached_prompt=False))
    assert type(error) is RuntimeError


def test_a_codex_exit_is_always_retried():
    error = _unprivileged.exit_error("codex", 1, "", SimpleNamespace(reached_prompt=False))
    assert type(error) is RuntimeError and str(error).startswith("Codex exited with status 1")


@pytest.fixture
def sub_agent(tmp_path, monkeypatch):
    """Run the real `subagent` against a `claude` whose stdout and exit status the test chooses."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(program, *args, **kwargs):
        return await real_exec(str(script) if program == "claude" else program, *args, **kwargs)

    async def from_local(path):
        return SimpleNamespace(path=path)

    async def report(text, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_session_setup, "_seed_team", lambda work: {"dev"})
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path))
    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "slot").mkdir()

    def run(body: str):
        script.write_text(f"#!{sys.executable}\nimport json, sys\n{body}\nsys.exit(1)\n")
        script.chmod(0o755)
        return agent.subagent.func(brief="b", engine="claude", routed=True)

    return run


async def test_a_sub_agent_that_exits_without_a_result_is_not_retried(sub_agent):
    with pytest.raises(flyte.errors.NonRecoverableError, match=ROOT_REFUSAL) as failed:
        await sub_agent(f"print({ROOT_REFUSAL!r})\nprint('boom', file=sys.stderr)")
    assert "boom" in str(failed.value) and "will not be retried" in str(failed.value)


async def test_a_sub_agent_refused_by_its_usage_limit_keeps_its_handling(sub_agent):
    limited = (
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,"
        " 'result': \"You've hit your limit · resets 3pm (UTC)\", 'num_turns': 12}))"
    )
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK) as limit:
        await sub_agent(limited)
    assert not isinstance(limit.value, flyte.errors.NonRecoverableError)


async def test_a_sub_agent_that_fails_with_a_result_is_still_retried(sub_agent):
    failed = "print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}))"
    with pytest.raises(RuntimeError, match="exited 1") as crash:
        await sub_agent(failed)
    assert not isinstance(crash.value, flyte.errors.NonRecoverableError)


async def test_a_sub_agent_killed_mid_conversation_is_still_retried(sub_agent):
    with pytest.raises(RuntimeError, match="exited -9") as crash:
        await sub_agent("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")
    assert not isinstance(crash.value, flyte.errors.NonRecoverableError)


async def test_a_non_recoverable_error_in_the_sub_agent_is_not_rerouted(sub_agent, monkeypatch):
    """The usage-limit handler re-raises it unchanged, even if its text carries the limit mark."""
    message = f"{_usage.USAGE_LIMIT_MARK} in a message that happens to quote it"

    async def refuse(*args, **kwargs):
        raise flyte.errors.NonRecoverableError(message)

    monkeypatch.setattr(_unprivileged, "spawn", refuse)
    with pytest.raises(flyte.errors.NonRecoverableError, match="happens to quote"):
        await sub_agent("print('unused')")


class _ExitsAtStartup:
    """A `ClaudeProcess` stand-in that exits at once with `tail` unless its argv is the plain start."""

    started: list[list[str]] = []
    output = ""

    def __init__(self, argv, **kwargs):
        self.argv, self.reached_prompt = argv, False
        self.returncode = self.tail = None

    async def start(self):
        _ExitsAtStartup.started.append(self.argv)
        resuming = "--continue" in self.argv
        self.returncode = 1 if resuming else None
        self.tail = [_ExitsAtStartup.output] if resuming else []

    async def wait(self):
        # As `ClaudeProcess.wait`: the output is read to its end, then the exit status.
        return self.returncode

    async def stop(self, grace=0.0):
        pass


@pytest.fixture
def exits_at_startup(monkeypatch):
    from flyte_agent_factory import _author

    monkeypatch.setattr(_author, "ClaudeProcess", _ExitsAtStartup)
    capture = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(_session_setup, "_log_session_line", lambda *a, **k: capture)
    monkeypatch.setattr(_author, "_HEADLESS_START_CHECK_S", 0.0)
    _ExitsAtStartup.started = []
    timeline = _timeline._Timeline()
    candidates = _author.session_argv_candidates(["claude"], resume=True)
    return lambda: _author._start_session(candidates, env={}, timeline=timeline)


async def test_a_resumed_start_that_exits_before_its_prompt_is_not_retried(exits_at_startup):
    _ExitsAtStartup.output = "error: unknown option '--permission-mode'"
    with pytest.raises(flyte.errors.NonRecoverableError, match="unknown option") as failed:
        await exits_at_startup()
    assert convert_from_native_to_error(failed.value).recoverable is False
    assert _ExitsAtStartup.started == [["claude", "--continue"]]


async def test_nothing_to_resume_still_falls_back_to_a_fresh_start(exits_at_startup):
    _ExitsAtStartup.output = "No conversation found to continue"
    proc = await exits_at_startup()
    assert proc.argv == ["claude"] and _ExitsAtStartup.started == [["claude", "--continue"], ["claude"]]
