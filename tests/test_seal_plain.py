"""`sealed_plain`: the baseline arm runs one bare engine session, sealed, with the same record as any role."""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flyte.errors
import flyte.report
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _delegation,
    _seal_audit,
    _seal_plain,
    _seal_record,
    _seal_role,
    _seal_snapshot,
    _seal_subagent,
    _subagent_run,
    _unprivileged,
    agent,
)
from tests._seal_fakes import SPEC, assert_inside_the_jail, install_fake_claude, read_jsonl

needs_netns = pytest.mark.usefixtures("seal_namespaces")


def _git(path: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.fixture
def plain_env(tmp_path, monkeypatch):
    seen = SimpleNamespace(snapshots=[], base="")

    def snapshot(url, base_sha, dest):
        dest.mkdir(parents=True)
        _git(dest, "init", "-q")
        (dest / "a.txt").write_text("original\n")
        _git(dest, "add", "-A")
        _git(dest, "commit", "-qm", "base")
        seen.base = _git(dest, "rev-parse", "HEAD").strip()
        seen.snapshots.append((url, base_sha, dest))
        return dest

    async def from_local(path):
        return SimpleNamespace(path=path)

    async def report(text, **kwargs):
        return None

    monkeypatch.setattr(_seal_snapshot, "snapshot_task_repo", snapshot)
    # The fake snapshot's commit cannot have the fake spec's sha: diff against its HEAD, which is its base.
    real_diff = _delegation.work_diff
    monkeypatch.setattr(_delegation, "work_diff", lambda repo, base: real_diff(repo, "HEAD"))
    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    # The arm's scratch (`plain-*`) outlives `run` in its own container; here it goes with `tmp_path`.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return seen


def _install_editing_claude(tmp_path):
    install_fake_claude(tmp_path / "real")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "claude"
    wrapper.write_text(
        f'#!/bin/sh\necho "changed by the model" >> repo/a.txt\nexec {tmp_path / "real" / "claude"} "$@"\n'
    )
    wrapper.chmod(0o755)
    return bin_dir


@needs_netns
async def test_the_plain_arm_runs_sealed_and_returns_the_work_and_the_record(
    tmp_path, monkeypatch, plain_env
):
    # The fake it wraps is on PATH too: the seal's private /tmp carries only the PATH dirs of the harness's.
    bin_dir = _install_editing_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{tmp_path / 'real'}:{os.environ['PATH']}")
    text, output = await agent.sealed_plain.func(
        brief="fix the bug", seal=SPEC.to_json(), model="m", max_turns=5
    )

    assert "finished: no route out" in text and "curl_rc=0" not in text
    work = Path(output.path)
    # What the harness reads: the task repo, its patch, and nothing of the factory's.
    assert (work / "repo" / "a.txt").read_text() == "original\nchanged by the model\n"
    assert "+changed by the model" in (work / "model.patch").read_text()
    assert not (work / "CLAUDE.md").exists() and not (work / ".claude-flyte").exists()
    (role_dir,) = (work / _seal_record.BUNDLE_DIR / "roles").iterdir()
    assert role_dir.name.startswith("plain-")
    paths = _seal_record.role_paths(work / _seal_record.BUNDLE_DIR, role_dir.name)
    end = json.loads(paths.end.read_text())
    assert end["exit_code"] == 0
    assert {"role.json", "proxy.jsonl", "guard.jsonl", "transcript/projects/-work/session-1.jsonl"} <= set(
        end["files"]
    )
    connect = {
        line["target"]: line["decision"]
        for line in read_jsonl(paths.proxy_log)
        if line["method"] == "CONNECT"
    }
    assert connect == {"api.github.com:443": "deny", "github.com:443": "deny"}
    assert json.loads((paths.dir / "role.json").read_text())["seal"]["tools_off"] is True
    # The returned record is a whole record root: the audit reads it as it comes back (the harness's case).
    assert _seal_audit.audit(work / _seal_record.BUNDLE_DIR).verdict == "clean"
    # And it merges like any other, so the same audit reads it in a larger record.
    assert _seal_record.merge(work, tmp_path / "merged") == [role_dir.name]
    assert _seal_audit.audit(tmp_path / "merged").verdict == "clean"


async def test_the_brief_is_the_prompt_and_nothing_else_is_given(tmp_path, monkeypatch, plain_env):
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "ok"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        seen["argv"], seen["cwd"] = list(args), kwargs["cwd"]
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    text, output = await agent.sealed_plain.func(
        brief="fix the bug", seal=SPEC.to_json(), model="m", max_turns=5
    )
    argv = seen["argv"]
    tail = argv[argv.index("claude") :]
    assert tail[:3] == ["claude", "-p", "fix the bug"]
    assert tail[tail.index("--max-turns") + 1] == "5" and tail[tail.index("--model") + 1] == "m"
    assert "--agent" not in tail
    assert argv[argv.index("claude") - 1] == "--", "through the launcher"
    assert text == "ok"


async def test_a_plain_run_that_fails_raises(tmp_path, monkeypatch, plain_env):
    class Done:
        returncode = 2

        async def communicate(self):
            return b"", b"boom"

    async def fake_exec(*args, **kwargs):
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="exited 2"):
        await agent.sealed_plain.func(brief="b", seal=SPEC.to_json())


@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_a_plain_run_that_hangs_is_killed_after_its_timeout(tmp_path, monkeypatch, plain_env, engine):
    killed: list[bool] = []

    class Hung:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(30)
            return b"", b""

        def kill(self):
            killed.append(True)
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kwargs):
        return Hung()

    async def home(base):
        path = tmp_path / "codex-home"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(_subagent_run, "_codex_home", home)
    with pytest.raises(RuntimeError, match=f"plain {engine} exceeded 0.1s"):
        await asyncio.wait_for(_seal_plain.run("b", SPEC.to_json(), engine, timeout_s=0.1), timeout=10)
    assert killed == [True]


async def test_the_plain_arm_refuses_to_run_unsealed():
    with pytest.raises(RuntimeError, match="needs a seal spec"):
        await agent.sealed_plain.func(brief="b", seal="")


@pytest.mark.parametrize("task_dir", ["../x", "/abs", ""])
async def test_the_plain_arm_refuses_a_task_dir_outside_the_work_dir(task_dir):
    with pytest.raises(ValueError, match="task_dir"):
        seal = json.dumps({**json.loads(SPEC.to_json()), "task_dir": task_dir})
        await agent.sealed_plain.func(brief="b", seal=seal)


@needs_netns
async def test_a_plain_codex_run_is_sealed_with_web_search_off(tmp_path, monkeypatch, plain_env):
    seen: dict[str, Any] = {}

    class Done:
        returncode = 0

        async def communicate(self):
            seen["last"].write_text("codex answer")
            return b"", b""

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        seen["last"] = Path(args[args.index("-o") + 1])
        return Done()

    async def home(base):
        path = tmp_path / "codex-home"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(_subagent_run, "_codex_home", home)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    text, output = await agent.sealed_plain.func(brief="fix it", seal=SPEC.to_json(), engine="codex")
    argv = seen["argv"]
    assert text == "codex answer"
    assert argv[argv.index("codex") - 1] == "--"
    assert all(flag in argv for flag in _seal_role.CODEX_TOOLS_OFF_ARGS)
    assert argv[-2:] == ["--", "fix it"]
    (role_dir,) = (Path(output.path) / _seal_record.BUNDLE_DIR / "roles").iterdir()
    role = json.loads((role_dir / "role.json").read_text())
    assert role["engine"] == "codex" and role["seal"]["guard"] is False


# --- sealed and never root ---------------------------------------------------------------------------------


@needs_netns
@pytest.mark.parametrize(
    ("engine", "what"), [("claude", "the plain arm's Claude Code"), ("codex", "the plain arm's Codex")]
)
async def test_the_plain_arm_is_spawned_unprivileged_with_the_jails_argv_and_env(
    tmp_path, monkeypatch, plain_env, engine, what
):
    spawns: list[tuple[str, list[str], dict[str, str]]] = []
    last = Path()

    class Done:
        returncode = 0

        async def communicate(self):
            if engine == "codex":
                last.write_text("codex answer")
                return b"", b""
            return json.dumps({"result": "ok"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        nonlocal last
        if engine == "codex":
            last = Path(args[args.index("-o") + 1])
        return Done()

    real_spawn = _unprivileged.spawn

    async def record(what, *argv, **kwargs):
        spawns.append((what, list(argv), kwargs["env"]))
        return await real_spawn(what, *argv, **kwargs)

    async def home(base):
        path = tmp_path / "codex-home"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(_subagent_run, "_codex_home", home)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    roles = []
    real_open = _seal_subagent.SubagentSeal.open

    def opened(self, engine, config_dir):
        roles.append(real_open(self, engine, config_dir))
        return roles[-1]

    monkeypatch.setattr(_unprivileged, "spawn", record)
    monkeypatch.setattr(_seal_subagent.SubagentSeal, "open", opened)
    await agent.sealed_plain.func(brief="b", seal=SPEC.to_json(), engine=engine)
    [(started, argv, env)], [role] = spawns, roles
    assert started == what
    assert argv[0] != engine and argv[argv.index(engine) - 1] == "--", "the jail's wrapped argv"
    assert_inside_the_jail(env, role.env)


async def test_a_root_plain_arm_is_refused_once_before_anything_starts(monkeypatch):
    async def run(*args, **kwargs):
        raise AssertionError("the plain arm started")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(_seal_plain, "run", run)
    with pytest.raises(flyte.errors.NonRecoverableError, match="Refusing to start the plain arm as root"):
        await agent.sealed_plain.func(brief="b", seal=SPEC.to_json())
