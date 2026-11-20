"""How the structured author session is wired in: its argv, its turn ledger, and its limit resets."""

from __future__ import annotations

import json
import re
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _author, _inbox, _session_config, _usage
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._stream_process import STREAM_FLAGS, StreamClaudeProcess

# -- argv ------------------------------------------------------------------- #


def test_author_argv_is_the_structured_session_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_author.TUI_FALLBACK_VAR, raising=False)
    argv = _author._author_argv("demo", "bypassPermissions", "", "")
    assert argv[: 1 + len(STREAM_FLAGS)] == ["claude", *STREAM_FLAGS]
    assert argv[argv.index("--name") + 1] == "demo"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_author_argv_on_the_tui_fallback_has_no_stream_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_author.TUI_FALLBACK_VAR, "1")
    argv = _author._author_argv("demo", "bypassPermissions", "", "")
    assert argv[:3] == ["claude", "--name", "demo"]
    for flag in ("-p", "--input-format", "--output-format", "stream-json", "--permission-prompt-tool"):
        assert flag not in argv, argv


# -- the turn ledger ---------------------------------------------------------- #


def _row(session: str, cost: float, turns: int, **extra: Any) -> str:
    return json.dumps(
        {
            "at": 0,
            "session_id": session,
            "model": "claude-test",
            "num_turns": turns,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 100},
            **extra,
        }
    )


def test_author_ledger_sums_turns_and_takes_each_sessions_latest_cost(tmp_path: Path) -> None:
    ledger = tmp_path / "author-turns.jsonl"
    ledger.write_text(
        "\n".join(
            [
                _row("s1", 0.10, 2),
                _row("s1", 0.25, 1, is_error=True),
                "not json",
                json.dumps({"no": "session"}),
                _row("s2", 0.05, 3),
                "",
            ]
        )
    )
    got = _usage.read_author_ledger(ledger)
    assert got["sessions"] == {"s1", "s2"}
    # The cost is cumulative per conversation: the largest per session, summed over sessions.
    assert got["cost_usd"] == pytest.approx(0.30)
    assert got["turns"] == 6
    assert got["results"] == 3
    assert got["errors"] == 1
    assert got["usage"]["input_tokens"] == 30
    assert got["usage"]["cache_read_input_tokens"] == 300
    assert got["usage"]["modelUsage"] == {
        "claude-test": {
            "inputTokens": 30,
            "outputTokens": 12,
            "cacheReadInputTokens": 300,
            "cacheCreationInputTokens": 0,
        }
    }


def test_author_ledger_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    got = _usage.read_author_ledger(tmp_path / "absent.jsonl")
    assert got["sessions"] == set()
    assert (got["results"], got["turns"], got["errors"], got["cost_usd"]) == (0, 0, 0, 0)


def _transcript(path: Path, message_id: str, output_tokens: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "assistant",
        "message": {"id": message_id, "model": "claude-test", "usage": {"output_tokens": output_tokens}},
    }
    path.write_text(json.dumps(entry) + "\n")


def test_usage_split_reads_only_transcripts_the_ledger_does_not_cover(tmp_path: Path) -> None:
    config_dir, workspace = tmp_path / "config", tmp_path / "ws"
    folder = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    _transcript(folder / "s1.jsonl", "m1", 1000)  # covered: the ledger's figures stand for it
    _transcript(folder / "old.jsonl", "m2", 7)  # a TUI conversation: still read
    _transcript(folder / "s1" / "subagents" / "agent-a.jsonl", "m3", 50)  # a helper: always read
    ledger_file = tmp_path / "author-turns.jsonl"
    ledger_file.write_text(_row("s1", 0.1, 1) + "\n")

    split = _usage._author_usage_split(config_dir, workspace, _usage.read_author_ledger(ledger_file))
    assert split["coordinator"]["output_tokens"] == 4 + 7
    assert split["coordinator"]["input_tokens"] == 10
    assert split["helpers"]["output_tokens"] == 50

    # Without a ledger every main transcript is read, as before.
    plain = _usage._author_usage_split(config_dir, workspace)
    assert plain["coordinator"]["output_tokens"] == 1000 + 7


# -- limit resets ------------------------------------------------------------- #


def _proc(reset_at: float | None, text: str = "", seen: float | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(limit_reset_at=reset_at, limit_text=text, limit_seen_wall=seen)


#: A notice the TUI fallback would show, with a reset years ahead so the test does not age.
_NOTICE = "You've hit your limit · try again at Nov 6th, 2030 12:40 AM"
_NOTICE_AT = datetime(2030, 11, 6, 0, 40, tzinfo=UTC).timestamp()


@pytest.mark.real_reset
def test_structured_reset_is_preferred_over_the_text() -> None:
    ahead = time.time() + 3600
    proc = _proc(ahead, _NOTICE)
    assert _usage.session_reset_at(proc) == ahead
    assert _usage.session_refusal_until(proc) == ahead


@pytest.mark.real_reset
def test_without_a_structured_reset_the_text_is_read() -> None:
    proc = _proc(None, _NOTICE, seen=time.time())
    assert _usage.session_reset_at(proc) == _NOTICE_AT
    assert _usage.session_refusal_until(proc) == _NOTICE_AT


@pytest.mark.real_reset
def test_a_past_structured_reset_is_no_refusal() -> None:
    past = time.time() - 60
    proc = _proc(past, _NOTICE)
    assert _usage.session_reset_at(proc) == past
    # Already reopened: not a refusal to record, and the stale text is not consulted either.
    assert _usage.session_refusal_until(proc) is None


# -- an immediate exit at startup ------------------------------------------------ #


class _ExitedEarly:
    """A process that has exited but whose last stderr line is read only by `wait()`, as asyncio allows."""

    returncode = 1

    def __init__(self) -> None:
        self.tail: list[str] = []

    async def wait(self) -> int:
        self.tail.append("No conversation found with session ID: 0000")
        return 1


async def test_headless_start_check_reads_the_output_to_its_end_before_judging() -> None:
    proc = _ExitedEarly()
    with pytest.raises(Exception) as raised:
        await _author._check_headless_start(proc)  # type: ignore[arg-type]
    # The resume fallback matches this text; without it the run would end instead of starting fresh.
    assert _author._RECOVERABLE_START.search(str(raised.value))


# -- inbox direction --------------------------------------------------------------- #

_DIRECTION = "\n".join(f"Step {i}: a line of direction long enough to matter." for i in range(1, 15))


def _pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    inbox = tmp_path / _session_config._INBOX_DIR
    inbox.mkdir(parents=True)
    (inbox / "007.md").write_text(_DIRECTION + "\n")
    marker = inbox / "007.pending"
    marker.touch()
    return marker


async def test_inbox_direction_reaches_the_stream_session_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert len(_DIRECTION) > 400 and "\n" in _DIRECTION
    marker = _pending(monkeypatch, tmp_path)
    proc = StreamClaudeProcess(["claude"], cwd=tmp_path, env={})
    sent: list[str] = []

    async def submit(text: str, **kwargs: Any) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(proc, "submit", submit)
    await _inbox._deliver_direction(marker)(proc)
    assert len(sent) == 1
    assert sent[0].startswith("New direction arrived from Flyte (kept in .claude-flyte/inbox/007.md):")
    assert sent[0].endswith("\n\n" + _DIRECTION)
    assert not marker.exists()


async def test_inbox_direction_reaches_the_tui_as_a_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = _pending(monkeypatch, tmp_path)
    sent: list[str] = []

    class _Tui:
        async def submit(self, text: str, **kwargs: Any) -> bool:
            sent.append(text)
            return True

    await _inbox._deliver_direction(marker)(_Tui())  # type: ignore[arg-type]
    assert sent == ["New direction arrived from Flyte. Read .claude-flyte/inbox/007.md and act on it."]
    assert not marker.exists()


async def test_a_direction_the_session_could_not_take_keeps_its_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = _pending(monkeypatch, tmp_path)
    proc = StreamClaudeProcess([sys.executable, "-c", "pass"], cwd=tmp_path, env={})
    await proc.start()
    await proc.wait()
    await _inbox._deliver_direction(marker)(proc)
    # The restart path redelivers a message that never left; unlinking it would lose it.
    assert marker.exists()
    assert "[stream] not sent: the session is not running" in proc.tail


async def test_inbox_direction_reaches_a_wrapped_stream_session_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With a second engine configured the author is an AuthorSession around the stream process.
    marker = _pending(monkeypatch, tmp_path)
    inner = StreamClaudeProcess(["claude"], cwd=tmp_path, env={})
    sent: list[str] = []

    async def submit(text: str, **kwargs: Any) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(inner, "submit", submit)
    session = AuthorSession(inner, tmp_path / "pending.json")
    await _inbox._deliver_direction(marker)(session)
    assert len(sent) == 1
    assert sent[0].startswith("New direction arrived from Flyte (kept in .claude-flyte/inbox/007.md):")
    assert sent[0].endswith("\n\n" + _DIRECTION)
    assert not marker.exists()
