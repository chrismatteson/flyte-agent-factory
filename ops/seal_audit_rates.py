"""Measure the seal audit against the seeded corpus: confusion matrix, FP and FN rates, per-reason accuracy.

Builds every case of `tests/seal_corpus.py` in a temp dir, audits it and compares the verdict with the
case's true label. A void run is the positive: a false positive is a clean case called void (a valid run
thrown away), a false negative a void case called clean (a contaminated run counted).

`--check` compares the counts with `ops/seal_audit_rates.expected.json` and exits 1 on any change, so a
change to the audit, its failure signatures or the corpus shows up as a diff to review.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Run under `uv run`, which does not install this project (`package = false`): without this the imports
# below fail with ModuleNotFoundError (as in ops/bootstrap.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests import seal_corpus  # noqa: E402 -- needs the sys.path entries above

EXPECTED = Path(__file__).with_name("seal_audit_rates.expected.json")


def measure_rates() -> dict[str, Any]:
    """Audit every corpus case and return the counts, rates and misclassified case names."""
    matrix = {"true_void": 0, "false_clean": 0, "false_void": 0, "true_clean": 0}
    per_reason: dict[str, dict[str, int]] = {}
    misclassified: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for case in seal_corpus.CASES:
            verdict = seal_corpus.audit_case(case, Path(tmp) / case.name)
            kinds = {f.kind for f in verdict.reasons}
            if case.expected == "void":
                called_void = verdict.verdict == "void"
                matrix["true_void" if called_void else "false_clean"] += 1
                stats = per_reason.setdefault(case.reason, {"cases": 0, "found": 0})
                stats["cases"] += 1
                stats["found"] += case.reason in kinds
            else:
                called_void = verdict.verdict == "void"
                matrix["false_void" if called_void else "true_clean"] += 1
            if verdict.verdict != case.expected:
                misclassified.append(case.name)
    clean = matrix["true_clean"] + matrix["false_void"]
    void = matrix["true_void"] + matrix["false_clean"]
    return {
        "cases": len(seal_corpus.CASES),
        "matrix": matrix,
        "fp_rate": round(matrix["false_void"] / clean, 4) if clean else 0.0,
        "fn_rate": round(matrix["false_clean"] / void, 4) if void else 0.0,
        "per_reason": dict(sorted(per_reason.items())),
        "misclassified": sorted(misclassified),
    }


def render_text(rates: dict[str, Any]) -> str:
    """Return the rates as the report a person reads."""
    m = rates["matrix"]
    clean = m["false_void"] + m["true_clean"]
    void = m["false_clean"] + m["true_void"]
    lines = [
        f"cases: {rates['cases']}  (void is the positive)",
        "",
        "confusion matrix           called void   called clean",
        f"  truly void               {m['true_void']:>11}   {m['false_clean']:>12}",
        f"  truly clean              {m['false_void']:>11}   {m['true_clean']:>12}",
        "",
        f"FP rate (clean called void): {m['false_void']}/{clean} = {rates['fp_rate']:.1%}",
        f"FN rate (void called clean): {m['false_clean']}/{void} = {rates['fn_rate']:.1%}",
        "",
        "per-reason accuracy (void cases whose expected reason was found):",
    ]
    for reason, stats in rates["per_reason"].items():
        lines.append(f"  {reason:<16} {stats['found']}/{stats['cases']}")
    lines += ["", "misclassified cases:"]
    lines += [f"  {name}" for name in rates["misclassified"]] or ["  (none)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the measured rates; with `--check`, exit 1 when they differ from the recorded ones."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the rates as JSON")
    parser.add_argument(
        "--check", action="store_true", help=f"compare with {EXPECTED.name}; exit 1 on a change"
    )
    parser.add_argument(
        "--write-expected", action="store_true", help=f"record the measured rates in {EXPECTED.name}"
    )
    args = parser.parse_args(argv)
    rates = measure_rates()
    if args.write_expected:
        EXPECTED.write_text(json.dumps(rates, indent=2) + "\n")
    print(json.dumps(rates, indent=2) if args.json else render_text(rates))
    if args.check:
        expected = json.loads(EXPECTED.read_text())
        if expected != rates:
            print(
                f"measured rates differ from {EXPECTED.name}; review, then --write-expected", file=sys.stderr
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
