"""`_one_judge` with the finding check: when it runs, what it drops, that it fails closed."""

from __future__ import annotations

from typing import Any, cast

import pytest
from flyte.io import Dir

from flyte_agent_factory import _policy, _review, _reviewer, _timeline

EV = "command: app --empty | output: Traceback"
FAIL = f"FINDING: crash\nBreaks: C1\nEvidence: {EV}\nBLOCKING: 1\nVERDICT: FAIL"
TWO = f"FINDING: one\nEvidence: {EV}\nFINDING: two\nEvidence: {EV}\nBLOCKING: 2\nVERDICT: FAIL"
REDACTED_FAIL = (
    "FINDING: key file is world readable\nBreaks: C1\n"
    "Evidence: reproduction: tail -2 ~/.ssh/id_ed25519\n<redacted> -> returned unchanged\n"
    "BLOCKING: 1\nVERDICT: FAIL"
)
NO_EVIDENCE_FAIL = "Handling is poor.\nBLOCKING: 1\nVERDICT: FAIL"


class Calls:
    """A scripted `_call_reviewer`: the judge's reply, then the check's (an Exception is raised)."""

    def __init__(self, judge_reply: str, *check_replies: Any) -> None:
        self.judge_reply = judge_reply
        self.check_replies = list(check_replies)
        self.judge_calls: list[dict[str, Any]] = []
        self.check_calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> tuple[str, None]:
        if kwargs["purpose"].endswith("-check"):
            self.check_calls.append(kwargs)
            reply = self.check_replies[min(len(self.check_calls), len(self.check_replies)) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply, None
        self.judge_calls.append(kwargs)
        return self.judge_reply, None


async def judge_round(monkeypatch, calls: Calls, judge: str = "correctness", timeline=None):
    monkeypatch.setattr(_reviewer, "_call_reviewer", calls)
    timeline = timeline or _timeline._Timeline()
    nonblocking: list[str] = []
    blocking: list[int] = []
    new_reqs: list[str] = []
    name, passed, text = await _review._one_judge(
        judge,
        "inspect",
        cast("Dir[Any]", object()),  # the reviewer is stubbed; the snapshot is never read
        "brief",
        "",
        1,
        timeline,
        nonblocking=nonblocking,
        blocking=blocking,
        new_reqs=new_reqs,
    )
    rows = [row for _, _, row in timeline._rows]
    return passed, text, rows, nonblocking, blocking


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", ["correctness", "completeness"])
async def test_a_pass_runs_no_check(monkeypatch, judge):
    calls = Calls("fine\nBLOCKING: 0\nVERDICT: PASS")
    passed, _, _, _, blocking = await judge_round(monkeypatch, calls, judge)
    assert passed and blocking == [0] and calls.check_calls == []


@pytest.mark.asyncio
async def test_craft_never_runs_the_check(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_CRAFT_BLOCKING", "true")
    calls = Calls(FAIL)
    passed, _, _, _, _ = await judge_round(monkeypatch, calls, "craft")
    assert not passed and calls.check_calls == []


@pytest.mark.asyncio
async def test_an_evidenced_fail_is_checked_once_as_configured(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_FINDING_CHECK_MODEL", "haiku")
    monkeypatch.setenv("AGENT_FACTORY_FINDING_CHECK_EFFORT", "medium")
    calls = Calls(FAIL, "CHECK 1: REPRODUCED -- saw it")
    passed, text, _, _, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1]
    assert "FINDING: crash" in text and text.endswith("BLOCKING: 1\nVERDICT: FAIL")
    (check,) = calls.check_calls
    assert check["purpose"] == "correctness-check"
    assert (check["model"], check["effort"]) == ("haiku", "medium")
    assert check["max_turns"] == 40 and check["answer_only"] is True and check["role"] == "verify"
    assert "### Finding 1" in check["brief"]


@pytest.mark.asyncio
async def test_the_check_uses_the_policy_default(monkeypatch):
    calls = Calls(FAIL, "CHECK 1: REPRODUCED -- saw it")
    await judge_round(monkeypatch, calls)
    assert _policy.DEFAULTS["finding_check"] == _policy.Setting("sonnet", "low")
    (check,) = calls.check_calls
    assert (check["model"], check["effort"]) == tuple(_policy.resolve("finding_check"))


@pytest.mark.asyncio
async def test_findings_that_do_not_reproduce_turn_a_fail_into_a_pass(monkeypatch):
    calls = Calls(TWO, "CHECK 1: NOT REPRODUCED -- a\nCHECK 2: NOT REPRODUCED -- b")
    passed, _, rows, nonblocking, blocking = await judge_round(monkeypatch, calls)
    assert passed and blocking == [0]
    assert any("2 findings dropped (did not reproduce)" in row for row in rows)
    assert any("did not reproduce" in item and "FINDING: one" in item for item in nonblocking)


@pytest.mark.asyncio
async def test_a_redacted_finding_survives_a_not_reproduced_check(monkeypatch):
    calls = Calls(REDACTED_FAIL, "CHECK 1: NOT REPRODUCED -- ran it, fine")
    passed, text, rows, _, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1] and "FINDING:" in text
    assert not any("dropped" in row for row in rows)


@pytest.mark.asyncio
async def test_only_the_findings_that_reproduce_are_handed_on(monkeypatch):
    calls = Calls(TWO, "CHECK 1: NOT REPRODUCED -- a\nCHECK 2: REPRODUCED -- b")
    passed, text, rows, nonblocking, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1]
    assert "FINDING: two" in text and "FINDING: one" not in text
    assert any("1 findings dropped" in row for row in rows)
    assert any("FINDING: one" in item for item in nonblocking)


@pytest.mark.asyncio
async def test_a_check_that_cannot_run_keeps_the_fail_after_one_retry(monkeypatch):
    calls = Calls(FAIL, RuntimeError("pod did not schedule"))
    passed, text, rows, _, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1] and "FINDING: crash" in text
    assert len(calls.check_calls) == 2
    assert any("finding check could not run" in row for row in rows)


@pytest.mark.asyncio
async def test_a_check_that_fails_once_is_retried(monkeypatch):
    calls = Calls(TWO, RuntimeError("flaky"), "CHECK 1: NOT REPRODUCED -- a\nCHECK 2: NOT REPRODUCED -- b")
    passed, _, rows, _, _ = await judge_round(monkeypatch, calls)
    assert passed and len(calls.check_calls) == 2
    assert not any("could not run" in row for row in rows)


@pytest.mark.asyncio
async def test_a_fail_without_evidence_is_a_pass_unless_a_claim_reproduces(monkeypatch):
    calls = Calls(NO_EVIDENCE_FAIL, "CLAIM 1: poor handling\nCHECK 1: NOT REPRODUCED -- fine")
    passed, _, rows, _, blocking = await judge_round(monkeypatch, calls)
    assert passed and blocking == [0] and len(calls.check_calls) == 1
    assert "Handling is poor." in calls.check_calls[0]["brief"]
    assert any("no claim reproduced" in row for row in rows)


@pytest.mark.asyncio
async def test_a_fail_without_evidence_with_a_reproduced_claim_is_a_fail(monkeypatch):
    calls = Calls(NO_EVIDENCE_FAIL, "CLAIM 1: poor handling\nCHECK 1: REPRODUCED -- crashed on empty input")
    passed, text, _, _, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1]
    assert "poor handling -- crashed on empty input" in text


@pytest.mark.asyncio
async def test_a_fail_without_evidence_and_no_check_is_a_pass_with_nothing_to_keep(monkeypatch):
    passed, _, rows, _, _ = await judge_round(monkeypatch, Calls(NO_EVIDENCE_FAIL, RuntimeError("down")))
    assert passed
    assert any("the check did not run: nothing to keep, PASS" in row for row in rows)


@pytest.mark.asyncio
async def test_a_fail_without_evidence_and_no_check_is_a_pass_as_before(monkeypatch):
    # The old rule found nothing to block on in this reply; with no check, the new one has nothing to keep.
    assert _review._evidenced_findings(NO_EVIDENCE_FAIL)[0] == []
    passed, _, _, _, blocking = await judge_round(monkeypatch, Calls(NO_EVIDENCE_FAIL, RuntimeError("down")))
    assert passed and blocking == [0]


@pytest.mark.asyncio
async def test_an_evidenced_finding_under_verdict_pass_fails(monkeypatch):
    calls = Calls(f"FINDING: crash\nEvidence: {EV}\nBLOCKING: 0\nVERDICT: PASS", "CHECK 1: REPRODUCED -- yes")
    passed, _, _, _, blocking = await judge_round(monkeypatch, calls)
    assert not passed and blocking == [1]


@pytest.mark.asyncio
async def test_an_advisory_finding_goes_to_nonblocking_and_does_not_fail(monkeypatch):
    reply = f"FINDING (minor): typo\nEvidence: {EV}\nBLOCKING: 0\nVERDICT: PASS"
    calls = Calls(reply)
    passed, _, _, nonblocking, _ = await judge_round(monkeypatch, calls)
    assert passed and calls.check_calls == []
    assert any("typo" in item for item in nonblocking)


@pytest.mark.asyncio
async def test_a_finding_listed_under_non_blocking_is_carried_once(monkeypatch):
    reply = f"NON-BLOCKING:\n- FINDING: slow startup\n  Evidence: {EV}\nBLOCKING: 0\nVERDICT: PASS"
    calls = Calls(reply)
    passed, _, _, nonblocking, _ = await judge_round(monkeypatch, calls)
    assert passed and calls.check_calls == []
    assert sum("slow startup" in item for item in nonblocking) == 1


@pytest.mark.asyncio
async def test_an_advisory_finding_on_a_fail_is_still_carried(monkeypatch):
    reply = (
        f"FINDING: crash\nEvidence: {EV}\n\n"
        "NON-BLOCKING:\n- FINDING: slow startup\nBLOCKING: 1\nVERDICT: FAIL"
    )
    calls = Calls(reply, "CHECK 1: REPRODUCED -- yes")
    passed, _, _, nonblocking, _ = await judge_round(monkeypatch, calls)
    assert not passed
    assert sum("slow startup" in item for item in nonblocking) == 1


@pytest.mark.asyncio
async def test_a_check_that_cannot_run_on_a_bare_fail_says_so(monkeypatch):
    calls = Calls(NO_EVIDENCE_FAIL, RuntimeError("down"))
    _, _, rows, _, _ = await judge_round(monkeypatch, calls)
    row = next(row for row in rows if "finding check could not run" in row)
    assert "keeping the findings" not in row
    assert "FAIL without an evidenced finding and the check did not run: nothing to keep, PASS" in row


@pytest.mark.asyncio
async def test_a_check_that_cannot_run_on_findings_keeps_them_and_says_so(monkeypatch):
    _, _, rows, _, _ = await judge_round(monkeypatch, Calls(FAIL, RuntimeError("down")))
    assert any("could not run" in row and "keeping the findings" in row for row in rows)


@pytest.mark.asyncio
async def test_an_unevidenced_finding_on_a_pass_writes_the_downgrade_row(monkeypatch):
    reply = "FINDING: crash\nEvidence: it looked wrong\nBLOCKING: 0\nVERDICT: PASS"
    calls = Calls(reply)
    passed, _, rows, nonblocking, _ = await judge_round(monkeypatch, calls)
    assert passed and calls.check_calls == []
    assert any("1 findings downgraded to advisory (no evidence)" in row for row in rows)
    assert any("advisory (no evidence)" in item and "crash" in item for item in nonblocking)


@pytest.mark.asyncio
async def test_a_pass_carries_its_unverified_lines(monkeypatch):
    reply = "UNVERIFIED FROM HERE:\n- the cluster run\nBLOCKING: 0\nVERDICT: PASS"
    monkeypatch.setattr(_reviewer, "_call_reviewer", Calls(reply))
    unverified: list[str] = []
    name, passed, _ = await _review._one_judge(
        "correctness",
        "inspect",
        cast("Dir[Any]", object()),
        "brief",
        "",
        1,
        _timeline._Timeline(),
        nonblocking=[],
        blocking=[],
        new_reqs=[],
        unverified=unverified,
    )
    assert passed and any("the cluster run" in item for item in unverified)


@pytest.mark.asyncio
async def test_an_advisory_finding_with_a_short_title_is_still_carried(monkeypatch):
    reply = (
        "FINDING (minor): slow\nEvidence: command: time app | output: 9s\n"
        "NON-BLOCKING:\n- the CI job is slow to start, unrelated\nVERDICT: PASS"
    )
    passed, _, _, nonblocking, _ = await judge_round(monkeypatch, Calls(reply))
    assert passed
    assert any("the CI job is slow to start" in item for item in nonblocking)
    assert any(item.startswith("correctness advisory: FINDING: slow") for item in nonblocking)


@pytest.mark.asyncio
async def test_a_header_less_evidenced_fail_is_checked_and_labelled(monkeypatch):
    reply = "**1. Crash on empty input**\nEvidence: command: app --empty | output: Traceback\nVERDICT: FAIL"
    timeline = _timeline._Timeline()
    calls = Calls(reply, "CHECK 1: REPRODUCED -- saw it")
    passed, _, _, _, blocking = await judge_round(monkeypatch, calls, timeline=timeline)
    assert not passed and blocking == [1] and len(calls.check_calls) == 1
    (label,) = timeline.metric_labels
    assert label["evidence_kind"] == "command" and label["title"] == "1. Crash on empty input"


@pytest.mark.asyncio
async def test_a_reproduced_claim_is_labelled_as_a_claim(monkeypatch):
    timeline = _timeline._Timeline()
    calls = Calls(NO_EVIDENCE_FAIL, "CLAIM 1: poor handling\nCHECK 1: REPRODUCED -- crashed")
    passed, _, _, _, _ = await judge_round(monkeypatch, calls, timeline=timeline)
    assert not passed
    (label,) = timeline.metric_labels
    assert label["evidence_kind"] == "claim" and label["title"].startswith("poor handling")


@pytest.mark.asyncio
async def test_two_identical_findings_with_one_dropped_write_one_metric_row(monkeypatch):
    block = "FINDING: crash\nEvidence: file: src/a.py:3 -- x\n"
    reply = f"{block}\n{block}\nBLOCKING: 2\nVERDICT: FAIL"
    calls = Calls(reply, "CHECK 1: NOT REPRODUCED\nCHECK 2: REPRODUCED")
    timeline = _timeline._Timeline()
    passed, text, rows, _, _ = await judge_round(monkeypatch, calls, timeline=timeline)
    assert not passed
    assert len(timeline.metric_labels) == 1
    assert text.count("FINDING:") == 1 and "BLOCKING: 1" in text
    assert sum("did not reproduce" in row for row in rows) == 1
