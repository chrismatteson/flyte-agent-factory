"""Re-run the study's decisions through the changed judge (judge, finding check, mechanical verdict).

Mirrors the study's `rejudge.py` flow and reuses its code (budget, workspaces, usage pricing, call retry);
only the per-rep steps differ: after the judge call, a reply with blocking findings or a FAIL goes to the
engine's finding check in a second fresh workspace, and `_findings.decide` gives the verdict.

    python factory-improvement/judge_consistency/run.py estimate     # offline: writes cost_estimate.json
    python factory-improvement/judge_consistency/run.py --dry-run --limit 1
    python factory-improvement/judge_consistency/run.py --cap 150    # the paid run, resumable
    python factory-improvement/judge_consistency/run.py charge-interrupted --calls 4   # offline: ledger only

Raw replies are written under `--out` (default: the workspace's git-ignored harness directory), never under
this project.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import prompts
import study as study_mod
from flyte.io import Dir

_log = logging.getLogger(__name__)
REPS = (1, 2)
DEFAULT_CAP_USD = 150.0
EXIT_BUDGET = 3
ROLE = "verify"
NO_VERDICT_ATTEMPTS = 3  # `_review._NO_VERDICT_ATTEMPTS`
CHECK_ATTEMPTS = 2  # the engine's `_check_findings.run`: one retry
CHECK_MAX_TURNS = 40  # `_check_findings.run`
#: The judge's re-run cost is estimated from the study's own re-run cost, plus this much.
JUDGE_ESTIMATE_MARGIN = 1.15
#: What one finding check is expected to cost (Sonnet, low effort), and for how many study reps we expect
#: one: the ones whose raw verdict was FAIL.
CHECK_ESTIMATE_USD = 0.40
#: Each call reserves this multiple of its estimate. The study found real calls cost up to 1.8x the recorded
#: figure; here the estimate comes from real re-run costs, so the margin is smaller.
RESERVE_MARGIN = 1.6
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass(frozen=True)
class CallSetting:
    """How one reviewer call is made."""

    model: str
    effort: str
    max_turns: int


JUDGE_SETTING = CallSetting("opus", "high", 120)

#: One call: (prompt, workdir, setting) -> (reply text with the usage trailer, child action names).
CallFn = Callable[[str, Path, CallSetting], Awaitable[tuple[str, list[str]]]]
#: The study's retry wrappers: (call, prompt, workdir) -> the call's result, with its retries and queue waits.
Retry = Callable[
    [Callable[[str, Path], Awaitable[tuple[str, list[str]]]], str, Path], Awaitable[tuple[str, list[str]]]
]


@dataclass
class Run:
    """Everything a rep needs: the study, the engine's findings module, and where things go."""

    study: study_mod.Study
    findings: ModuleType
    budget: Any  # the study's `rejudge.Budget`
    call: CallFn
    out: Path
    source: Path
    work_root: Path
    check_setting: CallSetting
    briefs: dict[str, Any] = field(default_factory=dict)
    ledger: Path | None = None  # the committed spend floor (`write_ledger`); None in a dry run


def _make_zero_tokens() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_FIELDS, 0)


@dataclass
class _RepState:
    """What a rep has produced so far; also what `_abandon` must account for if it is cut short."""

    text: str = ""
    check_text: str | None = None
    attempts: int = 0
    judge_usd: float = 0.0
    check_usd: float = 0.0
    measured: bool = True
    actions: list[str] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    usage: dict[str, dict[str, int]] = field(
        default_factory=lambda: {p: _make_zero_tokens() for p in ("judge", "check")}
    )

    @property
    def spent(self) -> float:
        """Return what this rep's calls cost so far."""
        return self.judge_usd + self.check_usd


# --------------------------------------------------------------------------- #
# Decisions and estimates
# --------------------------------------------------------------------------- #


def select_decisions(study: study_mod.Study) -> list[dict[str, Any]]:
    """Return the sampled decisions whose two study reps both gave PASS or FAIL, in file order."""
    sample = study.recover.read_jsonl(study.dir / "data" / "sample.jsonl")
    decisions = study.analyze.load_decisions(sample, study.dir / "results" / "rejudge")
    return [d for d in decisions if study.analyze.is_pair(d)]


def estimate_decision(study: study_mod.Study, decision_id: str) -> tuple[float, float]:
    """Return (judge estimate per rep, check estimate for both reps) in USD for one decision."""
    reps = study.analyze.load_reps(study.dir / "results" / "rejudge", decision_id).values()
    judge = sum(float(r["usd"]) for r in reps) / len(reps) * JUDGE_ESTIMATE_MARGIN
    check = CHECK_ESTIMATE_USD * sum(r.get("raw_verdict") == "FAIL" for r in reps)
    return judge, check


def write_estimate(
    study: study_mod.Study, rows: list[dict[str, Any]], cap: float, out: Path, results: Path
) -> dict[str, Any]:
    """Write `results/cost_estimate.json`, aggregate only (it is committed), and return it."""
    ordered = study.rejudge.order_runs(rows, out)
    judge_total = check_total = reserved = 0.0
    fit = expected = 0
    for row in ordered:
        judge, check = estimate_decision(study, row["decision_id"])
        judge_total += judge * len(REPS)
        check_total += check
        reserved += (judge * len(REPS) + check) * RESERVE_MARGIN
        fit += reserved <= cap
        # Reservations are released as calls settle, so the count the cap is expected to bind at is this one.
        expected += judge_total + check_total <= cap
    estimate = {
        "decisions": len(rows),
        "reps": len(rows) * len(REPS),
        "judge_usd": round(judge_total, 2),
        "check_usd": round(check_total, 2),
        "total_usd": round(judge_total + check_total, 2),
        "reserved_usd": round(reserved, 2),
        "cap_usd": cap,
        "decisions_fitting_under_cap_in_run_order": fit,
        "decisions_expected_within_cap_in_run_order": expected,
        "judge_estimate_margin": JUDGE_ESTIMATE_MARGIN,
        "check_estimate_usd_per_study_fail_rep": CHECK_ESTIMATE_USD,
        "reserve_margin": RESERVE_MARGIN,
        "method": "judge = mean of the study's two rep costs x margin; check = fixed amount per study rep "
        "whose raw verdict was FAIL; a decision 'fits' while the running total of reservations "
        "(estimate x reserve margin) stays under the cap; 'expected' counts decisions while the running "
        "total of estimates does (reservations are released as calls settle)",
    }
    study.rejudge.write_json_atomic(results / "cost_estimate.json", estimate)
    return estimate


LEDGER_FILE = "spend_ledger.json"


def apply_ledger(budget: Any, ledger: Path) -> None:
    """Never count below the committed ledger's total.

    The rep files that rebuild the total live in a private, git-ignored directory; if that is lost, the cap
    would silently reset to zero. The ledger is aggregate only (a total), so it is committed.
    """
    try:
        floor = float(json.loads(ledger.read_text())["total_usd"])
    except FileNotFoundError:
        return
    if floor > budget.spent:
        budget.unrecorded += floor - budget.spent
        budget.spent = floor


def read_ledger(ledger: Path) -> dict[str, Any]:
    """Return the committed ledger's fields, or {} when there is none yet."""
    try:
        data: dict[str, Any] = json.loads(ledger.read_text())
    except FileNotFoundError:
        return {}
    return data


def write_ledger(run: Run) -> None:
    """Record the running total in the committed ledger (only ever raised; see `apply_ledger`).

    Read-modify-write: the fields `charge_interrupted` added stay when a later resume writes the total.
    """
    if run.ledger is not None:
        run.study.rejudge.write_json_atomic(
            run.ledger,
            {**read_ledger(run.ledger), "total_usd": round(run.budget.spent, 4), "cap_usd": run.budget.cap},
        )


def charge_interrupted(ledger: Path, rep_usds: list[float], calls: int, cap: float) -> dict[str, Any]:
    """Return the ledger with `calls` cut-short calls charged at the worst round the reps measured.

    Their real cost is not recorded anywhere here, so each is charged what the dearest measured round cost.
    That is the worst observed round, not an upper bound. The total is only raised: when the measured spend
    plus this charge is below what the ledger already holds, the ledger's figure stands.
    """
    if not rep_usds:
        raise ValueError("no rep files to take the worst round from")
    if calls < 1:
        raise ValueError(f"calls must be at least 1, got {calls}")
    measured = sum(rep_usds)
    per_call = max(rep_usds)
    charge = calls * per_call
    current = read_ledger(ledger)
    updated = {
        **current,
        "total_usd": round(max(float(current.get("total_usd", 0.0)), measured + charge), 4),
        "cap_usd": cap,
        "measured_usd": round(measured, 4),
        "interrupted_calls": calls,
        "charge_per_interrupted_call_usd": round(per_call, 4),
        "interrupted_charge_usd": round(charge, 4),
    }
    return updated


# --------------------------------------------------------------------------- #
# One rep
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Job:
    """One decision's inputs, built before any call is made so a broken input fails before money is spent."""

    row: dict[str, Any]
    prompt: str
    brief: str
    acceptance: str
    judge_estimate: float  # USD per rep
    check_estimate: float  # USD per rep, on average


def _take_from_held(run: Run, held: list[float], est: float) -> bool:
    """Move `est` out of the decision's held budget for one call, reserving more if it is short.

    Checked before every call, because a call can cost more than the estimate that was held for it.
    """
    if not run.budget.fits():
        return False
    short = est - held[0]
    if short > 0:
        if not run.budget.reserve(short):
            return False
        held[0] += short
    held[0] -= est
    return True


async def _call_and_settle(
    run: Run,
    est: float,
    fallback_usd: float,
    setting: CallSetting,
    prompt: str,
    workdir: Path,
    retry: Retry,
    state: _RepState,
    part: str,
) -> tuple[str, float]:
    """Make one call whose `est` is already taken, settle its cost, and return (reply text, USD)."""
    rejudge = run.study.rejudge

    async def call(p: str, w: Path) -> tuple[str, list[str]]:
        return await run.call(p, w, setting)

    try:
        raw, names = await retry(call, prompt, workdir)
    except BaseException:
        # A call that raised may have run, so the cap counts it at its estimate (fail-closed).
        # `charge-interrupted` remains for the historic ledger.
        run.budget.settle(est, est)
        raise
    state.actions += names
    text, rec = rejudge.split_usage(raw)
    cost = rejudge.price_usage(rec)
    if cost is None:
        state.measured, cost = False, fallback_usd
    else:
        state.models.update(rec.get("models") or [setting.model])
        for name in _TOKEN_FIELDS:
            state.usage[part][name] += int(rec.get(name) or 0)
    run.budget.settle(est, cost)
    return text, cost


async def _run_calls(
    run: Run, job: Job, held: list[float], judge_dir: Path, check_dir: Path, state: _RepState
) -> bool:
    """Make the judge call(s) and the finding check. Returns False when the budget stopped the rep."""
    rejudge, fm = run.study.rejudge, run.findings
    judge_res = job.judge_estimate * RESERVE_MARGIN
    check_res = CHECK_ESTIMATE_USD * RESERVE_MARGIN

    while state.attempts < NO_VERDICT_ATTEMPTS and not fm.VERDICT.search(state.text):
        if not _take_from_held(run, held, judge_res):
            return False
        state.attempts += 1
        state.text, cost = await _call_and_settle(
            run,
            judge_res,
            job.judge_estimate,
            JUDGE_SETTING,
            job.prompt,
            judge_dir,
            rejudge._call_with_retry,
            state,
            "judge",
        )
        state.judge_usd += cost
    if not fm.VERDICT.search(state.text) or not fm.needs_check(state.text):
        return True

    rejudge.prepare_workspace(run.source, job.row["head_sha"], check_dir)
    check_prompt = fm.build_check_prompt(
        job.row["judge"],
        job.brief,
        job.acceptance,
        fm.select_blocking(fm.parse_findings(state.text)),
        reply=state.text,
    )
    for attempt in range(1, CHECK_ATTEMPTS + 1):
        if not _take_from_held(run, held, check_res):
            return False
        try:
            state.check_text, cost = await _call_and_settle(
                run,
                check_res,
                CHECK_ESTIMATE_USD,
                run.check_setting,
                check_prompt,
                check_dir,
                rejudge._call_waiting_for_capacity,
                state,
                "check",
            )
        except Exception:  # noqa: BLE001 -- as the engine: a check that cannot run keeps the findings
            _log.exception("finding check failed (attempt %d/%d)", attempt, CHECK_ATTEMPTS)
            continue
        state.check_usd += cost
        break
    return True


async def run_rep(run: Run, job: Job, rep: int, held: list[float]) -> bool:
    """Run one rep and save it. Returns False when the budget stopped it (nothing saved)."""
    rejudge = run.study.rejudge
    row = job.row
    stem = rejudge.make_decision_stem(row["decision_id"])
    judge_dir = run.work_root / f"{stem}-rep{rep}"
    check_dir = run.work_root / f"{stem}-rep{rep}-check"
    state = _RepState()
    started = rejudge._stamp_now()
    try:
        rejudge.prepare_workspace(run.source, row["head_sha"], judge_dir)
        try:
            done = await _run_calls(run, job, held, judge_dir, check_dir, state)
        except BaseException:
            # Calls that did run cost money, and no rep file will say so.
            rejudge._abandon(run.budget, state.spent)
            raise
        if not done:
            return bool(rejudge._abandon(run.budget, state.spent))
        rejudge.write_json_atomic(
            rejudge.build_rep_path(run.out, row["decision_id"], rep),
            _build_rep_record(run, row, rep, state, started),
        )
        return True
    finally:
        shutil.rmtree(judge_dir, ignore_errors=True)
        shutil.rmtree(check_dir, ignore_errors=True)


def _build_rep_record(
    run: Run, row: dict[str, Any], rep: int, state: _RepState, started: str
) -> dict[str, Any]:
    fm = run.findings
    verdicts = fm.VERDICT.findall(state.text)
    found = fm.parse_findings(state.text)
    decision = fm.decide(state.text, state.check_text) if verdicts else None
    return {
        "decision_id": row["decision_id"],
        "rep": rep,
        "verdict": decision.verdict if decision else "NO_VERDICT",
        "raw_verdict": verdicts[-1].upper() if verdicts else None,
        "n_findings": len(found),
        "n_blocking": len(fm.select_blocking(found)),
        "n_kept": len(decision.kept) if decision else 0,
        "n_dropped": len(decision.dropped) if decision else 0,
        "check_ran": state.check_text is not None,
        "check_needed": bool(verdicts) and fm.needs_check(state.text),
        "check_reason": decision.reason if decision else "",
        "text": state.text,
        "check_text": state.check_text,
        "judge_usd": round(state.judge_usd, 4),
        "check_usd": round(state.check_usd, 4),
        "usd": round(state.spent, 4),
        "cost_measured": state.measured,
        "usage": state.usage,
        "models": sorted(state.models),
        "attempts": state.attempts,
        "started_at": started,
        "finished_at": run.study.rejudge._stamp_now(),
        "actions": state.actions,
    }


def build_job(run: Run, row: dict[str, Any]) -> Job:
    """Read a decision's stored prompt and rebuild the finding check's inputs, all offline."""
    study = run.study
    stem = study.rejudge.make_decision_stem(row["decision_id"])
    stored = json.loads((study.dir / "data" / "inputs" / f"{stem}.json").read_text())
    brief, acceptance = prompts.rebuild_check_inputs(row, study.dir, study.inputs, run.briefs)
    judge_est, check_est = estimate_decision(study, row["decision_id"])
    return Job(
        row,
        prompts.build_after_prompt(stored["prompt"], row["judge"], run.findings),
        brief,
        acceptance,
        judge_est,
        check_est / len(REPS),
    )


async def run_decision(run: Run, row: dict[str, Any]) -> bool:
    """Run the reps of a decision not yet on disk. Returns False when the budget stopped it.

    Admission reserves every rep's estimate at once; a decision admitted then finishes whatever else the
    budget refuses afterwards (the study fixed exactly this bug).
    """
    todo = [
        rep for rep in REPS if not run.study.rejudge.build_rep_path(run.out, row["decision_id"], rep).exists()
    ]
    if not todo:
        return True
    job = build_job(run, row)
    per_rep = (job.judge_estimate + job.check_estimate) * RESERVE_MARGIN
    if not run.budget.admit(per_rep * len(todo)):
        return False
    held = [per_rep * len(todo)]
    try:
        for rep in todo:
            if not await run_rep(run, job, rep, held):
                return False
    finally:
        run.budget.settle(held[0], 0.0)  # release what the decision did not use
    return True


async def run_all(run: Run, rows: list[dict[str, Any]], parallel: int) -> int:
    """Run the decisions `parallel` at a time; return the exit code (3 if the budget stopped it)."""
    gate = asyncio.Semaphore(parallel)

    async def run_one(row: dict[str, Any]) -> None:
        async with gate:
            if run.budget.stopped:
                return
            try:
                done = await run_decision(run, row)
            finally:
                write_ledger(run)
            print(f"{row['decision_id']}: {'done' if done else 'stopped by budget'}")

    # One broken decision must not stop the others: gather keeps each one's error, reported below.
    outcomes = await asyncio.gather(*(run_one(row) for row in rows), return_exceptions=True)
    failures = 0
    for row, outcome in zip(rows, outcomes, strict=True):
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome  # an interrupt or cancellation, not a failed decision
        if isinstance(outcome, Exception):
            failures += 1
            print(f"{row['decision_id']}: failed: {type(outcome).__name__}: {outcome}", file=sys.stderr)
    run.budget.write()
    if run.budget.stopped:
        print(f"STOPPED: {run.budget.stopped}", file=sys.stderr)
        return EXIT_BUDGET
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# The real call and the CLI
# --------------------------------------------------------------------------- #


async def call_engine(prompt: str, workdir: Path, setting: CallSetting) -> tuple[str, list[str]]:
    """Run one reviewer call as the engine does; needs `child.attach()` and the launch context set up."""
    # Importing `agent` is what registers the `subagent` task with `_reviewer`.
    from flyte_agent_factory import _reviewer, agent, child

    del agent
    names: list[str] = []
    with child.track_submissions(names.append):
        text, _out = await _reviewer._call_reviewer_raw(
            brief=prompt,
            workdir=await Dir.from_local(str(workdir)),
            role=ROLE,
            model=setting.model,
            effort=setting.effort,
            max_turns=setting.max_turns,
            answer_only=True,
            engine="claude",
            routed=True,
        )
    return text, names


def print_dry_run(run: Run, row: dict[str, Any]) -> None:
    """Prepare the workspace for rep 1 and print what would be called, submitting nothing."""
    rejudge = run.study.rejudge
    stem = rejudge.make_decision_stem(row["decision_id"])
    stored = json.loads((run.study.dir / "data" / "inputs" / f"{stem}.json").read_text())
    prompt = prompts.build_after_prompt(stored["prompt"], row["judge"], run.findings)
    brief, acceptance = prompts.rebuild_check_inputs(row, run.study.dir, run.study.inputs, run.briefs)
    dest = run.work_root / f"{stem}-rep1"
    rejudge.prepare_workspace(run.source, row["head_sha"], dest)
    judge_est, check_est = estimate_decision(run.study, row["decision_id"])
    print(f"{row['decision_id']}: workspace {dest} at {row['head_sha'][:10]} (clean, no origin)")
    print(f"  prompt: {len(prompt)} chars, head: {prompt[:120]!r}")
    print(f"  check input: brief {len(brief)} chars, acceptance {len(acceptance)} chars")
    s, c = JUDGE_SETTING, run.check_setting
    print(
        f"  would call subagent(brief=<prompt>, workdir=Dir({dest}), model={s.model!r}, effort={s.effort!r}, "
        f"max_turns={s.max_turns}, role={ROLE!r}, answer_only=True) x{len(REPS)} reps, estimate "
        f"${judge_est * len(REPS):.2f}; then, if findings or a FAIL, a check with model={c.model!r}, "
        f"effort={c.effort!r}, max_turns={c.max_turns} (estimate ${check_est:.2f})"
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument(
        "command",
        nargs="?",
        choices=("estimate", "verify-prompts", "charge-interrupted"),
        help="`estimate`: write the cost estimate only; `verify-prompts`: check the study's stored prompts; "
        "`charge-interrupted`: charge `--calls` cut-short calls to the committed ledger",
    )
    ap.add_argument("--expect", type=int, help="verify-prompts: the number of decisions the study must have")
    ap.add_argument("--calls", type=int, help="charge-interrupted: how many in-flight calls were cut short")
    ap.add_argument("--limit", type=int, help="only the first K decisions, in run order")
    ap.add_argument("--only", nargs="+", metavar="DECISION_ID", help="only these decision ids")
    ap.add_argument("--parallel", type=int, default=4, help="decisions at once (each runs its reps in turn)")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="stop before the total passes this")
    ap.add_argument("--out", type=Path, default=study_mod.DEFAULT_OUT_DIR, help="private output directory")
    ap.add_argument(
        "--study", type=Path, default=study_mod.DEFAULT_STUDY_DIR, help="the self-agreement study"
    )
    ap.add_argument("--source-repo", type=Path, default=study_mod.DEFAULT_SOURCE_REPO)
    ap.add_argument("--work-root", type=Path, default=Path.home() / ".cache" / "jc" / "work")
    ap.add_argument("--results", type=Path, default=study_mod.RESULTS_DIR, help="committed aggregates")
    ap.add_argument(
        "--dry-run", action="store_true", help="prepare a workspace, print the call, submit nothing"
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    args = parse_args(argv)
    # `install_launch_context` changes the cwd, so relative paths are fixed now.
    out, source = args.out.resolve(), args.source_repo.resolve()
    study = study_mod.load(args.study)
    if args.command == "verify-prompts":
        problems = prompts.verify_stored_prompts(study, study_mod.load_findings_module(), args.expect)
        print("\n".join(problems) or "stored prompts: all sound")
        return 1 if problems else 0
    if args.command == "charge-interrupted":
        if args.calls is None:
            raise SystemExit("charge-interrupted needs --calls")
        usds = [
            float(json.loads(path.read_text())["usd"])
            for row in select_decisions(study)
            for rep in REPS
            if (path := study.rejudge.build_rep_path(out, row["decision_id"], rep)).exists()
        ]
        ledger = args.results.resolve() / LEDGER_FILE
        updated = charge_interrupted(ledger, usds, args.calls, args.cap)
        study.rejudge.write_json_atomic(ledger, updated)
        print(json.dumps(updated, indent=1))
        return 0
    rows = study.rejudge.order_runs(select_decisions(study), out)
    if args.only:
        rows = [r for r in rows if r["decision_id"] in set(args.only)]
    if args.limit is not None:
        rows = rows[: args.limit]
    if args.command == "estimate":
        print(json.dumps(write_estimate(study, rows, args.cap, out, args.results.resolve()), indent=1))
        return 0

    model, effort = study_mod.read_finding_check_setting()
    ledger = args.results.resolve() / LEDGER_FILE
    budget = study.rejudge.load_budget(out, args.cap)
    apply_ledger(budget, ledger)
    run = Run(
        study=study,
        findings=study_mod.load_findings_module(),
        budget=budget,
        call=call_engine,
        out=out,
        source=source,
        work_root=args.work_root.resolve(),
        check_setting=CallSetting(model, effort, CHECK_MAX_TURNS),
        briefs=study.inputs.load_launch_briefs(),
        ledger=None if args.dry_run else ledger,
    )
    if args.dry_run:
        for row in rows:
            print_dry_run(run, row)
        return 0

    from flyte_agent_factory import child

    child.attach()
    print(f"judge image: {study.rejudge.install_launch_context()}", file=sys.stderr)
    return asyncio.run(run_all(run, rows, args.parallel))


if __name__ == "__main__":
    sys.exit(main())
