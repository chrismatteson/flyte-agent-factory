"""Usage limits shared between pods: who noted a limit, and who has seen it end.

A pod remembers a refusal's reset time (`_meter.MeterStore.note_limit`); so that every pod on one token learns
when one of them, or the owner, finds that limit over, each pod shares what it knows. Under
`{state uri}/limits/` a pod publishes the limit it noted per account, and writes `cleared_at` when a call on
that account succeeds although a limit is on record. Pods that remember a limit read those files now and
then and drop a limit noted before `cleared_at`. A person's reset is `engine-<engine>.json`, for every
account of that engine.

Only account hashes and times are stored, never a token or a secret name. The store is advice: a read or
write that fails is logged (once per interval; a missing file is no failure) and never fails or defers a
call. Callers construct a `LimitShare` only when a state uri is set (`_usage.open_meters`, `_factory_meters`).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from typing import Any

from flyte_agent_factory import _queue, _timeline
from flyte_agent_factory._errors import REMOTE_ERRORS

#: How often a pod holding a remembered limit looks for another pod's clear.
LIMIT_SHARE_POLL_S = 60.0
#: How long a record read on a successful call is trusted. Short: another pod may note a limit just after the
#: read, and a call (seconds to minutes) that succeeds after that must still publish the clear.
_CACHE_S = 5.0
#: The record fields that are times. Anyone with the store can write any JSON there: a `null` is absent;
#: a field of another type is malformed and read as absent.
_TIMES = ("cleared_at", "noted_at", "limited_until")


def _is_time(value: Any) -> bool:
    """Return whether `value` is a finite number that fits a float; a `bool` or an over-wide int is not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:  # a JSON integer wider than a double
        return False


class LimitShare:
    """The shared limit records under one `[state] uri`."""

    def __init__(self, uri: str, clock: Callable[[], float] = time.time) -> None:
        self.uri, self.clock = uri.rstrip("/"), clock
        self.poll_s = LIMIT_SHARE_POLL_S
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._logged = float("-inf")

    def _account_path(self, account: str) -> str:
        return f"{self.uri}/limits/{hashlib.sha256(account.encode()).hexdigest()[:16]}.json"

    def _engine_path(self, engine: str) -> str:
        return f"{self.uri}/limits/engine-{engine}.json"

    def _failed(self, what: str, exc: Exception) -> None:
        # One line per interval: a store that is down would otherwise log on every call.
        if self.clock() - self._logged >= self.poll_s:
            self._logged = self.clock()
            _timeline._log_line(f"[limits] shared {what} failed: {type(exc).__name__}: {exc}", err=True)

    def _read(self, path: str, *, fresh: bool = False) -> dict[str, Any]:
        cached = self._cache.get(path)
        if cached and not fresh and self.clock() - cached[0] < _CACHE_S:
            return cached[1]
        try:
            record = _queue.read_json_or_raise(path)
        except REMOTE_ERRORS as exc:  # advice only: a storage failure must not fail a call
            self._failed("read", exc)
            return cached[1] if cached else {}
        # A null time is absent, as `publish` writes it; anything else that is not a number is malformed.
        present = {k: v for k, v in record.items() if k not in _TIMES or v is not None}
        valid = {k: v for k, v in present.items() if k not in _TIMES or _is_time(v)}
        if len(valid) != len(present):
            self._failed("read", ValueError("malformed record"))
        self._cache[path] = (self.clock(), valid)
        return valid

    def _write(self, path: str, record: dict[str, Any]) -> bool:
        """Return whether the record was written; a failure is logged, not raised (as `_read`)."""
        try:
            _queue.write_json(path, record)
        except REMOTE_ERRORS as exc:
            self._failed("write", exc)
            return False
        self._cache[path] = (self.clock(), record)
        return True

    def publish(self, engine: str, account: str, limited_until: float | None, noted_at: float | None) -> None:
        """Record the limit this pod just noted for `account`, keeping the account's last `cleared_at`."""
        path = self._account_path(account)
        cleared_at = self._read(path, fresh=True).get("cleared_at") or 0.0
        self._write(
            path,
            {
                "engine": engine,
                "limited_until": limited_until,
                "noted_at": noted_at,
                "cleared_at": cleared_at,
            },
        )

    def succeeded(self, engine: str, account: str, at: float) -> None:
        """Record a call begun at `at` succeeded, if a limit noted by then is on record, uncleared."""
        path = self._account_path(account)
        record = self._read(path)
        noted_at = record.get("noted_at") or 0.0
        if record.get("limited_until") and noted_at <= at and (record.get("cleared_at") or 0.0) < noted_at:
            self._write(path, {**record, "engine": engine, "cleared_at": at})

    def clear_engine(self, engine: str, at: float | None = None) -> bool:
        """Record a person's reset: every limit of `engine` noted by `at` is over, whatever the account.

        Returns whether it was written: `factory.clear_limit` tells the person when it was not.
        """
        return self._write(self._engine_path(engine), {"cleared_at": self.clock() if at is None else at})

    def read_cleared_at(self, engine: str, account: str) -> float:
        """Return the latest clear published for `account` or its engine (0 for none), read afresh."""
        paths = (self._account_path(account), self._engine_path(engine))
        return max(float(self._read(path, fresh=True).get("cleared_at") or 0.0) for path in paths)
