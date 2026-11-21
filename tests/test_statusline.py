"""The coordinator's status line: its capture of the usage windows, and the supervisor's ingestion of it."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _auth, _dashboard, _meter, _session_config, _statusline, _timeline, _usage

NOW = 1_795_000_000.0
WEEK_RESET = 1_795_172_400.0  # what Claude Code 2.1.328 passed the command on 2026-11-19
FIVE_RESET = NOW + 3600
PAYLOAD = {
    "session_id": "abc",
    "model": {"id": "claude-opus-4-5", "display_name": "Opus"},
    "workspace": {"current_dir": "/w"},
    "cost": {"total_cost_usd": 1.5},
    "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": FIVE_RESET},
        "seven_day": {"used_percentage": 30, "resets_at": WEEK_RESET},
    },
}
WINDOWS = {
    "five_hour_pct": 12.0,
    "five_hour_resets_at": FIVE_RESET,
    "weekly_pct": 30.0,
    "weekly_resets_at": WEEK_RESET,
}


def run_main(monkeypatch, capsys, stdin: bytes, *argv: str) -> str:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    assert _statusline.main(list(argv)) == 0
    out: str = capsys.readouterr().out
    return out


# ---- parsing -------------------------------------------------------------------------------------------


def test_the_windows_are_the_four_numbers_and_nothing_else_of_the_payload():
    assert _statusline.extract_windows(PAYLOAD) == WINDOWS


def test_a_window_the_session_does_not_report_stays_absent():
    weekly_only = {"rate_limits": {"seven_day": {"used_percentage": 30, "resets_at": WEEK_RESET}}}
    assert _statusline.extract_windows(weekly_only) == {"weekly_pct": 30.0, "weekly_resets_at": WEEK_RESET}
    meter = _meter.from_statusline(
        {"observed_at": NOW, **_statusline.extract_windows(weekly_only)}, "claude t#abc"
    )
    assert meter is not None
    assert (meter.source, meter.account, meter.observed_at) == ("statusline", "claude t#abc", NOW)
    assert meter.five_hour_pct is None and meter.five_hour_resets_at is None
    assert (meter.weekly_pct, meter.weekly_resets_at) == (30.0, WEEK_RESET)
    assert not meter.error and not meter.rejected


@pytest.mark.parametrize(
    "pct",
    [float("nan"), float("inf"), -1, 1000.5, "30", None, True, [30], {"v": 30}],
    ids=lambda v: repr(v),
)
def test_a_percentage_that_is_not_one_records_nothing(pct):
    payload = {"rate_limits": {"seven_day": {"used_percentage": pct, "resets_at": WEEK_RESET}}}
    assert _statusline.extract_windows(payload) == {}


@pytest.mark.parametrize(
    "reset", [float("nan"), -5, WEEK_RESET * 1000, "1795172400", True, None], ids=lambda v: repr(v)
)
def test_a_reset_that_is_not_epoch_seconds_is_dropped_but_the_percentage_kept(reset):
    payload = {"rate_limits": {"seven_day": {"used_percentage": 30, "resets_at": reset}}}
    assert _statusline.extract_windows(payload) == {"weekly_pct": 30.0}


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", 3, {}, {"rate_limits": None}, {"rate_limits": []}, {"rate_limits": {"seven_day": 30}}],
)
def test_a_payload_of_the_wrong_shape_records_nothing(payload):
    assert _statusline.extract_windows(payload) == {}


def test_an_empty_capture_is_no_meter():
    assert _meter.from_statusline({}, "claude t") is None


# ---- the command -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdin", [b"", b"{", b"not json", b"\xff\xfe", b"[" * 100_000, b"null", b'{"rate_limits": "x"}']
)
def test_the_command_exits_0_with_a_placeholder_on_any_input(monkeypatch, capsys, tmp_path, stdin):
    capture = tmp_path / "c.json"
    assert run_main(monkeypatch, capsys, stdin, str(capture)).strip() == "usage ?"
    assert not capture.exists()


def test_the_command_prints_a_short_line_and_writes_only_numbers(monkeypatch, capsys, tmp_path):
    capture = tmp_path / "c.json"
    monkeypatch.setattr(_statusline, "time", SimpleNamespace(time=lambda: NOW))
    line = run_main(monkeypatch, capsys, json.dumps(PAYLOAD).encode(), str(capture)).strip()
    assert line == "5h 12% · week 30% (resets Fri 11:00Z)"
    saved = json.loads(capture.read_text())
    assert saved == {"observed_at": NOW, **WINDOWS}
    assert all(isinstance(v, float | int) for v in saved.values())
    assert _statusline.read_capture(capture) == {"observed_at": NOW, **WINDOWS}


def test_an_unwritable_capture_still_prints_the_line(monkeypatch, capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    out = run_main(monkeypatch, capsys, json.dumps(PAYLOAD).encode(), str(blocker / "c.json"))
    assert out.startswith("5h 12%")


def test_the_command_runs_as_the_module_settings_name(tmp_path):
    capture = tmp_path / "c.json"
    done = subprocess.run(
        shlex.split(_statusline.build_command(capture)),
        input=json.dumps(PAYLOAD).encode(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.decode().startswith("5h 12% · week 30%")
    assert _statusline.read_capture(capture)["weekly_pct"] == 30.0
    broken = subprocess.run(
        shlex.split(_statusline.build_command(capture)), input=b"{", capture_output=True, timeout=60
    )
    assert broken.returncode == 0 and broken.stdout.decode().strip() == "usage ?"


def test_an_unchanged_capture_is_rewritten_at_most_every_30_seconds(tmp_path):
    capture = tmp_path / "c.json"
    assert _statusline.write_capture(capture, WINDOWS, NOW)
    assert not _statusline.write_capture(capture, WINDOWS, NOW + 10)
    assert not _statusline.write_capture(capture, WINDOWS, NOW + _statusline.REWRITE_EVERY_S - 1)
    assert _statusline.read_capture(capture)["observed_at"] == NOW
    assert _statusline.write_capture(capture, WINDOWS, NOW + _statusline.REWRITE_EVERY_S)
    assert _statusline.read_capture(capture)["observed_at"] == NOW + _statusline.REWRITE_EVERY_S


def test_a_changed_value_is_written_at_once(tmp_path):
    capture = tmp_path / "c.json"
    assert _statusline.write_capture(capture, WINDOWS, NOW)
    assert _statusline.write_capture(capture, {**WINDOWS, "weekly_pct": 31.0}, NOW + 1)
    assert _statusline.read_capture(capture) == {"observed_at": NOW + 1, **WINDOWS, "weekly_pct": 31.0}


def test_the_capture_is_replaced_atomically_never_left_partial(tmp_path, monkeypatch):
    capture = tmp_path / "c.json"
    _statusline.write_capture(capture, WINDOWS, NOW)

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError):
        _statusline.write_capture(capture, {**WINDOWS, "weekly_pct": 99.0}, NOW + 1)
    assert _statusline.read_capture(capture) == {"observed_at": NOW, **WINDOWS}
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        '{"weekly_pct": 30}',  # no observed time
        '{"observed_at": 1795000000}',  # no window
        '{"observed_at": 1795000000, "weekly_pct": "30"}',
        '{"observed_at": "x", "weekly_pct": 30}',
    ],
)
def test_a_capture_that_does_not_validate_is_no_reading(tmp_path, text):
    capture = tmp_path / "c.json"
    capture.write_text(text)
    assert _statusline.read_capture(capture) == {}


def test_a_capture_keeps_only_its_known_fields(tmp_path):
    capture = tmp_path / "c.json"
    capture.write_text(json.dumps({"observed_at": NOW, "weekly_pct": 30, "account": "x", "token": "y"}))
    assert _statusline.read_capture(capture) == {"observed_at": NOW, "weekly_pct": 30.0}


def test_a_deeply_nested_capture_is_no_reading_not_a_crash(tmp_path):
    capture = tmp_path / "c.json"
    capture.write_text("[" * 100_000 + "]" * 100_000)
    assert _statusline.read_capture(capture) == {}


def test_a_missing_capture_is_no_reading(tmp_path):
    assert _statusline.read_capture(tmp_path / "absent.json") == {}


# ---- settings wiring -------------------------------------------------------------------------------------


def test_the_status_line_joins_the_other_settings_without_replacing_them(tmp_path):
    hooks = {
        "hooks": {"PreToolUse": [{"matcher": "Read|Bash", "hooks": [{"type": "command", "command": "x"}]}]}
    }
    capture = tmp_path / "d" / _statusline.CAPTURE_NAME
    written = json.loads(
        _auth.write_settings(
            tmp_path / "d", hold_dialogs=False, extra=_statusline.settings_extra(hooks, capture)
        ).read_text()
    )
    assert written["hooks"] == hooks["hooks"]
    assert written["statusLine"] == {"type": "command", "command": _statusline.build_command(capture)}
    assert written["permissions"]
    assert _statusline.settings_extra(None, capture) == {"statusLine": written["statusLine"]}


def test_every_supervisor_session_is_configured_with_the_status_line(tmp_path, monkeypatch):
    from flyte_agent_factory import _context_guard, _seal_session, _session_setup

    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv(_context_guard.ENV, "1")
    stale = _statusline.locate_capture(tmp_path)
    stale.write_text(json.dumps({"observed_at": NOW, "weekly_pct": 99}))
    _session_setup.write_author_settings()
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["statusLine"]["command"] == _statusline.build_command(stale)
    # The guard's hook (and the seal's, when sealed) are still there.
    expected = _seal_session.settings_extra(_context_guard.settings_extra()) or {}
    for key, value in expected.items():
        assert settings[key] == value
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Read|Bash"
    # A capture left by an earlier session, perhaps on another token, is not credited to this one.
    assert not stale.exists()


# ---- ingestion -----------------------------------------------------------------------------------------


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    config = tmp_path / "claude"
    config.mkdir()
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", config)
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_statusline_seen", 0.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-ingest-fixture-token")
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "work_token")
    return SimpleNamespace(store=store, capture=_statusline.locate_capture(config), root=tmp_path)


async def test_a_capture_reaches_the_store_the_report_and_the_snapshot(supervisor):
    _statusline.write_capture(supervisor.capture, WINDOWS, NOW)
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    account = _session_config._claude_account()
    assert account.startswith("claude work_token#")
    stored = supervisor.store.latest["claude"]
    assert (stored.account, stored.source, stored.weekly_pct, stored.five_hour_pct) == (
        account,
        "statusline",
        30.0,
        12.0,
    )
    page = timeline.render()
    (reported,) = [m for m in _meter.from_report(page) if m.account == account]
    assert (reported.weekly_pct, reported.weekly_resets_at, reported.source) == (
        30.0,
        WEEK_RESET,
        "statusline",
    )
    snapshot = _dashboard.from_report(page)
    assert snapshot is not None
    (meter,) = snapshot["cost"]["meters"]
    assert (meter["weekly_pct"], meter["five_hour_pct"], meter["source"]) == (30.0, 12.0, "statusline")
    history = (supervisor.root / "meters" / "history.jsonl").read_text()
    assert '"statusline"' in history


async def test_a_five_hour_only_event_keeps_the_measured_week_everywhere(supervisor):
    """A sub-agent's five-hour-only event replaces the store's reading, but not the week it measured."""
    from flyte_agent_factory import _factory_usage

    _statusline.write_capture(supervisor.capture, WINDOWS, NOW)
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    info = {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "utilization": 0.2,
        "resetsAt": int(FIVE_RESET),
    }
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "num_turns": 1}
    stream = json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n" + json.dumps(result)
    _usage._ingest_subagent_usage("ok" + _usage._usage_trailer(stream), "qa", 1, timeline, started=NOW)
    stored = supervisor.store.readings[_session_config._claude_account()]
    assert (stored.source, stored.five_hour_pct, stored.weekly_pct, stored.weekly_resets_at) == (
        "cli-event",
        20.0,
        30.0,
        WEEK_RESET,
    )
    page = timeline.render()
    snapshot = _dashboard.from_report(page)
    assert snapshot is not None
    (status,) = _factory_usage.expire_windows(snapshot["cost"]["meters"], NOW + 60)
    assert (status["five_hour_pct"], status["weekly_pct"]) == (20.0, 30.0)
    chat = _factory_usage.summarize_observed_windows(_meter.from_report(page), NOW + 60)
    assert chat is not None
    assert (chat["five_hour_pct"], chat["weekly_pct"], chat["weekly_expired"]) == (20.0, 30.0, False)


def measured_at(at: float) -> _meter.Meter:
    meter = _meter.from_statusline({"observed_at": at, **WINDOWS}, "claude a")
    assert meter is not None
    return meter


def test_the_store_carries_a_window_only_while_its_reset_is_ahead(tmp_path):
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    store.update(measured_at(NOW - 60))
    store.update(
        _meter.Meter("claude a", "cli-event", NOW, five_hour_pct=20.0, five_hour_resets_at=FIVE_RESET)
    )
    assert (store.readings["claude a"].weekly_pct, store.readings["claude a"].weekly_resets_at) == (
        30.0,
        WEEK_RESET,
    )
    # The week has turned over since: a new reading does not inherit the old week's share.
    later = _meter.MeterStore(tmp_path / "later", clock=lambda: WEEK_RESET + 1)
    later.update(measured_at(NOW))
    later.update(_meter.Meter("claude a", "cli-event", WEEK_RESET + 1, five_hour_pct=5.0))
    assert later.readings["claude a"].weekly_pct is None


def test_a_reading_that_saw_a_new_window_begin_does_not_inherit_the_old_share(tmp_path):
    store = _meter.MeterStore(tmp_path / "meters", clock=lambda: NOW)
    store.update(measured_at(NOW - 60))
    store.update(
        _meter.Meter("claude a", "cli-event", NOW, weekly_pct=1.0, five_hour_resets_at=FIVE_RESET + 18_000)
    )
    reading = store.readings["claude a"]
    assert (reading.five_hour_pct, reading.five_hour_resets_at) == (None, FIVE_RESET + 18_000)
    assert reading.weekly_pct == 1.0


async def test_the_same_capture_is_ingested_once(supervisor):
    _statusline.write_capture(supervisor.capture, WINDOWS, NOW)
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    await _usage._read_meters(timeline)
    lines = (supervisor.root / "meters" / "history.jsonl").read_text().splitlines()
    assert sum('"statusline"' in line for line in lines) == 1


async def test_a_missing_or_broken_capture_records_nothing_and_never_raises(supervisor):
    timeline = _timeline._Timeline()
    await _usage._read_meters(timeline)
    supervisor.capture.write_text("{")
    await _usage._read_meters(timeline)
    assert "claude" not in supervisor.store.latest


async def test_a_deeply_nested_capture_never_stops_the_meter_read(supervisor):
    supervisor.capture.write_text("[" * 100_000 + "]" * 100_000)
    await _usage._read_meters(_timeline._Timeline())
    assert "claude" not in supervisor.store.latest


async def test_a_capture_under_a_codex_author_is_not_credited_to_the_token(supervisor, monkeypatch):
    # Only a Claude author's session writes and clears the capture; under Codex it is a checkpoint's leftover.
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    _statusline.write_capture(supervisor.capture, WINDOWS, NOW)
    await _usage._read_meters(_timeline._Timeline())
    assert "claude" not in supervisor.store.latest


def test_status_marks_a_window_that_has_reset_as_expired(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    # A snapshot read after both windows reset: the readings are history, not current usage.
    now = time.time()
    passed = {
        "five_hour_pct": 12.0,
        "five_hour_resets_at": now - 120,
        "weekly_pct": 30.0,
        "weekly_resets_at": now - 60,
    }
    timeline = _timeline._Timeline()
    meter = _meter.from_statusline({"observed_at": now - 3600, **passed}, "claude work_token#0123456789ab")
    assert meter is not None
    timeline.meter(meter)
    page = timeline.render()
    monkeypatch.setattr(_factory_base, "report_html", lambda run: page)
    _factory.launch_project("brief", "metered")
    (meter,) = _factory.read_status("metered")["meters"]
    assert (meter["weekly_pct"], meter["weekly_expired"]) == (None, True)
    assert (meter["five_hour_pct"], meter["five_hour_expired"]) == (None, True)
    assert meter["weekly_resets_at"] == pytest.approx(now - 60)


def test_status_reports_the_observed_percentages(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    # Windows still open when status reads them; a passed one is expired, as the test above holds.
    now = time.time()
    current = {**WINDOWS, "five_hour_resets_at": now + 3600, "weekly_resets_at": now + 86400}
    timeline = _timeline._Timeline()
    meter = _meter.from_statusline({"observed_at": now, **current}, "claude work_token#0123456789ab")
    assert meter is not None
    timeline.meter(meter)
    page = timeline.render()
    monkeypatch.setattr(_factory_base, "report_html", lambda run: page)
    _factory.launch_project("brief", "metered")
    (meter,) = _factory.read_status("metered")["meters"]
    assert (meter["five_hour_pct"], meter["weekly_pct"]) == (12.0, 30.0)
    assert meter["account"] == "claude work_token#0123456789ab"


def test_the_capture_path_is_under_the_session_config_dir():
    assert _statusline.locate_capture(Path("/x/claude")) == Path("/x/claude") / _statusline.CAPTURE_NAME
