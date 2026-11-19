"""Gate basics every other gate module builds on: a check's status, running a tool, project paths."""

from __future__ import annotations

import contextlib
import enum
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from . import _lane_shards, _load_clock, _proc_health, _secret_env


class Status(enum.Enum):
    """The outcome of one check.

    A plain Enum, not an IntEnum, so a status can never be compared with a process exit code. The
    values are the labels written to standards-results.json.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    #: The check does not apply to this project and never blocks.
    NOT_APPLICABLE = "not applicable"
    #: History that cannot be judged. Visible like a gap, but never a pass.
    UNKNOWN = "unknown (legacy history)"
    #: The tool is not installed (exit 127).
    TOOL_UNAVAILABLE = "tool unavailable"
    #: The check could not be run here (a toolchain that cannot be installed), so nothing is known: neither
    #: the project's failure nor a pass. The reason is in the output.
    NOT_RUN = "not run"
    #: The test lanes could not be placed on the cluster (see `_capacity`): nothing is known about the
    #: project's change, so it blocks review like a gap, but it is not the project's failure.
    NO_CAPACITY = "no capacity"
    #: A FAIL the owner accepted in `waivers.toml`. It does not block.
    WAIVED = "WAIVED"
    #: The factory could not do its part of the check (it could not compute the project's changes), which is
    #: not the project's failure: it does not block, but it holds the work back from review (`holds`).
    HARNESS_FAULT = "harness fault"

    @classmethod
    def from_exit(cls, code: int) -> Status:
        # Only exit codes: a Status passed back in would map to FAIL, so a non-blocking result would block.
        if not isinstance(code, int) or isinstance(code, (bool, Status)):
            raise TypeError(f"from_exit takes an exit code, not {type(code).__name__}")
        return cls.PASS if code == 0 else cls.TOOL_UNAVAILABLE if code == 127 else cls.FAIL

    @property
    def blocks(self) -> bool:
        return self in {Status.FAIL, Status.TOOL_UNAVAILABLE, Status.NO_CAPACITY}

    @property
    def fails(self) -> bool:
        """Blocks because of the project, as opposed to the cluster: `NO_CAPACITY` is reported separately."""
        return self.blocks and self is not Status.NO_CAPACITY

    @property
    def holds(self) -> bool:
        """Whether the work must not go to review, though it is not the project's failure."""
        return self is Status.HARNESS_FAULT


_SKIP_DIRS = {".git", ".claude-flyte", "node_modules", ".venv"}


#: Seconds a tool may run, unless its rule says otherwise with a `Timeout: <seconds>` line.
_TOOL_TIMEOUT = 300


#: The fail-to-pass runs of a project's tests: the limit for a real suite. A round that touched many
#: test files (a debt paydown) runs most of the suite, which took ~6 minutes for this repository.
TEST_TIMEOUT = 1200


_MAX_TOOL_TIMEOUT = 7200


_OUTPUT_TAIL = 12000


_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def read_cpu_allowance() -> int:
    """CPUs this process may really use: the cgroup quota (`cpu.max`) when lower than the affinity set.

    `os.cpu_count()` is the node's, which is 16 on a 4-CPU-quota pod: pytest-xdist's `-n auto` would start
    16 workers there. The launcher's reading (`_lane_shards`, standard library only), so both agree.
    """
    return _lane_shards.read_cpu_allowance(_CPU_MAX)


def set_xdist_env(env: dict[str, str]) -> dict[str, str]:
    """`env` with xdist's `auto` worker count set to `read_cpu_allowance()`, unless the caller chose one."""
    return {"PYTEST_XDIST_AUTO_NUM_WORKERS": str(read_cpu_allowance()), **env}


def _run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = _TOOL_TIMEOUT,
    tail: int | None = _OUTPUT_TAIL,
) -> tuple[int, str]:
    """Run a tool or git in the workspace, which the session wrote: never with the supervisor's secrets.

    `env` is the caller's own (`_secret_env.child_env(...)`); without one the tool gets the scrubbed
    environment of this process. Git gets `_secret_env.GIT_SAFE_CONFIG`, so `.git/config` names no
    program it runs. `tail` keeps that many trailing characters of a tool's output; None keeps all.
    """
    if args[0] == "git":
        args = _secret_env.git_argv(*args[1:])
    # Files, not pipes: nothing has to drain them while the load clock is polled, and a grandchild that
    # outlives the tool cannot hold this call open by keeping a pipe's write end.
    with (
        tempfile.TemporaryFile("w+", errors="replace") as out,
        tempfile.TemporaryFile("w+", errors="replace") as err,
    ):
        try:
            # Tools print whatever bytes they like; a replacement character beats a crashed gate. A new
            # session lets the clock follow, and the kill reach, every process the tool started.
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                env=_secret_env.child_env() if env is None else env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return 127, f"{exc.filename} not found"
        except PermissionError as exc:
            # A restored workspace can lose exec bits; say so rather than let the supervisor's gate stage die.
            return (
                126,
                f"{exc.filename or args[0]} is not executable ({type(exc).__name__}: {exc.strerror or exc})",
            )
        except OSError as exc:
            return (
                126,
                f"{exc.filename or args[0]} could not start ({type(exc).__name__}: {exc.strerror or exc})",
            )
        # The limit is on load-adjusted time: a busy shared node must not fail a tool that is working.
        clock = _load_clock.LoadClock(timeout, proc.pid)
        try:
            # Poll before waiting, so a command is judged at least once: the clock's first scan of /proc can
            # outlast a short command in a pod with a large process table (#34).
            while not clock.poll():
                try:
                    proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
            else:
                _kill_group(proc)
                return 124, _describe_timeout(timeout, clock)
        except BaseException:
            _kill_group(proc)  # this thread was interrupted: no tool is left running behind the gate
            raise
        out.seek(0)
        err.seek(0)
        output = out.read() + err.read()
    return proc.returncode, output if args[0] == "git" or tail is None else output[-tail:]


def _describe_timeout(timeout: int, clock: _load_clock.LoadClock) -> str:
    message = f"command timed out after {timeout} seconds ({clock.attribution()})"
    health = _proc_health.snapshot()
    if health.burdened:
        message += (
            f"; this pod has {health.zombies} zombie processes under PID 1 {health.init or 'unknown'}, "
            "which slows every scan of /proc"
        )
    return message


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def is_workspace(checkout: Path) -> bool:
    """Return True when the default branch of `checkout` has a `projects/` directory at its root.

    That directory (the template commits `projects/.gitkeep`) is what makes a repo a workspace. It is
    read from `origin/HEAD`, not the working tree: a root-layout project that creates a
    `projects/` directory of its own on its branch must not change what layout it is judged by.
    """
    if not checkout.is_dir():
        return False
    code, kind = _run(["git", "cat-file", "-t", "origin/HEAD:projects"], checkout)
    return not code and kind.strip() == "tree"


def project_rel(name: str, layout: str = "sandbox", folder: str = "") -> str:
    """Return the project's directory relative to the workspace: `.` for a root-layout project.

    `folder` is the `projects/<folder>/` a workspace launch is judged in when it is not the launch's
    name (`_project_folder`); other layouts have no such choice.
    """
    return {"root": ".", "workspace": f"projects/{folder or name}"}.get(layout, name)


def project_dir(workspace: Path, name: str, layout: str = "sandbox", folder: str = "") -> Path:
    """Return what the gates treat as "the project".

    That is `<workspace>/<name>` in the shared sandbox repo, `<workspace>/projects/<folder or name>` in a
    workspace (`folder` is the launch's routed folder), and the whole repo for a project on any other repo
    (see `_git.project_layout`).
    """
    return workspace / project_rel(name, layout, folder)


def checks_rel(name: str, layout: str = "sandbox", folder: str = "") -> str:
    """Where the project's `checks.sh` is, relative to the workspace.

    A root-layout project has no `<name>/` directory to keep it in; the harness directory is
    git-ignored there, so the script is committed with `git add -f`.
    """
    return (
        f".claude-flyte/{name}/checks.sh"
        if layout == "root"
        else f"{project_rel(name, layout, folder)}/checks.sh"
    )


def _project_files(project: Path) -> list[str]:
    """Project-relative paths, without dependency and harness trees."""
    paths: list[str] = []
    for directory, children, names in os.walk(project):
        children[:] = [n for n in children if n not in _SKIP_DIRS]
        paths.extend(str((Path(directory) / n).relative_to(project)) for n in names)
    return paths
