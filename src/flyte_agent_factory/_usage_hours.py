"""Claude usage per credential and UTC hour: what `factory.usage` estimates a credential's week from.

A setup-token has no provider usage endpoint, so when no status-line reading exists the only figure there
is, is what the factory itself spent on the credential. The ledger (`usage.json`, keyed `role@round`)
keeps no time and no account, and a weekly window starts at an hour of Anthropic's choosing, so this keeps
the same tokens again by hour and account:

- sub-agent results (`record_subagent`, from `_usage._record_usage`): their tokens and the cost the CLI
  reported, in `STATE_ROOT/review/usage_hours.json`, which survives a restart like the ledger;
- the coordinator's own tokens, priced at list price so that both sources count alike: the turns the
  structured session reported (`add_author_turn`, from `_usage.read_author_ledger`) by the time they ended,
  and the transcripts of any conversation that ledger does not cover (`add_author_entry`, from
  `_usage._author_usage_split`) by each reply's timestamp.

Each run's report snapshot carries both under `cost.claude_usage` (`build_claude_usage`), which is where a
factory task reads them from across pods. Only the last `KEEP_S` is kept: the estimate needs a week, and
the report is rewritten on every change. Hours are UTC, keyed `YYYY-MM-DDTHH`; a bucket holds the four
token fields of `_session_config._USAGE_FIELDS`, `usd` and `calls` (sub-agent results, or the coordinator's
replies).
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flyte_agent_factory import _session_config
from flyte_agent_factory._bridge_common import _write_atomic

#: Buckets older than this are dropped: a weekly window plus a day of slack.
KEEP_S = 8 * 86400
HOUR_S = 3600.0
_HOUR_FORMAT = "%Y-%m-%dT%H"
_BUCKET_FIELDS = (*_session_config._USAGE_FIELDS, "usd", "calls")

Hours = dict[str, dict[str, float]]


def format_hour_key(at: float) -> str:
    """Return the UTC hour `at` falls in, as `YYYY-MM-DDTHH`."""
    return datetime.fromtimestamp(at, timezone.utc).strftime(_HOUR_FORMAT)


def parse_hour_start(key: str) -> float | None:
    """Return the epoch second an hour key starts at, or None for a key that is not one."""
    try:
        return datetime.strptime(key, _HOUR_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def add(hours: Hours, at: float, tokens: Mapping[str, Any], usd: float) -> None:
    """Add one call's tokens and dollars to the bucket of the hour `at` falls in."""
    bucket = hours.setdefault(format_hour_key(at), dict.fromkeys(_BUCKET_FIELDS, 0.0))
    for field in _session_config._USAGE_FIELDS:
        bucket[field] += _coerce_count(tokens.get(field))
    bucket["usd"] += _coerce_count(usd)
    bucket["calls"] += 1


def _coerce_count(value: object) -> float:
    """Return a non-negative finite number, else 0: a malformed count must not poison a week's sum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, float(value))


def validate_recent_hours(hours: object, now: float) -> Hours:
    """Return the well-formed buckets of `hours` that start within `KEEP_S` of `now`."""
    kept: Hours = {}
    if not isinstance(hours, Mapping):
        return kept
    for key, bucket in hours.items():
        start = parse_hour_start(key)
        if start is None or start < now - KEEP_S or not isinstance(bucket, Mapping):
            continue
        kept[key] = {field: _coerce_count(bucket.get(field)) for field in _BUCKET_FIELDS}
    return kept


def locate_ledger() -> Path:
    """Return where the supervisor keeps its sub-agents' hourly usage (`usage_hours.json`)."""
    return _session_config.STATE_ROOT / "review" / "usage_hours.json"


def read_ledger(now: float) -> dict[str, Hours]:
    """Return the sub-agent ledger, account -> hours, within `KEEP_S`; {} when it is missing or unreadable."""
    try:
        saved = json.loads(locate_ledger().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {
        account: validate_recent_hours(hours, now)
        for account, hours in saved.items()
        if isinstance(account, str)
    }


def record_subagent(rec: Mapping[str, Any], account: str, at: float) -> None:
    """Add one sub-agent result to the hourly ledger under `account`; a Codex result is not Claude usage.

    Claude results name no engine (the trailer predates engines) or `claude`; Codex results say `codex`.
    """
    if rec.get("engine") == "codex":
        return
    ledger = read_ledger(at)
    add(ledger.setdefault(account, {}), at, rec, _coerce_count(rec.get("cost_usd")))
    # Like the ledger's own write: accounting must not fail the call it accounts for.
    with contextlib.suppress(OSError):
        locate_ledger().parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(locate_ledger(), ledger)


def add_author_entry(hours: Hours, entry: Mapping[str, Any], usage: Mapping[str, Any], model: str) -> None:
    """Add one coordinator reply (a transcript line and its `message.usage`) by its timestamp, at list price.

    A line without a readable timestamp is left out of the hours; it still counts in the run's totals.
    """
    stamp = entry.get("timestamp")
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return
    # Claude Code writes UTC with a `Z`; a stamp without an offset is read as UTC too, not as pod-local time.
    at = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    add_author_turn(hours, at, usage, model)


def add_author_turn(hours: Hours, at: float, usage: Mapping[str, Any], model: str) -> None:
    """Add coordinator tokens used at Unix time `at`, at list price: a transcript reply or a ledger turn."""
    tokens = {field: _coerce_count(usage.get(field)) for field in _session_config._USAGE_FIELDS}
    add(hours, at, tokens, _session_config._list_price_usd(tokens, model) or 0.0)


def build_claude_usage(account: str, author_hours: Hours, now: float) -> dict[str, Any]:
    """Return the snapshot's `cost.claude_usage`: this run's Claude credential and its recent hours."""
    return {
        "account": account,
        "author_by_hour": validate_recent_hours(author_hours, now),
        "subagents_by_hour": read_ledger(now).get(account, {}),
    }
