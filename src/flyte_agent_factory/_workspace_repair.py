"""Put a restored workspace back in a runnable state: exec bits, then the project's virtualenv.

A workspace restored from an older run (file-by-file `Dir`) or from a checkpoint can have lost its
POSIX modes. Without them `.venv/bin/python` cannot start, and the supervisor's gates fail with a
PermissionError. This runs once after `_state.restore` and never raises: a project that cannot be
repaired is reported, and the session finds out when it runs its own checks.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path

from . import _gate_base, _timeline

_PROBE_TIMEOUT_S = 30
_UV_SYNC_TIMEOUT_S = 300
# Trees that can hold very many files and never a project virtualenv worth repairing.
_SKIP_DIRS = {".git", "node_modules"}


def _add_exec(path: Path) -> bool:
    """Set the x bits wherever the r bits are, on a regular file lacking owner-x; True when changed."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & stat.S_IXUSR:
        return False
    path.chmod(info.st_mode | ((info.st_mode & 0o444) >> 2) | stat.S_IXUSR)
    return True


def _is_program(path: Path) -> bool:
    """Return True for a venv's interpreter or a file the kernel can run, not `activate` or `*.bat`."""
    if path.name.startswith("python"):
        return True
    try:
        with path.open("rb") as file:
            head = file.read(4)
    except OSError:
        return False
    return head == b"\x7fELF" or head.startswith(b"#!")


def _restore_exec_bits(workspace: Path) -> int:
    """Repair `.venv/bin/*` and the files git records as 100755; return how many files changed."""
    repaired = 0
    for directory, children, _ in os.walk(workspace, followlinks=False):
        if Path(directory).name == ".venv":
            bin_dir = Path(directory) / "bin"
            if bin_dir.is_dir() and not bin_dir.is_symlink():
                repaired += sum(_add_exec(p) for p in bin_dir.iterdir() if _is_program(p))
            children[:] = []  # the rest of a venv is site-packages
            continue
        children[:] = [c for c in children if c not in _SKIP_DIRS]
    code, listing = _gate_base._run(["git", "ls-files", "-s", "-z"], workspace, tail=None)
    if code == 0:
        for entry in listing.split("\0"):
            meta, _, name = entry.partition("\t")
            if meta.startswith("100755 ") and name:
                path = workspace / name
                if path.is_file() and not path.is_symlink():
                    repaired += _add_exec(path)
    return repaired


def _python_problem(python: Path, workspace: Path) -> str:
    """Why the project's interpreter cannot run, or "" when it can."""
    code, output = _gate_base._run([str(python), "-c", ""], workspace, timeout=_PROBE_TIMEOUT_S)
    return "" if code == 0 else (output.strip().splitlines() or [f"exit {code}"])[-1]


def _repair(workspace: Path) -> tuple[int, str, str]:
    """(files repaired, problem or "", what was tried) for a restored workspace."""
    repaired = _restore_exec_bits(workspace)
    python = workspace / ".venv/bin/python"
    # A venv whose interpreter is gone (a per-file upload that skipped symlinks) is unusable, not absent.
    if not python.parent.is_dir():
        return repaired, "", ""
    problem = _python_problem(python, workspace)
    if not problem:
        return repaired, "", ""
    tried = "try `uv sync`"
    if (workspace / "uv.lock").is_file() and shutil.which("uv"):
        _gate_base._run(["uv", "sync", "--frozen"], workspace, timeout=_UV_SYNC_TIMEOUT_S)
        tried = "ran `uv sync`"
        problem = _python_problem(python, workspace)
    return repaired, problem, tried


async def repair_workspace(workspace: Path, timeline: _timeline._Timeline) -> None:
    """Repair a restored workspace and report it on the timeline. Never raises."""
    try:
        repaired, problem, tried = await asyncio.to_thread(_repair, workspace)
    except OSError as exc:  # a failed repair must not stop the project from starting
        timeline.row("⚠️", f"workspace repair failed: {type(exc).__name__}: {exc}")
        return
    if repaired:
        timeline.row("🔧", f"restored exec bits on {repaired} files")
    if problem:
        timeline.row("⚠️", f"project environment unusable: .venv/bin/python {problem}; {tried}")
