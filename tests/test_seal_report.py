from __future__ import annotations

import json
from typing import Any

import pytest

from flyte_agent_factory._seal_report import main, next_run_name, summarize

RUNS = [
    {"name": "a-factory", "task": "t1", "arm": "factory", "resolved": True},
    {"name": "b-factory", "task": "t2", "arm": "factory", "resolved": False},
    {"name": "c-factory", "task": "t3", "arm": "factory", "resolved": True},
    {"name": "a-plain", "task": "t1", "arm": "plain", "resolved": True},
    {"name": "d-plain", "task": "t4", "arm": "plain", "resolved": True},
]
VERDICTS: dict[str, dict[str, Any]] = {
    "a-factory": {"verdict": "clean", "gold": True, "reasons": []},
    "b-factory": {"verdict": "clean", "gold": True, "reasons": []},
    "c-factory": {"verdict": "void", "reasons": [{"kind": "reached"}, {"kind": "egress"}]},
    "a-plain": {"verdict": "void", "reasons": ["route-succeeded"]},
    # d-plain has no verdict at all
}
TASKS = {
    "t1": {"created_at": "2024-01-05"},
    "t2": {"created_at": "2025-09-01T10:00:00Z"},
    "t3": {"created_at": "2023-03-03"},
    "t4": {"created_at": "2026-02-02"},
}


def test_rates_count_clean_runs_only():
    summary = summarize(RUNS, VERDICTS, TASKS, "2025-01-01")
    assert summary.arms["factory"].counted == 2
    assert summary.arms["factory"].resolved == 1
    assert summary.arms["factory"].rate == 0.5
    assert summary.arms["plain"].counted == 0
    assert summary.arms["plain"].rate is None


def test_void_lists_first_reason_and_missing_verdicts_are_void():
    summary = summarize(RUNS, VERDICTS, TASKS, "2025-01-01")
    assert summary.void == [
        ("c-factory", "t3", "factory", "reached"),
        ("a-plain", "t1", "plain", "route-succeeded"),
        ("d-plain", "t4", "plain", "no audit"),
    ]
    assert summary.missing == ["d-plain"]


def test_recallable_tasks_are_those_created_before_the_cutoff():
    summary = summarize(RUNS, VERDICTS, TASKS, "2025-01-01")
    assert summary.recallable == ["t1", "t3"]


def test_markdown_has_one_results_table_and_the_void_and_recallable_sections():
    text = summarize(RUNS, VERDICTS, TASKS, "2025-01-01").to_markdown()
    assert text.count("| Arm | Counted |") == 1
    assert "| factory | 2 | 1 | 50.0% |" in text
    assert "## Void (excluded)" in text
    assert "| d-plain | t4 | plain | no audit |" in text
    assert "## Recallable tasks" in text
    assert "- t1" in text
    assert "clean only" not in text.lower()


def test_markdown_without_void_or_recallable_says_none():
    runs = [{"name": "a", "task": "t", "arm": "x", "resolved": True}]
    text = summarize(
        runs,
        {"a": {"verdict": "clean", "gold": True, "reasons": []}},
        {"t": {"created_at": "2026-01-01"}},
        "2025-01-01",
    ).to_markdown()
    assert text.count("(none)") == 2


def test_next_run_name_adds_r2_then_counts_up():
    assert next_run_name("swe-1", ["swe-1"]) == "swe-1-r2"
    assert next_run_name("swe-1", ["swe-1", "swe-1-r2", "swe-1-r3"]) == "swe-1-r4"
    assert next_run_name("swe-1-r2", ["swe-1", "swe-1-r2"]) == "swe-1-r3"


def test_next_run_name_ignores_other_projects_and_skips_taken_names():
    assert next_run_name("swe-1", ["swe-10", "swe-10-r7", "other"]) == "swe-1-r2"
    assert next_run_name("swe-1", []) == "swe-1-r2"


def test_the_cli_prints_one_table_the_void_runs_with_rerun_names_and_the_recallable_tasks(tmp_path, capsys):
    (tmp_path / "runs.json").write_text(json.dumps(RUNS))
    (tmp_path / "tasks.json").write_text(json.dumps(TASKS))
    verdicts = tmp_path / "verdicts"
    verdicts.mkdir()
    for name, verdict in VERDICTS.items():
        (verdicts / f"{name}.json").write_text(json.dumps(verdict))
    assert (
        main(
            [
                "--runs",
                str(tmp_path / "runs.json"),
                "--verdicts",
                str(verdicts),
                "--tasks",
                str(tmp_path / "tasks.json"),
                "--model-cutoff",
                "2025-01-01",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("| Arm | Counted | Resolved | Rate |") == 1
    assert "| factory | 2 | 1 | 50.0% |" in out
    assert "| plain | 0 | 0 | n/a |" in out
    assert "| d-plain | t4 | plain | no audit |" in out
    assert "- c-factory -> c-factory-r2" in out
    assert "- t1" in out and "- t3" in out and "- t2" not in out
    assert next_run_name("django-r5", []) == "django-r5-r2"
    assert next_run_name("django-r5", ["django-r5"]) == "django-r5-r2"
    assert next_run_name("django-r5", ["django", "django-r2"]) == "django-r3"


def _one(name, task="t", arm="x", resolved=True):
    return {"name": name, "task": task, "arm": arm, "resolved": resolved}


CLEAN = {"verdict": "clean", "gold": True, "reasons": []}
#: The tasks the `_one` runs name: every run's task must be in --tasks.
KNOWN = {task: {"created_at": "2024-01-01"} for task in ("t", "t2", "t3")}


def test_a_clean_verdict_without_gold_is_void_no_gold():
    for verdict in ({"verdict": "clean", "gold": False, "reasons": []}, {"verdict": "clean", "reasons": []}):
        summary = summarize([_one("a")], {"a": verdict}, KNOWN, "2025-01-01")
        assert summary.arms["x"].counted == 0
        assert summary.void == [("a", "t", "x", "no gold")]


@pytest.mark.parametrize("resolved", ["false", "true", 1, 0, None])
def test_resolved_must_be_a_real_bool(resolved):
    with pytest.raises(ValueError, match="run a: resolved"):
        summarize([_one("a", resolved=resolved)], {"a": CLEAN}, KNOWN, "2025-01-01")


def test_two_clean_runs_of_one_task_in_one_arm_raise_naming_both():
    runs = [_one("a"), _one("a-r2")]
    with pytest.raises(ValueError, match="a and a-r2"):
        summarize(runs, {"a": CLEAN, "a-r2": CLEAN}, KNOWN, "2025-01-01")


def test_a_rerun_after_a_void_counts_once():
    runs = [_one("a"), _one("a-r2")]
    void = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    summary = summarize(runs, {"a": void, "a-r2": CLEAN}, KNOWN, "2025-01-01")
    assert summary.arms["x"].counted == 1
    assert [v[0] for v in summary.void] == ["a"]


def test_a_void_run_with_a_later_clean_rerun_needs_no_rerun():
    runs = [_one("a-t2", task="t2"), _one("a-t2-r2", task="t2"), _one("b", task="t3")]
    void = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    summary = summarize(runs, {"a-t2": void, "a-t2-r2": CLEAN, "b": void}, KNOWN, "2025-01-01")
    assert [v[0] for v in summary.void] == ["a-t2", "b"]
    assert summary.needs_rerun == ["b"]


def test_a_void_run_after_the_clean_one_still_needs_a_rerun():
    runs = [_one("a"), _one("a-r2")]
    void = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    assert summarize(runs, {"a": CLEAN, "a-r2": void}, KNOWN, "2025-01-01").needs_rerun == ["a-r2"]


def test_duplicate_run_names_raise():
    with pytest.raises(ValueError, match="run a is listed more than once"):
        summarize([_one("a"), _one("a")], {"a": CLEAN}, KNOWN, "2025-01-01")


CLEAN_TEXT = json.dumps(CLEAN)


def _cli(tmp_path, runs, tasks=None, verdicts=None, cutoff="2025-01-01"):
    (tmp_path / "runs.json").write_text(runs if isinstance(runs, str) else json.dumps(runs))
    (tmp_path / "tasks.json").write_text(json.dumps(KNOWN if tasks is None else tasks))
    directory = tmp_path / "verdicts"
    directory.mkdir(exist_ok=True)
    for name, text in (verdicts or {}).items():
        (directory / f"{name}.json").write_text(text)
    return main(
        [
            "--runs", str(tmp_path / "runs.json"),
            "--verdicts", str(directory),
            "--tasks", str(tmp_path / "tasks.json"),
            "--model-cutoff", cutoff,
        ]
    )  # fmt: skip


def test_the_cli_lists_no_rerun_for_a_void_run_with_a_later_clean_one(tmp_path, capsys):
    void = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    runs = [_one("a-t2", task="t2"), _one("a-t2-r2", task="t2")]
    verdicts = {"a-t2": json.dumps(void), "a-t2-r2": json.dumps(CLEAN)}
    assert _cli(tmp_path, runs, verdicts=verdicts) == 0
    out = capsys.readouterr().out
    assert "| a-t2 | t2 | x | reached |" in out
    assert "a-t2 ->" not in out and "Reruns" not in out


@pytest.mark.parametrize(
    ("runs", "verdicts", "cutoff", "message"),
    [
        ([_one("a"), _one("a")], {}, "2025-01-01", "run a is listed more than once"),
        ("{", {}, "2025-01-01", "error:"),
        ({"name": "a"}, {}, "2025-01-01", "--runs must be a JSON list"),
        ([{"name": "a"}], {}, "2025-01-01", "--runs entry 0 lacks task, arm, resolved"),
        ([_one("a", resolved="yes")], {}, "2025-01-01", "run a: resolved"),
        ([_one("a")], {"a": "{"}, "2025-01-01", "verdict a.json is not JSON"),
        ([_one("a")], {"a": "[]"}, "2025-01-01", "verdict a.json is not a JSON object"),
        ([_one("a"), _one("b")], {"a": CLEAN_TEXT, "b": CLEAN_TEXT}, "2025-01-01", "both clean"),
        ([_one("a")], {}, "soon", "error:"),
        ([_one("a", task="t3x")], {"a": CLEAN_TEXT}, "2025-01-01", "--tasks has no entry for task(s): t3x"),
    ],
)  # fmt: skip
def test_the_cli_reports_bad_input_as_an_error_line_and_exit_1(
    tmp_path, capsys, runs, verdicts, cutoff, message
):
    assert _cli(tmp_path, runs, verdicts=verdicts, cutoff=cutoff) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ") and message in captured.err
    assert "Traceback" not in captured.err and captured.out == ""


def test_the_cli_reports_a_missing_runs_file_as_an_error(tmp_path, capsys):
    argv = ["--runs", str(tmp_path / "none.json"), "--verdicts", str(tmp_path)]
    assert main([*argv, "--tasks", str(tmp_path / "t.json"), "--model-cutoff", "2025-01-01"]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_a_run_whose_task_is_not_in_tasks_is_an_error():
    runs = [_one("a", task="t3x"), _one("b", task="t4y"), _one("c")]
    with pytest.raises(
        ValueError, match=r"no entry for task\(s\): t3x, t4y; every run's task needs a created_at"
    ):
        summarize(runs, {"a": CLEAN, "b": CLEAN, "c": CLEAN}, KNOWN, "2025-01-01")


def test_two_void_runs_of_one_task_get_one_rerun(tmp_path, capsys):
    reached = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    incomplete = {"verdict": "void", "gold": True, "reasons": ["incomplete"]}
    runs = [_one("t1", task="t"), _one("t1-r2", task="t")]
    assert summarize(runs, {"t1": reached, "t1-r2": incomplete}, KNOWN, "2025-01-01").needs_rerun == ["t1-r2"]
    verdicts = {"t1": json.dumps(reached), "t1-r2": json.dumps(incomplete)}
    assert _cli(tmp_path, runs, verdicts=verdicts) == 0
    out = capsys.readouterr().out
    reruns = out.split("## Reruns (fresh projects, nothing inherited)\n\n")[1]
    assert reruns == "- t1-r2 -> t1-r3\n"


def test_void_runs_in_two_arms_of_one_task_each_get_a_rerun():
    void = {"verdict": "void", "gold": True, "reasons": ["reached"]}
    runs = [_one("a", arm="x"), _one("b", arm="y"), _one("a-r2", arm="x")]
    summary = summarize(runs, {"a": void, "b": void, "a-r2": void}, KNOWN, "2025-01-01")
    assert summary.needs_rerun == ["a-r2", "b"]


def test_every_duplicate_run_name_is_named():
    with pytest.raises(ValueError, match="run a is listed more than once; run b is listed more than once"):
        summarize([_one("a"), _one("b"), _one("a"), _one("b")], {}, KNOWN, "2025-01-01")
