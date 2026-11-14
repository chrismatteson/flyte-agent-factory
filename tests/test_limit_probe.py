"""A remembered usage limit is a hint: the router probes it after `LIMIT_PROBE_S`; success clears it."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from flyte_agent_factory import (
    _limit_share,
    _meter,
    _policy,
    _reviewer,
    _router,
    _session_config,
    _timeline,
    _usage,
)

T0 = 1_800_000_000.0
H = 3600.0
PROBE = _meter.LIMIT_PROBE_S


def limited(account="claude x", until=T0 + 12 * H, noted_at: float | None = T0, **kw) -> _meter.Meter:
    return _meter.Meter(account, "limit-text", T0, limited_until=until, limit_noted_at=noted_at, **kw)


def route(meters, *, now, **kw):
    kw.setdefault("available", ("claude", "codex"))
    return _router.route("planner", meters, now=now, **kw)


# --- the router ----------------------------------------------------------------------------------------


def test_a_fresh_limit_keeps_its_reset_and_adds_the_probe_time():
    decision = route({"claude": limited()}, now=T0 + 60, available=("claude",))
    assert decision.deferred
    assert decision.retry_at == T0 + 12 * H, "the remembered reset is never replaced by the probe time"
    assert decision.probe_at == T0 + PROBE
    assert "retry at 20:00 UTC; re-check at 08:30 UTC" in decision.row()
    assert _router.pick_wake_time(decision.retry_at, decision.probe_at) == T0 + PROBE


def test_a_limit_that_is_due_a_check_is_taken_as_a_probe():
    decision = route({"claude": limited()}, now=T0 + PROBE, available=("claude",))
    assert (decision.engine, decision.probe) == ("claude", True)
    assert "remembered limit re-checked" in decision.reason


def test_a_probe_ranks_after_an_engine_that_is_available():
    meters = {"claude": limited(), "codex": _meter.Meter("codex", "app-server", T0 + PROBE, 10, T0 + 4 * H)}
    decision = route(meters, now=T0 + PROBE)
    assert (decision.engine, decision.probe) == ("codex", False)


def test_the_preferred_engine_is_probed_even_with_the_other_available():
    meters = {"claude": limited(), "codex": _meter.Meter("codex", "app-server", T0 + PROBE, 10, T0 + 4 * H)}
    decision = route(meters, now=T0 + PROBE, preferred="claude")
    assert (decision.engine, decision.probe) == ("claude", True)


def test_a_rejected_login_and_a_limit_without_a_note_time_are_never_probed():
    for meter in (limited(error="login rejected"), limited(noted_at=None)):
        decision = route({"claude": meter}, now=T0 + 2 * PROBE, available=("claude",))
        assert decision.deferred and decision.retry_at == T0 + 12 * H
        assert decision.probe_at is None and "re-check" not in decision.row()


def test_a_failed_meter_read_does_not_switch_probing_off(tmp_path):
    """`note_failure` writes an error reading that inherits the limit; only a rejected login skips probing."""
    clock = {"t": T0}
    store = _meter.MeterStore(tmp_path, clock=lambda: clock["t"])
    store.note_limit("claude", T0 + 12 * H, "claude x")
    clock["t"] += 60
    store.note_failure("claude x", "TimeoutError")
    reading = store.readings["claude x"]
    assert reading.error == "TimeoutError" and reading.limited_until == T0 + 12 * H
    decision = route({"claude": reading}, now=T0 + PROBE, available=("claude",))
    assert (decision.engine, decision.probe) == ("claude", True)


@pytest.mark.parametrize("later", ["a reading", "a failed read"])
def test_a_rejected_login_stays_unprobed_after_a_later_reading_or_failed_read(tmp_path, later):
    """A sub-agent's late Codex report, or a meter read that failed, must not make a dead login probe-able."""
    clock = {"t": T0}
    store = _meter.MeterStore(tmp_path, clock=lambda: clock["t"])
    store.note_limit("codex", T0 + 6 * H, "codex tok", error="codex login unusable")
    clock["t"] += 60
    if later == "a reading":
        store.update(_meter.Meter("codex tok", "codex-exec", clock["t"], five_hour_pct=10))
    else:
        store.note_failure("codex tok", "TimeoutError")
    reading = store.readings["codex tok"]
    assert reading.limited_until == T0 + 6 * H
    decision = route({"codex": reading}, now=T0 + 2 * PROBE, available=("codex",))
    assert decision.deferred and decision.probe_at is None and not decision.probe
    assert _meter.MeterStore(tmp_path, clock=lambda: clock["t"]).readings["codex tok"].rejected


def test_a_limit_ending_before_the_probe_reopens_at_its_reset():
    decision = route({"claude": limited(until=T0 + 600)}, now=T0 + 60, available=("claude",))
    assert decision.retry_at == T0 + 600
    assert decision.probe_at is None, "a re-check after the reset is no re-check"


def test_a_hard_pin_waits_for_the_reset_and_re_checks_at_the_probe_time():
    meters = {"claude": limited(), "codex": _meter.Meter("codex", "app-server", T0, 10, T0 + 4 * H)}
    decision = route(meters, now=T0 + 60, pin="claude", hard_pin=True)
    assert (decision.deferred, decision.pin) == (True, "claude")
    assert (decision.retry_at, decision.probe_at) == (T0 + 12 * H, T0 + PROBE)
    due = route(meters, now=T0 + PROBE, pin="claude", hard_pin=True)
    assert (due.engine, due.probe) == ("claude", True), "at the probe time the pin's engine takes one call"


def test_a_deferral_re_checks_at_the_earliest_probe_of_the_engines_it_waits_on():
    meters = {"claude": limited(), "codex": limited("codex", until=T0 + 4 * H, noted_at=T0 + 600)}
    decision = route(meters, now=T0 + 700)
    assert (decision.retry_at, decision.probe_at) == (T0 + 4 * H, T0 + PROBE)
    assert decision.record()["probe_at"] == T0 + PROBE
    paused = _router.paused(decision, now=T0 + 700)
    assert (paused.engine, paused.until, paused.probe_at) == ("codex", T0 + 4 * H, T0 + PROBE)
    assert paused.as_dict()["probe_at"] == T0 + PROBE


# --- the store -----------------------------------------------------------------------------------------


def store_at(tmp_path, now):
    return _meter.MeterStore(tmp_path / "meters", clock=lambda: now["t"])


def test_note_limit_stamps_the_probe_clock_and_it_survives_a_restart(tmp_path):
    now = {"t": T0}
    store = store_at(tmp_path, now)
    store.note_limit("claude", T0 + 12 * H, "claude x")
    now["t"] = T0 + 100
    store.note_limit("claude", T0 + 600, "claude x")  # a repeat: never shortened, but re-armed
    meter = store_at(tmp_path, now).readings["claude x"]
    assert (meter.limited_until, meter.limit_noted_at) == (T0 + 12 * H, T0 + 100)


def test_clear_limit_overrides_never_shorten_and_is_not_undone_by_an_older_reading(tmp_path):
    now = {"t": T0}
    store = store_at(tmp_path, now)
    store.note_limit("claude", T0 + 12 * H, "claude x")
    now["t"] = T0 + 500
    assert store.clear_limit("claude", "claude x")
    meter = store.readings["claude x"]
    assert (meter.limited_until, meter.limit_noted_at) == (None, None)
    # A sub-agent's reading, taken before the clear and ingested after it, still carries the limit.
    store.update(limited(account="claude x", noted_at=T0 + 10))
    assert store.readings["claude x"].limited_until is None
    assert store_at(tmp_path, now).readings["claude x"].limited_until is None, (
        "and it stays cleared on reload"
    )
    # A refusal after the clear is a new limit and stands.
    now["t"] = T0 + 900
    store.note_limit("claude", T0 + 5 * H, "claude x")
    assert store.readings["claude x"].limited_until == T0 + 5 * H


def test_a_clear_does_not_drop_a_limit_noted_after_the_call_began(tmp_path):
    now = {"t": T0}
    store = store_at(tmp_path, now)
    store.note_limit("claude", T0 + 12 * H, "claude x")  # noted at T0 + 0 ... then a newer refusal:
    now["t"] = T0 + 50
    store.note_limit("claude", T0 + 12 * H, "claude x")
    assert not store.clear_limit("claude", "claude x", at=T0 + 10), "the call began before the refusal"
    assert store.readings["claude x"].limited_until == T0 + 12 * H


def test_clear_limit_without_an_account_clears_every_account_of_the_engine(tmp_path):
    now = {"t": T0}
    store = store_at(tmp_path, now)
    store.note_limit("claude", T0 + H, "claude a")
    store.note_limit("claude", T0 + H, "claude b")
    store.note_limit("codex", T0 + H)
    now["t"] = T0 + 1
    assert store.clear_limit("claude")
    assert [m.limited_until for m in store.readings.values()] == [None, None, T0 + H]
    assert store.wake_count() == 1


def test_claiming_a_probe_keeps_the_pods_other_routes_from_probing(tmp_path):
    now = {"t": T0}
    store = store_at(tmp_path, now)
    store.note_limit("claude", T0 + 12 * H, "claude x")
    now["t"] = T0 + PROBE
    first = route({"claude": store.readings["claude x"]}, now=now["t"], available=("claude",))
    assert first.probe
    store.claim_probe("claude x")
    second = route({"claude": store.readings["claude x"]}, now=now["t"], available=("claude",))
    assert second.deferred and second.probe_at == now["t"] + PROBE and second.retry_at == T0 + 12 * H


# --- a judge call through the reviewer -----------------------------------------------------------------


@pytest.fixture
def engines(tmp_path, monkeypatch):
    """`_call_reviewer` over a fake sub-agent that answers on any engine, with a fake wall clock."""
    now = {"t": T0}
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: now["t"])
    calls: list[str] = []
    refuse: set[str] = set()

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        if kwargs["engine"] in refuse:
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {kwargs['engine']}: limit, resets 1:50am (UTC)")
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: now["t"]))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "{}")
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.delenv("AGENT_FACTORY_PLANNER_ENGINE", raising=False)
    monkeypatch.setattr(_policy, "resolve_codex", lambda *a, **k: SimpleNamespace(model="m", effort="e"))
    return SimpleNamespace(
        store=store, now=now, calls=calls, refuse=refuse, account=_session_config._claude_account()
    )


async def test_a_remembered_weekly_limit_is_probed_within_one_interval_and_cleared(engines):
    """The incident: a weekly limit remembered 12 h ahead although the owner had reset it."""
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)

    await _reviewer._call_reviewer(purpose="planner")
    assert engines.calls == ["codex"], "inside the interval the remembered limit still routes around Claude"

    engines.now["t"] = T0 + PROBE
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.calls == ["codex", "claude"], "one probe interval later Claude is asked again"
    meter = engines.store.readings[engines.account]
    assert meter.limited_until is None, "and its answer clears the limit"
    assert engines.store.wake_count() == 1


async def test_a_judge_with_every_engine_limited_runs_at_the_probe_and_not_before(engines):
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    engines.store.note_limit("codex", T0 + 12 * H, _usage._limit_account("codex"))

    with pytest.raises(_router.RouterDeferred) as deferred:
        await _reviewer._call_reviewer(purpose="correctness")
    assert (deferred.value.retry_at, deferred.value.probe_at) == (T0 + 12 * H, T0 + PROBE)
    assert "re-check at 08:30 UTC" in str(deferred.value)

    engines.now["t"] = T0 + PROBE
    await _reviewer._call_reviewer(purpose="correctness")
    assert len(engines.calls) == 1, "the first probe is the only call"
    assert engines.store.wake_count() == 1


async def test_a_probe_that_is_refused_re_arms_the_limit(engines):
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    engines.refuse.add("claude")
    engines.now["t"] = T0 + PROBE
    await _reviewer._call_reviewer(purpose="planner")  # refused on Claude, answered on Codex
    assert engines.calls == ["claude", "codex"]
    meter = engines.store.readings[engines.account]
    assert (meter.limited_until, meter.limit_noted_at) == (T0 + 12 * H, T0 + PROBE), "still the weekly reset"
    assert engines.store.wake_count() == 0
    engines.now["t"] = T0 + PROBE + 60
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.calls == ["claude", "codex", "codex"], "not asked again until the next interval"


async def test_concurrent_calls_send_one_probe_per_limited_engine(engines):
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    engines.now["t"] = T0 + PROBE
    decisions = [_usage._route("planner", None) for _ in range(3)]
    assert [(d.engine, d.probe) for d in decisions] == [("claude", True), ("codex", False), ("codex", False)]


async def test_a_cross_check_that_falls_back_to_the_builder_claims_its_probe(engines, monkeypatch):
    """The other engine fails outright and the judge runs on the builder's: that call is the probe too."""
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    engines.store.note_limit("codex", T0 + 12 * H)
    engines.now["t"] = T0 + PROBE
    in_flight = asyncio.Event()

    async def raw(**kwargs):
        engines.calls.append(kwargs["engine"])
        if kwargs["engine"] == "codex":
            raise ValueError("connection reset")
        in_flight.set()
        await asyncio.sleep(0.2)
        return "answer", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    judge = asyncio.create_task(_reviewer._call_reviewer(purpose="correctness"))
    await asyncio.wait_for(in_flight.wait(), 5)
    assert engines.calls == ["codex", "claude"]
    assert _usage._route("planner", None).deferred, "Claude's probe is in flight: no second one"
    await judge


async def _no_read(_timeline):
    return None


@pytest.mark.parametrize("both_limited", [False, True])
async def test_a_delegation_routed_as_the_probe_makes_that_call(engines, monkeypatch, both_limited):
    """`_delegation_engine` claims the probe; the worker's `_call_reviewer` must not defer on that claim."""
    monkeypatch.setattr(_usage, "_read_meters", _no_read)
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    if both_limited:
        engines.store.note_limit("codex", T0 + 12 * H, _usage._limit_account("codex"))
    else:
        monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    engines.now["t"] = T0 + PROBE
    engine = await _usage._delegation_engine(_timeline._Timeline())()
    assert _session_config._routes[-1]["probe"], "the delegation was chosen as the probe"
    await _reviewer._call_reviewer(purpose="delegate", engine=engine)
    assert engines.calls == [engine], "the probed engine is the one called"
    assert engine == "claude"
    assert engines.store.readings[engines.account].limited_until is None, "its answer clears the limit"


@pytest.mark.parametrize(
    "record",
    [
        {"cleared_at": "soon"},
        {"cleared_at": [1]},
        {"cleared_at": float("nan")},
        {"cleared_at": 10**400},
        {"noted_at": 10**400},
    ],
)
async def test_a_malformed_shared_record_does_not_fail_the_probe_call(engines, tmp_path, record):
    uri = str(tmp_path / "state")
    (tmp_path / "state" / "limits").mkdir(parents=True)
    (tmp_path / "state" / "limits" / "engine-claude.json").write_text(json.dumps(record))
    engines.store.share = _limit_share.LimitShare(uri, clock=lambda: engines.now["t"])
    engines.store.note_limit("claude", T0 + 12 * H, engines.account)
    engines.now["t"] = T0 + PROBE
    await _reviewer._call_reviewer(purpose="planner")
    assert engines.calls == ["claude"], "the probe runs whatever the record says"


def test_a_limit_saved_before_probing_existed_is_probed_after_one_interval(tmp_path):
    """Upgrade case: `latest.json` has a weekly limit and no `limit_noted_at`; noted at its reading."""
    old = _meter.Meter("claude x", "limit-text", T0, limited_until=T0 + 5 * 24 * H)
    saved = old.to_dict()
    saved.pop("limit_noted_at", None)
    (tmp_path / "latest.json").write_text(json.dumps({"latest": {"claude x": saved}, "warned": []}))
    store = _meter.MeterStore(tmp_path, clock=lambda: T0 + PROBE)
    decision = route({"claude": store.readings["claude x"]}, now=T0 + PROBE, available=("claude",))
    assert (decision.engine, decision.probe) == ("claude", True)
    decision = route({"claude": store.readings["claude x"]}, now=T0 + PROBE - 1, available=("claude",))
    assert decision.deferred and decision.probe_at == T0 + PROBE


def test_a_rejected_login_saved_without_a_note_time_stays_unprobed(tmp_path):
    old = _meter.Meter("codex x", "limit-text", T0, limited_until=T0 + 5 * 24 * H, error="login rejected")
    saved = old.to_dict()
    saved.pop("limit_noted_at", None)
    (tmp_path / "latest.json").write_text(json.dumps({"latest": {"codex x": saved}, "warned": []}))
    store = _meter.MeterStore(tmp_path, clock=lambda: T0 + 2 * PROBE)
    assert store.readings["codex x"].limit_noted_at is None
