"""A fake of the study's `analyze`: simple, deterministic stand-ins with the same shapes."""

import json
from pathlib import Path

import rejudge

REPS = (1, 2)
VERDICTS = ("PASS", "FAIL")
SUBSETS = {
    "all": lambda d: True,
    "first": lambda d: d["round_kind"] == "first",
    "later": lambda d: d["round_kind"] == "later",
    "correctness": lambda d: d["judge"] == "correctness",
    "completeness": lambda d: d["judge"] == "completeness",
    "swe": lambda d: bool(d["swe"]),
    "other": lambda d: not d["swe"],
    "medium_or_better": lambda d: d["confidence"] in ("high", "medium"),
    "high": lambda d: d["confidence"] == "high",
}


def load_reps(rejudge_dir: Path, decision_id: str) -> dict[int, dict]:
    reps = {}
    for rep in REPS:
        path = rejudge.build_rep_file(rejudge_dir, decision_id, rep)
        if path.exists():
            reps[rep] = json.loads(path.read_text())
    return reps


def load_decisions(sample: list[dict], rejudge_dir: Path, drift_causes: dict | None = None) -> list[dict]:
    out = []
    for row in sample:
        reps = load_reps(rejudge_dir, row["decision_id"])
        out.append({**row, **{f"rep{r}": reps[r]["verdict"] if r in reps else None for r in REPS}})
    return out


def is_pair(d: dict) -> bool:
    return d["rep1"] in VERDICTS and d["rep2"] in VERDICTS


def measure_self_agreement(pairs: list[dict]) -> dict:
    both_pass = sum(d["rep1"] == d["rep2"] == "PASS" for d in pairs)
    both_fail = sum(d["rep1"] == d["rep2"] == "FAIL" for d in pairs)
    rate = (both_pass + both_fail) / len(pairs) if pairs else None
    return {
        "self_agreement": rate,
        "wilson_95": None,
        "cluster_bootstrap_95": None,
        "cohen_kappa": None,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "split": len(pairs) - both_pass - both_fail,
    }


def collect_per_group(items: list[tuple[str, int, int]]) -> list[tuple[int, int]]:
    sums: dict[str, list[int]] = {}
    for group, hits, total in items:
        acc = sums.setdefault(group, [0, 0])
        acc[0] += hits
        acc[1] += total
    return [(h, t) for _, (h, t) in sorted(sums.items())]


def bootstrap_by_cluster(arms: list[list[tuple[int, int]]], paired: bool = False) -> list[float] | None:
    """Not a bootstrap: the smallest and largest per-group difference, so a test can predict it."""
    diffs = [a[0] / a[1] - b[0] / b[1] for a, b in zip(arms[0], arms[1], strict=True)]
    return [min(diffs), max(diffs)] if paired and diffs else None
