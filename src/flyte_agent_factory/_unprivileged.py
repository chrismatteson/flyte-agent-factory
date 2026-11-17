"""Refuse to run a model session as root, and tell a failure that repeats from one that may not.

Claude Code refuses `--permission-mode bypassPermissions` as root and exits 1. Flyte retries a plain
`RuntimeError`, so a pod whose image ends `USER root` once republished its workspace 21 times for a failure
that was identical every time. `flyte.errors.NonRecoverableError` ends the run without retries.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import flyte.errors

#: A process that dies on a signal reports a negative status, or 128+n through a shell.
_SIGNAL_EXIT = 128

_announced = False


def refuse_root(what: str) -> None:
    """Raise `NonRecoverableError` when this process is root.

    No override, and nothing here may tell Claude Code it is sandboxed (`IS_SANDBOX`): that would only
    switch off the one check that notices the image is wrong.
    """
    home = os.environ.get("HOME", "")
    if (uid := os.geteuid()) != 0:
        global _announced
        if not _announced:  # the first lines of a task's log say who it runs as
            print(f"Starting {what} as uid {uid}, HOME={home}", flush=True)
            _announced = True
        return
    raise flyte.errors.NonRecoverableError(
        f"Refusing to start {what} as root (uid 0, HOME={home}). The factory runs as an "
        "unprivileged user and Claude Code refuses its permission mode as root. The image's final USER is "
        "root: rebuild the factory images with this engine version (they end `USER flyte`) and redeploy; "
        "see docs/unprivileged-images.md."
    )


async def spawn(what: str, *argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """Start a model-session process: not as root, and a missing binary is not retried.

    `what` names the process in the refusal ("Claude Code", "a Codex turn").
    """
    refuse_root(what)
    try:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    except (FileNotFoundError, PermissionError) as exc:
        if (cwd := kwargs.get("cwd")) is not None and not os.path.isdir(cwd):
            raise  # the working directory is missing, not the binary
        # The same image on the next attempt has the same file system.
        raise flyte.errors.NonRecoverableError(
            f"Cannot start {what}: `{argv[0]}` is missing or not executable ({exc}). "
            "It will not be retried because it would fail identically."
        ) from exc


def no_result_error(status: int | None, stdout: str, stderr: str) -> RuntimeError:
    """Return the error for a `claude -p` sub-agent that exited non-zero without any result JSON.

    It never started a conversation (a refused flag, a broken install, a login that is gone), and it would
    do the same again. `claude -p` puts its own errors on stdout, so both tails are shown.
    """
    return flyte.errors.NonRecoverableError(
        f"sub-agent exited {status} without a result.\n"
        f"stdout: {stdout[-1500:] or '(empty)'}\n"
        f"stderr: {stderr[-1500:] or '(empty)'}\n"
        "It will not be retried because it would fail identically."
    )


def exit_error(engine: str, status: int, tail: str, proc: object) -> RuntimeError:
    """Return the error for an author session that exited with `status`.

    One that exited with a plain failure status before it ever drew its input prompt (a refused flag, a
    broken install, no login) fails the same way on every retry. Anything else, a crash mid-conversation or a
    kill, resumes from the last checkpoint. Codex has no input prompt (one process per turn), so its exit is
    always retried; so is any process that does not say whether it reached one.
    """
    what = "Codex" if engine == "codex" else "Claude Code"
    reached_prompt = engine == "codex" or getattr(proc, "reached_prompt", True)
    if 0 < status < _SIGNAL_EXIT and not reached_prompt:
        return flyte.errors.NonRecoverableError(
            f"{tail}\n\n{what} exited with status {status} before reaching its input prompt. "
            "It will not be retried because it would fail identically."
        )
    return RuntimeError(f"{what} exited with status {status}; retrying from the last checkpoint.\n{tail}")
