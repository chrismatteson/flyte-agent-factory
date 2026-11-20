"""Judge replies with no verdict: evidence, measurement, report, parser variants, same-session follow-up."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _factory_status,
    _findings,
    _no_verdict,
    _review,
    _reviewer,
    _session_config,
    _timeline,
    _unprivileged,
    _usage,
    _verdict_recovery,
)
from tests import test_judge_outcomes as outcomes
from tests.test_judge_outcomes import FAIL_WITH_EVIDENCE, PASS, Reviewers

run = outcomes.run  # the fixture that drives `_evaluate` with a scripted `_call_reviewer`

SEEN = {"model": "opus", "effort": "high", "prompt_sha256": "ab" * 32}
EV = "command: pytest | output: 1 failed"
# No finding with evidence, so a recovered PASS stands: findings decide (`_findings.decide`), not the line.
UNEVIDENCED = "Reviewed it all.\nNON-BLOCKING:\n- style nit\nBLOCKING: 0"
FINDINGS_ONLY = "Reviewed it all.\nFINDING: tidy\nBreaks: none\nEvidence: file: a.py:3 naming\nBLOCKING: 0"


# --- D: parser variants ---


@pytest.mark.parametrize(
    ("line", "word"),
    [
        ("Final verdict: PASS", "PASS"),
        ("Overall verdict: FAIL", "FAIL"),
        ("Overall VERDICT: FAIL", "FAIL"),
        ("**Final Verdict:** PASS", "PASS"),
        ("**Final verdict**: PASS", "PASS"),
        ("VERDICT = PASS", "PASS"),
        ("VERDICT=FAIL", "FAIL"),
        ("Verdict: **Pass**", "PASS"),
    ],
)
def test_harmless_verdict_variants_parse(line, word):
    assert [w.upper() for w in _findings.verdict_words(f"report\n{line}\n")] == [word]


@pytest.mark.parametrize(
    "line",
    [
        "the verdict is not PASS",
        "I would not give VERDICT: PASS yet because the tests are missing",
        "Final thoughts: the verdict is PASS",
        "VERDICT: PASS — but the tests FAIL",
        "VERDICT: PASS - FAIL",
        "VERDICT: PASS-through is not a verdict",
        "VERDICT: maybe FAIL",
        "Overall, PASS or FAIL?",
        "Final verdict",
        "VERDICT: PASS - but it fails on X",
        "VERDICT: PASS - though 3 tests FAILED",
        "VERDICT: PASS - except criterion 7 does not hold",
        "VERDICT: FAIL - otherwise PASSes",
        "VERDICT: FAIL - reproduced on HEAD",
        "VERDICT: PASS - nothing blocks",
        "Final verdict: PASS - once the crash is fixed",
    ],
)
def test_variants_do_not_loosen_what_a_verdict_is(line):
    assert not _findings.verdict_words(line)


def test_a_variant_never_overrides_a_strict_line():
    # A strict line, even a quoted one, is the verdict as it always was; the variants are only a fallback.
    text = "> VERDICT: PASS\nbut the code is broken\nFinal verdict: FAIL"
    assert _findings.verdict_words(text)[-1].upper() == "PASS"
    assert _findings.verdict_words("quoted: PASS\nFinal verdict: FAIL")[-1].upper() == "FAIL"


# --- failure shapes ---


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("", "empty"),
        (_reviewer._NO_FINAL_MESSAGE, "empty"),
        ("API Error: 529 overloaded", "error"),
        ('{"subtype":"error_max_turns"}', "turn-limit"),
        ("Looks fine.\nMy verdict: probably PASS, pending CI.", "near-miss-verdict"),
        ("Review done.\nVERDICT: MAYBE", "near-miss-verdict"),
        (FINDINGS_ONLY, "findings-no-verdict"),
        ("NON-BLOCKING:\n- style\nAll good.", "findings-no-verdict"),
        ("I started checking the handler and then", "truncated"),
        ("Here is the repro:\n```\n$ pytest\nE assert", "truncated"),
        ("I looked around and everything is fine.", "prose-only"),
    ],
)
def test_failure_shape(text, shape):
    assert _no_verdict.failure_shape(text) == shape


# --- A: the evidence store ---


def record(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "round_": 2,
        "judge": "correctness",
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "prompt_sha256": "cd" * 32,
        "attempt": 1,
        "classification": "unparseable",
        "reply": "I looked around and everything is fine.",
        "recovery": "not-attempted",
        "rerun": True,
    }
    return _no_verdict.make_record(**{**fields, **over})


def test_a_record_carries_every_field():
    rec = record(now=1000.0)
    assert rec == {
        "ts": 1000.0,
        "round": 2,
        "judge": "correctness",
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "prompt_sha256": "cd" * 32,
        "attempt": 1,
        "classification": "unparseable",
        "shape": "prose-only",
        "reply_chars": len("I looked around and everything is fine."),
        "reply": "I looked around and everything is fine.",
        "recovery": "not-attempted",
        "rerun": True,
    }


def test_the_reply_is_redacted_and_bounded_keeping_head_and_tail():
    secret = "sk-ant-api03-" + "A" * 60
    rec = record(reply=f"HEAD {secret} " + "x" * 50_000 + " TAIL")
    assert secret not in rec["reply"]
    assert len(rec["reply"]) < _no_verdict.MAX_REPLY_CHARS + 100
    assert rec["reply"].startswith("HEAD ") and rec["reply"].endswith(" TAIL")
    assert "chars cut" in rec["reply"]
    assert rec["reply_chars"] > 50_000


def test_the_store_is_under_the_state_root_and_nothing_goes_in_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.chdir(workspace)
    assert _no_verdict.append_record(record())
    path = _no_verdict.store_path()
    assert path.is_relative_to(tmp_path / "state") and path.exists()
    assert list(workspace.iterdir()) == []
    assert _no_verdict.read_records()[0]["judge"] == "correctness"


def test_retention_drops_the_oldest_records_beyond_the_bound(tmp_path):
    path = tmp_path / "nv.jsonl"
    now = time.time()
    for n in range(_no_verdict.MAX_RECORDS + 5):
        assert _no_verdict.append_record(record(round_=n, now=now), now=now, path=path)
    kept = _no_verdict.read_records(path)
    assert len(kept) == _no_verdict.MAX_RECORDS
    assert [r["round"] for r in kept][0] == 5 and kept[-1]["round"] == _no_verdict.MAX_RECORDS + 4


def test_retention_drops_records_older_than_the_age_bound(tmp_path):
    path = tmp_path / "nv.jsonl"
    now = time.time()
    old = now - (_no_verdict.MAX_AGE_DAYS + 1) * 86400
    _no_verdict.append_record(record(round_=1, now=old), now=old, path=path)
    _no_verdict.append_record(record(round_=2, now=now), now=now, path=path)
    assert [r["round"] for r in _no_verdict.read_records(path)] == [2]


def test_a_damaged_line_is_skipped(tmp_path):
    path = tmp_path / "nv.jsonl"
    _no_verdict.append_record(record(), path=path)
    path.write_text("not json\n" + path.read_text())
    assert len(_no_verdict.read_records(path)) == 1


def test_an_unwritable_state_dir_logs_once_and_does_not_raise(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setattr(_session_config, "STATE_ROOT", blocker)
    monkeypatch.setattr(_no_verdict, "_write_failed", False)
    assert not _no_verdict.append_record(record())
    assert not _no_verdict.append_record(record())
    assert capsys.readouterr().err.count("could not keep the evidence") == 1


# --- B: measurement ---


def test_the_timeline_counts_calls_followups_and_reruns_and_restores_them(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    for judge, engine in (("craft", "claude"), ("craft", "codex"), ("correctness", "claude")):
        timeline.count_judge_call(judge, engine)
    timeline.count_followup("craft", "claude", recovered=True)
    timeline.count_followup("craft", "codex", recovered=False)
    timeline.count_rerun("craft", "codex")
    snapshot = timeline.snapshot()["review"]
    assert snapshot["judge_calls"] == {
        "by_judge": {"craft": 2, "correctness": 1},
        "by_engine": {"claude": 2, "codex": 1},
    }
    assert snapshot["followups"]["attempted"]["by_judge"] == {"craft": 2}
    assert snapshot["followups"]["recovered"]["by_engine"] == {"claude": 1}
    assert snapshot["reruns"]["by_judge"] == {"craft": 1}
    timeline._save_metrics()
    restored = _timeline._Timeline()
    assert (restored.judge_calls, restored.followups, restored.reruns) == (
        timeline.judge_calls,
        timeline.followups,
        timeline.reruns,
    )


def test_a_metrics_file_from_before_the_new_counts_still_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    timeline.last_round_head = "abc"
    timeline.count_no_verdict("craft", "claude")
    state = timeline._metrics_state()
    del state["judge_calls"], state["followups"], state["reruns"]
    path = timeline._metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    restored = _timeline._Timeline()
    assert restored.last_round_head == "abc" and restored.no_verdict["by_judge"] == {"craft": 1}
    # Each no-verdict reply was a call, so the calls are seeded from them (the rate cannot exceed 1).
    assert restored.judge_calls == {"by_judge": {"craft": 1}, "by_engine": {"claude": 1}}
    assert restored.followups["attempted"] == {"by_judge": {}, "by_engine": {}}


def test_factory_status_shows_the_rate_per_judge_and_engine():
    review = _factory_status._review(
        {
            "review": {
                "no_verdict": {"by_judge": {"craft": 1}, "by_engine": {"claude": 1}},
                "judge_calls": {"by_judge": {"craft": 4, "correctness": 2}, "by_engine": {"claude": 6}},
                "followups": {"attempted": {"by_judge": {"craft": 1}}},
                "reruns": {"by_judge": {}},
            }
        }
    )
    assert review["no_verdict_rate"]["by_judge"] == {
        "correctness": {"calls": 2, "no_verdict": 0, "rate": 0.0},
        "craft": {"calls": 4, "no_verdict": 1, "rate": 0.25},
    }
    assert review["no_verdict_rate"]["by_engine"]["claude"] == {"calls": 6, "no_verdict": 1, "rate": 0.1667}
    assert review["followups"] == {"attempted": {"by_judge": {"craft": 1}}}


def test_factory_status_tolerates_snapshots_without_the_new_keys():
    assert _factory_status._review({"review": {"scores": [1]}}) == {"scores": [1]}
    old = {"review": {"no_verdict": {"by_judge": {"craft": 2}, "by_engine": {}}}}
    rate = _factory_status._review(old)["no_verdict_rate"]
    assert rate["by_judge"]["craft"] == {"calls": 0, "no_verdict": 2, "rate": None}
    assert "no_verdict_rate" not in _factory_status._review({"review": {"judge_calls": "garbage"}})


# --- C: the report ---


def fixture_records() -> list[dict[str, Any]]:
    return [
        record(reply="", classification="no output", engine="claude"),
        record(
            reply=FINDINGS_ONLY, judge="craft", engine="codex", recovery="followup-recovered", rerun=False
        ),
        record(reply=FINDINGS_ONLY, judge="craft", recovery="followup-failed: timeout"),
        record(reply="Verdict: probably PASS", recovery="followup-recovered", rerun=False),
    ]


def test_the_report_groups_by_shape_judge_and_engine_and_counts_followups():
    report = _no_verdict.build_report(fixture_records())
    assert report["records"] == 4
    assert report["by_shape"] == {"empty": 1, "findings-no-verdict": 2, "near-miss-verdict": 1}
    assert report["by_judge"] == {"correctness": 2, "craft": 2}
    assert report["by_engine"] == {"claude": 3, "codex": 1}
    assert report["by_judge_shape"]["craft"] == {"findings-no-verdict": 2}
    assert report["followup"] == {"attempted": 3, "recovered": 2, "recovery_rate": 0.667}
    assert report["full_reruns"] == 2


def test_the_report_command_prints_text_and_json(tmp_path, capsys):
    path = tmp_path / "nv.jsonl"
    for rec in fixture_records():
        _no_verdict.append_record(rec, path=path)
    assert _no_verdict.main(["report", "--path", str(path)]) == 0
    out = capsys.readouterr().out
    assert "4 replies with no verdict" in out and "findings-no-verdict: 2" in out
    assert "follow-ups: 3 attempted, 2 recovered (67%)" in out
    assert _no_verdict.main(["report", "--json", "--path", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["by_shape"]["empty"] == 1


def test_the_report_of_an_empty_store_says_so(tmp_path, capsys):
    assert _no_verdict.main(["report", "--path", str(tmp_path / "none.jsonl")]) == 0
    assert "0 replies with no verdict" in capsys.readouterr().out


# --- E: the follow-up in the sub-agent ---


def runner_returning(answer: dict[str, Any] | Exception):
    calls: list[tuple[str, str]] = []

    async def go(session: str, prompt: str) -> dict[str, Any]:
        calls.append((session, prompt))
        if isinstance(answer, Exception):
            raise answer
        return answer

    go.calls = calls  # type: ignore[attr-defined]
    return go


def structured(value: object) -> dict[str, Any]:
    return {"subtype": "success", "is_error": False, "structured_output": {"verdict": value}}


@pytest.mark.asyncio
async def test_a_reply_with_findings_and_no_verdict_is_recovered_with_one_call_and_unchanged_findings():
    runner = runner_returning(structured("FAIL"))
    result = await _verdict_recovery.recover(FINDINGS_ONLY, {"session_id": "s-1"}, runner)
    assert result.outcome == "recovered"
    assert runner.calls == [("s-1", _verdict_recovery.PROMPT)]
    assert result.text == f"{FINDINGS_ONLY}{_no_verdict.recovered_suffix('FAIL')}"
    assert _findings.VERDICT.findall(result.text)[-1] == "FAIL"
    # The findings the reply wrote are the findings it still has.
    before = _findings.parse_findings(FINDINGS_ONLY)
    assert _findings.parse_findings(result.text) == before and before
    assert _no_verdict.original_reply(result.text) == FINDINGS_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [PASS, FAIL_WITH_EVIDENCE, "ok\nFinal verdict: PASS"])
async def test_a_reply_that_has_a_verdict_never_asks(text):
    runner = runner_returning(structured("PASS"))
    result = await _verdict_recovery.recover(text, {"session_id": "s"}, runner)
    assert (result.text, result.outcome, runner.calls) == (text, "", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "",
        _reviewer._NO_FINAL_MESSAGE,
        '{"type":"result","subtype":"error_max_turns"}',
        "API Error: 529 overloaded",
        f"{_usage.USAGE_LIMIT_MARK} resets 5pm",
    ],
)
async def test_only_unparseable_replies_get_a_followup(text):
    runner = runner_returning(structured("PASS"))
    result = await _verdict_recovery.recover(text, {"session_id": "s"}, runner)
    assert (result.text, result.outcome, runner.calls) == (text, "", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (RuntimeError("exit 1"), "failed: RuntimeError"),
        (asyncio.TimeoutError(), "failed: timeout"),
        (ValueError("bad json"), "failed: ValueError"),
        (structured("MAYBE"), "failed: not a single PASS or FAIL"),
        (structured("pass"), "failed: not a single PASS or FAIL"),
        (structured(None), "failed: not a single PASS or FAIL"),
        ({"structured_output": {"verdict": "PASS"}, "is_error": True}, "failed: not a single PASS or FAIL"),
        ({"subtype": "error_max_turns"}, "failed: not a single PASS or FAIL"),
        ({"result": "VERDICT: PASS\nand FAIL too"}, "failed: not a single PASS or FAIL"),
        ({"result": "I think it passes"}, "failed: not a single PASS or FAIL"),
        ({}, "failed: not a single PASS or FAIL"),
    ],
)
async def test_a_followup_that_fails_leaves_the_text_untouched(answer, why):
    result = await _verdict_recovery.recover(FINDINGS_ONLY, {"session_id": "s"}, runner_returning(answer))
    assert result.text == FINDINGS_ONLY and result.outcome == why


@pytest.mark.asyncio
async def test_a_session_without_an_id_cannot_be_resumed():
    runner = runner_returning(structured("PASS"))
    result = await _verdict_recovery.recover(FINDINGS_ONLY, {}, runner)
    assert result.text == FINDINGS_ONLY and result.outcome == "skipped: no session id" and not runner.calls


def test_a_plain_result_line_is_the_fallback_when_the_cli_returns_no_structured_field():
    assert _verdict_recovery.read_verdict({"result": "VERDICT: PASS"}) == "PASS"
    assert _verdict_recovery.read_verdict({"result": "\n**VERDICT: FAIL**\n"}) == "FAIL"


def test_the_trailer_carries_the_outcome_and_adds_the_followups_usage():
    trailer = _usage._usage_trailer(json.dumps({"total_cost_usd": 1.0, "num_turns": 5, "usage": {}}))
    followup = _verdict_recovery.Followup(
        "t", "recovered", {"total_cost_usd": 0.25, "num_turns": 1, "usage": {"output_tokens": 7}}
    )
    clean, rec = _usage._split_usage("text" + _verdict_recovery.with_followup(trailer, followup))
    assert clean == "text" and rec["verdict_followup"] == "recovered"
    assert rec["cost_usd"] == 1.25 and rec["turns"] == 6 and rec["output_tokens"] == 7
    assert _verdict_recovery.with_followup(trailer, _verdict_recovery.Followup("t", "")) == trailer


@pytest.mark.asyncio
async def test_the_claude_call_resumes_the_session_with_the_schema_and_a_small_turn_cap(
    tmp_path, monkeypatch
):
    seen: dict[str, Any] = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps(structured("PASS")).encode(), b""

    async def spawn(_what, *argv, **kwargs):
        seen["argv"], seen["kwargs"] = list(argv), kwargs
        return Proc()

    monkeypatch.setattr(_unprivileged, "spawn", spawn)
    answer = await _verdict_recovery.run_claude("claude", tmp_path, {}, "opus", "high", "s-9", "ask")
    argv = seen["argv"]
    assert answer["structured_output"] == {"verdict": "PASS"}
    assert argv[argv.index("--resume") + 1] == "s-9"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == _verdict_recovery.SCHEMA
    assert argv[argv.index("--max-turns") + 1] == "2"
    assert argv[argv.index("--tools") + 1] == ""


# --- E, F: the supervisor's side ---


class FollowupReviewers(Reviewers):
    """`Reviewers` where a reply may come with what the sub-agent's verdict follow-up did."""

    def __init__(self, replies, followups=None, **kw):
        super().__init__(replies, **kw)
        self.followups = followups or {}
        self.asked: list[tuple[str, Any]] = []

    async def __call__(self, *, on_followup=None, on_setting=None, verdict_followup=False, **kw):
        if on_setting:
            on_setting("claude", "opus", "high")
        reply, out = await super().__call__(**kw)
        judge = self.calls[-1][0]
        self.asked.append((judge, verdict_followup))
        n = self.count(judge)
        script = self.followups.get(judge, [])
        if on_followup and script and script[min(n, len(script)) - 1]:
            on_followup(script[min(n, len(script)) - 1])
        return reply, out


def recovered_text(original: str, verdict: str) -> str:
    return f"{original}{_no_verdict.recovered_suffix(verdict)}"


@pytest.mark.asyncio
async def test_a_recovered_verdict_is_used_with_no_full_rerun_and_is_kept_as_evidence(run):
    reviewers = FollowupReviewers(
        {"completeness": [recovered_text(UNEVIDENCED, "PASS")]}, {"completeness": ["recovered"]}
    )
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("completeness") == 1
    assert timeline.judges["completeness"]["outcome"] == "PASS"
    assert any("verdict recovered by a follow-up" in text for _, _, text in timeline._rows)
    assert timeline.followups["attempted"]["by_judge"] == {"completeness": 1}
    assert timeline.followups["recovered"]["by_judge"] == {"completeness": 1}
    assert timeline.reruns == {"by_judge": {}, "by_engine": {}}
    assert timeline.judge_calls["by_judge"]["completeness"] == 1
    (kept,) = _no_verdict.read_records()
    assert (kept["judge"], kept["recovery"], kept["rerun"], kept["attempt"]) == (
        "completeness",
        "followup-recovered",
        False,
        1,
    )
    assert kept["reply"] == UNEVIDENCED and kept["classification"] == "unparseable"
    assert (kept["model"], kept["effort"], kept["engine"], kept["round"]) == ("opus", "high", "claude", 1)
    assert len(kept["prompt_sha256"]) == 64


@pytest.mark.asyncio
async def test_a_failed_followup_falls_back_to_the_full_rerun(run):
    reviewers = FollowupReviewers(
        {"completeness": [UNEVIDENCED, PASS]}, {"completeness": ["failed: timeout", ""]}
    )
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("completeness") == 2
    assert timeline.followups["attempted"]["by_judge"] == {"completeness": 1}
    assert timeline.followups["recovered"]["by_judge"] == {}
    assert timeline.reruns["by_judge"] == {"completeness": 1}
    assert timeline.judge_calls["by_judge"]["completeness"] == 2
    (kept,) = _no_verdict.read_records()
    assert (kept["recovery"], kept["rerun"]) == ("followup-failed: timeout", True)


@pytest.mark.asyncio
async def test_a_missing_verdict_that_is_never_recovered_ends_no_verdict_and_every_attempt_is_kept(run):
    reviewers = FollowupReviewers(
        {"completeness": [UNEVIDENCED]}, {"completeness": ["failed: not a single PASS or FAIL"]}
    )
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewIncomplete) as raised:
        await run(reviewers, timeline=timeline)
    assert list(raised.value.missing) == ["completeness"]
    assert reviewers.count("completeness") == _review._NO_VERDICT_ATTEMPTS
    assert timeline.judges["completeness"]["outcome"] == "NO_VERDICT"
    kept = _no_verdict.read_records()
    assert [r["attempt"] for r in kept] == [1, 2, 3]
    assert [r["rerun"] for r in kept] == [True, True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [PASS, FAIL_WITH_EVIDENCE])
async def test_a_reply_with_a_verdict_is_judged_as_before_and_triggers_nothing(run, reply):
    reviewers = FollowupReviewers({"completeness": [reply]})
    timeline = _timeline._Timeline()
    try:
        await run(reviewers, timeline=timeline)
    except _reviewer.ReviewIncomplete:
        pytest.fail("a verdict was returned")
    assert timeline.judges["completeness"]["outcome"] == ("PASS" if reply == PASS else "FAIL")
    assert timeline.judges["completeness"]["attempts"] == 1
    assert timeline.followups["attempted"] == {"by_judge": {}, "by_engine": {}}
    assert timeline.no_verdict == {"by_judge": {}, "by_engine": {}}
    assert _no_verdict.read_records() == []


@pytest.mark.asyncio
async def test_no_output_goes_to_the_existing_retry_and_is_kept_as_not_attempted(run):
    reviewers = FollowupReviewers({"completeness": ["", PASS]})
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("completeness") == 2
    (kept,) = _no_verdict.read_records()
    assert (kept["classification"], kept["shape"], kept["recovery"]) == (
        "no output",
        "empty",
        "not-attempted",
    )
    assert timeline.followups["attempted"] == {"by_judge": {}, "by_engine": {}}


@pytest.mark.asyncio
async def test_judges_ask_for_the_followup_but_an_advisory_craft_does_not(run):
    reviewers = FollowupReviewers({})
    await run(reviewers)
    asked = dict(reviewers.asked)
    assert asked["correctness"] and asked["completeness"]
    assert asked["craft"] is False


@pytest.mark.asyncio
async def test_a_recovered_pass_does_not_override_an_evidenced_blocking_finding(run):
    blocking = f"FINDING: crash\nBreaks: C1\nEvidence: {EV}\nBLOCKING: 1"
    assert _findings.select_blocking(_findings.parse_findings(blocking))
    reviewers = FollowupReviewers(
        {"correctness": [recovered_text(blocking, "PASS")]}, {"correctness": ["recovered"]}
    )
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert not passed
    assert timeline.judges["correctness"]["outcome"] == "FAIL"
    assert reviewers.count("correctness") == 1


@pytest.mark.asyncio
async def test_call_reviewer_hands_the_followup_outcome_to_the_caller_and_strips_it(monkeypatch):
    trailer = _verdict_recovery.with_followup(
        _usage._usage_trailer(json.dumps({"total_cost_usd": 0.5})),
        _verdict_recovery.Followup("t", "recovered"),
    )

    async def sealed(purpose, engine, **kwargs):
        return "text" + trailer, None

    monkeypatch.setattr(_reviewer, "_call_sealed", sealed)
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda _engine: False)
    got: list[str] = []
    text, _ = await _reviewer._call_reviewer(purpose="correctness", engine="claude", on_followup=got.append)
    assert text == "text" and got == ["recovered"]


def test_the_module_size_gate_still_holds():
    from flyte_agent_factory import _module_size

    src = Path(_review.__file__).parent
    assert all(len(p.read_text().splitlines()) <= _module_size.MODULE_LINE_LIMIT for p in src.glob("*.py"))


# --- Codex: the same follow-up on the same thread ---

TURN = (
    '{{"type":"turn.completed","usage":{{"input_tokens":{i},"cached_input_tokens":{c},"output_tokens":{o}}}}}'
)
THREAD = "019a0000-0000-7000-8000-000000000001"


def _fake_codex(
    tmp_path, monkeypatch, *, first: str, resume: str = "", resume_exit: int = 0, thread: bool = True
):
    """Put a `codex` on PATH: it answers `first`; a `resume` call logs its argv and answers `resume`."""
    import os
    import stat

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    started = f'echo \'{{"type":"thread.started","thread_id":"{THREAD}"}}\'\n' if thread else ""
    fake = bin_dir / "codex"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = login ]; then cat > "$CODEX_HOME/auth.json"; exit 0; fi\n'
        'out=""; schema=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; [ "$prev" = "--output-schema" ] && schema="$a"; '
        'prev="$a"; done\n'
        'if [ "$2" = resume ]; then\n'
        f'  printf "%s\\n" "$@" >> "{log}"; cat "$schema" > "{tmp_path}/schema.copy"\n'
        f"  [ {resume_exit} -ne 0 ] && exit {resume_exit}\n"
        f"  printf '%s' '{resume}' > \"$out\"\n"
        f"  echo '{TURN.format(i=10, c=4, o=3)}'\n"
        "  exit 0\nfi\n"
        f"printf '%s' '{first}' > \"$out\"\n" + started + f"echo '{TURN.format(i=50, c=20, o=7)}'\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "x")
    return log


async def _run_codex_judge(tmp_path, followup=True):
    from flyte_agent_factory import _subagent_run

    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / ".claude-flyte").mkdir(exist_ok=True)
    (work / ".claude-flyte" / "BRIEF.md").write_text("review it")
    out = await _subagent_run._run_codex(
        work,
        tmp_path,
        "review it",
        answer_only=True,
        role="",
        model="",
        effort="medium",
        verdict_followup=followup,
    )
    return _usage._split_usage(out)


FINDINGS = "FINDING: the loop never ends\nevidence: a.py:3"


@pytest.mark.asyncio
async def test_a_codex_reply_without_a_verdict_is_resumed_once_on_its_thread(tmp_path, monkeypatch):
    log = _fake_codex(tmp_path, monkeypatch, first=FINDINGS, resume='{"verdict":"PASS"}')
    text, rec = await _run_codex_judge(tmp_path)
    assert text == FINDINGS + _no_verdict.recovered_suffix("PASS")
    assert rec["verdict_followup"] == "recovered"
    argv = log.read_text().splitlines()
    assert argv.count("resume") == 1 and argv[:2] == ["exec", "resume"]
    assert argv[-3:] == ["--", THREAD, _verdict_recovery.PROMPT]
    assert "--output-schema" in argv and "-o" in argv and "--json" in argv
    assert json.loads((tmp_path / "schema.copy").read_text()) == _verdict_recovery.SCHEMA
    # the follow-up's tokens are added to the judge's own (50-20 + 10-4 uncached input; 7 + 3 output)
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read_input_tokens"]) == (36, 10, 24)


@pytest.mark.asyncio
async def test_a_codex_reply_that_has_a_verdict_makes_no_resume_call(tmp_path, monkeypatch):
    log = _fake_codex(tmp_path, monkeypatch, first="all good\nVERDICT: PASS", resume='{"verdict":"FAIL"}')
    text, rec = await _run_codex_judge(tmp_path)
    assert text == "all good\nVERDICT: PASS" and "verdict_followup" not in rec
    assert not log.exists()


@pytest.mark.asyncio
async def test_a_codex_followup_is_not_made_unless_asked_for(tmp_path, monkeypatch):
    log = _fake_codex(tmp_path, monkeypatch, first=FINDINGS, resume='{"verdict":"PASS"}')
    text, _ = await _run_codex_judge(tmp_path, followup=False)
    assert text == FINDINGS and not log.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume,exit_code",
    [('{"verdict":"PASS"}', 3), ('{"verdict":"MAYBE"}', 0), ("I could not decide", 0), ("", 0)],
)
async def test_a_failed_codex_followup_leaves_the_text_untouched(tmp_path, monkeypatch, resume, exit_code):
    log = _fake_codex(tmp_path, monkeypatch, first=FINDINGS, resume=resume, resume_exit=exit_code)
    text, rec = await _run_codex_judge(tmp_path)
    assert text == FINDINGS
    assert rec["verdict_followup"].startswith("failed: ")
    assert log.read_text().splitlines().count("resume") == 1


@pytest.mark.asyncio
async def test_a_codex_run_without_a_thread_id_cannot_be_resumed(tmp_path, monkeypatch):
    log = _fake_codex(tmp_path, monkeypatch, first=FINDINGS, resume='{"verdict":"PASS"}', thread=False)
    text, rec = await _run_codex_judge(tmp_path)
    assert text == FINDINGS and rec["verdict_followup"] == "skipped: no session id"
    assert not log.exists()


# --- review fixes: the strict pattern is the section boundary, variants only read a verdict ---

# The pattern as it was before the variants existed; the strict one must stay exactly this.
ORIGINAL_VERDICT = re.compile(
    r"^[ \t>#*_`]*VERDICT[*_`]*[ \t]*[:\-–—][*_`]*[ \t]*[*_`]*(PASS|FAIL)[*_`]*[ \t]*[.!]?"
    r"[ \t]*(?:\([^()\n]{0,80}\))?[ \t]*[.!]?[*_`]*[ \t\r]*$",
    re.MULTILINE | re.IGNORECASE,
)
PER_FINDING_VERDICT = (
    "FINDING: crash on empty input\nBreaks: C2\nVerdict: FAIL - reproduced on HEAD\n"
    "Evidence: command: f '' | output: Traceback\n\nVERDICT: FAIL\n"
)
FLIP_CLAIMS = "The empty input crashes.\nVERDICT: FAIL\n\nFinal verdict: PASS — once the crash is fixed\n"


def test_the_strict_pattern_is_the_original_one():
    assert _findings.VERDICT.pattern == ORIGINAL_VERDICT.pattern
    assert _findings.VERDICT.flags == ORIGINAL_VERDICT.flags


def test_a_per_finding_verdict_with_a_reason_does_not_cut_the_finding_off_its_evidence():
    (finding,) = _findings.parse_findings(PER_FINDING_VERDICT)
    assert "Traceback" in finding.evidence
    assert _findings.decide(PER_FINDING_VERDICT, None).verdict == "FAIL"


def test_a_later_prose_final_verdict_does_not_flip_a_strict_fail():
    check = "CLAIM 1: crash on empty input\nCHECK 1: REPRODUCED -- traceback seen\n"
    assert _findings.verdict_words(FLIP_CLAIMS) == ["FAIL"]
    assert _findings.needs_check(FLIP_CLAIMS)
    assert _findings.decide(FLIP_CLAIMS, check).verdict == "FAIL"
    assert _findings.decide(FLIP_CLAIMS, check) == _findings.decide(FLIP_CLAIMS, check)


@pytest.mark.parametrize("text", [PER_FINDING_VERDICT, FLIP_CLAIMS, "x\n> VERDICT: PASS\nVERDICT: FAIL\n"])
def test_a_text_with_a_strict_line_reads_exactly_as_before(text):
    assert _findings.verdict_words(text) == ORIGINAL_VERDICT.findall(text)


def test_a_variant_counts_only_when_there_is_no_strict_line():
    assert _findings.verdict_words("VERDICT: PASS\nFinal verdict: FAIL") == ["PASS"]
    assert _findings.verdict_words("Final verdict: FAIL\nOverall verdict = PASS") == ["PASS"]


@pytest.mark.parametrize(
    "line",
    [
        "VERDICT: PASS - but it fails on X",
        "VERDICT: PASS - though 3 tests FAILED",
        "VERDICT: PASS - except criterion 7 does not hold",
        "VERDICT: FAIL - otherwise PASSes",
    ],
)
def test_a_hedged_reason_is_not_a_verdict_for_the_review_or_the_follow_up(line):
    text = f"FINDING: x\nEvidence: file: a.py:3 bad\n{line}\n"
    assert _findings.verdict_words(text) == []
    assert _review._find_usable_result_gap(text, False) == "unparseable"
    assert _verdict_recovery.wants_followup(text)
    assert _verdict_recovery.read_verdict({"result": line}) is None


def test_the_follow_up_fallback_accepts_a_variant_line():
    assert _verdict_recovery.read_verdict({"result": "Final verdict: FAIL"}) == "FAIL"


# --- review fixes: the recovered suffix leaves the findings as they were ---

LAST_FINDING = "FINDING: crash\nBreaks: C2\nEvidence: command: f '' | output: Traceback\n  more output line\n"
RECOVERY_SHAPES = {
    "finding last, multi-line evidence": LAST_FINDING,
    "no trailing newline": LAST_FINDING.rstrip(),
    "trailing blank lines": LAST_FINDING + "\n\n\n",
    "non-blocking last": "FINDING: a\nEvidence: file: a.py:3 x\nNON-BLOCKING:\n- style nit\n",
    "non-blocking finding last": "NON-BLOCKING:\nFINDING: a\nEvidence: file: a.py:3 x\n",
    "ends with blocking count": UNEVIDENCED,
    "findings only": FINDINGS_ONLY,
    "evidence in a fence": "FINDING: a\nEvidence:\n```\n$ pytest\nE assert\n```",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("original", list(RECOVERY_SHAPES.values()), ids=list(RECOVERY_SHAPES))
@pytest.mark.parametrize("word", ["PASS", "FAIL"])
async def test_a_recovered_reply_parses_to_the_same_findings_and_strips_back(original, word):
    result = await _verdict_recovery.recover(
        original, {"session_id": "s"}, runner_returning(structured(word))
    )
    assert result.outcome == "recovered"
    assert _findings.parse_findings(result.text) == _findings.parse_findings(original)
    assert _no_verdict.original_reply(result.text) == original
    assert _findings.verdict_words(result.text)[-1] == word


@pytest.mark.asyncio
async def test_a_reply_cut_off_inside_a_fence_is_not_recovered():
    text = "FINDING: a\nEvidence:\n```\n$ pytest\nE assert"
    result = await _verdict_recovery.recover(text, {"session_id": "s"}, runner_returning(structured("PASS")))
    assert result.text == text and result.outcome == "skipped: unclosed code fence"


@pytest.mark.asyncio
async def test_an_unexpected_runner_error_is_a_failed_follow_up_not_a_crash():
    result = await _verdict_recovery.recover(
        FINDINGS_ONLY, {"session_id": "s"}, runner_returning(KeyError("session"))
    )
    assert result.text == FINDINGS_ONLY and result.outcome == "failed: KeyError"


def test_a_follow_up_mark_in_the_middle_of_a_reply_is_not_stripped():
    text = f"a{_no_verdict.recovered_suffix('PASS')}b"
    assert _no_verdict.original_reply(text) == text


# --- review fixes: the evidence file and the counters survive damage ---


def test_a_file_with_invalid_utf8_still_reads_and_appends(tmp_path):
    path = tmp_path / "nv.jsonl"
    _no_verdict.append_record({"ts": time.time(), "i": "good"}, path=path)
    path.write_bytes(b"\xff\xfe\x80 not utf-8\n" + path.read_bytes())
    assert [r.get("i") for r in _no_verdict.read_records(path)] == ["good"]
    assert _no_verdict.append_record({"ts": time.time(), "i": "next"}, path=path)
    assert [r.get("i") for r in _no_verdict.read_records(path)] == ["good", "next"]
    assert b"\xff" not in path.read_bytes()


def test_the_report_command_survives_invalid_utf8(tmp_path, capsys):
    path = tmp_path / "nv.jsonl"
    path.write_bytes(b"\xff\xfe\n")
    assert _no_verdict.main(["report", "--path", str(path)]) == 0
    assert "0 replies with no verdict" in capsys.readouterr().out


def test_a_record_dated_in_the_future_is_dropped_by_retention(tmp_path):
    path = tmp_path / "nv.jsonl"
    now = time.time()
    _no_verdict.append_record({"ts": now + 10 * 86400, "i": "future"}, path=path, now=now)
    _no_verdict.append_record({"ts": now, "i": "now"}, path=path, now=now)
    assert [r.get("i") for r in _no_verdict.read_records(path)] == ["now"]


def test_an_old_metrics_file_seeds_the_calls_so_the_rate_stays_at_most_one(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    timeline.count_no_verdict("correctness", "claude")
    timeline.count_no_verdict("correctness", "claude")
    state = timeline._metrics_state()
    del state["judge_calls"]
    path = timeline._metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    restored = _timeline._Timeline()
    assert restored.judge_calls["by_judge"] == {"correctness": 2}
    restored.count_judge_call("correctness", "claude")
    rate = _factory_status._review({"review": restored.snapshot()["review"]})["no_verdict_rate"]
    assert rate["by_judge"]["correctness"] == {"calls": 3, "no_verdict": 2, "rate": 0.6667}


def test_the_status_rate_is_never_above_one():
    review = _factory_status._review(
        {"review": {"no_verdict": {"by_judge": {"x": 5}}, "judge_calls": {"by_judge": {"x": 2}}}}
    )
    assert review["no_verdict_rate"]["by_judge"]["x"]["rate"] == 1.0


# --- second review: a variant is only the judge's own closing line; recovery adds only the verdict line ---


@pytest.mark.parametrize(
    "text",
    [
        "The author's notes say:\n> Final verdict: PASS\n\nIt crashes on empty input.\n"
        "VERDICT: FAIL - crashes on empty input",
        "Final verdict: PASS\n\nBut then I re-ran it and it crashes.",
        "> Final verdict: PASS",
        "```\nFinal verdict: PASS",
    ],
)
def test_a_variant_that_is_not_the_judges_own_last_line_is_no_verdict(text):
    assert _findings.verdict_words(text) == []
    assert _review._find_usable_result_gap(text, advisory_craft=False) == "unparseable"


def test_a_variant_as_the_last_line_still_counts():
    assert _findings.verdict_words("It works.\n\n**Final verdict:** PASS\n\n") == ["PASS"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        "The thing breaks.\nEvidence: command: pytest -q | output: 1 failed",
        "```FINDING: crash\nEvidence: command: x | output: boom\n```\ntrailing note",
    ],
)
async def test_a_recovered_reply_reads_as_if_the_judge_had_written_the_verdict(original):
    result = await _verdict_recovery.recover(
        original, {"session_id": "s"}, runner_returning(structured("FAIL"))
    )
    assert result.outcome == "recovered"
    written = f"{original}\n\nVERDICT: FAIL\n"
    assert result.text == written
    assert _findings.parse_findings(result.text) == _findings.parse_findings(written)
    assert all("recovered" not in f.evidence for f in _findings.parse_findings(result.text))


@pytest.mark.parametrize("outcome", ["skipped: no session id", "skipped: unclosed code fence"])
def test_a_follow_up_that_made_no_call_is_not_counted_as_attempted(outcome, tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    _no_verdict.note_reply(
        timeline,
        judge="correctness",
        engine="claude",
        round_=1,
        attempt=1,
        text=FINDINGS_ONLY,
        reason="unparseable",
        followup=outcome,
        seen={},
        last=False,
    )
    assert timeline.followups["attempted"]["by_judge"] == {}
    (record,) = _no_verdict.read_records()
    assert not record["recovery"].startswith("followup-")
    assert _no_verdict.build_report([record])["followup"]["attempted"] == 0


# --- an empty Claude result is no output, never a review to follow up ---

EMPTY_RESULT = json.dumps(
    {"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": "s-1"}
)


def test_an_empty_claude_result_is_no_text_not_the_cli_payload():
    from flyte_agent_factory import _subagent_run

    assert _subagent_run._final_text(EMPTY_RESULT) == ""
    # the raw-stdout fallback still holds for an answer in an unexpected shape
    odd = json.dumps({"answer": "x"})
    assert _subagent_run._final_text(odd) == odd


@pytest.mark.asyncio
async def test_an_empty_claude_result_makes_no_follow_up_and_stays_no_output(tmp_path, monkeypatch):
    from flyte_agent_factory import _subagent_run

    calls: list[str] = []

    async def run_claude(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("resume")
        return {"subtype": "success", "structured_output": {"verdict": "PASS"}}

    monkeypatch.setattr(_verdict_recovery, "run_claude", run_claude)
    out = await _subagent_run._claude_summary(
        EMPTY_RESULT.encode(),
        b"",
        0,
        tmp_path,
        ["claude"],
        {},
        brief="review it",
        role="",
        model="opus",
        effort="high",
        max_turns=10,
        answer_only=True,
        partial_base="",
        jail=None,
        verdict_followup=True,
    )
    text, rec = _usage._split_usage(out)
    assert calls == []
    assert text == _reviewer._NO_FINAL_MESSAGE
    assert "verdict_followup" not in rec
    assert (_no_verdict.classify(text), _no_verdict.failure_shape(text)) == ("no output", "empty")
    # no verdict line, so the judge's reply is unusable and goes to the retry path, never a PASS
    assert _findings.verdict_words(text) == []

    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    _no_verdict.note_reply(
        timeline,
        judge="correctness",
        engine="claude",
        round_=1,
        attempt=1,
        text=text,
        reason=_no_verdict.classify(text),
        followup=rec.get("verdict_followup", ""),
        seen={},
        last=False,
    )
    (record,) = _no_verdict.read_records()
    assert (record["classification"], record["shape"], record["recovery"]) == (
        "no output",
        "empty",
        "not-attempted",
    )
    assert timeline.followups["attempted"] == {"by_judge": {}, "by_engine": {}}


@pytest.mark.asyncio
async def test_a_cli_payload_reaching_recovery_is_never_followed_up():
    calls: list[str] = []

    async def runner(session: str, prompt: str) -> dict[str, Any]:
        calls.append(session)
        return {"subtype": "success", "structured_output": {"verdict": "PASS"}}

    payload = json.loads(EMPTY_RESULT)
    assert _verdict_recovery.wants_followup(EMPTY_RESULT) is False
    followup = await _verdict_recovery.recover(EMPTY_RESULT, payload, runner)
    assert calls == [] and followup.outcome == "" and followup.text == EMPTY_RESULT
    assert _findings.verdict_words(followup.text) == []


# --- the report splits follow-ups and re-runs by judge and engine, as `factory.status` does ---


def test_the_report_splits_followups_and_reruns_by_judge_and_engine():
    records = [
        {"judge": "correctness", "engine": "claude", "recovery": "followup-recovered", "rerun": False},
        {"judge": "completeness", "engine": "codex", "recovery": "followup-failed: timeout", "rerun": True},
        {
            "judge": "craft",
            "engine": "claude",
            "recovery": "not-attempted (skipped: no session id)",
            "rerun": False,
        },
    ]
    report = _no_verdict.build_report(records)
    assert report["followups"] == {
        "attempted": {
            "by_judge": {"completeness": 1, "correctness": 1},
            "by_engine": {"claude": 1, "codex": 1},
        },
        "recovered": {"by_judge": {"correctness": 1}, "by_engine": {"claude": 1}},
    }
    assert report["reruns"] == {"by_judge": {"completeness": 1}, "by_engine": {"codex": 1}}
    text = _no_verdict.format_report(report)
    assert (
        "follow-ups by judge:\n  completeness: 1 attempted, 0 recovered\n"
        "  correctness: 1 attempted, 1 recovered" in text
    )
    assert (
        "follow-ups by engine:\n  claude: 1 attempted, 1 recovered\n  codex: 1 attempted, 0 recovered" in text
    )
    assert "full judge re-runs by judge:\n  completeness: 1" in text
    assert "full judge re-runs by engine:\n  codex: 1" in text
    bare = _no_verdict.build_report([{"recovery": "followup-failed: timeout", "rerun": True}])
    assert bare["followups"]["attempted"]["by_judge"] == {"unknown": 1}
    assert bare["reruns"]["by_engine"] == {"unknown": 1}


def test_an_empty_report_says_none_for_every_group():
    text = _no_verdict.format_report(_no_verdict.build_report([]))
    assert "by judge and shape:\n  (none)" in text
    for title in ("follow-ups by judge", "follow-ups by engine", "full judge re-runs by judge"):
        assert f"{title}:\n  (none)" in text


def test_the_record_time_is_documented_as_epoch_seconds_utc():
    docs = (Path(__file__).resolve().parents[1] / "docs" / "architecture.md").read_text()
    assert "epoch" in (_no_verdict.make_record.__doc__ or "") and "UTC" in (
        _no_verdict.make_record.__doc__ or ""
    )
    assert "`ts` (seconds since the Unix epoch, UTC)" in docs
