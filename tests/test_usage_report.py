"""`factory.usage`: each Claude credential's observed windows and a labelled estimate, in one chat call."""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import secrets
import string
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from flyte_agent_factory import (
    _dashboard,
    _factory,
    _factory_base,
    _factory_meters,
    _factory_usage,
    _meter,
    _profile,
    _session_config,
    _statusline,
    _timeline,
    _usage,
    _usage_hours,
)

NOW = 1_795_000_000.0  # 2026-11-18T11:06:40Z
WEEK_RESET = 1_795_172_400.0  # 2026-11-20T11:00Z: this week began 2026-11-13T11:00Z
WEEK_START = WEEK_RESET - _factory_usage.WEEK_S
#: A fake setup-token of the real shape. Random per run, so no fixture or golden file can hold it.
TOKEN = "sk-ant-oat01-" + "".join(
    secrets.choice(string.ascii_letters + string.digits + "-_") for _ in range(80)
)
SECRET = _factory_base.PROFILE.secrets.claude_token
FINGERPRINT = _meter.token_fingerprint(TOKEN)
LABEL = f"claude {SECRET}{_meter.FINGERPRINT_SEP}{FINGERPRINT}"


def assert_no_token(text: str) -> None:
    """Neither the token nor any 12-character piece of it."""
    assert TOKEN not in text
    for start in range(len(TOKEN) - 11):
        assert TOKEN[start : start + 12] not in text, f"a piece of the token leaked: offset {start}"


def report(meters=(), usage=None, *, snapshot=True, cost_extra=None) -> str:
    page = "".join(_meter.report_comment(m) for m in meters)
    if snapshot:
        cost = {"meters": [m.to_dict() for m in meters], **(cost_extra or {})}
        if usage is not None:
            cost["claude_usage"] = usage
        page += _dashboard.status_script({"cost": cost})
    return f"<html>{page}</html>"


def hours(at: float, usd: float = 0.0, **tokens: float) -> _usage_hours.Hours:
    found: _usage_hours.Hours = {}
    _usage_hours.add(found, at, tokens, usd)
    return found


def usage_record(account=LABEL, author=None, subagents=None) -> dict[str, Any]:
    return {"account": account, "author_by_hour": author or {}, "subagents_by_hour": subagents or {}}


def run(name: str, project: str = "") -> SimpleNamespace:
    labels = {_factory_base.LABEL_PROJECT: project} if project else {}
    return SimpleNamespace(name=name, pb2=SimpleNamespace(labels=labels))


@pytest.fixture
def factory(monkeypatch):
    """The factory pod: the work token mounted, slot runs and benchmark runs listed from `world`."""
    monkeypatch.setenv(_factory_meters.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(_factory_meters, "_token_fingerprint", None)
    world = SimpleNamespace(slot_runs={}, bench_runs=[], pages={}, listings=[])

    def listall(**kw):
        world.listings.append(kw)
        found = world.slot_runs.get(kw.get("task_name"), [])
        if isinstance(found, BaseException):
            raise found
        return found

    def list_runs(labels, /, *, keys=(), **kw):
        world.listings.append({"keys": list(keys), **kw})
        if isinstance(world.bench_runs, BaseException):
            raise world.bench_runs
        return world.bench_runs

    def report_html(r):
        page = world.pages[r.name]
        if isinstance(page, BaseException):
            raise page
        return page

    monkeypatch.setattr("flyte.remote.Run.listall", listall)
    monkeypatch.setattr(_factory_base, "list_runs", list_runs)
    monkeypatch.setattr(_factory_base, "report_html", report_html)
    # `factory.usage` reads the clock itself; these fixtures are dated.
    monkeypatch.setattr(_factory_usage, "time", SimpleNamespace(time=lambda: NOW))
    return world


def add_run(world, name, page, project="", slot=None):
    slot = slot or _factory_base.SLOTS[0]
    world.slot_runs.setdefault(slot, []).append(run(name, project))
    world.pages[name] = page


def only(result):
    (entry,) = result["credentials"]
    return entry


# ---- observed ------------------------------------------------------------------------------------------


def test_the_newest_reading_of_the_credential_is_observed(factory):
    older = _meter.Meter(LABEL, "statusline", NOW - 900, 10.0, NOW + 3600, 28.0, WEEK_RESET)
    newer = _meter.Meter(LABEL, "statusline", NOW - 60, 12.0, NOW + 3600, 30.0, WEEK_RESET)
    failed = _meter.Meter(LABEL, "refresh-failed", NOW - 10, error="auth-or-meter-unavailable")
    unsupported = _meter.Meter(LABEL, _factory_usage.PROVIDER_USAGE, NOW - 5)
    other = _meter.Meter(
        "claude someone_else#ffffffffffff", "statusline", NOW, 99.0, NOW + 60, 99.0, WEEK_RESET
    )
    add_run(factory, "r1", report([older]), "alpha")
    add_run(factory, "r2", report([newer, failed, unsupported, other]), "beta", _factory_base.SLOTS[1])
    entry = only(_factory_usage.build_usage_report(NOW))
    assert entry["account"] == LABEL
    seen = entry["observed"]
    assert seen["five_hour_pct"] == 12.0 and seen["weekly_pct"] == 30.0
    assert seen["weekly_resets_at"] == WEEK_RESET and seen["weekly_resets_at_iso"] == "2026-11-20T11:00:00Z"
    assert seen["five_hour_resets_at"] == NOW + 3600
    assert seen["observed_at"] == NOW - 60 and seen["observed_at_iso"] == "2026-11-18T11:05:40Z"
    assert seen["age_s"] == 60 and seen["source"] == "statusline"
    assert seen["five_hour_expired"] is False and seen["weekly_expired"] is False
    assert entry["provider_usage"] == "unsupported-for-setup-token"


def test_a_cli_event_reading_counts_like_any_other(factory):
    event = _meter.Meter(LABEL, "cli-event", NOW - 30, None, None, 41.0, WEEK_RESET)
    add_run(factory, "r1", report([event]), "alpha")
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert (seen["source"], seen["weekly_pct"], seen["five_hour_pct"]) == ("cli-event", 41.0, None)


def test_without_a_reading_the_entry_says_there_is_no_provider_endpoint(factory):
    add_run(
        factory, "r1", report([_meter.Meter(LABEL, "refresh-failed", NOW, error="auth-or-meter-unavailable")])
    )
    entry = only(_factory_usage.build_usage_report(NOW))
    assert entry["observed"] is None
    assert entry["account"] == LABEL
    assert entry["provider_usage"] == "unsupported-for-setup-token"
    assert "no usage endpoint for setup-tokens" in entry["provider_usage_note"]
    assert "refresh-failed" not in json.dumps(entry) and "auth-or-meter-unavailable" not in json.dumps(entry)
    assert entry["estimate"]["label"] == "estimate"
    assert entry["estimate"]["window_start"] == NOW - _factory_usage.WEEK_S
    assert entry["estimate"]["window"].startswith("last 7 days")


def test_a_window_whose_reset_passed_is_expired_not_current(factory):
    stale = _meter.Meter(LABEL, "statusline", NOW - 6 * 3600, 90.0, NOW - 3600, 70.0, NOW - 60)
    add_run(factory, "r1", report([stale]))
    entry = only(_factory_usage.build_usage_report(NOW))
    seen = entry["observed"]
    assert seen["five_hour_pct"] is None and seen["five_hour_expired"] is True
    assert seen["weekly_pct"] is None and seen["weekly_expired"] is True
    assert seen["weekly_resets_at"] == NOW - 60 and seen["age_s"] == 6 * 3600
    # A passed weekly reset says nothing about when this week began.
    assert entry["estimate"]["window_start"] == NOW - _factory_usage.WEEK_S


def test_a_newer_reading_of_one_window_does_not_hide_the_other(factory):
    week = _meter.Meter(LABEL, "statusline", NOW - 60, None, None, 30.0, WEEK_RESET)
    five = _meter.Meter(LABEL, "cli-event", NOW - 5, 50.0, NOW + 3600, None, None)
    add_run(factory, "r1", report([week]), "alpha")
    add_run(factory, "r2", report([five]), "beta", _factory_base.SLOTS[1])
    entry = only(_factory_usage.build_usage_report(NOW))
    seen = entry["observed"]
    assert (seen["weekly_pct"], seen["weekly_source"], seen["weekly_observed_at"]) == (
        30.0,
        "statusline",
        NOW - 60,
    )
    assert (seen["five_hour_pct"], seen["five_hour_source"]) == (50.0, "cli-event")
    assert (seen["observed_at"], seen["source"]) == (NOW - 5, "cli-event")
    assert entry["estimate"]["window_start"] == WEEK_START


def store_readings(tmp_path, *readings: _meter.Meter) -> _meter.Meter:
    """Return what one supervisor's store holds after recording `readings` in order."""
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    for reading in readings:
        store.update(reading)
    return store.readings[LABEL]


def test_one_stores_carried_week_keeps_the_time_and_source_it_was_measured_at(factory, tmp_path):
    # The readings of the test above, recorded by one supervisor's store, which is how a run report gets them.
    held = store_readings(
        tmp_path,
        _meter.Meter(LABEL, "statusline", NOW - 60, None, None, 30.0, WEEK_RESET),
        _meter.Meter(LABEL, "cli-event", NOW - 5, 50.0, NOW + 3600, None, None),
    )
    add_run(factory, "r1", report([held]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert (seen["weekly_pct"], seen["weekly_source"], seen["weekly_observed_at"]) == (
        30.0,
        "statusline",
        NOW - 60,
    )
    assert (seen["five_hour_pct"], seen["five_hour_source"], seen["five_hour_observed_at"]) == (
        50.0,
        "cli-event",
        NOW - 5,
    )
    assert (seen["observed_at"], seen["source"]) == (NOW - 5, "cli-event")


def test_a_refusal_does_not_redate_the_week_it_carries(factory, tmp_path):
    two_days = 2 * 86400
    held = store_readings(
        tmp_path,
        _meter.Meter(LABEL, "statusline", NOW - two_days, 10.0, NOW - two_days + 3600, 40.0, WEEK_RESET),
        _meter.Meter(LABEL, "cli-headers", NOW - 1, limited_until=WEEK_RESET, limit_noted_at=NOW - 1),
    )
    assert held.find_window_origin("weekly") == (NOW - two_days, "statusline")
    add_run(factory, "r1", report([held]))
    entry = only(_factory_usage.build_usage_report(NOW))
    seen = entry["observed"]
    assert (seen["weekly_pct"], seen["weekly_left_pct"]) == (40.0, 60.0)
    assert (seen["weekly_source"], seen["weekly_observed_at"]) == ("statusline", NOW - two_days)
    # Nothing newer was measured: the reading's age is the week's, not the refusal's.
    assert (seen["source"], seen["age_s"]) == ("statusline", two_days)
    assert seen["five_hour_pct"] is None and seen["limited_until"] == WEEK_RESET


def test_a_carried_window_reaches_read_login_meter_with_its_own_date(tmp_path):
    held = store_readings(
        tmp_path,
        _meter.Meter(LABEL, "statusline", NOW - 600, None, None, 40.0, WEEK_RESET),
        _meter.Meter(LABEL, "limit-text", NOW - 1, limited_until=WEEK_RESET, limit_noted_at=NOW - 1),
    )
    echoed = _meter.Meter.from_dict(json.loads(json.dumps(held.to_dict())))
    assert echoed.measured and echoed.find_measured_pct("weekly") == 40.0
    assert echoed.find_window_origin("weekly") == (NOW - 600, "statusline")
    # A limit-text refusal measures nothing of its own.
    assert not _meter.Meter(LABEL, "limit-text", NOW, weekly_pct=100.0).measured


def test_a_reading_with_nothing_carried_keeps_the_meter_shape():
    keys = set(_meter.Meter(LABEL, "statusline", NOW, 1.0, NOW + 60, 2.0, WEEK_RESET).to_dict())
    assert not {k for k in keys if k.endswith(("_observed_at", "_source"))}


def test_a_current_window_is_preferred_to_a_newer_expired_one(factory):
    current = _meter.Meter(LABEL, "statusline", NOW - 600, None, None, 30.0, WEEK_RESET)
    passed = _meter.Meter(LABEL, "limit-text", NOW - 5, None, None, 100.0, NOW - 1)
    add_run(factory, "r1", report([current, passed]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert (seen["weekly_pct"], seen["weekly_expired"]) == (30.0, False)


def test_the_share_left_is_reported_beside_the_share_used(factory):
    add_run(factory, "r1", report([_meter.Meter(LABEL, "statusline", NOW, 12.0, NOW + 60, 30.0, WEEK_RESET)]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert (seen["weekly_pct"], seen["weekly_left_pct"]) == (30.0, 70.0)
    assert (seen["five_hour_pct"], seen["five_hour_left_pct"]) == (12.0, 88.0)


def test_a_refusal_in_force_is_named_even_with_no_percentage(factory):
    refused = _meter.Meter(LABEL, "limit-text", NOW - 30, limited_until=WEEK_RESET)
    add_run(factory, "r1", report([refused]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert seen is not None
    assert (seen["limited_until"], seen["limited_until_iso"]) == (WEEK_RESET, "2026-11-20T11:00:00Z")
    assert seen["weekly_pct"] is None and seen["login_rejected"] is False


def test_a_reading_at_its_limit_carries_the_refusal(factory):
    full = _meter.Meter(LABEL, "statusline", NOW - 30, None, None, 97.0, WEEK_RESET, limited_until=WEEK_RESET)
    add_run(factory, "r1", report([full]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert (seen["weekly_pct"], seen["limited_until"]) == (97.0, WEEK_RESET)


def test_a_refusal_that_has_passed_is_not_named(factory):
    add_run(factory, "r1", report([_meter.Meter(LABEL, "limit-text", NOW - 7200, limited_until=NOW - 60)]))
    assert only(_factory_usage.build_usage_report(NOW))["observed"] is None


def test_a_rejected_login_is_named(factory):
    reading = _meter.Meter(LABEL, "statusline", NOW - 600, None, None, 30.0, WEEK_RESET)
    rejected = _meter.Meter(LABEL, "cli-event", NOW - 5, error="auth")
    add_run(factory, "r1", report([reading, rejected]))
    seen = only(_factory_usage.build_usage_report(NOW))["observed"]
    assert seen["login_rejected"] is True and seen["weekly_pct"] == 30.0
    assert_no_token(json.dumps(seen))


def test_a_benchmark_cases_reading_is_found_too(factory):
    meter = _meter.Meter(LABEL, "statusline", NOW - 30, 5.0, NOW + 60, 50.0, WEEK_RESET)
    factory.bench_runs = [run("case-1", "bench-case-1")]
    factory.pages["case-1"] = report([meter])
    assert only(_factory_usage.build_usage_report(NOW))["observed"]["weekly_pct"] == 50.0
    bench_listing = next(kw for kw in factory.listings if "keys" in kw)
    assert bench_listing["keys"] == ["factory-bench"]
    assert all(kw["updated_at"].after.timestamp() == NOW - _usage_hours.KEEP_S for kw in factory.listings)


def test_every_claude_credential_has_an_entry(factory, monkeypatch):
    profile = dataclasses.replace(
        _factory_base.PROFILE, bench=_profile.Bench(repo="o/r", repo_key="k", claude_token="bench_token")
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    bench_meter = _meter.Meter(
        "claude bench_token#aaaaaaaaaaaa", "statusline", NOW - 5, None, None, 7.0, WEEK_RESET
    )
    add_run(factory, "r1", report([bench_meter]))
    work, bench = _factory_usage.build_usage_report(NOW)["credentials"]
    assert work["account"] == LABEL and work["observed"] is None
    assert bench["account"] == "claude bench_token#aaaaaaaaaaaa" and bench["observed"]["weekly_pct"] == 7.0
    assert bench["provider_usage"] == "unsupported-for-setup-token"


# ---- estimate ------------------------------------------------------------------------------------------


def test_the_estimate_sums_the_credentials_hours_in_the_observed_week(factory):
    reading = _meter.Meter(LABEL, "statusline", NOW - 60, 12.0, NOW + 3600, 30.0, WEEK_RESET)
    in_week = NOW - 86400
    first_hour = WEEK_START + 1800  # the hour the week starts in counts whole
    before = WEEK_START - 3600
    alpha_1 = usage_record(
        author=hours(in_week, input_tokens=100, output_tokens=10, usd=1.0),
        subagents=hours(in_week, input_tokens=1000, cache_read_input_tokens=5, usd=2.0),
    )
    # The resumed project's next run reports the same hours again, grown: counted once, at the larger count.
    alpha_2 = usage_record(
        author={
            **hours(in_week, input_tokens=150, output_tokens=15, usd=1.5),
            **hours(first_hour, output_tokens=7, usd=0.1),
        },
        subagents=hours(in_week, input_tokens=1000, cache_read_input_tokens=5, usd=2.0),
    )
    beta = usage_record(
        subagents={
            **hours(in_week, cache_creation_input_tokens=40, usd=0.5),
            **hours(before, input_tokens=999_999),
        }
    )
    someone_else = usage_record("claude other#bbbbbbbbbbbb", subagents=hours(in_week, input_tokens=777))
    # Newest first, as a listing may return them: the merge must not depend on the order.
    add_run(factory, "a2", report([], alpha_2), "alpha")
    add_run(factory, "a1", report([reading], alpha_1), "alpha")
    add_run(factory, "b1", report([], beta), "beta")
    add_run(factory, "x1", report([], someone_else), "gamma")
    add_run(factory, "old", report([]), "delta")  # written before the hourly records
    add_run(factory, "wf", report([], snapshot=False))  # no project snapshot at all
    estimate = only(_factory_usage.build_usage_report(NOW))["estimate"]
    assert estimate["label"] == "estimate"
    assert "outside the factory" in estimate["basis"]
    assert estimate["window"] == "observed weekly reset less 7 days"
    assert (estimate["window_start"], estimate["window_end"]) == (WEEK_START, NOW)
    assert estimate["window_start_iso"] == "2026-11-13T11:00:00Z"
    assert estimate["window_end_iso"] == "2026-11-18T11:06:40Z"
    assert estimate["input_tokens"] == 150 + 1000
    assert estimate["output_tokens"] == 15 + 7
    assert estimate["cache_read_input_tokens"] == 5
    assert estimate["cache_creation_input_tokens"] == 40
    assert estimate["usd_author_list_price"] == pytest.approx(1.6)
    assert estimate["usd_subagents_cli_reported"] == pytest.approx(2.5)
    assert estimate["usd"] == pytest.approx(4.1)
    assert estimate["subagent_calls"] == 2 and estimate["author_replies"] == 2
    assert estimate["runs_counted"] == 3
    assert estimate["runs_without_hourly_records"] == 1


def test_a_report_with_bad_hours_contributes_only_what_validates(factory):
    bad = {
        "account": LABEL,
        "author_by_hour": {
            "not-an-hour": {"input_tokens": 5},
            _usage_hours.format_hour_key(NOW): {"input_tokens": "9"},
        },
        "subagents_by_hour": "garbage",
    }
    add_run(factory, "r1", report([], bad), "alpha")
    add_run(factory, "r2", report([], "not a record"), "beta")
    add_run(factory, "r3", "<html>no snapshot, no meters</html>", "gamma")
    estimate = only(_factory_usage.build_usage_report(NOW))["estimate"]
    assert estimate["input_tokens"] == 0 and estimate["usd"] == 0
    assert estimate["runs_counted"] == 1 and estimate["runs_without_hourly_records"] == 1


def test_a_reports_record_end_to_end_from_a_supervisor_timeline(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", SECRET)
    assert _session_config._claude_account() == LABEL
    _usage_hours.record_subagent({"input_tokens": 11, "cost_usd": 0.3}, LABEL, NOW - 7200)
    timeline = _timeline._Timeline()
    timeline.meter(_meter.Meter(LABEL, "statusline", NOW - 60, 12.0, NOW + 3600, 30.0, WEEK_RESET))
    timeline.set_author_usage(
        {"coordinator": {}, "helpers": {}}, {}, hours=hours(NOW - 3600, output_tokens=4, usd=0.2)
    )
    page = report(
        [_meter.Meter(LABEL, "statusline", NOW - 60, 12.0, NOW + 3600, 30.0, WEEK_RESET)], snapshot=False
    )
    add_run(factory, "r1", page + _dashboard.status_script(timeline.snapshot(NOW)), "alpha")
    entry = only(_factory_usage.build_usage_report(NOW))
    assert entry["observed"]["weekly_pct"] == 30.0
    assert entry["estimate"]["input_tokens"] == 11 and entry["estimate"]["output_tokens"] == 4
    assert entry["estimate"]["usd"] == pytest.approx(0.5)


# ---- kind, the five-hour estimate, truncation ----------------------------------------------------------


def test_a_current_measured_reading_makes_the_entry_measured(factory):
    reading = _meter.Meter(LABEL, "statusline", NOW - 60, 12.0, NOW + 3600, 30.0, WEEK_RESET)
    add_run(factory, "r1", report([reading]))
    entry = only(_factory_usage.build_usage_report(NOW))
    assert (entry["kind"], entry["measured"]) == ("measured", True)


def test_a_stale_reading_is_not_measured_and_spend_makes_it_an_estimate(factory):
    stale = _meter.Meter(LABEL, "statusline", NOW - 6 * 3600, 90.0, NOW - 3600, 70.0, NOW - 60)
    add_run(factory, "r1", report([stale]))
    entry = only(_factory_usage.build_usage_report(NOW))
    assert (entry["kind"], entry["measured"]) == ("unknown", False)
    assert "no usage endpoint for setup-tokens" in entry["provider_usage_note"]
    spend = usage_record(subagents=hours(NOW - 3600, usd=0.5, input_tokens=10))
    add_run(factory, "r2", report([], spend), "alpha")
    entry = only(_factory_usage.build_usage_report(NOW))
    assert (entry["kind"], entry["measured"]) == ("estimate", False)
    assert "no usage endpoint for setup-tokens" in entry["provider_usage_note"]


def test_a_limit_text_meter_is_not_a_measurement(factory):
    refusal = _meter.Meter(
        LABEL, "limit-text", NOW - 90, limited_until=NOW + 600, weekly_resets_at=WEEK_RESET
    )
    assert not refusal.measured
    assert not _meter.Meter(LABEL, "statusline", NOW).measured  # a measured source with no percent
    assert _meter.Meter(LABEL, "statusline", NOW, weekly_pct=1.0).measured
    add_run(factory, "r1", report([refusal]), "alpha")
    entry = only(_factory_usage.build_usage_report(NOW))
    assert (entry["kind"], entry["measured"]) == ("unknown", False)
    assert entry["observed"]["limited_until"] == NOW + 600  # the refusal is still named
    assert entry["observed"]["weekly_pct"] is None


def test_the_estimate_sums_the_last_five_hours_too(factory):
    recent = usage_record(
        author=hours(NOW - 3600, input_tokens=5, usd=0.25),
        subagents={
            **hours(NOW - 2 * 3600, output_tokens=7, usd=1.0),
            **hours(NOW - 86400, input_tokens=1000, usd=3.0),
        },
    )
    add_run(factory, "r1", report([], recent), "alpha")
    estimate = only(_factory_usage.build_usage_report(NOW))["estimate"]
    five = estimate["five_hour"]
    assert (five["input_tokens"], five["output_tokens"], five["usd"]) == (5, 7, pytest.approx(1.25))
    assert five["window_start"] == NOW - 5 * 3600
    assert estimate["input_tokens"] == 1005 and estimate["usd"] == pytest.approx(4.25)
    assert "five hours" in estimate["basis"] and "weekly" in estimate["basis"]
    assert estimate["lower_bound"] is False and "lower bound" not in estimate["basis"]


def test_a_listing_that_fills_its_bound_or_fails_makes_the_estimate_a_lower_bound(factory, monkeypatch):
    monkeypatch.setattr(_factory_usage, "_LIST_LIMIT", 2)
    slot = _factory_base.SLOTS[0]
    for name in ("r1", "r2"):
        add_run(
            factory, name, report([], usage_record(subagents=hours(NOW - 60, input_tokens=1))), name, slot
        )
    result = _factory_usage.build_usage_report(NOW)
    assert result["errors"] == [f"listing {slot}: truncated at 2"]
    estimate = only(result)["estimate"]
    assert estimate["lower_bound"] is True and "lower bound" in estimate["basis"]
    assert estimate["input_tokens"] == 2
    # A failed listing is the same.
    factory.slot_runs.clear()
    factory.slot_runs[_factory_base.SLOTS[1]] = RuntimeError("down")
    monkeypatch.setattr(_factory_usage, "_LIST_LIMIT", 100)
    estimate = only(_factory_usage.build_usage_report(NOW))["estimate"]
    assert estimate["lower_bound"] is True and "lower bound" in estimate["basis"]


# ---- the task, errors, secrets, no network -------------------------------------------------------------


async def test_the_chat_task_returns_the_report_as_json(factory):
    add_run(
        factory, "r1", report([_meter.Meter(LABEL, "statusline", NOW - 60, None, None, 30.0, WEEK_RESET)])
    )
    result = json.loads(await _factory.usage.func())
    assert set(result) == {"credentials", "generated_at", "generated_at_iso", "errors"}
    (entry,) = result["credentials"]
    assert set(entry) == {
        "account",
        "kind",
        "measured",
        "observed",
        "provider_usage",
        "provider_usage_note",
        "estimate",
    }
    assert entry["observed"]["weekly_pct"] == 30.0


def test_listing_and_report_failures_are_named_by_class_only(factory):
    factory.slot_runs[_factory_base.SLOTS[1]] = RuntimeError(f"listing failed for {TOKEN}")
    factory.bench_runs = ConnectionRefusedError(f"https://x/?sig={TOKEN}")
    add_run(factory, "r1", OSError(f"report {TOKEN}"), "alpha")
    result = _factory_usage.build_usage_report(NOW)
    assert result["errors"] == [
        f"listing {_factory_base.SLOTS[1]}: RuntimeError",
        "listing benchmark runs: ConnectionRefusedError",
        "report of r1: OSError",
    ]
    assert only(result)["observed"] is None
    assert_no_token(json.dumps(result))


async def test_the_probe_answers_honestly_for_a_setup_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    out = await _factory.read_login_meter.func(f"claude {SECRET}")
    meter = _meter.Meter.from_dict(json.loads(out))
    assert meter.source == "unsupported-for-setup-token" and not meter.error and not meter.rejected
    assert meter.five_hour_pct is None and meter.weekly_pct is None
    assert_no_token(out)


async def test_the_probe_still_says_refresh_failed_when_a_supported_read_fails(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setattr(_meter, "SETUP_TOKEN_USAGE_SUPPORTED", True)

    async def broken(*a, **k):
        raise _meter.MeterError(f"usage request failed: HTTP 401 {TOKEN}")

    monkeypatch.setattr(_meter, "read_claude", broken)
    out = await _factory.read_login_meter.func(f"claude {SECRET}")
    assert json.loads(out)["source"] == "refresh-failed"
    assert_no_token(out)


async def test_read_login_meter_returns_the_measured_reading_unchanged(monkeypatch, no_http):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    mine = f"claude {SECRET}"
    measured = _meter.Meter(mine, "statusline", NOW - 500, 30.0, NOW + 60, 8.0, NOW + 600)
    readings = {
        mine: measured,
        "claude other-secret": _meter.Meter("claude other-secret", "statusline", NOW, 99.0, NOW + 60),
        f"{mine}-limit": _meter.Meter(mine, "limit-text", NOW, limited_until=NOW + 60),
    }
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kw: readings)
    out = await _factory.read_login_meter.func(mine)
    assert _meter.Meter.from_dict(json.loads(out)) == measured  # including observed_at
    assert_no_token(out)
    assert no_http == []  # no network call while setup-tokens have no usage endpoint


async def test_read_login_meter_without_a_measured_reading_is_unsupported(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    mine = f"claude {SECRET}"
    only_text = {mine: _meter.Meter(mine, "limit-text", NOW, limited_until=NOW + 60)}
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kw: only_text)
    assert json.loads(await _factory.read_login_meter.func(mine))["source"] == "unsupported-for-setup-token"

    def broken(**kw):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(_factory_meters, "login_meters", broken)
    assert json.loads(await _factory.read_login_meter.func(mine))["source"] == "unsupported-for-setup-token"


def test_an_echoed_reading_is_no_newer_duplicate_and_a_failed_read_is_never_a_measurement(monkeypatch):
    mine = f"claude {SECRET}"
    measured = _meter.Meter(mine, "statusline", NOW - 500, 30.0, NOW + 60)
    echo = _meter.Meter.from_dict(measured.to_dict())
    failed = _meter.Meter(mine, "refresh-failed", NOW, error="auth-or-meter-unavailable")
    unsupported = _meter.Meter(mine, _factory_usage.PROVIDER_USAGE, NOW)
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {})
    for outputs in ([measured, echo], [echo, measured]):
        listed = [SimpleNamespace(outputs=lambda m=m: (json.dumps(m.to_dict()),)) for m in outputs]
        monkeypatch.setattr("flyte.remote.Run.listall", lambda listed=listed, **kwargs: iter(listed))
        found = _factory_meters.login_meters([], now=NOW, raw=True, bench=False)
        assert found[mine].observed_at == NOW - 500 and found[mine].source == "statusline"
    # However new, a failed or unsupported answer is not what `read_login_meter` echoes.
    candidates = [m for m in (failed, unsupported, echo) if m.measured]
    assert _factory_meters.token_meter(SECRET, candidates) == measured


@pytest.fixture
def no_http(monkeypatch):
    """Any HTTP request, sync or async, fails the test: httpx's real transports refuse to send."""
    asked = []

    def refuse(self, request):
        asked.append(str(request.url))
        raise AssertionError(f"unexpected HTTP request to {request.url.host}")

    async def refuse_async(self, request):
        return refuse(self, request)

    monkeypatch.setattr("httpx.HTTPTransport.handle_request", refuse)
    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", refuse_async)
    return asked


async def test_reading_meters_makes_no_http_request(factory, no_http, monkeypatch):
    with pytest.raises(AssertionError):  # the guard works: a login-file meter read is an HTTP request
        await _meter.read_claude(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    no_http.clear()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    add_run(
        factory, "r1", report([_meter.Meter(LABEL, "statusline", NOW - 60, 1.0, NOW + 60, 2.0, WEEK_RESET)])
    )
    json.loads(await _factory.usage.func())
    json.loads(await _factory.read_login_meter.func(f"claude {SECRET}"))
    assert no_http == []


async def test_no_token_reaches_any_output_file_or_log(factory, tmp_path, monkeypatch, capsys, caplog):
    """The token, through the env and the mounted secret, along every new path including the error paths."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    config = tmp_path / "claude"
    config.mkdir()
    monkeypatch.setattr(_session_config, "CONFIG_DIR", config)
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_statusline_seen", 0.0)
    capture = _statusline.locate_capture(config)
    monkeypatch.setattr(_statusline, "time", SimpleNamespace(time=lambda: NOW))
    outputs: list[str] = []

    # The status-line command: a payload carrying the token in fields it must not keep, then broken input.
    payload = {
        "session_id": TOKEN,
        "transcript_path": f"/x/{TOKEN}",
        "rate_limits": {"seven_day": {"used_percentage": 30, "resets_at": WEEK_RESET, "token": TOKEN}},
    }
    for stdin in (json.dumps(payload).encode(), f'{{"rate_limits": "{TOKEN}"'.encode(), TOKEN.encode()):
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
        assert _statusline.main([str(capture)]) == 0
    outputs.append(capture.read_text())

    # Ingestion, of a good capture and of one a session rewrote with the token in it.
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    capture.write_text(json.dumps({"observed_at": NOW + 1, "weekly_pct": TOKEN, "account": TOKEN}))
    await _usage._read_meters(timeline)
    capture.write_text(TOKEN)
    await _usage._read_meters(timeline)
    _usage_hours.record_subagent({"input_tokens": 3, "session_id": TOKEN, "result": TOKEN}, LABEL, NOW)
    page = timeline.render()
    outputs.append(page)

    # The chat call, over that report and over failing listings and reports.
    add_run(factory, "r1", page, "alpha")
    add_run(factory, "r2", "{" + TOKEN, "beta")
    add_run(factory, "r3", OSError(TOKEN), "gamma")
    factory.bench_runs = RuntimeError(TOKEN)
    outputs.append(await _factory.usage.func())
    outputs.append(await _factory.read_login_meter.func(f"claude {SECRET}"))
    result = json.loads(outputs[-2])
    assert result["credentials"][0]["observed"]["weekly_pct"] == 30.0, "the paths ran on real data"
    # ...and the same call when a measured reading of the credential exists to be returned.
    held = _meter.Meter(f"claude {SECRET}", "statusline", NOW - 5, 1.0, NOW + 60, 30.0, WEEK_RESET)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **kw: {held.account: held})
    measured_out = await _factory.read_login_meter.func(f"claude {SECRET}")
    assert json.loads(measured_out)["source"] == "statusline", "the measured path ran"
    outputs.append(measured_out)
    seen = capsys.readouterr()
    outputs += [seen.out, seen.err, caplog.text]
    # The capture last held what the test itself wrote there, as a session could; it was checked above.
    outputs += [p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file() and p != capture]
    for text in outputs:
        assert_no_token(text)


def test_chat_is_steered_to_the_usage_call():
    from pathlib import Path

    from flyte_agent_factory import _factory_mcp

    skill = (Path(__file__).resolve().parents[1] / "skills/agent-factory/SKILL.md").read_text()
    assert "usage" in _factory_mcp.CHAT_TASKS
    for text in (_factory_mcp.INSTRUCTIONS, skill):
        assert "factory.usage" in text and "no usage endpoint for setup-tokens" in text
        assert "estimate" in text


# ---- a reading with no reset ages out with its window ------------------------------------------------


def test_a_reading_without_a_reset_ages_out_after_its_window(factory):
    week_old = _meter.Meter(LABEL, "statusline", NOW - 7 * 86400, five_hour_pct=80.0)
    add_run(factory, "r1", report([week_old]))
    entry = only(_factory_usage.build_usage_report(NOW))
    assert entry["measured"] is False
    assert entry["kind"] in ("estimate", "unknown")
    assert entry["observed"]["five_hour_expired"] is True
    assert entry["observed"]["five_hour_pct"] is None


def test_a_recent_reading_without_a_reset_is_still_measured(factory):
    recent = _meter.Meter(LABEL, "statusline", NOW - 3600, five_hour_pct=80.0)
    add_run(factory, "r1", report([recent]))
    entry = only(_factory_usage.build_usage_report(NOW))
    assert entry["measured"] is True
    assert entry["observed"]["five_hour_expired"] is False
    assert entry["observed"]["five_hour_pct"] == 80.0


def test_status_marks_a_window_without_a_reset_as_expired_after_its_window():
    old = {"observed_at": NOW - 6 * 3600, "five_hour_pct": 80.0, "weekly_pct": 30.0}
    new = {"observed_at": NOW - 60, "five_hour_pct": 80.0}
    old_out, new_out = _factory_usage.expire_windows([old, new], NOW)
    assert old_out["five_hour_expired"] is True and old_out["five_hour_pct"] is None
    assert "weekly_expired" not in old_out and old_out["weekly_pct"] == 30.0
    assert "five_hour_expired" not in new_out and new_out["five_hour_pct"] == 80.0


def test_the_benchmark_label_matches_the_benchmarks():
    from flyte_agent_factory import _bench_flow

    assert _factory_usage._LABEL_BENCH == _bench_flow.LABEL_BENCH
