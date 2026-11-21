"""`_meter.from_claude_events` and stream-json `rate_limit_event`s."""

from __future__ import annotations

from typing import Any

import pytest

from flyte_agent_factory import _meter

NOW = 1_795_000_000.0
FIVE_RESET = NOW + 3600
WEEK_RESET = NOW + 3 * 86400


def event(info: dict[str, Any] | None) -> dict[str, Any]:
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"}


def unified(
    five: dict[str, Any] | None = None, week: dict[str, Any] | None = None, **top: Any
) -> dict[str, Any]:
    windows = {k: v for k, v in (("five_hour", five), ("seven_day", week)) if v is not None}
    return event({"status": "allowed", "unifiedWindows": windows, **top})


def test_both_windows_are_read_as_percentages_with_their_resets():
    meter = _meter.from_claude_events(
        [
            unified(
                {"utilization": 0.12, "resetsAt": FIVE_RESET, "status": "allowed"},
                {"utilization": 0.3, "resetsAt": WEEK_RESET, "status": "allowed_warning"},
            )
        ],
        "claude work#abc",
        NOW,
    )
    assert meter is not None
    assert (meter.account, meter.source, meter.observed_at) == ("claude work#abc", "cli-event", NOW)
    assert meter.five_hour_pct == 12.0 and meter.five_hour_resets_at == FIVE_RESET
    assert meter.weekly_pct == 30.0 and meter.weekly_resets_at == WEEK_RESET
    assert meter.limited_until is None and not meter.rejected


def test_without_unified_windows_the_top_level_type_fills_its_window():
    meter = _meter.from_claude_events(
        [
            event(
                {
                    "status": "allowed",
                    "rateLimitType": "seven_day",
                    "utilization": 0.55,
                    "resetsAt": WEEK_RESET,
                }
            )
        ],
        now=NOW,
    )
    assert meter is not None
    assert meter.weekly_pct == pytest.approx(55.0) and meter.weekly_resets_at == WEEK_RESET
    assert meter.five_hour_pct is None and meter.five_hour_resets_at is None


def test_an_iso_reset_is_read_too():
    meter = _meter.from_claude_events(
        [event({"rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": "2026-11-18T12:06:40Z"})],
        now=NOW,
    )
    assert meter is not None and meter.five_hour_resets_at == NOW + 3600


def test_a_rejected_window_is_a_refusal_until_its_reset():
    meter = _meter.from_claude_events(
        [
            unified(
                {"utilization": 0.4, "resetsAt": FIVE_RESET, "status": "allowed"},
                {"utilization": 1.0, "resetsAt": WEEK_RESET, "status": "rejected"},
                status="rejected",
            )
        ],
        now=NOW,
    )
    assert meter is not None
    assert meter.limited_until == WEEK_RESET and meter.limit_noted_at == NOW
    assert meter.weekly_pct == 100.0


def test_a_top_level_rejection_without_a_reset_backs_off_boundedly():
    meter = _meter.from_claude_events([event({"status": "rejected", "rateLimitType": "five_hour"})], now=NOW)
    assert meter is not None
    assert meter.limited_until == NOW + 300 and meter.five_hour_pct is None


def test_a_later_event_keeps_what_an_earlier_one_knew():
    meter = _meter.from_claude_events(
        [
            event({"rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": WEEK_RESET}),
            event({"rateLimitType": "five_hour", "utilization": 0.1, "resetsAt": FIVE_RESET}),
        ],
        now=NOW,
    )
    assert meter is not None
    assert (meter.five_hour_pct, meter.weekly_pct) == (10.0, 30.0)


def test_malformed_events_record_nothing_and_never_raise():
    garbage = [
        event({}),
        event(None),
        {"type": "rate_limit_event"},
        {"type": "rate_limit_event", "rate_limit_info": "x"},
        event({"unifiedWindows": "x"}),
        event({"unifiedWindows": {"five_hour": "x", "seven_day": [1]}}),
        event({"rateLimitType": "seven_day", "utilization": "0.3"}),
        event({"rateLimitType": "seven_day", "utilization": float("nan")}),
        event({"rateLimitType": "seven_day", "utilization": -0.1}),
        event({"rateLimitType": "seven_day", "utilization": 11.0}),
        event({"rateLimitType": "seven_day", "utilization": True}),
        event({"rateLimitType": "weird", "utilization": 0.3}),
        event({"rateLimitType": ["seven_day"], "utilization": 0.3}),
        event({"rateLimitType": {"x": 1}, "utilization": 0.3}),
        event({"rateLimitType": "seven_day", "utilization": 10**400}),
        event({"rateLimitType": "seven_day", "resetsAt": 10**400}),
        unified(week={"resetsAt": 10**400}),
        "not an event",
    ]
    assert _meter.from_claude_events(garbage, now=NOW) is None


def test_an_out_of_range_reset_is_dropped_not_kept():
    meter = _meter.from_claude_events(
        [event({"rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": 1e20})], now=NOW
    )
    assert meter is not None
    assert meter.weekly_pct == 30.0 and meter.weekly_resets_at is None


def test_a_rate_limit_event_and_quota_headers_in_one_stream_merge():
    headers = {
        "type": "system",
        "response_headers": {
            "anthropic-ratelimit-unified-5h-utilization": "0.2",
            "anthropic-ratelimit-unified-5h-reset": str(FIVE_RESET),
        },
    }
    meter = _meter.from_claude_events(
        [headers, event({"rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": WEEK_RESET})], now=NOW
    )
    assert meter is not None
    assert (meter.five_hour_pct, meter.weekly_pct, meter.source) == (20.0, 30.0, "cli-event")


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your limit · resets 12:40am (UTC)",
        "You'vehityourlimit·resets12:40am(UTC)",
        "Claude AI usage limit reached|1795000000",
        "ClaudeAIusagelimitreached",
    ],
)
def test_a_claude_refusal_is_recognised_spaced_or_squashed(text):
    assert _meter._CLAUDE_LIMIT.search(text)


@pytest.mark.parametrize("text", ["GitHub API rate limit exceeded", "HTTP 429 Too Many Requests"])
def test_other_rate_limits_are_not_a_claude_refusal(text):
    assert not _meter._CLAUDE_LIMIT.search(text)
