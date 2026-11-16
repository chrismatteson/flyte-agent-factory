"""The before/after analysis on a hand-built fixture; committed results must name no decision."""

import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.judge_consistency_support import (
    FINDING_REPLY,
    HARNESS_DIR,
    NOT_REPRODUCED,
    PASS_REPLY,
    REPRODUCED,
    analyze_after,
    decision_row,
    make_study,
    rep_record,
    study_mod,
)

# Hand-computed expectations are in the tests below.
F, P = "FAIL", "PASS"


def before(did: str, rep: int, verdict: str, usd: float = 1.0, **kw: Any) -> dict[str, Any]:
    return rep_record(did, rep, verdict, usd, **kw)


def after(
    did: str, rep: int, verdict: str, raw: str | None, blocking: int, kept: int, judge: float, check: float
) -> dict[str, Any]:
    return {
        "decision_id": did,
        "rep": rep,
        "verdict": verdict,
        "raw_verdict": raw,
        "n_blocking": blocking,
        "n_kept": kept,
        "n_dropped": blocking - kept,
        "check_ran": check > 0,
        "judge_usd": judge,
        "check_usd": check,
        "usd": judge + check,
        "text": FINDING_REPLY if blocking else PASS_REPLY,
        "check_text": (REPRODUCED if kept else NOT_REPRODUCED) if check > 0 else None,
    }


ROWS = [
    decision_row(1, group="pr-a", judge="correctness", round_kind="first", confidence="high", swe=False),
    decision_row(2, group="pr-b", judge="completeness", round_kind="later", confidence="medium", swe=False),
    decision_row(3, group="pr-c", judge="correctness", round_kind="later", confidence="high", swe=True),
    decision_row(4, group="pr-d", judge="correctness", round_kind="first", confidence="low", swe=False),
    decision_row(5, group="pr-e"),  # one after rep has no verdict: not in the completed subset
]
IDS = [r["decision_id"] for r in ROWS]
BEFORE = {
    IDS[0]: (before(IDS[0], 1, F, 2.0), before(IDS[0], 2, F, 2.0)),
    IDS[1]: (before(IDS[1], 1, P, raw_verdict=F, evidenced_findings=0), before(IDS[1], 2, F)),
    IDS[2]: (before(IDS[2], 1, F), before(IDS[2], 2, F)),
    IDS[3]: (before(IDS[3], 1, P), before(IDS[3], 2, P)),
    IDS[4]: (before(IDS[4], 1, P), before(IDS[4], 2, P)),
}
AFTER = {
    IDS[0]: (after(IDS[0], 1, P, F, 1, 0, 2.0, 0.5), after(IDS[0], 2, P, P, 0, 0, 1.0, 0.0)),
    IDS[1]: (after(IDS[1], 1, F, P, 1, 1, 2.0, 0.5), after(IDS[1], 2, F, F, 1, 1, 2.0, 0.5)),
    IDS[2]: (after(IDS[2], 1, F, F, 1, 1, 2.0, 0.5), after(IDS[2], 2, F, F, 1, 1, 2.0, 0.5)),
    IDS[3]: (after(IDS[3], 1, P, P, 0, 0, 1.0, 0.0), after(IDS[3], 2, P, P, 0, 0, 1.0, 0.0)),
    IDS[4]: (after(IDS[4], 1, P, P, 0, 0, 1.0, 0.0), after(IDS[4], 2, "NO_VERDICT", None, 0, 0, 1.0, 0.0)),
}


@pytest.fixture
def summary_and_private(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    study = make_study(tmp_path, ROWS, BEFORE)
    out = tmp_path / "out"
    for did, reps in AFTER.items():
        for n, rec in enumerate(reps, 1):
            study.rejudge.write_json_atomic(study.rejudge.build_rep_path(out, did, n), rec)
    return analyze_after.summarize(study, study_mod.load_findings_module(), out)


def test_the_completed_subset_and_self_agreement_before_and_after(summary_and_private: Any) -> None:
    summary, _ = summary_and_private
    assert summary["completed_decisions"] == 4 and summary["decisions_in_study_sample"] == 5
    everything = summary["subsets"]["all"]
    # before: decisions 1, 3, 4 agree and 2 splits = 3/4; after: all four agree.
    assert everything["before"]["self_agreement"] == 0.75
    assert (
        everything["before"]["both_pass"],
        everything["before"]["both_fail"],
        everything["before"]["split"],
    ) == (1, 2, 1)
    assert everything["after"]["self_agreement"] == 1.0
    assert (
        everything["after"]["both_pass"],
        everything["after"]["both_fail"],
        everything["after"]["split"],
    ) == (2, 2, 0)
    assert everything["before"]["n_pairs"] == 4 and everything["before"]["n_groups"] == 4
    assert everything["difference"]["after_minus_before"] == 0.25
    # the fake study's interval is the range of per-PR differences: 0, +1, 0, 0
    assert everything["difference"]["paired_cluster_bootstrap_95"] == [0.0, 1.0]


def test_subsets_use_the_studys_definitions(summary_and_private: Any) -> None:
    summary, _ = summary_and_private
    rates = {
        n: (s["before"]["self_agreement"], s["after"]["self_agreement"])
        for n, s in summary["subsets"].items()
    }
    assert rates == {
        "all": (0.75, 1.0),
        "high": (1.0, 1.0),  # decisions 1, 3
        "medium_or_high": (2 / 3, 1.0),  # 1, 2, 3
        "first_rounds": (1.0, 1.0),  # 1, 4
        "later_rounds": (0.5, 1.0),  # 2, 3
        "correctness": (1.0, 1.0),  # 1, 3, 4
        "completeness": (0.0, 1.0),  # 2
        "swe": (1.0, 1.0),  # 3
        "other": (2 / 3, 1.0),  # 1, 2, 4
    }
    assert summary["subsets"]["completeness"]["difference"]["after_minus_before"] == 1.0


def test_an_empty_subset_has_no_difference(tmp_path: Path) -> None:
    rows = [decision_row(1, swe=False)]
    study = make_study(tmp_path, rows)
    out = tmp_path / "out"
    for n in (1, 2):
        study.rejudge.write_json_atomic(
            study.rejudge.build_rep_path(out, rows[0]["decision_id"], n),
            after(rows[0]["decision_id"], n, P, P, 0, 0, 1.0, 0.0),
        )
    summary, _ = analyze_after.summarize(study, study_mod.load_findings_module(), out)
    swe = summary["subsets"]["swe"]
    assert swe["after"]["self_agreement"] is None
    assert swe["difference"] == {"after_minus_before": None, "paired_cluster_bootstrap_95": None}


def test_cost_per_round(summary_and_private: Any) -> None:
    cost = summary_and_private[0]["cost_per_round_usd"]
    # before: (2+2+1+1+1+1+1+1)/8 = 1.25; after: (2.5+1+2.5+2.5+2.5+2.5+1+1)/8 = 1.9375
    assert cost["before"] == 1.25 and cost["after"] == 1.9375
    assert cost["ratio"] == pytest.approx(1.55)
    assert cost["after_judge_only"] == 1.625  # 13/8
    assert cost["after_check_per_round"] == 0.3125  # five checks of 0.5 over 8 rounds
    assert cost["after_check_per_check"] == 0.5
    assert cost["share_of_rounds_with_a_check"] == 0.625


def test_pass_rate_before_and_after_overall_and_per_rep(summary_and_private: Any) -> None:
    rate = summary_and_private[0]["pass_rate"]
    assert (rate["before"], rate["after"]) == (0.375, 0.5)  # 3/8 and 4/8
    assert rate["per_rep"] == {"rep1": {"before": 0.5, "after": 0.5}, "rep2": {"before": 0.25, "after": 0.5}}


def test_moves_have_opaque_labels_and_mechanisms(summary_and_private: Any) -> None:
    summary, private = summary_and_private
    moves = summary["moves"]
    assert summary["transitions"] == {"FAIL->FAIL": 3, "FAIL->PASS": 2, "PASS->FAIL": 1, "PASS->PASS": 2}
    assert moves["by_mechanism"] == {
        "FAIL->PASS": {"check_dropped_all": 1, "judge_raw_pass_no_findings": 1},
        "PASS->FAIL": {"kept_blocking_finding": 1},
    }
    assert moves["by_old_rule"] == {
        "FAIL->PASS": {"evidenced_fail": 2},
        "PASS->FAIL": {"no_evidence_downgrade": 1},  # the old rule had made that PASS out of a raw FAIL
    }
    label = analyze_after.label_decision(IDS[0])
    assert re.fullmatch(r"[0-9a-f]{10}", label)
    assert moves["list"] == [
        {
            "label": label,
            "rep": 1,
            "direction": "FAIL->PASS",
            "mechanism": "check_dropped_all",
            "old_rule": "evidenced_fail",
        },
        {
            "label": label,
            "rep": 2,
            "direction": "FAIL->PASS",
            "mechanism": "judge_raw_pass_no_findings",
            "old_rule": "evidenced_fail",
        },
        {
            "label": analyze_after.label_decision(IDS[1]),
            "rep": 1,
            "direction": "PASS->FAIL",
            "mechanism": "kept_blocking_finding",
            "old_rule": "no_evidence_downgrade",
        },
    ]
    # the private table is the only place the label meets the decision id, and it names the findings
    assert private[label]["decision_id"] == IDS[0]
    assert private[label]["before"] == {"rep1": F, "rep2": F} and private[label]["after"] == {
        "rep1": P,
        "rep2": P,
    }
    assert private[label]["after_finding_titles"] == {"rep1": ["login crashes on an empty name"], "rep2": []}


@pytest.mark.parametrize(
    ("before_verdict", "raw", "blocking", "kept", "expected"),
    [
        (F, P, 0, 0, "judge_raw_pass_no_findings"),
        (F, P, 1, 0, "check_dropped_all"),
        (F, F, 0, 0, "no_evidence_fail_not_reproduced"),
        (F, F, 2, 1, "other"),  # a FAIL that stayed a FAIL is not a move; anything unclassified is `other`
        (P, F, 1, 1, "kept_blocking_finding"),
        (P, F, 0, 1, "claim_reproduced_without_finding"),
        (P, P, 0, 0, "other"),
    ],
)
def test_mechanism_categories(
    before_verdict: str, raw: str | None, blocking: int, kept: int, expected: str
) -> None:
    record = {"n_blocking": blocking, "n_kept": kept, "raw_verdict": raw}
    assert analyze_after.classify_mechanism(before_verdict, record) == expected


def test_old_rule_basis() -> None:
    assert analyze_after.classify_old_rule({"raw_verdict": P}) == "raw_pass"
    assert analyze_after.classify_old_rule({"raw_verdict": F, "evidenced_findings": 2}) == "evidenced_fail"
    assert (
        analyze_after.classify_old_rule({"raw_verdict": F, "evidenced_findings": 0})
        == "no_evidence_downgrade"
    )


def test_with_no_after_reps_summarize_refuses_and_writes_nothing(tmp_path: Path) -> None:
    # Replaces `test_with_no_after_reps_the_summary_is_empty_rather_than_an_error`, added in this PR: an
    # all-null summary written with exit 0 hides a missing or mistyped `--out`.
    study = make_study(tmp_path, ROWS[:2], {k: v for k, v in BEFORE.items() if k in IDS[:2]})
    with pytest.raises(FileNotFoundError, match="no completed"):
        analyze_after.summarize(study, study_mod.load_findings_module(), tmp_path / "out")
    results = tmp_path / "results"
    results.mkdir()
    committed = results / "summary.json"
    committed.write_text('{"kept": true}')
    with pytest.raises(FileNotFoundError, match="no completed"):
        analyze_after.main(
            ["--study", str(study.dir), "--out", str(tmp_path / "missing"), "--results", str(results)]
        )
    assert committed.read_bytes() == b'{"kept": true}'


def test_after_reps_are_also_read_under_the_current_rule(tmp_path: Path, summary_and_private: Any) -> None:
    summary, _ = summary_and_private
    assert summary["after_under_current_rule"]["reps_differing_from_recorded"] == 0

    second = tmp_path / "second"
    second.mkdir()
    study = make_study(second, ROWS, BEFORE)
    out = second / "out"
    for did, reps in AFTER.items():
        for n, rec in enumerate(reps, 1):
            if did == IDS[2] and n == 1:
                # The check was handed a masked finding and answered NOT REPRODUCED; the run recorded PASS.
                rec = {
                    **rec,
                    "verdict": P,
                    "text": FINDING_REPLY.replace("1 failed", "<redacted> 1 failed"),
                    "check_text": NOT_REPRODUCED,
                }
            study.rejudge.write_json_atomic(study.rejudge.build_rep_path(out, did, n), rec)
    summary, _ = analyze_after.summarize(study, study_mod.load_findings_module(), out)
    current = summary["after_under_current_rule"]
    assert current["reps_differing_from_recorded"] == 1
    assert current["split"] == summary["subsets"]["all"]["after"]["split"] - 1


def test_main_writes_the_committed_summary_and_the_private_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    study = make_study(tmp_path, ROWS, BEFORE)
    out, results = tmp_path / "out", tmp_path / "results"
    for did, reps in AFTER.items():
        for n, rec in enumerate(reps, 1):
            study.rejudge.write_json_atomic(study.rejudge.build_rep_path(out, did, n), rec)
    code = analyze_after.main(["--study", str(study.dir), "--out", str(out), "--results", str(results)])
    assert code == 0 and "summary.json" in capsys.readouterr().out
    committed = (results / "summary.json").read_text()
    assert json.loads(committed)["completed_decisions"] == 4
    assert leaks(committed, ids=IDS, runs=[r["run"] for r in ROWS]) == []
    private = json.loads((out / "moves_private.json").read_text())
    assert {v["decision_id"] for v in private.values()} == set(IDS[:4])


def test_analyze_after_works_as_a_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    study = make_study(tmp_path, ROWS[:1], {IDS[0]: BEFORE[IDS[0]]})
    # summarize refuses to write when no after pair is complete, so give it one
    for n, rec in enumerate(AFTER[IDS[0]], 1):
        study.rejudge.write_json_atomic(study.rejudge.build_rep_path(tmp_path / "out", IDS[0], n), rec)
    argv = ["--study", str(study.dir), "--out", str(tmp_path / "out"), "--results", str(tmp_path / "results")]
    monkeypatch.setattr(sys, "argv", ["analyze_after.py", *argv])
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(Path(analyze_after.__file__)), run_name="__main__")
    assert stop.value.code == 0
    assert json.loads((tmp_path / "results" / "summary.json").read_text())["completed_decisions"] == 1


# --- nothing committed names a decision -------------------------------------------------------------------

RUN_NAME = re.compile(r"-\d-\d-[a-z0-9]{6}")
DECISION_ID = re.compile(r":r\d+:")
RESULTS = HARNESS_DIR / "results"


def leaks(text: str, ids: list[str], runs: list[str]) -> list[str]:
    """Return what in `text` names a study decision or run."""
    found = [i for i in ids if i in text] + [r for r in runs if r in text]
    return found + RUN_NAME.findall(text) + DECISION_ID.findall(text)


def test_the_leak_check_catches_an_id_a_run_name_and_their_shapes() -> None:
    assert leaks("clean aggregate 0.75", [], []) == []
    assert leaks(f"id {IDS[0]}", IDS, []) and leaks(f"run {ROWS[0]['run']}", [], [ROWS[0]["run"]])
    assert leaks("proj-1-0-abc123", [], []) == ["-1-0-abc123"]
    assert leaks("x:r2:correctness", [], []) == [":r2:"]


def test_committed_results_name_no_decision_or_run() -> None:
    sample = study_mod.DEFAULT_STUDY_DIR / "data" / "sample.jsonl"
    ids: list[str] = []
    runs: list[str] = []
    if sample.is_file():
        rows = [json.loads(line) for line in sample.read_text().splitlines() if line.strip()]
        ids, runs = [r["decision_id"] for r in rows], sorted({r["run"] for r in rows})
    for path in sorted(RESULTS.glob("*.json")):
        assert leaks(path.read_text(), ids, runs) == [], path.name


def write_after_reps(study: Any, out: Path) -> None:
    for did, reps in AFTER.items():
        for n, rec in enumerate(reps, 1):
            study.rejudge.write_json_atomic(study.rejudge.build_rep_path(out, did, n), rec)


def test_summary_carries_offline_rule_drop_counts_and_spend(tmp_path: Path) -> None:
    study = make_study(tmp_path, ROWS, BEFORE)
    write_after_reps(study, tmp_path / "out")
    ledger = {"total_usd": 9.0, "cap_usd": 150.0, "measured_usd": 5.0, "interrupted_charge_usd": 4.0}
    summary, _ = analyze_after.summarize(study, study_mod.load_findings_module(), tmp_path / "out", ledger)
    # the study's own replies read by the new rule, no check: every sampled pair, not only the completed ones
    offline = summary["offline_new_rule_on_study_replies"]
    assert (offline["n_pairs"], offline["agreement"]) == (5, 0.8)  # decision 2 splits
    assert "wilson_95" in offline  # the interval itself is the study's, faked here
    # only decision 1's rep 1 had a blocking finding that the check dropped
    assert summary["check_drops"] == {"rounds": 8, "rounds_with_a_drop": 1, "findings_dropped": 1}
    assert summary["spend"] == {**ledger, "remaining_under_cap_usd": 141.0, "unrecorded_usd": 4.0}


def test_summary_spend_carries_remaining_under_cap(tmp_path: Path) -> None:
    study = make_study(tmp_path, ROWS, BEFORE)
    write_after_reps(study, tmp_path / "out")
    ledger = {"total_usd": 136.0535, "cap_usd": 150.0, "measured_usd": 115.5544}
    summary, _ = analyze_after.summarize(study, study_mod.load_findings_module(), tmp_path / "out", ledger)
    assert summary["spend"]["remaining_under_cap_usd"] == 13.9465
    assert summary["spend"]["unrecorded_usd"] == 20.4991
    assert summary["spend"]["total_usd"] == 136.0535  # the ledger's own figures are unchanged


def test_summary_refuses_a_rep_whose_blocking_count_the_current_rule_does_not_match(tmp_path: Path) -> None:
    study = make_study(tmp_path, ROWS, BEFORE)
    write_after_reps(study, tmp_path / "out")
    bad = after(IDS[0], 1, P, F, 2, 0, 2.0, 0.5)  # records 2 blocking findings; its text holds one
    study.rejudge.write_json_atomic(study.rejudge.build_rep_path(tmp_path / "out", IDS[0], 1), bad)
    with pytest.raises(ValueError, match="n_blocking=2"):
        analyze_after.summarize(study, study_mod.load_findings_module(), tmp_path / "out")


def test_spend_without_an_interrupted_charge_has_nothing_unrecorded() -> None:
    figures = analyze_after._add_remaining_figures({"total_usd": 10.0, "cap_usd": 150.0})
    assert figures is not None
    assert figures["remaining_under_cap_usd"] == 140.0
    assert figures["unrecorded_usd"] == 0.0
