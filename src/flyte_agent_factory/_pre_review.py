"""The pre-review checks a declared DONE runs before any review: readiness, `checks.sh`, the standards gate.

`_supervise._pump` runs them in steps (`begin`, `check`, `finish`) and decides what follows: a bounce, an
escalation, a review round. It also catches what `check` raises, at its loop boundary (`could_not_run`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import time
from pathlib import Path

from flyte_agent_factory import (
    _capacity,
    _demo_checks,
    _gate_tests,
    _gates,
    _inbox,
    _session_config,
    _supervise_support,
    _timeline,
)


@dataclasses.dataclass
class PreReview:
    """What the pre-review checks found."""

    work: _gate_tests.GateWork
    #: Every problem, in the order the session should fix them: a repair needs the whole checklist.
    problems: list[str] = dataclasses.field(default_factory=list)
    #: A declared check or the gate failed (a "preflight" bounce), rather than readiness ("done").
    check_failed: bool = False
    started: float = 0.0
    checks_seconds: float = 0.0
    gate_problem: str = ""
    #: Why `checks.sh` found no room for its test lanes (`_capacity`): not the project's problem.
    checks_capacity: str = ""


async def begin(workspace: Path, demo_package: str) -> PreReview:
    """Start the pre-review checks: the demo package and DONE readiness, and the gate's shared work."""
    started = time.monotonic()
    problems: list[str] = []
    demo_problem = await _demo_checks._demo_package_problem(workspace, demo_package)
    if demo_problem:
        problems.append(demo_problem)
    not_ready = await _supervise_support._done_preflight(workspace)
    if not_ready:
        problems.append(not_ready)
    # checks.sh leaves its pytest run here; the gate reads it instead of repeating it,
    # unless this round changed what decides that run's results (checks.sh, conftest).
    harness = _gates.changed_test_harness(
        workspace,
        _session_config._project_name,
        _gates.resolve_base(workspace, _session_config._project_name),
    )
    work = _gate_tests.GateWork(
        junit_dir=Path(tempfile.mkdtemp(prefix="factory-junit-")),
        checks_started=time.time(),
        reuse_refused=", ".join(harness),
        child_lanes=True,
    )
    return PreReview(work, problems, started=started)


async def check(checked: PreReview, workspace: Path, timeline: _timeline._Timeline, note: str) -> None:
    """Run the declared `checks.sh` and the gate on the DONE `note`; a tool that cannot start raises."""
    work = checked.work
    checks_rel = _session_config._checks_rel(_session_config._project_name, _session_config._layout())
    if (workspace / checks_rel).is_file():
        checks_problem = await _supervise_support._run_declared_checks(workspace, timeline, work.junit_dir)
        # What the run left for the gate: whether it timed out (and why), the image.
        left = timeline.checks or {}
        work.checks_load = left.get("load", "")
        work._image = left.get("image")
        if isinstance(checks_problem, _capacity.ChecksNoCapacity):
            checked.checks_capacity = checks_problem.capacity
        else:
            # A run cut off while its lanes found no room is the cluster's: the gate's own run may find it.
            work.checks_timed_out = left.get("timed_out", "")
        checked.checks_seconds = time.monotonic() - checked.started
        if checks_problem and not checked.checks_capacity:
            checked.problems.append(checks_problem)
            checked.check_failed = True
    else:
        checked.problems.append(
            f"Write `{checks_rel}` with the bash commands "
            "that prove the build and tests pass, run from the workspace root."
        )
    # Fetching vendored upstream blocks: it is done here, off the loop, so the
    # standards selection below (on the loop) only reads the cache.
    await asyncio.to_thread(_session_config.warm_vendored_cache)
    checked.gate_problem = await asyncio.to_thread(
        _gates.run,
        workspace,
        _session_config._project_name,
        _session_config._project_brief,
        _session_config._demo_kind,
        note,
        _session_config._applicable_standards(),
        timeline,
        _session_config._layout(),
        list(_inbox._directions),
        work,
        _session_config._project_repo,
        _session_config._project_folder,
    )


def could_not_run(checked: PreReview, timeline: _timeline._Timeline, exc: Exception) -> None:
    """Record that `check` raised: a tool that cannot start (a restored .venv without exec bits) bounces."""
    checked.problems.append(
        f"pre-review could not run: {type(exc).__name__}: {exc} -- "
        "fix the environment (for example `uv sync`, or chmod +x the interpreter)."
    )
    checked.check_failed = True
    timeline.row("⚠️", f"pre-review could not run: {type(exc).__name__}: {exc}")


def finish(checked: PreReview, timeline: _timeline._Timeline) -> None:
    """Clear the gate's scratch, add the gate's problem and record the pre-review row on `timeline`."""
    work = checked.work
    if work.junit_dir:
        shutil.rmtree(work.junit_dir, ignore_errors=True)
    if checked.gate_problem:
        checked.problems.append(checked.gate_problem)
        checked.check_failed = True
    timeline.row(
        "🚧",
        _supervise_support._pre_review_row(time.monotonic() - checked.started, checked.checks_seconds, work),
    )
