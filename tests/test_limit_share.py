"""Remembered limits shared by pods through the state store: a clear found by one reaches the rest (#195)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import (
    _factory,
    _factory_base,
    _factory_meters,
    _inbox,
    _limit_share,
    _meter,
    _profile,
    _queue,
    _review_wait,
    _reviewer,
    _router,
    _session_config,
    _state_store,
    _supervise_support,
    _timeline,
    _usage,
)
from tests._wait import wait_for
from tests.state_git import make_store
from tests.test_inbox_engine_direction import inbox, loop, paused_session, stop_loop  # noqa: F401 -- fixtures used by name

T0 = 1_800_000_000.0
H = 3600.0
X = "claude work#0123456789ab"  # an account key: the secret's name and the token's fingerprint
POLL = _limit_share.LIMIT_SHARE_POLL_S


class Pods:
    """Pods that share one state root and one fake wall clock."""

    def __init__(self, tmp_path: Path, root: str) -> None:
        self.now = {"t": T0}
        self.root, self.tmp_path = root, tmp_path

    def pod(self, name: str) -> _meter.MeterStore:
        share = _limit_share.LimitShare(self.root, clock=lambda: self.now["t"])
        return _meter.MeterStore(self.tmp_path / name, clock=lambda: self.now["t"], share=share)

    def advance(self, seconds: float) -> None:
        self.now["t"] += seconds


@pytest.fixture
def pods(tmp_path, state_store) -> Pods:
    return Pods(tmp_path, f"{_state_store.ROOT}/pods")


def routed(store: _meter.MeterStore, now: float) -> _router.Decision:
    return _router.route("planner", {"claude": store.readings.get(X)}, available=("claude",), now=now)


def test_a_success_on_one_pod_ends_the_deferral_of_another(pods):
    """Pod B defers on account X; pod A succeeds on X; one poll interval later B routes."""
    a, b = pods.pod("a"), pods.pod("b")
    b.note_limit("claude", T0 + 12 * H, X)
    assert routed(b, pods.now["t"]).deferred

    pods.advance(10)
    a.succeeded("claude", X, at=pods.now["t"])  # a call on X answered, whatever A remembered

    pods.advance(POLL)
    assert b.poll_shared()
    assert not routed(b, pods.now["t"]).deferred
    assert b.readings[X].limited_until is None


def test_the_waiting_review_of_that_pod_wakes(pods, monkeypatch):
    a, b = pods.pod("a"), pods.pod("b")
    monkeypatch.setattr(_usage, "_meters", b)
    clock = SimpleNamespace(time=lambda: pods.now["t"])
    monkeypatch.setattr(_review_wait, "time", clock)
    monkeypatch.setattr(_review_wait.ReviewWait, "save", lambda *args: None)
    b.note_limit("claude", T0 + 12 * H, X)
    wait = _review_wait.ReviewWait()
    deferral = _reviewer.ReviewLimited("retry at 09:00 UTC", retry_at=T0 + 1800)
    wait.wait_for_limit(deferral, 1, _timeline._Timeline())
    assert not wait.is_due()

    pods.advance(10)
    a.succeeded("claude", X, at=pods.now["t"])
    pods.advance(POLL)
    assert not wait.is_due(), "not before this pod has read the clear"
    _usage.poll_limit_share()
    assert wait.is_due(), "the wait ends long before its own time"
    assert wait.at == T0 + 1800, "the router's own retry time, with no slack added"


def test_the_waiting_review_ends_at_the_routers_retry_time_not_later(monkeypatch):
    monkeypatch.setattr(_usage, "_meters", None)
    clock = SimpleNamespace(time=lambda: T0)
    monkeypatch.setattr(_review_wait, "time", clock)
    monkeypatch.setattr(_review_wait.ReviewWait, "save", lambda *args: None)
    timeline = _timeline._Timeline()
    wait = _review_wait.ReviewWait()
    wait.wait_for_limit(_reviewer.ReviewLimited("deferred", retry_at=T0 + 1800), 1, timeline)
    assert (wait.at, timeline._phase["limited_until"]) == (T0 + 1800, T0 + 1800)
    assert not wait.is_due()
    clock.time = lambda: T0 + 1800
    assert wait.is_due()


def test_a_deferral_known_only_from_its_text_keeps_the_slack_and_the_minimum(monkeypatch):
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_review_wait, "time", SimpleNamespace(time=lambda: T0))
    monkeypatch.setattr(_review_wait.ReviewWait, "save", lambda *args: None)
    wait = _review_wait.ReviewWait()
    # `retry at` is wall-clock text: minutes of resolution, so it is an estimate.
    wait.wait_for_limit(_reviewer.ReviewLimited("retry at 12:00 UTC"), 1, _timeline._Timeline())
    assert wait.at is not None and wait.at >= T0 + 600


def test_a_clear_noted_before_a_newer_limit_leaves_that_limit_alone(pods):
    a, b = pods.pod("a"), pods.pod("b")
    b.note_limit("claude", T0 + 12 * H, X)
    a.succeeded("claude", X, at=T0 + 1)  # needs the record B published
    pods.advance(100)
    b.note_limit("claude", T0 + 12 * H, X)  # B was refused again after A's success
    pods.advance(POLL)
    assert not b.poll_shared()
    assert b.readings[X].limited_until == T0 + 12 * H


def test_nothing_is_written_per_call_and_nothing_read_without_a_limit(pods, monkeypatch):
    a = pods.pod("a")
    writes, reads = [], []
    real_write, real_read = _queue.write_json, _queue.read_json_or_raise

    def write_json(path, value):
        writes.append(path)
        return real_write(path, value)

    def read_json(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(_queue, "write_json", write_json)
    monkeypatch.setattr(_queue, "read_json_or_raise", read_json)
    for _ in range(5):
        a.succeeded("claude", X, at=pods.now["t"])
    assert writes == [], "no limit on record: a success writes nothing"
    assert len(reads) == 1, "and reads the record once per interval"
    assert not a.poll_shared()
    assert len(reads) == 1, "a pod with no limit polls nothing"

    pods.pod("b").note_limit("claude", T0 + H, X)
    writes.clear()
    pods.advance(POLL)
    for _ in range(5):
        a.succeeded("claude", X, at=pods.now["t"])
    assert len(writes) == 1, "one clear is written, not one per call"


def test_a_success_soon_after_another_pods_limit_still_publishes_the_clear(pods):
    """A's record was read (no limit) just before B noted one: A's next success must not trust that read."""
    a, b = pods.pod("a"), pods.pod("b")
    a.succeeded("claude", X, at=pods.now["t"])
    pods.advance(2)
    b.note_limit("claude", T0 + 12 * H, X)
    pods.advance(10)
    a.succeeded("claude", X, at=pods.now["t"])
    pods.advance(POLL)
    assert b.poll_shared()
    assert b.readings[X].limited_until is None


def test_a_record_with_null_times_still_takes_and_publishes_the_clear(pods, capsys):
    """A `null` time in the store reads as absent, not malformed, and the clear still reaches pod B."""
    a, b = pods.pod("a"), pods.pod("b")
    path = _limit_share.LimitShare(pods.root)._account_path(X)
    _queue.write_json(path, {"engine": "claude", "limited_until": None, "noted_at": None, "cleared_at": None})
    b.note_limit("claude", T0 + 12 * H, X)  # publish must not copy the null cleared_at forward
    pods.advance(10)
    a.succeeded("claude", X, at=pods.now["t"])
    assert _queue.read_json(path)["cleared_at"] == pods.now["t"], "the shared clear was never published"
    pods.advance(POLL)
    assert b.poll_shared() and b.readings[X].limited_until is None
    assert "[limits]" not in capsys.readouterr().err, "a null time is absent, not a malformed record"


def test_claiming_the_probe_does_not_hide_a_clear_written_since_the_last_poll(pods):
    """The claim re-dates the limit; a hand clear that is not read yet must be read before that."""
    a, b = pods.pod("a"), pods.pod("b")
    b.note_limit("claude", T0 + 12 * H, X)
    pods.advance(_meter.LIMIT_PROBE_S - 30)
    assert a.share is not None
    a.share.clear_engine("claude")  # a person's reset, in the gap since B last read the store
    pods.advance(30)
    assert routed(b, pods.now["t"]).candidates[0].state == "probe"

    b.claim_probe(X)
    pods.advance(POLL)
    b.poll_shared()
    assert b.readings[X].limited_until is None


def test_a_manual_clear_for_the_engine_reaches_every_account_noted_before_it(pods):
    a, b = pods.pod("a"), pods.pod("b")
    b.note_limit("claude", T0 + 12 * H, X)
    b.note_limit("claude", T0 + 12 * H, "claude other#ffffffffffff")
    b.note_limit("codex", T0 + 12 * H)
    pods.advance(5)
    a.share.clear_engine("claude")  # what `factory.clear_limit` writes
    pods.advance(POLL)
    assert b.poll_shared()
    assert [m.limited_until for m in b.readings.values()] == [None, None, T0 + 12 * H]


def test_the_shared_files_hold_hashes_and_times_never_the_account_or_a_token(pods):
    pods.pod("b").note_limit("claude", T0 + H, X)
    files = _queue.listing(f"{pods.root}/limits")
    assert files, "the limit was never written to the store"
    for path in files:
        text = json.dumps(_queue.read_json(path))
        assert "work" not in text and "0123456789ab" not in text
        assert set(json.loads(text)) == {"engine", "limited_until", "noted_at", "cleared_at"}
    assert not any(X in str(p) for p in files)


def test_a_failing_shared_store_never_fails_a_call_and_logs_once_per_interval(pods, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("store down")

    monkeypatch.setattr(_queue, "read_json", broken)
    monkeypatch.setattr(_queue, "write_json", broken)
    a = pods.pod("a")
    a.note_limit("claude", T0 + H, X)
    for _ in range(4):
        a.succeeded("claude", X, at=pods.now["t"])
        a.poll_shared()
    assert a.readings[X].limited_until is None, "the local clear does not depend on the shared store"
    assert capsys.readouterr().err.count("shared") == 1


def test_a_shared_store_that_refuses_reads_is_logged_and_never_fails_a_call(pods, monkeypatch, capsys):
    """The real reader, on a store that denies access: the failure is reported, the call goes on."""
    a = pods.pod("a")
    a.note_limit("claude", T0 + H, X)

    class Denied:
        def __getattr__(self, name):
            def denied(*args, **kwargs):
                raise PermissionError(name)

            return denied

    monkeypatch.setattr(_queue, "_fs", lambda path: Denied())
    capsys.readouterr()
    pods.advance(POLL)
    assert a.poll_shared() is False
    assert "shared read failed: PermissionError" in capsys.readouterr().err
    pods.advance(1)
    a.succeeded("claude", X, at=pods.now["t"])
    assert a.readings[X].limited_until is None, "the local clear does not depend on the shared store"
    assert "shared" not in capsys.readouterr().err, "logged at most once per interval"


def test_a_missing_shared_record_is_no_failure(pods, capsys):
    a = pods.pod("a")
    capsys.readouterr()
    a.succeeded("claude", X, at=pods.now["t"])
    assert "shared" not in capsys.readouterr().err


def test_a_codex_success_on_one_pod_ends_another_pods_codex_limit(pods):
    """Codex usage is recorded under its credential's account; a plain Codex success must still clear it."""
    a, b = pods.pod("a"), pods.pod("b")
    for pod in (a, b):
        pod.update(_meter.Meter("codex fingerprint-0123", "exec-event", pods.now["t"], five_hour_pct=10))
    b.note_limit("codex", T0 + 12 * H, "")
    assert b.latest["codex"].limited_until

    pods.advance(10)
    a.succeeded("codex", "", at=pods.now["t"])

    pods.advance(POLL)
    assert b.poll_shared()
    assert b.latest["codex"].limited_until is None
    decision = _router.route("planner", {"codex": b.latest["codex"]}, available=("codex",), now=pods.now["t"])
    assert not decision.deferred


@pytest.fixture
def codex_logins(monkeypatch):
    """Return a function that makes this process run on the Codex login of a slot, as the deploy sets it."""

    def run_on(secret: str) -> str:
        monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", secret)
        return _usage._limit_account("codex")

    return run_on


@pytest.mark.parametrize("noted_with_reading", [True, False])
def test_a_codex_clear_reaches_a_pod_on_the_same_login_whether_or_not_it_has_a_reading(
    pods, codex_logins, noted_with_reading
):
    """The login keys the limit, not whether a usage reading came yet (the double key this replaced)."""
    account = codex_logins("login-a")
    with_reading, without = pods.pod("a"), pods.pod("b")
    with_reading.update(_meter.Meter(account, "exec-event", pods.now["t"], five_hour_pct=10))
    noter, clearer = (with_reading, without) if noted_with_reading else (without, with_reading)
    noter.note_limit("codex", T0 + 12 * H, account)

    pods.advance(10)
    clearer.succeeded("codex", account, at=pods.now["t"])

    pods.advance(POLL)
    assert noter.poll_shared()
    assert noter.latest["codex"].limited_until is None


def test_a_codex_success_on_another_slots_login_leaves_a_codex_limit(pods, codex_logins):
    """Every slot has its own Codex login (`codex_login`): one's success says nothing of another's limit."""
    a, b = pods.pod("a"), pods.pod("b")
    limited = codex_logins("login-a")
    other = codex_logins("login-b")
    assert limited != other
    a.note_limit("codex", T0 + 12 * H, limited)

    pods.advance(10)
    b.succeeded("codex", other, at=pods.now["t"])

    pods.advance(POLL)
    assert not a.poll_shared()
    assert a.readings[limited].limited_until == T0 + 12 * H
    share = _limit_share.LimitShare(pods.root)
    record = share._read(share._account_path(limited), fresh=True)
    assert record["limited_until"] == T0 + 12 * H
    assert "login" not in json.dumps(record) and limited not in json.dumps(record), "hashes and times only"


def test_a_codex_clear_reaches_a_pod_noting_before_it_has_a_reading(pods, monkeypatch, codex_logins):
    """The first Codex call of a pod records its success before its usage reading is ingested (`_usage`)."""
    account = codex_logins("login-a")
    noter, caller = pods.pod("a"), pods.pod("b")
    monkeypatch.setattr(_usage, "_meters", caller)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    noter.update(_meter.Meter(account, "exec-event", pods.now["t"], five_hour_pct=10))
    noter.note_limit("codex", T0 + 12 * H, account)
    pods.advance(10)
    reading = _meter.Meter(account, "exec-event", pods.now["t"], five_hour_pct=11)
    report = f"{_usage._USAGE_MARK}{json.dumps({'rate_limits': reading.to_dict()})}-->"
    _usage._ingest_subagent_usage("text" + report, "judge", 1, None, engine="codex", started=pods.now["t"])

    pods.advance(POLL)
    assert noter.poll_shared()
    assert noter.latest["codex"].limited_until is None


def test_without_a_state_uri_limits_stay_local(tmp_path, monkeypatch):
    """A profile without `[state] uri` shares limits on the state branch; with no profile they stay local."""
    assert _factory_base.PROFILE.state.uri == ""
    monkeypatch.setattr(_session_config, "_PROFILE", _factory_base.PROFILE)
    shared = _usage.open_meters(tmp_path / "shared")
    assert shared.share is not None and shared.share.root == _state_store.ROOT
    shared.note_limit("claude", T0 + H, X)
    assert _queue.listing(f"{_state_store.ROOT}/limits"), "a profile without [state] uri still shares"
    monkeypatch.setattr(_session_config, "_PROFILE", None)
    store = _usage.open_meters(tmp_path / "meters")
    assert store.share is None
    store.note_limit("claude", T0 + H, X)
    store.succeeded("claude", X)
    assert store.readings[X].limited_until is None and not store.poll_shared()


async def test_a_clear_limit_direction_clears_here_and_for_the_other_pods(pods, monkeypatch, inbox):  # noqa: F811 -- `inbox` is the imported fixture
    a, b = pods.pod("a"), pods.pod("b")
    monkeypatch.setattr(_usage, "_meters", a)
    a.note_limit("claude", T0 + 12 * H, X)
    b.note_limit("claude", T0 + 12 * H, X)
    pods.advance(5)
    session, engine, timeline = paused_session(pods.tmp_path)
    stop = asyncio.Event()
    # No callback: the default is the production one (`agent.project` passes none).
    task = asyncio.create_task(_inbox._inbox_loop(session, timeline, stop, None))  # type: ignore[arg-type]
    await wait_for(lambda: inbox.opened)
    await inbox.messages.put("/clear-limit")
    await wait_for(lambda: len(inbox.opened) >= 2, what="the next direct- condition")

    assert a.readings[X].limited_until is None
    assert any("claude limit cleared" in text for _, _, text in timeline._rows)
    assert engine.submitted == [] and session.pending == [], "acted on here, not sent to the session"
    pods.advance(POLL)
    assert b.poll_shared() and b.readings[X].limited_until is None
    await stop_loop(inbox, task, stop)


def test_a_clear_limit_direction_wakes_the_waiters_even_with_no_limit_on_record_here(pods, monkeypatch):
    """The session may be paused on a notice the meter never recorded: a person's clear still wakes it."""
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    before = store.wake_count()
    _usage.clear_limit_direction("claude")
    assert store.wake_count() > before


def test_a_clear_limit_direction_says_when_other_pods_were_not_told(pods, monkeypatch):
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    store.note_limit("claude", T0 + 12 * H, X)

    def broken(*args):
        raise RuntimeError("store down")

    monkeypatch.setattr(_queue, "write_json", broken)
    row = _usage.clear_limit_direction("claude")
    assert store.readings[X].limited_until is None
    assert "cleared here" in row and "not told" in row
    assert "tried again now" not in row, "nothing says every pod was cleared"


@pytest.mark.parametrize(
    ("text", "engine"),
    [
        ("/clear-limit", "claude"),
        ("/clear-limit codex", "codex"),
        ("  /CLEAR-LIMIT Claude. ", "claude"),
        ("/clear-limit gpt", None),
        ("/clear-limit claude now", None),
        ("please /clear-limit", None),
        ("clear-limit", None),
    ],
)
def test_clear_limit_grammar(text, engine):
    assert _inbox.parse_clear_limit(text) == engine


def test_a_sub_agent_that_was_refused_on_an_engine_does_not_clear_it(pods, monkeypatch):
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: pods.now["t"]))
    account = _usage._limit_account("claude")
    store.note_limit("claude", T0 + 12 * H, account)
    pods.advance(5)
    refused = f"{_usage._USAGE_MARK}{json.dumps({'refusals': {'claude': T0 + 12 * H}})}-->"
    _usage._ingest_subagent_usage("text" + refused, "judge", 1, None, engine="claude")
    assert store.readings[account].limited_until == T0 + 12 * H
    _usage._ingest_subagent_usage("text", "judge", 1, None, engine="claude", started=pods.now["t"])
    assert store.readings[account].limited_until is None


def test_a_report_without_its_start_time_never_clears_a_limit(pods, monkeypatch):
    """The dashboard's re-read of every child report (stale, failed, from before a restart) proves nothing."""
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: pods.now["t"]))
    account = _usage._limit_account("claude")
    pods.advance(5)
    store.note_limit("claude", T0 + 12 * H, account)  # noted after the child started at T0
    pods.advance(5)
    _usage._ingest_subagent_usage("text", "judge", 1, None, engine="claude")
    assert store.readings[account].limited_until == T0 + 12 * H
    assert store.wake_count() == 0


def test_a_re_read_report_without_its_start_does_not_bring_a_cleared_limit_back(pods, monkeypatch):
    """A re-attached or failed child's report is re-read with no start; its old refusal stays over."""
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: pods.now["t"]))
    account = _usage._limit_account("claude")
    store.note_limit("claude", T0 + 12 * H, account)
    pods.advance(60)
    store.clear_limit("claude")
    pods.advance(60)
    refused = f"{_usage._USAGE_MARK}{json.dumps({'refusals': {'claude': T0 + 12 * H}})}-->"

    _usage._ingest_subagent_usage("answer" + refused, "judge", 1, None, engine="claude")

    assert store.readings[account].limited_until is None
    assert store.share is not None
    record = _queue.read_json(store.share._account_path(account))
    assert record["noted_at"] == T0, "no newer limit was published"


def test_a_child_report_after_a_clear_notes_its_refusal_but_one_from_before_it_does_not(pods, monkeypatch):
    """Every limit episode ends in a clear; later refusals in child reports must still be noted."""
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: pods.now["t"]))
    account = _usage._limit_account("claude")
    store.note_limit("claude", T0 + 12 * H, account)
    pods.advance(60)
    store.clear_limit("claude")
    child_before = pods.now["t"] - 30  # a child that ran before the clear
    pods.advance(3 * 86400)
    child_after = pods.now["t"] - 30
    until = pods.now["t"] + 48 * H
    refused = f"{_usage._USAGE_MARK}{json.dumps({'refusals': {'claude': until}})}-->"

    _usage._ingest_subagent_usage("answer" + refused, "judge", 1, None, action_started_at=child_before)
    assert store.readings[account].limited_until is None, "a report from before the clear is old news"

    _usage._ingest_subagent_usage("answer" + refused, "judge", 1, None, action_started_at=child_after)
    assert store.readings[account].limited_until == until


async def test_the_dashboards_child_report_read_dates_a_refusal_by_when_the_child_started(pods, monkeypatch):
    """The `subagent()` children's reports are read with no call start: the action's own start dates them."""
    store = pods.pod("a")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(_usage, "time", SimpleNamespace(time=lambda: pods.now["t"]))
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="run")))
    account = _usage._limit_account("claude")
    store.note_limit("claude", T0 + 12 * H, account)
    pods.advance(60)
    store.clear_limit("claude")
    pods.advance(3 * 86400)
    until = pods.now["t"] + 48 * H
    record = {"route": {"role": "dev", "engine": "claude"}, "refusals": {"claude": until}}
    report = f"\n<!--factory-usage {json.dumps(record)}-->"
    started = {"child-old": T0 + 10, "child-new": pods.now["t"] - 30}
    actions = [
        SimpleNamespace(
            name=name,
            task_name="env.subagent",
            phase="SUCCEEDED",
            get_report=lambda: report,
            start_time=datetime.fromtimestamp(at, timezone.utc),
        )
        for name, at in started.items()
    ]
    monkeypatch.setattr(remote.Action, "listall", lambda **kwargs: actions[:1])
    await _supervise_support._refresh_child_usage(_timeline._Timeline())
    assert store.readings[account].limited_until is None, "a child that ran before the clear"

    monkeypatch.setattr(remote.Action, "listall", lambda **kwargs: actions[1:])
    await _supervise_support._refresh_child_usage(_timeline._Timeline())
    assert store.readings[account].limited_until == until


# --- the factory ----------------------------------------------------------------------------------------


def test_the_factory_task_writes_the_engine_wide_clear_and_the_dashboard_applies_it(queue_state):
    root = _state_store.ROOT
    now = time.time()  # the task stamps the clear with the real clock
    held = _meter.Meter(
        "claude work#0123456789ab",
        "limit-text",
        now - 60,
        limited_until=now + 12 * H,
        limit_noted_at=now - 60,
    )
    found = {held.account: held}
    _factory_meters._apply_shared_clears(found, now)
    assert held.limited_until == now + 12 * H, "nothing cleared yet"

    reply = json.loads(asyncio.run(_factory.clear_limit.func("claude")))
    assert reply == {"engine": "claude", "status": "cleared"}
    assert _queue.read_json(f"{root}/limits/engine-claude.json")["cleared_at"] >= now
    _factory_meters._apply_shared_clears(found, now)
    assert held.limited_until is None, "the dashboard no longer shows the cleared account as limited"


def test_a_newer_reading_of_a_limited_account_keeps_when_the_limit_was_noted(queue_state, monkeypatch):
    """A reading that inherits `limited_until` inherits `limit_noted_at`: a later hand clear applies."""
    root = _state_store.ROOT
    now = time.time()
    noted = _meter.Meter(
        X, "limit-text", now - H, limited_until=now + 12 * H, limit_noted_at=now - H
    )  # a pod's report
    newer = _meter.Meter(X, "oauth-usage", now - 60)  # a `read_login_meter` run, a minute old
    run = SimpleNamespace(outputs=lambda: (json.dumps(newer.to_dict()),))
    monkeypatch.setattr(remote.Run, "listall", staticmethod(lambda **kwargs: [run]))
    _limit_share.LimitShare(root).clear_engine("claude", at=now - H / 2)  # cleared by hand, after the limit

    found = _factory_meters.login_meters(reports={"slot": [noted]}, now=now, raw=True, bench=False)
    assert found[X].observed_at == now - 60, "the newer reading is the one shown"
    assert found[X].limited_until is None


def test_the_factory_task_says_so_when_the_clear_could_not_be_written(queue_state, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("store down")

    monkeypatch.setattr(_queue, "write_json", broken)
    reply = json.loads(asyncio.run(_factory.clear_limit.func("claude")))
    assert reply["status"] != "cleared" and reply["engine"] == "claude"
    assert "state" in reply["error"], "the owner is told what failed"
    assert capsys.readouterr().err.count("shared write failed") == 1, "still logged, once"


def test_the_factory_task_ignores_the_engines_case(queue_state):
    assert json.loads(asyncio.run(_factory.clear_limit.func("Claude"))) == {
        "engine": "claude",
        "status": "cleared",
    }


def test_the_factory_task_without_a_state_uri_fails_saying_so(monkeypatch):
    """With no `[state] uri` the clear goes to the state branch; if it cannot be written the reply says so."""
    monkeypatch.setattr(
        _factory_base, "PROFILE", dataclasses.replace(_factory_base.PROFILE, state=_profile.State(uri=""))
    )
    assert json.loads(asyncio.run(_factory.clear_limit.func("claude")))["status"] == "cleared"

    def refused(*args):
        raise _state_store.StateError("push rejected")

    monkeypatch.setattr(_queue, "write_json", refused)
    reply = json.loads(asyncio.run(_factory.clear_limit.func("claude")))
    assert reply["status"] == "not cleared"
    assert "state branch" in reply["error"] and "[state] uri" not in reply["error"]


def test_two_stores_on_one_remote_see_each_others_clears(tmp_path, state_store):
    """Pods have their own cache of the one remote branch; a clear written by one is read by the other."""
    remote = Path(state_store.remote)
    stores = {
        "a": make_store(tmp_path, remote=remote, name="a"),
        "b": make_store(tmp_path, remote=remote, name="b"),
    }
    share = _limit_share.LimitShare(clock=lambda: T0)

    def on(pod: str) -> _limit_share.LimitShare:
        _state_store.configure(stores[pod])  # what a pod's `current()` is
        return share

    on("a").clear_engine("claude", at=T0 + 5)
    assert on("b").read_cleared_at("claude", X) == T0 + 5
    share.publish("claude", X, T0 + H, T0)
    share.succeeded("claude", X, at=T0 + 10)
    share._cache.clear()  # not the 5 s record cache: the other store's own copy is what is under test
    assert on("a").read_cleared_at("claude", X) == T0 + 10


def test_the_factory_task_rejects_an_unknown_engine(queue_state):
    with pytest.raises(ValueError, match="unknown engine"):
        asyncio.run(_factory.clear_limit.func("gpt"))


def test_the_chat_may_run_the_clear_limit_task():
    from flyte_agent_factory import _factory_mcp

    assert "clear_limit" in _factory_mcp.CHAT_TASKS
    assert "factory.clear_limit" in _factory_mcp.INSTRUCTIONS


@pytest.mark.parametrize(
    ("record", "logged"),
    [
        ({"cleared_at": "soon"}, True),
        ({"cleared_at": [1]}, True),
        ({"cleared_at": None}, False),
        ({"cleared_at": True}, True),
        ({"cleared_at": 10**400}, True),
        ({"noted_at": 10**400}, True),
    ],
)
def test_a_malformed_record_is_no_clear_for_the_dashboard_or_the_pods(queue_state, record, logged, capsys):
    root = _state_store.ROOT
    now = time.time()
    _queue.write_json(f"{root}/limits/engine-claude.json", record)
    held = _meter.Meter(X, "limit-text", now - 60, limited_until=now + 12 * H, limit_noted_at=now - 60)
    _factory_meters._apply_shared_clears({X: held}, now)
    assert held.limited_until == now + 12 * H
    assert _limit_share.LimitShare(root).read_cleared_at("claude", X) == 0.0
    assert ("[limits] shared read failed" in capsys.readouterr().err) is logged


def test_a_project_pod_mounts_the_deploy_key_the_limit_share_pushes_with():
    """`agent.project` builds a `LimitShare` (`_usage.open_meters`); its pod must carry the store's key."""
    pairs = dict(item.split("=", 1) for item in _factory_base.PROFILE.agent_secrets.split(","))
    assert pairs[_factory_base.PROFILE.secrets.deploy_key] == "GIT_DEPLOY_KEY"
    assert [s.key for s in _factory_base.state_secrets(_factory_base.PROFILE)] == [
        _factory_base.PROFILE.secrets.deploy_key
    ]
