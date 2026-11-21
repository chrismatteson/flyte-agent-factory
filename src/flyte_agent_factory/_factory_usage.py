"""`factory.usage`: what each Claude credential the factory holds has used this week, in one chat call.

A setup-token cannot be metered by asking Anthropic: `/api/oauth/usage` needs the `user:profile` scope,
which `claude setup-token` does not grant (`_meter.SETUP_TOKEN_USAGE_SUPPORTED`). What the factory has
instead is what its own sessions saw and spent, in the reports of its runs:

- `observed`: the newest reading of the credential's windows a supervisor recorded (`_meter.from_report`),
  from its status line (`statusline`), a stream-json `rate_limit_event` (`cli-event`), response headers
  (`cli-headers`) or a refusal's text (`limit-text`). A window whose reset has passed is reported as
  expired with no percentage: it says nothing about the window now running.
- `estimate`: the tokens and dollars the factory's sessions spent on the credential since the current
  weekly window began (the observed weekly reset less seven days, else the last seven days), summed from
  each run's hourly records (`_usage_hours`, snapshot `cost.claude_usage`). It is labelled an estimate
  because it is one: it misses anything spent on the credential outside the factory, and the coordinator's
  share is priced at list price since its session reports no dollars.

Reading costs no paid call and no call to Anthropic: only the Flyte run listings and reports `factory.status`
reads too. A report written before the hourly records existed contributes nothing to the estimate and is
counted in `runs_without_hourly_records`, so an operator can tell a quiet week from an unrecorded one.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from flyte_agent_factory import (
    _factory_base,
    _factory_meters,
    _meter,
    _session_config,
    _usage_hours,
)
from flyte_agent_factory._errors import REMOTE_ERRORS

PROVIDER_USAGE = "unsupported-for-setup-token"
PROVIDER_USAGE_NOTE = (
    "Anthropic exposes no usage endpoint for setup-tokens (it needs the user:profile scope, which "
    "`claude setup-token` does not grant), so these figures come from the factory's own sessions."
)
ESTIMATE_BASIS = (
    "tokens and USD the factory's own sessions spent on this credential in the weekly window and in the last "
    "five hours: sub-agent results at the cost the CLI reported, the coordinator's replies at list price; "
    "usage outside the factory is not seen"
)
LOWER_BOUND_BASIS = " (a lower bound: some runs could not be listed or read, so the sums may miss spend)"
MEASURED_NOTE = (
    "The windows marked measured are a reading a factory session recorded; Anthropic exposes no usage "
    "endpoint for setup-tokens, so the estimate comes from the factory's own sessions."
)
WEEK_S = 7 * 86400
FIVE_HOUR_S = 5 * 3600
WINDOW_S = {"five_hour": FIVE_HOUR_S, "weekly": WEEK_S}
_SPLIT = tuple(
    f"{field}_{kind}" for field in ("usd", "calls") for kind in ("author_by_hour", "subagents_by_hour")
)
#: Runs a listing returns at most, per slot task and for the benchmark's cases. A listing that fills it may
#: have been cut short, which `_factory_meters.list_recent_runs` reports rather than undercount silently.
_LIST_LIMIT = 10000
#: The label every benchmark case run carries (`_bench_flow.LABEL_BENCH`, which imports this module's
#: importer, so it is not imported here); `test_the_benchmark_label_matches_the_benchmarks` holds them equal.
_LABEL_BENCH = "factory-bench"


async def probe_meter(account: str) -> _meter.Meter:
    """`factory.read_login_meter`'s reading of the setup-token its override mounts; never secret material.

    While Anthropic has no usage endpoint for setup-tokens (`_meter.SETUP_TOKEN_USAGE_SUPPORTED`) the honest
    answer is the newest measured reading of the same credential (a Claude Code session's), else a meter of
    source `unsupported-for-setup-token` with no window and no error: not
    `refresh-failed`, since nothing failed, and not an error, which `Meter.rejected` would read as a
    refusal. It stays a meter because `_factory_meters.login_meters` and the benchmark's quota guard parse
    the probe's output as one.
    """
    try:
        return await _meter.read_claude(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(), account, setup_token=True
        )
    except _meter.MeterError:
        if not _meter.SETUP_TOKEN_USAGE_SUPPORTED:
            # Keeps its own `observed_at`: read back through `login_meters`, the echo is no newer reading.
            return await asyncio.to_thread(find_measured_reading, account) or _meter.Meter(
                account, PROVIDER_USAGE, time.time()
            )
        # Raw exceptions may carry credentials; persist only a fixed category in run outputs.
        return _meter.Meter(account, "refresh-failed", time.time(), error="auth-or-meter-unavailable")


def find_measured_reading(account: str) -> _meter.Meter | None:
    """Return the newest measured reading of `account`'s credential from the stores and reports, else None.

    Never raises: this is advice, and a failed listing must not turn the honest answer into an error.
    """
    # Called with the bare "claude <secret>"; `token_account_secret` would want a fingerprint too.
    secret = account.removeprefix("claude ").partition(_meter.FINGERPRINT_SEP)[0]
    try:
        readings = _factory_meters.login_meters(raw=True).values()
    except REMOTE_ERRORS:
        return None
    return _factory_meters.token_meter(secret, (m for m in readings if m.measured))


def format_iso_utc(at: float | None) -> str | None:
    return None if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def list_claude_credentials() -> dict[str, str]:
    """Return secret name -> account label of every Claude credential: the work token and the benchmark's.

    The work token is labelled with the fingerprint of the token mounted into this pod, so a swapped token
    is not credited with its predecessor's readings; the benchmark's own token, when it names a different
    secret, is not mounted here and is labelled by its name until a reading names its fingerprint.
    """
    profile = _factory_base.PROFILE
    found: dict[str, str] = {}
    if secret := profile.secrets.claude_token:
        fingerprint = _factory_meters.mounted_token_fingerprint()
        found[secret] = (
            f"claude {secret}{_meter.FINGERPRINT_SEP}{fingerprint}" if fingerprint else f"claude {secret}"
        )
    if profile.bench and profile.bench.claude_token and profile.bench.claude_token not in found:
        found[profile.bench.claude_token] = f"claude {profile.bench.claude_token}"
    return found


def _belongs(account: str, secret: str, label: str) -> bool:
    """Whether a reading or record keyed `account` speaks for the credential `secret` labelled `label`."""
    if account in (label, f"claude {secret}"):
        return True
    # A label without a fingerprint (nothing mounted here) takes any fingerprint of the secret.
    return _meter.FINGERPRINT_SEP not in label and _meter.token_account_secret(account) == secret


def is_window_current(resets_at: float | None, observed_at: float, prefix: str, now: float) -> bool:
    """Whether a reading of window `prefix` still speaks for the window running now.

    A window is current while its reset is ahead. A status line may report a percentage with no reset; such a
    reading is current only while it is younger than the window, or a week-old 80% would be measured forever.
    The supervisor's store carries a window into a later reading only by the first rule
    (`_meter._keep_current_windows`).
    """
    if resets_at is not None:
        return resets_at > now
    return now - observed_at < WINDOW_S[prefix]


def summarize_observed_windows(meters: Iterable[_meter.Meter], now: float) -> dict[str, Any] | None:
    """Return one credential's readings for chat; None when there is none.

    Each window comes from the newest reading that has it, preferring one whose reset is still ahead: a
    newer five-hour-only event does not hide the week a statusLine reported a minute before. A passed
    window is expired, with no percentage. `*_pct` is the share used, `*_left_pct` what remains. A refusal
    still in force (`limited_until`) and a refused login (`login_rejected`) are named even with no window.
    """
    meters = sorted(meters, key=_read_observed_at, reverse=True)
    readings = [m for m in meters if not m.error]
    limited = [m.limited_until for m in meters if m.limited_until is not None and m.limited_until > now]
    rejected = bool(meters) and meters[0].rejected
    entry: dict[str, Any] = {}
    # Each window is dated by when it was measured, which for one carried into a later reading is earlier.
    used: list[tuple[float, str]] = []
    for prefix in _meter.WINDOW_PREFIXES:
        have = [
            (m.find_window_origin(prefix), m) for m in readings if m.find_measured_pct(prefix) is not None
        ]
        have.sort(key=lambda pair: pair[0][0], reverse=True)
        current = [
            (at, m)
            for at, m in have
            if is_window_current(getattr(m, f"{prefix}_resets_at"), at[0], prefix, now)
        ]
        origin, chosen = next(iter(current or have), (None, None))
        pct = getattr(chosen, f"{prefix}_pct", None)
        reset = getattr(chosen, f"{prefix}_resets_at", None)
        expired = origin is not None and not is_window_current(reset, origin[0], prefix, now)
        if origin is not None:
            used.append(origin)
        entry[f"{prefix}_pct"] = None if expired else pct
        entry[f"{prefix}_left_pct"] = None if expired or pct is None else max(0.0, 100.0 - pct)
        entry[f"{prefix}_resets_at"] = reset
        entry[f"{prefix}_resets_at_iso"] = format_iso_utc(reset)
        entry[f"{prefix}_expired"] = expired
        entry[f"{prefix}_observed_at"] = origin[0] if origin else None
        entry[f"{prefix}_source"] = origin[1] if origin else None
    if not used and not limited and not rejected:
        return None
    latest_at, latest_source = max(used) if used else (meters[0].observed_at, meters[0].source)
    return {
        "observed_at": latest_at,
        "observed_at_iso": format_iso_utc(latest_at),
        "age_s": round(max(0.0, now - latest_at)),
        "source": latest_source,
        **entry,
        "limited_until": max(limited, default=None),
        "limited_until_iso": format_iso_utc(max(limited, default=None)),
        "login_rejected": rejected,
    }


def expire_windows(meters: Any, now: float) -> list[Any]:
    """Return a report's meter dicts with each passed window marked `*_expired` and its percentage withheld.

    `factory.status` reads a snapshot that may be hours old: a reading of 30% for a week that has since reset
    is not the current 30%. An entry that is not a meter dict is passed through as it came; a `meters`
    value that is not a list is no meters, [].
    """
    out: list[Any] = []
    for meter in meters if isinstance(meters, list) else []:
        if isinstance(meter, dict):
            meter = dict(meter)
            for prefix in ("five_hour", "weekly"):
                reset = meter.get(f"{prefix}_resets_at")
                if not isinstance(reset, int | float) or isinstance(reset, bool):
                    reset = None
                seen = meter.get(f"{prefix}_observed_at", meter.get("observed_at"))
                if not isinstance(seen, int | float) or isinstance(seen, bool):
                    seen = now  # no usable age: nothing to call stale
                # A reset-less window with no percentage has nothing to withhold.
                expired = (reset is not None or meter.get(f"{prefix}_pct") is not None) and not (
                    is_window_current(reset, seen, prefix, now)
                )
                if expired:  # a current window keeps the meter's shape as `factory.status` always gave it
                    meter[f"{prefix}_expired"] = True
                    meter[f"{prefix}_pct"] = None
        out.append(meter)
    return out


def choose_estimate_window(reading: Mapping[str, Any] | None, now: float) -> tuple[float, float, str]:
    """Return the estimate's window and how it was chosen: the observed week, else the last seven days.

    A passed weekly reset is not rolled forward by whole weeks: a reading that old says nothing about the
    window now running, which is what `summarize_observed_windows` reports for it and what
    `test_a_window_whose_reset_passed_is_expired_not_current` holds the estimate to as well.
    """
    reset = reading.get("weekly_resets_at") if reading else None
    if reset is not None and now < reset <= now + WEEK_S:
        return reset - WEEK_S, now, "observed weekly reset less 7 days"
    return now - WEEK_S, now, "last 7 days (no current weekly reset observed)"


def _merge_max(into: dict[str, dict[str, float]], hours: _usage_hours.Hours) -> None:
    """Fold one run's hours into its project's, keeping each hour's larger bucket.

    A resumed project restores the same transcript and ledger in its next run, whose report repeats the
    hours the last one already reported, now possibly grown. The larger bucket is the later count; adding
    them would count the week twice.
    """
    for key, bucket in hours.items():
        held = into.get(key)
        if held is None or _measure_bucket(bucket) > _measure_bucket(held):
            into[key] = bucket


def _measure_bucket(bucket: Mapping[str, float]) -> tuple[float, float]:
    return bucket.get("calls", 0.0), sum(bucket.get(field, 0.0) for field in _session_config._USAGE_FIELDS)


def _sum_window(
    by_project: Mapping[tuple[str, str], Mapping[str, dict[str, float]]], start: float, end: float
) -> dict[str, float]:
    """Sum the hours that touch `[start, end]`: the token fields, and `usd` and `calls` per kind of hours."""
    sums = dict.fromkeys((*_session_config._USAGE_FIELDS, *_SPLIT), 0.0)
    for (_, kind), hours in by_project.items():
        for key, bucket in hours.items():
            begins = _usage_hours.parse_hour_start(key)
            if begins is None or begins + _usage_hours.HOUR_S <= start or begins > end:
                continue
            for field in _session_config._USAGE_FIELDS:
                sums[field] += bucket.get(field, 0.0)
            sums[f"usd_{kind}"] += bucket.get("usd", 0.0)
            sums[f"calls_{kind}"] += bucket.get("calls", 0.0)
    return sums


def estimate(
    secret: str,
    label: str,
    usage: Iterable[tuple[str, dict[str, Any] | None]],
    start: float,
    end: float,
    *,
    lower_bound: bool = False,
) -> dict[str, Any]:
    """Return the labelled estimate: the credential's hourly records summed over `[start, end]`.

    An hour counts whole when any of it is in the window: records are hourly, and the window's own start is
    a reset Anthropic places on the hour. `five_hour` sums the same buckets over the last five hours.
    `lower_bound` says some runs were not listed or read, so the sums may miss spend.
    """
    by_project: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    runs_counted = runs_without = 0
    for project, record in usage:
        if record is None:
            runs_without += 1
            continue
        if not _belongs(str(record.get("account") or ""), secret, label):
            continue
        runs_counted += 1
        for kind in ("author_by_hour", "subagents_by_hour"):
            # every bucket is validated again: a report is a page anyone with the run could have written.
            hours = _usage_hours.validate_recent_hours(record.get(kind), end)
            _merge_max(by_project.setdefault((project, kind), {}), hours)
    week = _sum_window(by_project, start, end)
    five = _sum_window(by_project, end - FIVE_HOUR_S, end)
    return {
        "label": "estimate",
        "basis": ESTIMATE_BASIS + (LOWER_BOUND_BASIS if lower_bound else ""),
        "lower_bound": lower_bound,
        "window_start": start,
        "window_start_iso": format_iso_utc(start),
        "window_end": end,
        "window_end_iso": format_iso_utc(end),
        **{field: int(week[field]) for field in _session_config._USAGE_FIELDS},
        "usd": round(week["usd_author_by_hour"] + week["usd_subagents_by_hour"], 4),
        "usd_subagents_cli_reported": round(week["usd_subagents_by_hour"], 4),
        "usd_author_list_price": round(week["usd_author_by_hour"], 4),
        "subagent_calls": int(week["calls_subagents_by_hour"]),
        "author_replies": int(week["calls_author_by_hour"]),
        "five_hour": {
            "window_start": end - FIVE_HOUR_S,
            "window_start_iso": format_iso_utc(end - FIVE_HOUR_S),
            **{field: int(five[field]) for field in _session_config._USAGE_FIELDS},
            "usd": round(five["usd_author_by_hour"] + five["usd_subagents_by_hour"], 4),
            "subagent_calls": int(five["calls_subagents_by_hour"]),
            "author_replies": int(five["calls_author_by_hour"]),
        },
        "runs_counted": runs_counted,
        "runs_without_hourly_records": runs_without,
    }


def _has_records(estimated: Mapping[str, Any]) -> bool:
    """Whether the factory recorded any tokens, dollars or calls for the credential in either window."""
    keys = (*_session_config._USAGE_FIELDS, "usd", "subagent_calls", "author_replies")
    return any(estimated[key] or estimated["five_hour"][key] for key in keys)


def build_usage_report(now: float | None = None) -> dict[str, Any]:
    """Return `factory.usage`'s answer: per Claude credential, its observed windows and the estimate.

    Never raises for a remote failure: what could not be read is named, by error class only, in `errors`.
    """
    now = time.time() if now is None else now
    errors: list[str] = []
    since = datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=_usage_hours.KEEP_S)
    runs = _factory_meters.list_recent_runs(since, _LABEL_BENCH, _LIST_LIMIT, errors)
    meters, usage = _factory_meters.read_run_reports(runs, errors)
    # Any listing or report that failed, or a listing cut short, means the sums may miss spend.
    lower_bound = bool(errors)
    entries = []
    for secret, label in list_claude_credentials().items():
        mine = [m for m in meters if _belongs(m.account, secret, label) and m.source != "refresh-failed"]
        reading = summarize_observed_windows(mine, now)
        start, end, chosen = choose_estimate_window(reading, now)
        estimated = {**estimate(secret, label, usage, start, end, lower_bound=lower_bound), "window": chosen}
        # A window whose reset has passed carries no percentage, so a stale reading is not a measurement.
        measured = reading is not None and any(
            reading[f"{p}_pct"] is not None for p in ("five_hour", "weekly")
        )
        entries.append(
            {
                "account": max(mine, key=_read_observed_at).account if mine else label,
                "kind": "measured" if measured else "estimate" if _has_records(estimated) else "unknown",
                "measured": measured,
                "observed": reading,
                "provider_usage": PROVIDER_USAGE,
                "provider_usage_note": MEASURED_NOTE if measured else PROVIDER_USAGE_NOTE,
                "estimate": estimated,
            }
        )
    return {
        "credentials": entries,
        "generated_at": now,
        "generated_at_iso": format_iso_utc(now),
        "errors": errors,
    }


def _read_observed_at(meter: _meter.Meter) -> float:
    return meter.observed_at
