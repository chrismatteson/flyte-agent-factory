"""`_usage_hours`: Claude usage per credential and UTC hour, as each run's report carries it."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from flyte_agent_factory import _session_config, _timeline, _usage, _usage_hours

NOW = 1_795_000_000.0  # 2026-11-18T11:06:40Z
HOUR = _usage_hours.format_hour_key(NOW)
ACCOUNT = "claude work_token#0123456789ab"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    return tmp_path


def test_hour_keys_are_utc_and_round_trip():
    assert HOUR == "2026-11-18T11"
    assert _usage_hours.parse_hour_start(HOUR) == NOW - 400
    assert _usage_hours.parse_hour_start("garbage") is None


def test_a_subagent_result_is_recorded_under_its_account_and_hour(state):
    rec = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30, "cost_usd": 0.5}
    _usage_hours.record_subagent(rec, ACCOUNT, NOW)
    _usage_hours.record_subagent({**rec, "cost_usd": 0.25}, ACCOUNT, NOW + 60)
    _usage_hours.record_subagent(rec, ACCOUNT, NOW + 3600)
    ledger = _usage_hours.read_ledger(NOW + 3600)
    bucket = ledger[ACCOUNT][HOUR]
    assert bucket["input_tokens"] == 20 and bucket["output_tokens"] == 40
    assert bucket["cache_read_input_tokens"] == 60 and bucket["cache_creation_input_tokens"] == 0
    assert bucket["usd"] == 0.75 and bucket["calls"] == 2
    assert ledger[ACCOUNT][_usage_hours.format_hour_key(NOW + 3600)]["calls"] == 1
    # Only numbers and the account key are kept.
    saved = json.loads(_usage_hours.locate_ledger().read_text())
    assert set(saved) == {ACCOUNT}


def test_a_codex_result_is_not_claude_usage(state):
    _usage_hours.record_subagent({"engine": "codex", "input_tokens": 10}, ACCOUNT, NOW)
    assert _usage_hours.read_ledger(NOW) == {}


def test_old_buckets_are_dropped(state):
    _usage_hours.record_subagent({"input_tokens": 1}, ACCOUNT, NOW - _usage_hours.KEEP_S - 3600)
    _usage_hours.record_subagent({"input_tokens": 2}, ACCOUNT, NOW)
    assert list(_usage_hours.read_ledger(NOW)[ACCOUNT]) == [HOUR]


@pytest.mark.parametrize("text", ["", "{", "[]", '{"x": 3}', '{"x": {"nothour": {}}}'])
def test_an_unreadable_ledger_is_empty_and_is_rewritten(state, text):
    _usage_hours.locate_ledger().parent.mkdir(parents=True, exist_ok=True)
    _usage_hours.locate_ledger().write_text(text)
    assert all(not hours for hours in _usage_hours.read_ledger(NOW).values())
    _usage_hours.record_subagent({"input_tokens": 3}, ACCOUNT, NOW)
    assert _usage_hours.read_ledger(NOW)[ACCOUNT][HOUR]["input_tokens"] == 3


def test_malformed_counts_add_nothing():
    hours: _usage_hours.Hours = {}
    _usage_hours.add(
        hours, NOW, {"input_tokens": "9", "output_tokens": float("nan"), "cache_read_input_tokens": -4}, -1
    )
    assert hours[HOUR] == {**dict.fromkeys(_usage_hours._BUCKET_FIELDS, 0.0), "calls": 1.0}


def test_recent_revalidates_buckets_from_a_report():
    hours = {
        HOUR: {"input_tokens": 5, "usd": "x", "extra": 1},
        "not-an-hour": {"input_tokens": 9},
        "2020-01-01T00": {"input_tokens": 9},
        _usage_hours.format_hour_key(NOW - 3600): "x",
    }
    assert _usage_hours.validate_recent_hours(hours, NOW) == {
        HOUR: {**dict.fromkeys(_usage_hours._BUCKET_FIELDS, 0.0), "input_tokens": 5.0}
    }
    assert _usage_hours.validate_recent_hours("x", NOW) == {}


def test_a_recorded_call_goes_to_the_hours_under_the_running_credential(state, monkeypatch):
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-hours-fixture")
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "work_token")
    monkeypatch.setattr(_usage, "read_wall_clock", lambda: NOW)
    rec = {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.1, "call_id": "c1"}
    _usage._record_usage("delegate", 0, rec)
    _usage._record_usage("delegate", 0, rec)  # the same call reported twice counts once
    (account,) = _usage_hours.read_ledger(NOW)
    assert account == _session_config._claude_account() and account.startswith("claude work_token#")
    bucket = _usage_hours.read_ledger(NOW)[account][HOUR]
    assert (bucket["input_tokens"], bucket["calls"], bucket["usd"]) == (7, 1, 0.1)


def _reply(mid: str, at: str | None, model: str = "claude-opus-4-5", **usage: int) -> str:
    entry = {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}}
    if at is not None:
        entry["timestamp"] = at
    return json.dumps(entry)


def test_the_coordinators_replies_are_bucketed_by_timestamp_at_list_price(tmp_path):
    workspace = tmp_path / "ws"
    folder = tmp_path / "cfg" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    stamp = datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z")
    (folder / "s.jsonl").write_text(
        "\n".join(
            [
                _reply("m1", stamp, input_tokens=1_000_000),
                _reply("m1", stamp, input_tokens=1_000_000),  # another block of the same reply
                _reply("m2", "2026-11-18T12:00:00", output_tokens=1_000_000),  # no offset: UTC
                _reply("m3", "not a time", input_tokens=5),
            ]
        )
    )
    hours: _usage_hours.Hours = {}
    split = _usage._author_usage_split(tmp_path / "cfg", workspace, hours=hours)
    assert split["coordinator"]["input_tokens"] == 1_000_005  # the untimed reply still counts in totals
    assert hours[HOUR]["input_tokens"] == 1_000_000 and hours[HOUR]["usd"] == 5.0
    assert hours["2026-11-18T12"]["output_tokens"] == 1_000_000 and hours["2026-11-18T12"]["usd"] == 25.0
    assert sum(b["calls"] for b in hours.values()) == 2


def test_the_structured_sessions_ledger_turns_are_bucketed_and_its_transcript_is_not_read_twice(tmp_path):
    workspace = tmp_path / "ws"
    folder = tmp_path / "cfg" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    stamp = datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z")
    # The structured session's own transcript: the ledger covers it, so its replies are not counted again.
    (folder / "covered.jsonl").write_text(_reply("m1", stamp, input_tokens=7))
    # A conversation the ledger does not cover (a TUI one) is read from its transcript.
    (folder / "tui.jsonl").write_text(_reply("m2", stamp, output_tokens=1_000_000))
    ledger_file = tmp_path / "author-turns.jsonl"
    rows = [
        {
            "at": NOW,
            "session_id": "covered",
            "model": "claude-opus-4-5",
            "usage": {"input_tokens": 1_000_000},
        },
        {"at": NOW - 3600, "session_id": "covered", "model": "", "usage": {"output_tokens": 3}},
        {"session_id": "covered", "usage": {"input_tokens": 2}},  # no time: totals only
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in rows))
    hours: _usage_hours.Hours = {}
    ledger = _usage.read_author_ledger(ledger_file, hours)
    split = _usage._author_usage_split(tmp_path / "cfg", workspace, ledger, hours=hours)
    assert split["coordinator"]["input_tokens"] == 1_000_002
    assert split["coordinator"]["output_tokens"] == 1_000_003
    assert hours[HOUR]["input_tokens"] == 1_000_000 and hours[HOUR]["usd"] == 5.0 + 25.0
    assert hours[HOUR]["output_tokens"] == 1_000_000 and hours[HOUR]["calls"] == 2
    earlier = _usage_hours.format_hour_key(NOW - 3600)
    assert hours[earlier]["output_tokens"] == 3 and hours[earlier]["usd"] == 0.0
    assert sum(b["calls"] for b in hours.values()) == 3


def test_the_snapshot_carries_this_runs_claude_usage(state, monkeypatch):
    monkeypatch.setattr(_session_config, "_token_meter_account", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-hours-fixture")
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "work_token")
    account = _session_config._claude_account()
    _usage_hours.record_subagent({"input_tokens": 4, "cost_usd": 0.2}, account, NOW)
    timeline = _timeline._Timeline()
    author: _usage_hours.Hours = {}
    _usage_hours.add(author, NOW, {"output_tokens": 9}, 0.3)
    timeline.set_author_usage({"coordinator": {}, "helpers": {}}, {}, hours=author)
    usage = timeline.snapshot(NOW)["cost"]["claude_usage"]
    assert usage["account"] == account
    assert usage["author_by_hour"][HOUR]["output_tokens"] == 9
    assert usage["subagents_by_hour"][HOUR]["input_tokens"] == 4
