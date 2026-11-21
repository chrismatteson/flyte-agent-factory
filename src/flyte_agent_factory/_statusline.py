"""The coordinator's Claude Code status line: the one place a setup-token's usage windows are visible.

Anthropic's usage endpoint (`/api/oauth/usage`) needs the `user:profile` scope, which a setup-token lacks, and
headless `claude -p --output-format json` results carry no windows. The interactive TUI does: Claude Code
passes its `statusLine` command a JSON object on stdin whose `rate_limits.seven_day` (and `five_hour`, while
the API reports it) hold `used_percentage` (0-100) and `resets_at` (epoch seconds). So the supervisor's
session is configured with this command (`settings_extra`), which keeps those four numbers, and nothing else
of that payload, in a small capture file; the supervisor ingests it into its meters
(`_usage._ingest_statusline`).

The capture sits in the session's `CLAUDE_CONFIG_DIR`: a sealed session may write only its workspace, the
bridge and that directory, and the command runs inside the seal. For the same reason the supervisor
treats the file as untrusted input and keeps only fields that validate (`read_capture`): a session that
rewrote it can make a meter wrong, which is advice, but cannot get anything else read.

Run by Claude Code after each message (debounced), so it must be cheap: stdlib and `_bridge_common` only
(`_meter` alone takes seconds to import), no network, and it never fails: a bad payload prints a placeholder
and exits 0. Like `_context_guard` it is run by `python -m`, so it must stay a statically imported `.py`.
"""

from __future__ import annotations

import contextlib
import json
import math
import shlex
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flyte_agent_factory._bridge_common import _write_atomic

#: The capture file's name inside the session's `CLAUDE_CONFIG_DIR`.
CAPTURE_NAME = "statusline-meter.json"
#: Rewrite an unchanged capture at most this often: the TUI calls the command after every message.
REWRITE_EVERY_S = 30.0
#: A used share above this is not a reading. Over 100 is possible (an overage) but not by ten times.
MAX_PCT = 1000.0
#: Reset times outside these bounds (2001-09-09 .. 2286-11-20, epoch seconds) are not epoch seconds:
#: milliseconds, or garbage.
_MIN_EPOCH_S = 1e9
_MAX_EPOCH_S = 1e10
#: The statusLine payload is a few KB; never read an unbounded stdin or capture file.
_MAX_PAYLOAD_BYTES = 1 << 20
#: statusLine window name -> the `Meter` field prefix it fills.
WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly"}
#: Every field a capture holds: `write_capture` writes no other and `read_capture` returns no other.
FIELDS = ("observed_at", "five_hour_pct", "five_hour_resets_at", "weekly_pct", "weekly_resets_at")


def locate_capture(config_dir: Path) -> Path:
    """Return where the command writes, for a session whose `CLAUDE_CONFIG_DIR` is `config_dir`."""
    return config_dir / CAPTURE_NAME


def build_command(capture: Path) -> str:
    """Return the statusLine command line that runs this module to write `capture`."""
    # `sys.executable`, so the module imports in the pod whatever interpreter the supervisor runs on.
    return f"{shlex.quote(sys.executable)} -m flyte_agent_factory._statusline {shlex.quote(str(capture))}"


def settings_extra(base: dict[str, Any] | None, capture: Path) -> dict[str, Any]:
    """Return `base` (the session's other `write_settings` extra, or None) with the status line added.

    `statusLine` is a top-level key nothing else sets, so the context guard's and the seal's hooks stay as
    they are.
    """
    return {**(base or {}), "statusLine": {"type": "command", "command": build_command(capture)}}


def _parse_pct(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= MAX_PCT else None


def parse_epoch_s(value: object) -> float | None:
    """Return `value` as epoch seconds when it plausibly is one (see `_MIN_EPOCH_S`), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and _MIN_EPOCH_S <= value <= _MAX_EPOCH_S else None


def extract_windows(payload: object) -> dict[str, float]:
    """Return the valid `{five_hour,weekly}_{pct,resets_at}` of a statusLine payload; {} when there are none.

    A window's reset is kept only beside its percentage: a reset alone says nothing about how much is used.
    """
    limits = payload.get("rate_limits") if isinstance(payload, Mapping) else None
    found: dict[str, float] = {}
    if not isinstance(limits, Mapping):
        return found
    for name, prefix in WINDOWS.items():
        window = limits.get(name)
        if not isinstance(window, Mapping) or (pct := _parse_pct(window.get("used_percentage"))) is None:
            continue
        found[f"{prefix}_pct"] = pct
        if (reset := parse_epoch_s(window.get("resets_at"))) is not None:
            found[f"{prefix}_resets_at"] = reset
    return found


def read_capture(path: Path) -> dict[str, float]:
    """Return the capture's validated fields, or {} when it is missing, unreadable or holds no window."""
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.read(_MAX_PAYLOAD_BYTES))
    except (OSError, ValueError, RecursionError):  # the session can write this file: nesting is input too
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, float] = {}
    for prefix in WINDOWS.values():
        if (pct := _parse_pct(data.get(f"{prefix}_pct"))) is not None:
            found[f"{prefix}_pct"] = pct
            if (reset := parse_epoch_s(data.get(f"{prefix}_resets_at"))) is not None:
                found[f"{prefix}_resets_at"] = reset
    observed = parse_epoch_s(data.get("observed_at"))
    return {"observed_at": observed, **found} if found and observed is not None else {}


def write_capture(path: Path, found: Mapping[str, float], now: float) -> bool:
    """Write `found` with `observed_at` unless the file already says the same and is under 30 s old.

    Returns whether it wrote. Only `FIELDS` are written, all numbers: `found` comes from `extract_windows`.
    """
    if not found:
        return False
    previous = read_capture(path)
    same = {k: v for k, v in previous.items() if k != "observed_at"} == dict(found)
    if same and 0 <= now - previous["observed_at"] < REWRITE_EVERY_S:
        return False
    _write_atomic(path, {k: v for k, v in {"observed_at": now, **found}.items() if k in FIELDS})
    return True


def _format_reset_clock(at: float | None) -> str:
    return "" if at is None else datetime.fromtimestamp(at, timezone.utc).strftime(" (resets %a %H:%MZ)")


def format_line(found: Mapping[str, float]) -> str:
    """`5h 12% · week 30% (resets Fri 03:00Z)`; `usage ?` when the session reports no window."""
    parts = [
        f"{label} {found[f'{prefix}_pct']:.0f}%"
        + (_format_reset_clock(found.get(f"{prefix}_resets_at")) if long else "")
        for label, prefix, long in (("5h", "five_hour", False), ("week", "weekly", True))
        if f"{prefix}_pct" in found
    ]
    return " · ".join(parts) or "usage ?"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    found: dict[str, float] = {}
    # Never fail and never block the TUI: bad stdin (undecodable, or nested past the parser's recursion
    # limit), a payload of the wrong shape or an unwritable capture leaves the line at its placeholder.
    # Narrow on purpose (tests/test_narrow_excepts.py).
    with contextlib.suppress(OSError, ValueError, RecursionError):
        found = extract_windows(json.loads(sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES) or b"null"))
        if argv and found:
            write_capture(Path(argv[0]), found, time.time())
    print(format_line(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
