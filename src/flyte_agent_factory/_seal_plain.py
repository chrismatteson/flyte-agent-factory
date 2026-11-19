"""The plain (baseline) arm of a sealed benchmark: one bare engine session, sealed like every other role.

No factory memory, roles, bridge or `ask-human`: the work dir holds the task snapshot and nothing else, and
the engine is given the brief as its prompt. It runs inside `sealed_role(kind="plain")`, so its record has
the same shape as a factory arm's and goes through the same audit. The result is the final message and the
work dir (the task repo, `model.patch` with what changed in it, and `.seal-record/`).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from flyte.io import Dir

from . import (
    _auth,
    _delegation,
    _policy,
    _seal_record,
    _seal_role,
    _seal_snapshot,
    _seal_subagent,
    _session_setup,
    _subagent_run,
    _unprivileged,
)
from ._seal_spec import SealSpec

#: The patch the harness reads: the task repo's working tree against the base commit.
PATCH_FILE = "model.patch"
_GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Plain Arm", "GIT_COMMITTER_NAME": "Plain Arm"}
#: How long one plain engine session may run. A SWE-bench task at the default 200 turns takes well under an
#: hour; two hours leaves room for a slow model without letting a hung engine hold the task forever.
_ENGINE_TIMEOUT_S = 2 * 60 * 60


def _spec_for(seal: str) -> SealSpec:
    if not seal:
        raise RuntimeError("sealed_plain needs a seal spec: this arm exists only sealed")
    # Used exactly as the operator wrote it, so the record's `spec_sha256` is the hash of the operator's spec.
    return SealSpec.from_json(seal)


async def _communicate_bounded(
    proc: asyncio.subprocess.Process, sealed: _seal_subagent.SubagentSeal, engine: str, timeout_s: float
) -> tuple[bytes, bytes]:
    """Return the engine's stdout and stderr; kill it and raise `RuntimeError` once it exceeds `timeout_s`."""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        sealed.exit_code = proc.returncode
        raise RuntimeError(f"plain {engine} exceeded {timeout_s:g}s and was killed") from None
    sealed.exit_code = proc.returncode
    return out, err


def _write_patch(work: Path, spec: SealSpec) -> None:
    """Write `model.patch`; a diff that cannot be taken is recorded as `model.patch.error`, not skipped."""
    try:
        diff = _delegation.work_diff(work / spec.task_dir, spec.base_sha)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        (work / f"{PATCH_FILE}.error").write_text(f"{type(exc).__name__}: {exc}\n")
        return
    (work / PATCH_FILE).write_bytes(diff)


async def _run_claude(
    sealed: _seal_subagent.SubagentSeal,
    work: Path,
    brief: str,
    model: str,
    max_turns: int,
    timeout_s: float,
) -> str:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("no Claude setup-token available for the plain arm ([secrets] claude_token)")
    config_dir = sealed.config_dir("claude")
    role = sealed.open("claude", config_dir)
    _auth.write_settings(config_dir, hold_dialogs=False, extra=_seal_subagent.claude_settings(role))
    _auth.mark_trusted(config_dir, work)
    argv = [
        "claude",
        "-p",
        brief,
        "--output-format",
        "json",
        *(["--model", model] if model else []),
        "--max-turns",
        str(max_turns),
        # Nothing can answer a permission prompt here.
        "--permission-mode",
        "bypassPermissions",
    ]
    env = {
        **_session_setup._session_env(dict(os.environ)),
        **_GIT_IDENTITY,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        **role.env,
    }
    with _auth.token_fd(env, token) as (child_env, pass_fds):
        proc = await _unprivileged.spawn(
            "the plain arm's Claude Code",
            *role.wrap(argv),
            cwd=str(work),
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=pass_fds,
        )
    raw, err = await _communicate_bounded(proc, sealed, "claude", timeout_s)
    stdout = raw.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"plain claude exited {proc.returncode}.\nstdout: {stdout[-1500:] or '(empty)'}\n"
            f"stderr: {err.decode(errors='replace')[-1500:] or '(empty)'}"
        )
    _subagent_run._raise_on_cli_error(stdout)
    return _subagent_run._final_text(stdout)


async def _run_codex(
    sealed: _seal_subagent.SubagentSeal, work: Path, base: Path, brief: str, model: str, timeout_s: float
) -> str:
    home = await _subagent_run._codex_home(base)
    role = sealed.open("codex", home)
    last = base / "codex-last.txt"
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'cli_auth_credentials_store="file"',
        "-o",
        str(last),
        *_seal_role.CODEX_TOOLS_OFF_ARGS,
        # `_run_codex` drops a Claude alias here too: Codex has no "sonnet".
        *(["-m", model] if model and not _policy.is_claude_model(model) else []),
        "--",
        brief,
    ]
    env = {
        **_session_setup._session_env(dict(os.environ)),
        **_GIT_IDENTITY,
        "CODEX_HOME": str(home),
        **role.env,
    }
    proc = await _unprivileged.spawn(
        "the plain arm's Codex",
        *role.wrap(argv),
        cwd=str(work),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await _communicate_bounded(proc, sealed, "codex", timeout_s)
    answer = last.read_text(errors="replace").strip() if last.is_file() else ""
    if proc.returncode != 0 or not answer:
        detail = (err.decode(errors="replace") or out.decode(errors="replace"))[-600:]
        raise RuntimeError(f"plain codex exited {proc.returncode}: {detail}")
    return answer


async def run(
    brief: str,
    seal: str,
    engine: str = "claude",
    model: str = "",
    max_turns: int = 200,
    timeout_s: float = _ENGINE_TIMEOUT_S,
) -> tuple[str, Dir]:
    """Run the plain arm sealed; return its final message and the work dir with `.seal-record/` in it.

    The engine is killed after `timeout_s` seconds and the call raises; the record is closed as failed.
    """
    spec = _spec_for(seal)
    if engine not in _seal_record.ENGINES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {_seal_record.ENGINES}")
    base = Path(tempfile.mkdtemp(prefix="plain-"))
    work = base / "workspace"
    work.mkdir()
    # Outside the seal: this is the one step that needs the network.
    _seal_snapshot.snapshot_task_repo(spec.task_repo, spec.base_sha, work / spec.task_dir)
    sealed = _seal_subagent.SubagentSeal(spec, _seal_record.new_role_id("plain"), base, work)
    try:
        if engine == "codex":
            text = await _run_codex(sealed, work, base, brief, model, timeout_s)
        else:
            text = await _run_claude(sealed, work, brief, model, max_turns, timeout_s)
        # After the engine, before the record is bundled: the patch is part of what the audit sees.
        _write_patch(work, spec)
        return text, await sealed.result_dir(whole=True)
    finally:
        sealed.close(failed=True)  # a no-op after a result; closes the record of a run that failed


__all__ = ["PATCH_FILE", "run"]
