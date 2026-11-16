"""Before and after the changed judge, on the decisions both runs completed.

BEFORE is the study's two reps (`rejudge.py`, the old judge); AFTER is this harness's two reps (`run.py`).
Only decisions with PASS/FAIL in all four are compared, so both sides see the same decisions. The statistics
are the study's own (`analyze.py`): Wilson and by-PR cluster bootstrap intervals, Cohen's kappa, and the same
subset definitions.

Writes `results/summary.json`, which is committed: aggregates, opaque labels and mechanism categories only,
never decision ids, run names or reply text. The label-to-decision table and the finding titles go to
`<out>/moves_private.json`.

    python factory-improvement/judge_consistency/analyze_after.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import study as study_mod

REPS = (1, 2)
SUBSET_NAMES = (
    "all",
    "high",
    "medium_or_high",
    "first_rounds",
    "later_rounds",
    "correctness",
    "completeness",
    "swe",
    "other",
)
# The study's own subset definitions, by the name it gave them.
_STUDY_SUBSET = {
    "medium_or_high": "medium_or_better",
    "first_rounds": "first",
    "later_rounds": "later",
}


def label_decision(decision_id: str) -> str:
    """Return a label that identifies a decision across the two files without naming it."""
    return hashlib.sha256(decision_id.encode()).hexdigest()[:10]


def classify_old_rule(before_rec: dict[str, Any]) -> str:
    """Return what the study's old verdict rule (a FAIL without an evidenced finding is a PASS) did."""
    if before_rec["raw_verdict"] != "FAIL":
        return "raw_pass"
    return "evidenced_fail" if before_rec.get("evidenced_findings") else "no_evidence_downgrade"


def classify_mechanism(before: str, after_rec: dict[str, Any]) -> str:
    """Return how the after record reached its verdict, for a move away from `before`."""
    blocking, kept, raw = after_rec["n_blocking"], after_rec["n_kept"], after_rec["raw_verdict"]
    if before == "FAIL":
        if blocking == 0 and raw == "PASS":
            return "judge_raw_pass_no_findings"
        if blocking > 0 and kept == 0:
            return "check_dropped_all"
        if blocking == 0 and raw == "FAIL":
            return "no_evidence_fail_not_reproduced"
    elif blocking > 0 and kept > 0:
        return "kept_blocking_finding"
    elif blocking == 0 and raw == "FAIL":
        return "claim_reproduced_without_finding"
    return "other"


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _list_agreement_items(decisions: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    return [(d["group"], int(d["rep1"] == d["rep2"]), 1) for d in decisions]


def compare_subset(
    analyze: ModuleType, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return before and after self-agreement and their paired difference for decisions in one subset."""
    figures = {}
    for name, decisions in (("before", before), ("after", after)):
        figures[name] = {
            **analyze.measure_self_agreement(decisions),
            "n_pairs": len(decisions),
            "n_groups": len({d["group"] for d in decisions}),
        }
    # Both arms are the same decisions, so their groups line up one to one.
    arms = [analyze.collect_per_group(_list_agreement_items(d)) for d in (after, before)]
    rate_before, rate_after = figures["before"]["self_agreement"], figures["after"]["self_agreement"]
    paired = rate_before is not None and rate_after is not None
    figures["difference"] = {
        "after_minus_before": rate_after - rate_before if paired else None,
        "paired_cluster_bootstrap_95": analyze.bootstrap_by_cluster(arms, paired=True) if paired else None,
    }
    return figures


def check_blocking_counts(findings_mod: ModuleType, after_recs: dict[str, dict[int, dict[str, Any]]]) -> None:
    """Raise unless every after rep's recorded `n_blocking` is what the current evidence rule counts.

    The `CHECK <n>` numbering and the mechanism categories assume the rule that ran is the rule that reads
    the reply now; a mismatch means the committed figures would no longer line up with the findings.
    """
    for did, reps in after_recs.items():
        for rep, rec in reps.items():
            counted = len(findings_mod.select_blocking(findings_mod.parse_findings(rec["text"])))
            if counted != rec["n_blocking"]:
                raise ValueError(
                    f"rep {rep} of {label_decision(did)}: recorded n_blocking={rec['n_blocking']}, "
                    f"the evidence rule counts {counted}"
                )


def measure_offline_rule(
    study: study_mod.Study, findings_mod: ModuleType, sample: list[dict[str, Any]], before_dir: Path
) -> dict[str, Any]:
    """Return self-agreement of the new verdict rule (no finding check) applied to the study's own replies.

    Covers every decision with two study verdicts, not only those both runs completed: it needs no new
    calls, so it shows what the rule alone does on the whole sample.
    """
    analyze = study.analyze
    rows = []
    for d in analyze.load_decisions(sample, before_dir):
        if analyze.is_pair(d):
            reps = analyze.load_reps(before_dir, d["decision_id"])
            verdicts = {f"rep{r}": findings_mod.decide(reps[r]["text"], None).verdict for r in REPS}
            rows.append({"group": d["group"], **verdicts})
    figures = analyze.measure_self_agreement(rows)
    return {
        "n_pairs": len(rows),
        "agreement": figures["self_agreement"],
        "wilson_95": figures["wilson_95"],
    }


def measure_after_under_current_rule(
    analyze: ModuleType,
    findings_mod: ModuleType,
    after: list[dict[str, Any]],
    after_recs: dict[str, dict[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Return the after reps re-decided by the engine's current `decide` from each stored and check reply.

    The committed figures are the verdicts computed when the run happened. This costs nothing (the check
    reply is stored, nothing is re-run) and shows what the rule that lands says on the same replies, and how
    many reps it moves from the recorded verdict.
    """
    rows, differing = [], 0
    for a in after:
        recs = after_recs[a["decision_id"]]
        verdicts = {}
        for r in REPS:
            verdicts[f"rep{r}"] = findings_mod.decide(recs[r]["text"], recs[r].get("check_text")).verdict
            differing += verdicts[f"rep{r}"] != a[f"rep{r}"]
        rows.append({"group": a["group"], **verdicts})
    return {
        **analyze.measure_self_agreement(rows),
        "n_pairs": len(rows),
        "reps_differing_from_recorded": differing,
    }


def _add_remaining_figures(spend: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the ledger plus the two figures the READBACK cites, so no reader does the subtraction.

    `unrecorded_usd` is what the ledger counts that no rep file backs (the interrupted calls' charge, or the
    ledger's earlier higher figure), the same quantity `run.py`'s budget calls `unrecorded`. `measured_usd`
    appears in the ledger only after `charge-interrupted`; a ledger from a clean run has none, so
    everything it counts is measured and `unrecorded_usd` is 0.
    """
    if spend is None:
        return None
    return {
        **spend,
        "remaining_under_cap_usd": round(spend["cap_usd"] - spend["total_usd"], 4),
        "unrecorded_usd": round(spend["total_usd"] - spend.get("measured_usd", spend["total_usd"]), 4),
    }


def summarize(
    study: study_mod.Study, findings_mod: ModuleType, out: Path, spend: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (the committed summary, the private moves table).

    `spend` is the committed spend ledger, copied into the summary so the READBACK cites one file for it.
    """
    analyze = study.analyze
    sample = study.recover.read_jsonl(study.dir / "data" / "sample.jsonl")
    before_dir, after_dir = study.dir / "results" / "rejudge", out / "rejudge"
    after_by_id = {d["decision_id"]: d for d in analyze.load_decisions(sample, after_dir)}
    pairs = [
        (b, after_by_id[b["decision_id"]])
        for b in analyze.load_decisions(sample, before_dir)
        if analyze.is_pair(b) and analyze.is_pair(after_by_id[b["decision_id"]])
    ]
    if not pairs:
        raise FileNotFoundError(
            f"no completed after-rep pairs under {after_dir}: nothing to summarize, nothing written"
        )
    before = [b for b, _ in pairs]
    after = [a for _, a in pairs]

    subsets = {}
    for name in SUBSET_NAMES:
        keep = analyze.SUBSETS[_STUDY_SUBSET.get(name, name)]
        chosen = [(b, a) for b, a in pairs if keep(b)]
        subsets[name] = compare_subset(analyze, [b for b, _ in chosen], [a for _, a in chosen])

    before_recs = {b["decision_id"]: analyze.load_reps(before_dir, b["decision_id"]) for b in before}
    after_recs = {a["decision_id"]: analyze.load_reps(after_dir, a["decision_id"]) for a in after}
    before_flat = [r for recs in before_recs.values() for r in recs.values()]
    after_flat = [r for recs in after_recs.values() for r in recs.values()]
    check_blocking_counts(findings_mod, after_recs)

    moves, private = [], {}
    transitions: Counter[str] = Counter()
    for b, a in pairs:
        did = b["decision_id"]
        label = label_decision(did)
        private[label] = {
            "decision_id": did,
            "before": {f"rep{r}": b[f"rep{r}"] for r in REPS},
            "after": {f"rep{r}": a[f"rep{r}"] for r in REPS},
            "after_finding_titles": {
                f"rep{r}": [f.title for f in findings_mod.parse_findings(after_recs[did][r]["text"])]
                for r in REPS
            },
        }
        for r in REPS:
            was, now = b[f"rep{r}"], a[f"rep{r}"]
            transitions[f"{was}->{now}"] += 1
            if was != now:
                moves.append(
                    {
                        "label": label,
                        "rep": r,
                        "direction": f"{was}->{now}",
                        "mechanism": classify_mechanism(was, after_recs[did][r]),
                        "old_rule": classify_old_rule(before_recs[did][r]),
                    }
                )

    cost_before = _average([float(r["usd"]) for r in before_flat])
    cost_after = _average([float(r["usd"]) for r in after_flat])
    checked = [float(r["check_usd"]) for r in after_flat if r["check_ran"]]
    summary = {
        "completed_decisions": len(pairs),
        "decisions_in_study_sample": len(sample),
        "subsets": subsets,
        "cost_per_round_usd": {
            "before": cost_before,
            "after": cost_after,
            "ratio": cost_after / cost_before if cost_before and cost_after is not None else None,
            "after_judge_only": _average([float(r["judge_usd"]) for r in after_flat]),
            "after_check_per_round": _average([float(r["check_usd"]) for r in after_flat]),
            "after_check_per_check": _average(checked),
            "share_of_rounds_with_a_check": _average([float(bool(r["check_ran"])) for r in after_flat]),
        },
        "pass_rate": {
            "before": _average([float(r["verdict"] == "PASS") for r in before_flat]),
            "after": _average([float(r["verdict"] == "PASS") for r in after_flat]),
            "per_rep": {
                f"rep{r}": {
                    "before": _average([float(b[f"rep{r}"] == "PASS") for b in before]),
                    "after": _average([float(a[f"rep{r}"] == "PASS") for a in after]),
                }
                for r in REPS
            },
        },
        "transitions": dict(sorted(transitions.items())),
        "offline_new_rule_on_study_replies": measure_offline_rule(study, findings_mod, sample, before_dir),
        "after_under_current_rule": measure_after_under_current_rule(
            analyze, findings_mod, after, after_recs
        ),
        "check_drops": {
            "rounds": len(after_flat),
            "rounds_with_a_drop": sum(r["n_dropped"] > 0 for r in after_flat),
            "findings_dropped": sum(r["n_dropped"] for r in after_flat),
        },
        "spend": _add_remaining_figures(spend),
        "moves": {
            "by_mechanism": {
                direction: dict(
                    sorted(Counter(m["mechanism"] for m in moves if m["direction"] == direction).items())
                )
                for direction in ("FAIL->PASS", "PASS->FAIL")
            },
            "by_old_rule": {
                direction: dict(
                    sorted(Counter(m["old_rule"] for m in moves if m["direction"] == direction).items())
                )
                for direction in ("FAIL->PASS", "PASS->FAIL")
            },
            "list": sorted(moves, key=lambda m: (m["direction"], m["label"], m["rep"])),
        },
    }
    return summary, private


def main(argv: list[str] | None = None) -> int:
    """Write results/summary.json and the private moves table."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--study", type=Path, default=study_mod.DEFAULT_STUDY_DIR)
    ap.add_argument("--out", type=Path, default=study_mod.DEFAULT_OUT_DIR, help="private run output")
    ap.add_argument("--results", type=Path, default=study_mod.RESULTS_DIR, help="committed aggregates")
    args = ap.parse_args(argv)
    study = study_mod.load(args.study)
    ledger = args.results / "spend_ledger.json"
    spend = json.loads(ledger.read_text()) if ledger.exists() else None
    summary, private = summarize(study, study_mod.load_findings_module(), args.out, spend)
    study.rejudge.write_json_atomic(args.results / "summary.json", summary)
    study.rejudge.write_json_atomic(args.out / "moves_private.json", private)
    print(f"wrote {args.results / 'summary.json'} and {args.out / 'moves_private.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
