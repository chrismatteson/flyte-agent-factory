"""The results table of a sealed benchmark (design layer 8): counted runs, void runs, recallable tasks.

A run counts only if its seal audit (`_seal_audit`) called it clean AND the audit had the gold patch
(`"gold": true`): without gold the audit never checked what the run reached. A run with no verdict is void,
with the reason "no audit": a result nobody checked is not a result. There is one table; nothing is
reported for void runs except that they are void and why.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

NO_AUDIT = "no audit"
NO_GOLD = "no gold"


@dataclass(frozen=True)
class ArmStats:
    """One arm's counted runs (the clean runs), how many resolved, and the rate (None if nothing counted)."""

    counted: int
    resolved: int

    @property
    def rate(self) -> float | None:
        """Return resolved / counted, or None when no run was counted."""
        return self.resolved / self.counted if self.counted else None


@dataclass
class Summary:
    """What a benchmark reports: per-arm results, the void runs, and the tasks a model may recall."""

    arms: dict[str, ArmStats] = field(default_factory=dict)
    void: list[tuple[str, str, str, str]] = field(default_factory=list)  # (name, task, arm, first reason)
    missing: list[str] = field(default_factory=list)
    recallable: list[str] = field(default_factory=list)
    #: Per (task, arm) with no counted run after it, the latest void run: the ones that still need a rerun.
    needs_rerun: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Return the results table, the void table and the recallable list."""
        lines = [
            "## Results",
            "",
            "| Arm | Counted | Resolved | Rate |",
            "| --- | --- | --- | --- |",
        ]
        for arm, stats in self.arms.items():
            rate = "n/a" if stats.rate is None else f"{stats.rate:.1%}"
            lines.append(f"| {arm} | {stats.counted} | {stats.resolved} | {rate} |")
        lines += ["", "## Void (excluded)", ""]
        if self.void:
            lines += ["| Run | Task | Arm | Reason |", "| --- | --- | --- | --- |"]
            lines += [f"| {name} | {task} | {arm} | {reason} |" for name, task, arm, reason in self.void]
        else:
            lines.append("(none)")
        lines += ["", "## Recallable tasks", ""]
        lines += [f"- {task}" for task in self.recallable] or ["(none)"]
        return "\n".join(lines) + "\n"


def first_reason(verdict: dict[str, Any]) -> str:
    """Return the first reason of an audit verdict (a kind string, or a finding's `kind`)."""
    reasons = verdict.get("reasons") or []
    if not reasons:
        return "void"
    first = reasons[0]
    return str(first.get("kind", "void")) if isinstance(first, dict) else str(first)


def summarize(
    runs: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    model_cutoff: str,
) -> Summary:
    """Return the benchmark's summary: rates over clean runs, the void runs, the recallable tasks."""
    counted: dict[str, list[bool]] = {}
    counted_by: dict[tuple[str, str], str] = {}  # (task, arm) -> the counted run's name
    counted_at: dict[tuple[str, str], int] = {}  # (task, arm) -> position of its counted run
    summary = Summary()
    names = [run["name"] for run in runs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError("; ".join(f"run {name} is listed more than once" for name in dupes))
    unknown = sorted({run["task"] for run in runs} - set(tasks))
    if unknown:
        # A task with no created_at could never be listed as recallable, so its runs cannot be counted.
        raise ValueError(
            f"--tasks has no entry for task(s): {', '.join(unknown)}; every run's task needs a created_at"
        )
    for position, run in enumerate(runs):
        if not isinstance(run["resolved"], bool):  # bool("false") is True: never coerce
            raise ValueError(f"run {run['name']}: resolved must be true or false, not {run['resolved']!r}")
        counted.setdefault(run["arm"], [])
        verdict = verdicts.get(run["name"])
        if verdict is None:
            summary.missing.append(run["name"])
            summary.void.append((run["name"], run["task"], run["arm"], NO_AUDIT))
        elif verdict.get("verdict") == "clean" and verdict.get("gold") is not True:
            summary.void.append((run["name"], run["task"], run["arm"], NO_GOLD))
        elif verdict.get("verdict") == "clean":
            key = (run["task"], run["arm"])
            if key in counted_by:
                raise ValueError(
                    f"runs {counted_by[key]} and {run['name']} are both clean for task {run['task']} in arm "
                    f"{run['arm']}: a task counts once per arm, and a rerun only follows a void run"
                )
            counted_by[key] = run["name"]
            counted_at[key] = position
            counted[run["arm"]].append(run["resolved"])
        else:
            summary.void.append((run["name"], run["task"], run["arm"], first_reason(verdict)))
    latest_void: dict[tuple[str, str], str] = {}  # (task, arm) -> its last void run, in input order
    for name, task, arm, _ in summary.void:
        latest_void[(task, arm)] = name
    summary.needs_rerun = [
        name for key, name in latest_void.items() if counted_at.get(key, -1) < names.index(name)
    ]
    summary.arms = {arm: ArmStats(len(r), sum(r)) for arm, r in sorted(counted.items())}
    cutoff = date.fromisoformat(model_cutoff[:10])
    summary.recallable = sorted(
        t for t, info in tasks.items() if date.fromisoformat(info["created_at"][:10]) < cutoff
    )
    return summary


def next_run_name(name: str, existing_names: list[str]) -> str:
    """Return the name of the next rerun: `<base>-r<N+1>`, at least r2, not already taken.

    A rerun is a fresh project (new branch, new record root, no `workspace_in`), so it needs its own name.
    """
    stripped = re.sub(r"-r\d+$", "", name)
    # A trailing `-rN` is a rerun suffix only if its base is a known project; else it is part of the name.
    is_rerun = stripped != name and any(
        e == stripped or (e != name and re.fullmatch(re.escape(stripped) + r"-r\d+", e))
        for e in existing_names
    )
    base = stripped if is_rerun else name
    highest = 1
    for existing in existing_names:
        if existing == base:
            continue
        match = re.fullmatch(re.escape(base) + r"-r(\d+)", existing)
        if match:
            highest = max(highest, int(match.group(1)))
    number = highest + 1
    while f"{base}-r{number}" in existing_names:
        number += 1
    return f"{base}-r{number}"


def load_verdicts(directory: Path) -> dict[str, dict[str, Any]]:
    """Return the audit verdicts in `directory`: `<run name>.json`, each `_seal_audit --json` output."""
    verdicts: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            verdict = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"verdict {path.name} is not JSON: {exc}") from exc
        if not isinstance(verdict, dict):
            raise ValueError(f"verdict {path.name} is not a JSON object")
        verdicts[path.stem] = verdict
    return verdicts


def check_inputs(runs: Any, tasks: Any) -> None:
    """Raise ValueError naming what is wrong with the runs list or the tasks object the CLI was given."""
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        raise ValueError("--runs must be a JSON list of objects")
    for index, run in enumerate(runs):
        missing = [key for key in ("name", "task", "arm", "resolved") if key not in run]
        if missing:
            raise ValueError(f"--runs entry {index} lacks {', '.join(missing)}")
    if not isinstance(tasks, dict) or not all(
        isinstance(t, dict) and "created_at" in t for t in tasks.values()
    ):
        raise ValueError('--tasks must be a JSON object of {task: {"created_at": date}}')


def main(argv: list[str] | None = None) -> int:
    """Print the results table, the void runs with each one's rerun name, and the recallable tasks."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._seal_report", description=__doc__)
    parser.add_argument(
        "--runs", type=Path, required=True, help='JSON list of {"name","task","arm","resolved"}'
    )
    parser.add_argument(
        "--verdicts", type=Path, required=True, help="directory of <run name>.json audit outputs"
    )
    parser.add_argument("--tasks", type=Path, required=True, help='JSON object {task: {"created_at": date}}')
    parser.add_argument("--model-cutoff", required=True, help="the model's training cutoff, YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        runs = json.loads(args.runs.read_text())
        tasks = json.loads(args.tasks.read_text())
        check_inputs(runs, tasks)
        summary = summarize(runs, load_verdicts(args.verdicts), tasks, args.model_cutoff)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # A bad input file is the operator's to fix: say which, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = summary.to_markdown()
    if summary.needs_rerun:
        names = [run["name"] for run in runs]
        text += "\n## Reruns (fresh projects, nothing inherited)\n\n"
        for name in summary.needs_rerun:
            rerun = next_run_name(name, names)
            names.append(rerun)
            text += f"- {name} -> {rerun}\n"
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
