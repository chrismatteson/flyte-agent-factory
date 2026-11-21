"""A `rate_limit_event` reaches the trailer, store and refusal path: a sub-agent's and the author's."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _meter, _session_config, _subagent_run, _usage
from flyte_agent_factory._stream_process import StreamClaudeProcess

STARTED = time.time()
FIVE_RESET = STARTED + 3600
WEEK_RESET = STARTED + 3 * 86400
RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.2, "resetsAt": FIVE_RESET},
            "seven_day": {"utilization": 0.4, "resetsAt": WEEK_RESET},
        },
    },
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "VERDICT: PASS",
    "num_turns": 3,
    "total_cost_usd": 0.5,
    "usage": {"input_tokens": 10, "output_tokens": 20},
    "modelUsage": {"claude-opus": {}},
}
ASSISTANT = {"type": "assistant", "message": {"content": []}}


def ndjson(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def trailer_of(stdout: str) -> dict[str, Any]:
    _, rec = _usage._split_usage("answer" + _usage._usage_trailer(stdout))
    return rec


def test_a_rate_limit_event_in_the_stream_reaches_the_trailer():
    rec = trailer_of(ndjson({"type": "system"}, RATE_LIMIT_EVENT, ASSISTANT, RESULT))
    assert rec["rate_limits"]["source"] == "cli-event"
    assert (rec["rate_limits"]["weekly_pct"], rec["rate_limits"]["five_hour_pct"]) == (40.0, 20.0)
    assert (rec["output_tokens"], rec["cost_usd"], rec["turns"]) == (20, 0.5, 3)


def test_a_verbose_array_reaches_the_trailer_too():
    rec = trailer_of(json.dumps([RATE_LIMIT_EVENT, RESULT]))
    assert rec["rate_limits"]["source"] == "cli-event" and rec["cost_usd"] == 0.5


def test_a_single_json_object_carries_no_rate_limits():
    rec = trailer_of(json.dumps(RESULT))
    assert "rate_limits" not in rec and rec["cost_usd"] == 0.5


def test_unparsable_lines_are_skipped_and_garbage_gives_nothing():
    events, result = _usage.parse_cli_output("oops\n" + ndjson(RATE_LIMIT_EVENT, RESULT) + "{broken")
    assert events == [RATE_LIMIT_EVENT, RESULT] and result == RESULT
    assert _usage.parse_cli_output("not json") == ([], {})
    assert _usage.parse_cli_output("") == ([], {})
    assert _usage.parse_cli_output("[1, null]") == ([], {})


def test_an_event_reading_reaches_the_supervisor_store(tmp_path, monkeypatch):
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_claude_account", lambda: "claude work#abc")
    text = "answer" + _usage._usage_trailer(ndjson(RATE_LIMIT_EVENT, RESULT))
    assert _usage._ingest_subagent_usage(text, "qa", 1, None, started=STARTED) == "answer"
    reading = store.readings["claude work#abc"]
    assert (reading.source, reading.five_hour_pct, reading.weekly_pct) == ("cli-event", 20.0, 40.0)
    assert (reading.five_hour_resets_at, reading.weekly_resets_at) == (FIVE_RESET, WEEK_RESET)
    assert reading.observed_at >= STARTED


def test_a_rejected_event_in_the_stream_is_a_usage_limit(monkeypatch):
    monkeypatch.setattr(_session_config, "_claude_account", lambda: "claude work#abc")
    rejected = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "resetsAt": FIVE_RESET, "rateLimitType": "five_hour"},
    }
    refused = {"type": "result", "is_error": True, "result": "You've hit your limit"}
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        _subagent_run._raise_on_cli_error(ndjson(rejected, refused))


def test_the_final_text_comes_from_the_result_line():
    assert _subagent_run._final_text(ndjson(ASSISTANT, RESULT)) == "VERDICT: PASS"
    assert _subagent_run._final_text("plain words") == "plain words"


def _author_session() -> StreamClaudeProcess:
    """The structured author session as `_author._start_session` wires it, with no process behind it."""
    return StreamClaudeProcess(
        ["claude"],
        cwd=Path("."),
        env={},
        on_rate_limit=lambda event: _usage.ingest_author_rate_limit(event, None),
    )


async def test_the_author_sessions_rate_limit_event_reaches_the_store(tmp_path, monkeypatch):
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_claude_account", lambda: "claude work#abc")
    proc = _author_session()
    await proc.handle(RATE_LIMIT_EVENT)
    reading = store.readings["claude work#abc"]
    assert (reading.source, reading.five_hour_pct, reading.weekly_pct) == ("cli-event", 20.0, 40.0)
    assert (reading.five_hour_resets_at, reading.weekly_resets_at) == (FIVE_RESET, WEEK_RESET)
    assert proc.limit_hit_at is None  # allowed: the session itself is not limited


async def test_the_author_sessions_refusal_is_its_limit_and_the_meters(tmp_path, monkeypatch):
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_claude_account", lambda: "claude work#abc")
    reset = int(WEEK_RESET)
    proc = _author_session()
    info = {"status": "rejected", "rateLimitType": "seven_day", "utilization": 1.0, "resetsAt": reset * 1000}
    await proc.handle({"type": "rate_limit_event", "rate_limit_info": info})
    # One reading of `resetsAt` (milliseconds here) for the session's refusal and for the meter.
    assert proc.limit_reset_at == reset == _usage.session_reset_at(proc)
    reading = store.readings["claude work#abc"]
    assert (reading.weekly_pct, reading.weekly_resets_at, reading.limited_until) == (100.0, reset, reset)
    assert reading.five_hour_pct is None


async def test_an_author_event_with_no_store_or_no_window_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(_usage, "_meters", None)
    await _author_session().handle(RATE_LIMIT_EVENT)  # outside `project`: nothing to record into
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    proc = _author_session()
    for info in ({"status": "allowed", "rateLimitType": "opus", "utilization": 0.5}, {}):
        await proc.handle({"type": "rate_limit_event", "rate_limit_info": info})
    # Malformed: the session's reader drops the event; the meter's own ingest must not raise on it either.
    _usage.ingest_author_rate_limit({"type": "rate_limit_event", "rate_limit_info": "junk"}, None)
    assert store.readings == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1794994800, 1794994800.0),
        (1794994800_000, 1794994800.0),  # milliseconds
        ("2026-11-18T09:40:00Z", datetime(2026, 11, 18, 9, 40, tzinfo=timezone.utc).timestamp()),
        (True, None),
        (-5, None),
        (float("nan"), None),
        ("soon", None),
        (None, None),
    ],
)
def test_one_reading_of_resets_at(value, expected):
    assert _meter.parse_event_reset(value) == expected


def test_a_redraw_is_judged_on_the_structured_reset_or_the_bare_text(monkeypatch):
    past, ahead = time.time() - 60, time.time() + 3600

    class Structured:
        limit_text = "resets 2026-11-18 09:40 UTC (seven_day)"

        def __init__(self, at: float) -> None:
            self.limit_reset_at = at

    assert _usage._redraw_passed(Structured(past)) and not _usage._redraw_passed(Structured(ahead))

    class Tui:
        limit_kind, limit_text = "weekly", "resets 3pm"

    seen: list[str] = []

    def reset_at(text: str, observed_at: float | None = None) -> float:
        seen.append(text)
        return past

    monkeypatch.setattr(_usage, "_reset_at", reset_at)
    assert _usage._redraw_passed(Tui())
    assert seen == ["resets 3pm"]  # no kind: a weekly reading would move a passed clock time to tomorrow
    assert _usage.session_reset_at(Tui()) == past and seen[-1] == "weekly limit · resets 3pm"
