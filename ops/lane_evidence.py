"""Re-run one of the six live lane measurements of docs/test-lanes.md; write its evidence file.

Each scenario runs the engine's own code, at a commit, the way the pre-review does, with its lanes sent to
child actions of the current run:

- `checks-2cpu`, `checks-4cpu`: the supervisor's checks.sh step (`_run_declared_checks`, 900 s limit).
- `gate-2cpu`, `gate-4cpu`: the standards gate's shared head run (`_run_head_pytest`, 1200 s limit).
- `gate-2cpu-failing`: the same at 2 CPUs with one deliberately failing test, committed
  in the scratch clone only.
- `gate-64cpu`: the same with 64-CPU shards, which no node of the cluster can place.

The commit is cloned into a scratch directory, so the working tree's state does not matter and nothing is
pushed. The evidence file records the commit, wall time, outcome, junit counts and failures, any capacity
reason, and the lanes' verdict lines, which name each shard's child action.

    python3 ops/lane_evidence.py gate-2cpu [--commit HEAD] [--out docs/test-lanes-evidence]

Run it with the supervisor's interpreter (`python3` on PATH in a session pod), which has the Flyte controller.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

#: scenario -> (step, CPUs per shard, add a failing test)
SCENARIOS: dict[str, tuple[str, int, bool]] = {
    "checks-2cpu": ("checks", 2, False),
    "checks-4cpu": ("checks", 4, False),
    "gate-2cpu": ("gate", 2, False),
    "gate-4cpu": ("gate", 4, False),
    "gate-2cpu-failing": ("gate", 2, True),
    "gate-64cpu": ("gate", 64, False),
}

FAILING_TEST = '''"""A deliberately failing test for the live demonstration (scratch clone only)."""


def test_a_deliberately_failing_test_for_the_demo():
    """Fail, so that the lane, the gate and the junit must say so."""
    assert False, "deliberately failing"
'''

#: Output lines worth keeping: verdicts, shards (with their child actions), failures and capacity.
_KEEP = re.compile(
    r"lane verdict|shard \d+/\d+|^FAILED |no capacity|ran here|could not be placed|test-lane: wall"
)


_CONTROLLER = re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+Z\s+(WARN|INFO|DEBUG|ERROR)\s")
_LAUNCHED = re.compile(r"Merged action: ===> (\w+)")


def _shorten(line: str, width: int = 300) -> str:
    return line if len(line) <= width else line[: width - 3] + "..."


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
    ).stdout.strip()


def _clone(commit: str, scratch: Path) -> tuple[Path, Path, str, str]:
    """Clone the workspace at `commit`; return (clone root, project in it, full sha, last code commit)."""
    top = Path(_git("rev-parse", "--show-toplevel", cwd=HERE))
    rel = HERE.relative_to(top)
    sha = _git("rev-parse", commit, cwd=top)
    code = _git(
        "log",
        "-1",
        "--format=%H",
        sha,
        "--",
        f"{rel}/src",
        f"{rel}/tests",
        f"{rel}/checks.sh",
        f"{rel}/factory-improvement/checks.sh",
        cwd=top,
    )
    clone = scratch / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", str(top), str(clone)], check=True, stdin=subprocess.DEVNULL
    )
    _git("checkout", "-q", sha, cwd=clone)
    # The run context the supervisor writes at the workspace root, which checks.sh links into the project.
    if (top / ".flyte").is_dir():
        shutil.copytree(top / ".flyte", clone / ".flyte")
    return clone, clone / rel, sha, code


def _add_failing_test(clone: Path, project: Path) -> None:
    (project / "tests" / "test_zz_demo_failure.py").write_text(FAILING_TEST)
    _git("add", "-A", cwd=clone)
    _git(
        "-c",
        "user.name=lane-evidence",
        "-c",
        "user.email=lane-evidence@localhost",
        "commit",
        "-q",
        "-m",
        "A deliberately failing test (scratch clone only)",
        cwd=clone,
    )


def _run_checks(clone: Path, project: Path, scratch: Path) -> tuple[str, str, list[object] | None, str]:
    """Run the supervisor's checks.sh step; return (outcome, output, junit cases, capacity reason)."""
    from flyte_agent_factory import _capacity, _gate_tests, _supervise_support, _timeline

    junit_dir = scratch / "junit"
    junit_dir.mkdir()
    script = str((project / "checks.sh").relative_to(clone))
    problem = asyncio.run(
        _supervise_support._run_declared_checks(clone, _timeline._Timeline(), junit_dir, script=script)
    )
    log = clone / _supervise_support._checks_log_rel("")
    output = log.read_text(errors="replace") if log.exists() else ""
    cases: list[object] = []
    for lane in ("fast", "slow"):
        if (junit_dir / f"{lane}.xml").exists():
            cases.extend(_gate_tests.parse_junit(junit_dir / f"{lane}.xml"))
    if not problem:
        return "PASS", output, cases, ""
    if isinstance(problem, _capacity.ChecksNoCapacity):
        return "NO CAPACITY", output, cases, str(problem)
    return f"FAIL: {problem}", output, cases, ""


def _run_gate(project: Path) -> tuple[str, str, list[object] | None, str]:
    """Run the gate's shared head run; return (outcome, output, junit cases, capacity reason)."""
    from flyte_agent_factory import _gate_tests

    head = _gate_tests._run_head_pytest(project, child_lanes=True)
    if head is None:
        return "NO PYTEST", "", None, ""
    status = _gate_tests.judge_shared_run(head, head.code)
    return f"{status.name} (exit {head.code})", head.output, list(head.cases or []), head.capacity


def main(argv: list[str] | None = None) -> int:
    """Run one scenario and write `<out>/<scenario>.txt`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--out", type=Path, default=HERE / "docs" / "test-lanes-evidence")
    args = parser.parse_args(argv)
    if not os.environ.get("FLYTE_INTERNAL_EXECUTION_ID"):
        print("lane_evidence: needs a Flyte run to send lanes to child actions", file=sys.stderr)
        return 2
    step, cpus, failing = SCENARIOS[args.scenario]
    limit = 900 if step == "checks" else 1200
    with tempfile.TemporaryDirectory(prefix="lane-evidence-") as tmp:
        scratch = Path(tmp)
        clone, project, sha, code_commit = _clone(args.commit, scratch)
        if failing:
            _add_failing_test(clone, project)
        sys.path.insert(0, str(project / "src"))
        os.environ["AGENT_FACTORY_LANE_CPUS"] = str(cpus)
        os.environ.pop("AGENT_FACTORY_LANE_RUN_HERE", None)
        started = time.monotonic()
        if step == "checks":
            outcome, output, cases, capacity = _run_checks(clone, project, scratch)
        else:
            outcome, output, cases, capacity = _run_gate(project)
        wall = time.monotonic() - started
    failed = [f"{c.classname}::{c.name}" for c in cases or [] if not c.passed]  # type: ignore[attr-defined]
    extra = ", one deliberately failing test" if failing else ""
    # The controller's own log lines are long; only the names of the child actions it launched are kept.
    launched = sorted(set(_LAUNCHED.findall(output)))
    kept = [
        _shorten(line) for line in output.splitlines() if _KEEP.search(line) and not _CONTROLLER.match(line)
    ]
    lines = [
        f"scenario: {args.scenario} ({step}, {cpus}-CPU shards{extra})",
        f"measured: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        f"commit: {sha} (code last changed at {code_commit})",
        f"run: {os.environ.get('FLYTE_INTERNAL_EXECUTION_ID', '')}",
        f"limit: {limit} s",
        f"wall: {wall:.0f} s",
        f"outcome: {outcome}",
        f"junit cases: {len(cases) if cases is not None else 'none'}, failed: {len(failed)}",
        *(f"failed: {name}" for name in failed),
        f"capacity: {capacity or '-'}",
        f"ran here (session pod): {'yes' if 'ran here' in output else 'no'}",
        f"child actions launched: {' '.join(launched) or '-'}",
        "",
        "--- lane verdict and shard lines ---",
        *kept,
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{args.scenario}.txt"
    target.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"lane_evidence: wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
