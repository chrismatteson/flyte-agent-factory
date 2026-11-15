"""Every waiter on a remembered limit wakes at the router's re-check, and every status shows that time (#195).

The reset (`retry_at`) stays what was remembered; `probe_at` is the earlier time the router lets one call
through. A waiter asks the router again at the earlier of the two, and the row, the phase and the dashboard
all say the time it really does.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from flyte_agent_factory import (
    _author,
    _dashboard,
    _delegation,
    _limit_share,
    _meter,
    _review,
    _review_wait,
    _reviewer,
    _router,
    _session_config,
    _timeline,
    _usage,
)
from flyte_agent_factory._delegation import Delegations
from flyte_agent_factory._router import RouterDeferred
from tests._wait import wait_for
from tests.test_delegate_limit import (  # noqa: F401 -- `repo` and `done_worker` are fixtures used by name
    NOW,
    Rows,
    _no_pins,
    answer,
    done_worker,
    repo,
)
from tests.test_engine_limit_handoff import (  # noqa: F401 -- `offline` is a fixture used by name
    CLAUDE_ACCOUNT,
    CLAUDE_NOTICE,
    CODEX_WEEKLY,
    _refused_in_pump,
    _resumes,
    _rig,
    _settle,
    offline,
)
from tests.test_limit_resume import WALL0, _clock, finish, rows
from tests.test_nudge import _agent_time

pytestmark = pytest.mark.real_reset  # the author notice's own reset time is read, as in the handoff tests

PROBE = _meter.LIMIT_PROBE_S
RESET = NOW + 12 * 3600


def clock_text(at: float) -> str:
    return _author._clock(at)


# --- the review wait ------------------------------------------------------------------------------------


@pytest.fixture
def review_clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(_usage, "_meters", None)
    fake = SimpleNamespace(**{**vars(time), "time": lambda: now["t"]})
    monkeypatch.setattr(_review_wait, "time", fake)
    monkeypatch.setattr(_review_wait.ReviewWait, "save", lambda *args: None)
    return now


def test_the_review_wakes_at_the_probe_time_and_the_status_shows_both_times(review_clock):
    timeline = _timeline._Timeline()
    wait = _review_wait.ReviewWait()
    limited = _reviewer.ReviewLimited("deferred", retry_at=RESET, probe_at=NOW + PROBE)
    wait.wait_for_limit(limited, 1, timeline)

    assert wait.at == NOW + PROBE, "exactly the re-check: no slack, no minimum"
    assert (timeline._phase["limited_until"], timeline._phase["probe_at"]) == (RESET, NOW + PROBE)
    assert wait.describe_activity().endswith(f"at {_review_wait.format_utc_hhmm(NOW + PROBE)} UTC")
    assert not wait.is_due()
    review_clock["t"] = NOW + PROBE
    assert wait.is_due()
    label = _dashboard.phase_label(timeline._phase, NOW)
    assert label.startswith("limited until") and f"re-check at {clock_text(NOW + PROBE)[:-4]}" in label


def test_a_review_wait_without_a_probe_wakes_at_the_reset_and_shows_no_re_check(review_clock):
    timeline = _timeline._Timeline()
    wait = _review_wait.ReviewWait()
    wait.wait_for_limit(_reviewer.ReviewLimited("deferred", retry_at=NOW + 900), 1, timeline)
    assert (wait.at, timeline._phase["limited_until"], timeline._phase["probe_at"]) == (
        NOW + 900,
        NOW + 900,
        None,
    )
    assert "re-check" not in _dashboard.phase_label(timeline._phase, NOW)


def test_the_review_status_is_kept_on_the_same_times_while_it_waits(review_clock):
    timeline = _timeline._Timeline()
    wait = _review_wait.ReviewWait()
    wait.wait_for_limit(
        _reviewer.ReviewLimited("deferred", retry_at=RESET, probe_at=NOW + PROBE), 1, timeline
    )
    timeline.phase("building")  # whatever else set the phase meanwhile
    wait.show(timeline)
    assert (timeline._phase["limited_until"], timeline._phase["probe_at"]) == (RESET, NOW + PROBE)


async def test_a_review_whose_limit_was_cleared_while_its_judges_ran_waits_no_longer(
    review_clock, monkeypatch, tmp_path
):
    """One judge's probe succeeds while the others defer; the deferral carries a re-check 30 min out."""
    store = _meter.MeterStore(tmp_path, clock=lambda: review_clock["t"])
    store.note_limit("claude", RESET, "claude acct")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_claude_account", lambda: "claude acct")
    timeline = _timeline._Timeline()

    async def evaluate(*args):
        _usage.note_success("claude", review_clock["t"] + 1)  # the probing judge's answer
        raise _reviewer.ReviewLimited("deferred", retry_at=RESET, probe_at=review_clock["t"] + PROBE)

    monkeypatch.setattr(_review, "_evaluate", evaluate)
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        await _review._review_round(tmp_path, "brief", "", 1, timeline)
    review_clock["t"] += 2
    wait = _review_wait.ReviewWait()
    wait.wait_for_limit(raised.value, 1, timeline)
    assert wait.is_due(), "re-run at the next tick, not at the re-check"


def test_the_judges_deferral_carries_the_earliest_reset_and_the_earliest_re_check():
    outcome = _review._JudgeOutcome
    missing = {
        "correctness": outcome(
            _review.DEFERRED, "text a", retry_at=RESET, probe_at=NOW + PROBE, limit_text="a"
        ),
        "security": outcome(
            _review.DEFERRED, "text b", retry_at=RESET + 60, probe_at=NOW + 100, limit_text="b"
        ),
    }
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        _review._raise_undecided(missing, 1, _timeline._Timeline())
    assert (raised.value.retry_at, raised.value.probe_at) == (RESET, NOW + 100)


# --- the author session ----------------------------------------------------------------------------------


async def test_the_author_pause_wakes_at_the_probe_time_and_the_phase_shows_the_routers_time(
    tmp_path, monkeypatch
):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", store=True)
    meters = _usage._meters
    assert meters is not None
    meters.note_limit("codex", CODEX_WEEKLY, "")  # nowhere else to go: the pause is a deferral
    meters.readings["codex"].limit_noted_at = None  # and Codex is not re-checked, only Claude
    stop, pump = await _refused_in_pump(rig, now, CLAUDE_NOTICE)

    session = rig.session
    reset = session.retry_at
    probe = session.probe_at
    refused_at = _agent_time().time()
    assert probe is not None and reset is not None and probe < reset
    assert probe == pytest.approx(refused_at + PROBE, abs=60), "LIMIT_PROBE_S after the refusal was noted"
    phase = rig.timeline._phase
    assert (phase["limited_until"], phase["probe_at"]) == (reset, probe)
    (router_row,) = [r for r in rows(rig.timeline, "router: author -> deferred")]
    assert f"retry at {clock_text(reset)}; re-check at {clock_text(probe)}" in router_row
    (paused_row,) = rows(rig.timeline, "coordinator paused")
    assert f"waiting for claude until Mon 19 Oct 22:40 UTC; re-check at {clock_text(probe)}" in paused_row

    offset = WALL0 - 10_000.0
    now["t"] = probe - offset - 30
    await _settle()
    assert _resumes(rig) == [] and session.on_limit_calls == 1, "not before the re-check"
    now["t"] = probe - offset + 1
    await wait_for(lambda: len(_resumes(rig)) == 1, what="the resume at the re-check")
    assert session.on_limit_calls == 2, "the router was asked again, and let this engine through"
    await finish(stop, pump)


async def test_a_session_limit_phase_shows_the_same_time_as_the_router_row(tmp_path, monkeypatch):
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", store=True)
    assert _usage._meters is not None
    _usage._meters.note_limit("codex", CODEX_WEEKLY, "")
    _usage._meters.readings["codex"].limit_noted_at = None
    stop, pump = await _refused_in_pump(rig, now, CLAUDE_NOTICE)
    phase = rig.timeline._phase
    assert phase["kind"] == "limited" and phase["probe_at"] is not None
    assert phase["limited_until"] > phase["probe_at"]
    (router_row,) = rows(rig.timeline, "router: author -> deferred")
    assert f"re-check at {clock_text(phase['probe_at'])}" in router_row
    label = _dashboard.phase_label(phase, WALL0)
    assert f"re-check at {_dashboard.clock_or_date(phase['probe_at'], WALL0)}" in label
    await finish(stop, pump)


# --- delegations -----------------------------------------------------------------------------------------


async def test_a_hard_pinned_delegation_wakes_at_the_re_check_and_says_so(repo, bridge_root, monkeypatch):  # noqa: F811 -- `repo` is the imported fixture
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    asked: list[str] = []

    def engine() -> str:
        asked.append("asked")
        if len(asked) == 1:
            raise RouterDeferred("USAGE_LIMIT router: deferred (pin codex exhausted)", RESET, NOW + PROBE)
        return "codex"

    rows_seen = Rows()
    supervisor = Delegations(bridge_root, repo, done_worker, engine, rows_seen, max_jobs=1)
    supervisor.clock = lambda: NOW
    waiting: list[dict[str, Any]] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        waiting.append(answer(bridge_root, "delegate-a"))

    supervisor.sleep = sleep
    await supervisor.service({"id": "delegate-a", "brief": "implement"})

    assert slept == [PROBE], "it asks the router again at the re-check, not at the reset"
    assert waiting == [{"state": "waiting", "engine": "codex", "until": RESET, "probe_at": NOW + PROBE}]
    assert (
        f"until {_router._reset_text(RESET)}; re-check at {clock_text(NOW + PROBE)[:-4]} UTC"
        in rows_seen.text()
    )
    assert json.loads((bridge_root / "answers" / "delegate-a.json").read_text())["state"] != "waiting"


def test_the_delegate_status_line_shows_the_re_check_after_the_reset():
    from flyte_agent_factory import _bridge

    waiting = {"state": "waiting", "engine": "codex", "until": NOW, "probe_at": NOW - 600}
    line = _bridge._format_status_line("delegate-a", waiting, NOW)  # type: ignore[arg-type]
    reset, recheck = (_review_wait.format_utc_hhmm(at) for at in (NOW, NOW - 600))
    assert line.endswith(f"until {reset} UTC; re-check at {recheck} UTC")
    plain = _bridge._format_status_line("delegate-a", {**waiting, "probe_at": None}, NOW)  # type: ignore[typeddict-item]
    assert "re-check" not in plain


def _clear_by_direction(now) -> None:
    _usage.clear_limit_direction("claude")


def _clear_by_success(now) -> None:
    _usage.note_success("claude", _agent_time().time() - 1)  # a judge on the same token answered


def _clear_by_another_pod(now) -> None:
    assert _usage._meters is not None and _usage._meters.share is not None
    other = _limit_share.LimitShare(_usage._meters.share.root, clock=lambda: _agent_time().time())
    other.clear_engine("claude")
    now["t"] += _limit_share.LIMIT_SHARE_POLL_S + 1  # this pod reads it at its next poll


@pytest.mark.parametrize("clear", [_clear_by_direction, _clear_by_success, _clear_by_another_pod])
@pytest.mark.parametrize("notice", [CLAUDE_NOTICE, "You've hit your limit"])
async def test_a_clear_resumes_the_paused_author_at_the_next_tick(tmp_path, monkeypatch, clear, notice):
    """C14/C33: `/clear-limit`, a success or another pod's clear ends the pause; no row per tick after it."""
    now = _clock(monkeypatch, WALL0)
    rig = _rig(tmp_path, monkeypatch, engine="claude", store=True)
    meters = _usage._meters
    assert meters is not None
    meters.share = _limit_share.LimitShare(clock=lambda: _agent_time().time())
    meters.note_limit("codex", CODEX_WEEKLY, "")  # nowhere else to go: the pause is a deferral
    meters.readings["codex"].limit_noted_at = None
    stop, pump = await _refused_in_pump(rig, now, notice)
    reset = rig.session.retry_at
    now["t"] += 60
    await _settle()
    assert _resumes(rig) == []

    clear(now)
    await wait_for(lambda: len(_resumes(rig)) == 1, what="the resume after the clear")
    assert rig.session.retry_at != reset or reset is None
    calls = rig.session.on_limit_calls
    await _settle(100)
    assert rig.session.on_limit_calls == calls, "the clear is acted on once, not on every tick"
    assert len(rows(rig.timeline, "(unchanged")) <= 1
    # The resumed session answers: the clear ends the episode, though the notice's own reset (a known
    # one, hours away) has not come.
    rig.proc.output_count += 9000
    for _ in range(3):
        now["t"] += 61
        await _settle()
    await wait_for(lambda: rows(rig.timeline, "running again"), what="the episode ending after the clear")
    assert rig.timeline._phase.get("kind") != "limited"
    await finish(stop, pump)


async def test_a_deferred_delegation_wakes_when_the_limit_is_cleared(
    repo,  # noqa: F811 -- `repo` is the imported fixture
    bridge_root,
    tmp_path,
    monkeypatch,
):
    """A delegation sleeping until the re-check runs once a success, a pod or a person clears the limit."""
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "claude")
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    store.note_limit("claude", RESET, "claude acct")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_delegation, "CLEAR_CHECK_S", 0.01)
    routed: list[str] = []

    def engine() -> str:
        routed.append("asked")
        if store.readings["claude acct"].limited_until:
            raise RouterDeferred("USAGE_LIMIT router: delegate -> deferred", RESET, probe_at=NOW + PROBE)
        return "claude"

    supervisor = Delegations(bridge_root, repo, done_worker, engine, Rows(), max_jobs=1)
    supervisor.clock = lambda: NOW
    sleeping = asyncio.Event()

    async def sleep(seconds: float) -> None:  # the real wait: half an hour, unless something ends it
        sleeping.set()
        await asyncio.sleep(3600)

    supervisor.sleep = sleep
    served = asyncio.create_task(supervisor.service({"id": "delegate-a", "brief": "implement"}))
    await asyncio.wait_for(sleeping.wait(), 5)
    _usage.clear_limit_direction("claude")
    await asyncio.wait_for(served, 5)
    assert routed == ["asked", "asked"], "routed again at once, not at the re-check"
    assert answer(bridge_root, "delegate-a")["state"] == "done"
