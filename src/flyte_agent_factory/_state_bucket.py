"""The factory's state in an object store: S3, GCS, an S3-compatible server, or a directory on a volume.

See `docs/state-store.md`. Two layers:

- An *adapter* is the few object operations the store needs (`list`, `head`, `get`, and writes that are
  create-only, if-match or unconditional). `ObstoreAdapter` here runs them over obstore (one dependency for
  every cloud, credentials from the environment or the pod's identity); `_state_volume` has one over a
  directory.
- `BucketStore` is the compare-and-swap logic on top, written once for every adapter.

A key's *tag* is the object's etag (and generation/version where the store has one). Object stores accept a
write that says "only if the tag is still X" (`If-Match`) or "only if absent" (`If-None-Match`), so a
transaction is: list, decide, write each changed key under the tag it was read with; if a write is refused
somebody else got there first, so list again and decide again, after a random pause.

Not covered, and why it is acceptable:

- **Delete is unconditional.** Object stores have no conditional delete. No transaction deletes a key that
  writers contend for: `_queue.release_lease`, the one that would, writes an expired lease instead
  (`Store.conditional_delete`). The deletes that remain are markers and requests only their owner removes.
- **A transaction of several keys is not atomic across keys.** Keys are written one by one in sorted order,
  each conditionally; if the second is refused, the first stays written and `decide` runs again against the
  new state.
  Only the moves (`ops/migrate_state.py`, `ops/move_state.py`) change several keys at once; both are
  idempotent.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
import random
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

import obstore
from obstore.exceptions import AlreadyExistsError, GenericError, NotFoundError, PreconditionError
from obstore.exceptions import BaseError as ObstoreError

from flyte_agent_factory._profile_state import has_credentials_or_query, plain_http_is_allowed
from flyte_agent_factory._state_store import (
    _BACKOFF,
    _BACKOFF_CAP,
    _RETRIES,
    SnapshotView,
    StateError,
    StateFS,
    _check_key,
)

T = TypeVar("T")
log = logging.getLogger(__name__)

STATE = (
    "state/"  # state keys live under this prefix, and audit records beside it, so `list` never shows audit
)
AUDIT = "audit/"
#: What a listing of ~1000 state files caches: a snapshot reads lazily, and most decisions read one key.
_CACHE_ENTRIES = 256
#: obstore raises its own NotFoundError for some stores and the builtin FileNotFoundError for others (memory).
_NOT_FOUND = (NotFoundError, FileNotFoundError)
#: S3 answers two writers racing on one key with 409 "ConditionalRequestConflict"; obstore has no class, so
#: the message is matched on its status phrasing (`status 409`, `status code: 409`) or the error's own code
#: element, not on a bare "409": the message also carries the request URL, and a key may contain 409.
_CONFLICT = re.compile(r"\bstatus(?: code)?:? 409\b|<Code>ConditionalRequestConflict</Code>")


@dataclass(frozen=True)
class Tag:
    """What a conditional write checks: the etag and, where there is one, the version (GCS generation)."""

    e_tag: str | None = None
    version: str | None = None

    def mode(self) -> dict[str, str]:
        """Return obstore's `UpdateVersion`: only the fields the store gave (obstore rejects None)."""
        return {k: v for k, v in (("e_tag", self.e_tag), ("version", self.version)) if v is not None}


class Adapter(Protocol):
    """The object operations `BucketStore` uses. Paths are relative to the store's root."""

    def list(self, prefix: str) -> dict[str, Tag]:
        """Return every object under `prefix` (a "directory", ending in "/") with its tag."""
        ...

    def list_dirs(self, prefix: str) -> builtins.list[str]:
        """Return the immediate sub-directories of `prefix`, as full paths ending in "/"."""
        ...

    def head(self, path: str) -> Tag | None: ...
    def get(self, path: str) -> tuple[bytes, Tag] | None: ...
    def create(self, path: str, body: bytes) -> bool:
        """Write only if absent; False when the object exists."""
        ...

    def replace(self, path: str, body: bytes, tag: Tag) -> bool:
        """Write only if the object still has `tag`; False when it changed or is gone."""
        ...

    def put(self, path: str, body: bytes) -> None: ...
    def delete(self, path: str) -> None:
        """Delete; not an error when it is already gone."""
        ...


class ObstoreAdapter:
    """An `Adapter` over any obstore store."""

    def __init__(self, store: obstore.store.ObjectStore) -> None:
        self._store = store

    def list(self, prefix: str) -> dict[str, Tag]:
        found = {}
        for batch in obstore.list(self._store, prefix.rstrip("/") or None):
            for meta in batch:
                found[meta["path"]] = Tag(meta.get("e_tag"), meta.get("version"))
        return found

    def list_dirs(self, prefix: str) -> builtins.list[str]:
        listing = obstore.list_with_delimiter(self._store, prefix.rstrip("/") or None)
        return sorted(p.rstrip("/") + "/" for p in listing["common_prefixes"])

    def head(self, path: str) -> Tag | None:
        try:
            meta = obstore.head(self._store, path)
        except _NOT_FOUND:
            return None
        return Tag(meta.get("e_tag"), meta.get("version"))

    def get(self, path: str) -> tuple[bytes, Tag] | None:
        try:
            result = obstore.get(self._store, path)
        except _NOT_FOUND:
            return None
        meta = result.meta
        return result.bytes().to_bytes(), Tag(meta.get("e_tag"), meta.get("version"))

    def create(self, path: str, body: bytes) -> bool:
        return self._put(path, body, "create")

    def replace(self, path: str, body: bytes, tag: Tag) -> bool:
        return self._put(path, body, tag.mode())

    def _put(self, path: str, body: bytes, mode: object) -> bool:
        try:
            obstore.put(self._store, path, body, mode=mode)  # type: ignore[arg-type]
        except (AlreadyExistsError, PreconditionError, *_NOT_FOUND):
            return False  # NotFound: an if-match write to an object that was deleted meanwhile
        except GenericError as exc:
            if _CONFLICT.search(str(exc)):
                return False
            raise
        return True

    def put(self, path: str, body: bytes) -> None:
        obstore.put(self._store, path, body)

    def delete(self, path: str) -> None:
        try:
            obstore.delete(self._store, path)
        except _NOT_FOUND:
            pass


class _SerialAdapter(ObstoreAdapter):
    """An `ObstoreAdapter` whose store calls run one at a time, for obstore's MemoryStore.

    obstore 0.11's MemoryStore deadlocks when threads put and list at once (reproduced with obstore alone),
    so its calls are serialised; everything above them (decide, the conditional writes) still races.
    """

    def __init__(self, store: obstore.store.ObjectStore) -> None:
        super().__init__(store)
        self._serial = threading.Lock()
        for name in ("list", "list_dirs", "head", "get", "_put", "put", "delete"):
            setattr(self, name, self._one_at_a_time(getattr(self, name)))

    def _one_at_a_time(self, method: Callable[..., T]) -> Callable[..., T]:
        def call(*args: object, **kwargs: object) -> T:
            with self._serial:
                return method(*args, **kwargs)

        return call


@dataclass
class Stats:
    """Counters for the benchmark harness; read them any time, they are only ever incremented."""

    writes: int = 0  # state writes that changed something (one per `write`/`transact` that wrote)
    lost_races: int = 0  # conditional writes an object store refused because another writer got there first
    objects_written: int = 0  # state objects put (audit records not counted)
    unreadable_audit: int = 0  # audit records `recent_changes` skipped because they did not parse
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(
        self, *, writes: int = 0, lost_races: int = 0, objects_written: int = 0, unreadable_audit: int = 0
    ) -> None:
        with self._lock:
            self.unreadable_audit += unreadable_audit
            self.writes += writes
            self.lost_races += lost_races
            self.objects_written += objects_written


class _Snapshot:
    """The state keys and their tags at one listing; bodies are fetched on first read."""

    def __init__(self, store: BucketStore, tags: dict[str, Tag]) -> None:
        self._store = store
        self._tags = tags
        digest = hashlib.sha256()
        for key in sorted(tags):
            digest.update(f"{key}\0{tags[key].e_tag}\0{tags[key].version}\n".encode())
        self.tip = digest.hexdigest()[:16]

    def read(self, key: str) -> bytes | None:
        tag = self._tags.get(_check_key(key))
        if tag is None:
            return None
        body = self._store._body(key, tag)
        if body is None:  # deleted since the listing
            self._tags.pop(key, None)
            return None
        # The object may have changed since it was listed: the body read is what `decide` saw, so a write
        # based on it must carry the tag it came with, not the older listed one.
        self._tags[key] = body[1]
        return body[0]

    def read_many(self, keys: builtins.list[str]) -> dict[str, bytes]:
        out = {}
        for key in keys:
            body = self.read(key)
            if body is not None:
                out[key] = body
        return out

    def exists(self, key: str) -> bool:
        return _check_key(key) in self._tags

    def list(self, prefix: str = "") -> builtins.list[str]:
        return _under(self._tags, prefix)


def _under(keys: Mapping[str, object] | builtins.list[str], prefix: str) -> builtins.list[str]:
    """Return the sorted keys under `prefix`: "queue/a" matches "queue/a/x", not "queue/ab/x"."""
    if not prefix.strip("/"):
        return sorted(keys)
    prefix = _check_key(prefix.rstrip("/"))
    return sorted(k for k in keys if k == prefix or k.startswith(prefix + "/"))


class BucketStore:
    """The state store over an `Adapter`: the compare-and-swap logic for every object backend."""

    conditional_delete = False

    def __init__(
        self,
        adapter: Adapter,
        *,
        retries: int = _RETRIES,
        retention_days: int = 30,
        host: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.retries = retries
        self.retention_days = retention_days
        self.stats = Stats()
        self._host = host or socket.gethostname()
        self._cache: dict[tuple[str, Tag], bytes] = {}
        self._cache_lock = threading.Lock()
        self._pruned = False

    # -- reads ---------------------------------------------------------------------------------------

    def read(self, key: str, *, fresh: bool = False) -> bytes | None:
        found = self.adapter.get(STATE + _check_key(key))
        return None if found is None else found[0]

    def exists(self, key: str, *, fresh: bool = False) -> bool:
        return self.adapter.head(STATE + _check_key(key)) is not None

    def list(self, prefix: str = "", *, fresh: bool = False) -> builtins.list[str]:
        return self.snapshot().list(prefix)

    def snapshot(self, *, fresh: bool = False) -> SnapshotView:
        """List the state prefix now. Always current: an object store has no stale cache to refresh."""
        return self._snapshot()

    def _snapshot(self) -> _Snapshot:
        listed = self.adapter.list(STATE)
        return _Snapshot(self, {p[len(STATE) :]: tag for p, tag in listed.items() if p.startswith(STATE)})

    def _body(self, key: str, tag: Tag) -> tuple[bytes, Tag] | None:
        """Return the body of `key` and its tag, from the cache when the listed tag is the one cached."""
        with self._cache_lock:
            cached = self._cache.get((key, tag))
        if cached is not None:
            return cached, tag
        found = self.adapter.get(STATE + key)
        if found is None:
            return None
        with self._cache_lock:
            if len(self._cache) >= _CACHE_ENTRIES:
                self._cache.clear()  # simple and bounded; a miss is one GET
            self._cache[key, found[1]] = found[0]
        return found

    # -- writes --------------------------------------------------------------------------------------

    def write(self, changes: Mapping[str, bytes | None], message: str) -> bool:
        """Put (or delete, for None) each key unconditionally, skipping unchanged ones; False if none changed.

        Unconditional is right for a plain write: a writer of a key has no read to protect, and last writer
        wins exactly as it does on the git backend's re-applied commit.
        """
        for key in changes:
            _check_key(key)
        snap = self._snapshot()
        changed = []
        for key, body in sorted(changes.items()):
            if body is None:
                if not snap.exists(key):
                    continue
                self.adapter.delete(STATE + key)
            elif body != snap.read(key):
                self.adapter.put(STATE + key, body)
            else:
                continue
            changed.append((key, body))
        if changed:
            self._record_audit(changed, message, "write")
        return bool(changed)

    def transact(
        self, decide: Callable[[SnapshotView], tuple[Mapping[str, bytes | None], T]], message: str
    ) -> T:
        """Apply what `decide` returns if nothing it read has changed; else decide again on the new state.

        `decide` must be pure apart from what it returns: it can run several times.
        """
        for attempt in range(1, self.retries + 1):
            if attempt > 1:
                time.sleep(random.uniform(0, min(_BACKOFF_CAP, _BACKOFF * 2 ** (attempt - 2))))  # full jitter
            snap = self._snapshot()
            changes, result = decide(snap)
            for key in changes:
                _check_key(key)
            if self._apply(snap, changes, message):
                return result
            self.stats.add(lost_races=1)
        raise StateError(f"state store: write refused {self.retries} times by concurrent writers; giving up")

    def _apply(self, snap: _Snapshot, changes: Mapping[str, bytes | None], message: str) -> bool:
        """Write the changes key by key; False as soon as a conditional write is refused."""
        done: builtins.list[tuple[str, bytes | None]] = []
        for key, body in sorted(changes.items()):
            present = snap.exists(key)
            if body is None:
                if present:
                    self.adapter.delete(STATE + key)  # see the module docstring: no conditional delete
                    done.append((key, None))
                continue
            # The tag is the one `decide` read the key under (or the listed one if it did not read it), so a
            # change made since then is a refused write, not a silent overwrite.
            tag = snap._tags.get(key)
            found = None if tag is None else self._body(key, tag)
            if found is not None and found == (body, tag):
                continue  # unchanged
            if tag is None:
                ok = self.adapter.create(STATE + key, body)
            else:
                ok = self.adapter.replace(STATE + key, body, tag)
            if not ok:
                if done:
                    self._record_audit(done, message, "transact")  # what was written stays written: record it
                return False
            done.append((key, body))
        if done:
            self._record_audit(done, message, "transact")
        return True

    def fs(self) -> StateFS:
        return StateFS(self)

    # -- audit ---------------------------------------------------------------------------------------

    def _record_audit(self, done: builtins.list[tuple[str, bytes | None]], message: str, action: str) -> None:
        puts = sum(body is not None for _, body in done)
        self.stats.add(writes=1, objects_written=puts)
        now = time.time()
        stamp = datetime.fromtimestamp(now, UTC)
        record = {
            "at": now,
            "message": message,
            "keys": [key for key, _ in done],
            "action": action,
            "size": sum(len(body) for _, body in done if body is not None),
            "host": self._host,
        }
        # Audit is best effort: the state write has happened, and a failure here must not report it as failed.
        try:
            path = f"{AUDIT}{stamp:%Y-%m-%d}/{time.time_ns()}-{secrets.token_hex(3)}.json"
            self.adapter.put(path, json.dumps(record, sort_keys=True).encode())
        except (ObstoreError, OSError) as exc:
            log.warning("state audit record not written: %s", type(exc).__name__)
            return
        try:
            self._prune_once()
        except (ObstoreError, OSError) as exc:
            log.warning("old audit records not pruned (the audit record was written): %s", type(exc).__name__)

    def _prune_once(self) -> None:
        """Delete audit days older than `retention_days`, on this process's first audit write."""
        if self._pruned:
            return
        self._pruned = True
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for day in self.adapter.list_dirs(AUDIT):
            if day[len(AUDIT) : -1] < cutoff:
                for path in self.adapter.list(day):
                    self.adapter.delete(path)

    def recent_changes(self, limit: int) -> builtins.list[dict[str, object]]:
        """Return the newest `limit` audit records, newest first (`at`, `message`, `keys`, `action`, ...)."""
        found: builtins.list[dict[str, object]] = []
        for day in reversed(self.adapter.list_dirs(AUDIT)):
            for path in sorted(self.adapter.list(day), reverse=True):  # names start with a ns timestamp
                if len(found) >= limit:
                    return found
                got = self.adapter.get(path)
                if got is None:
                    continue
                try:
                    record = json.loads(got[0])
                except ValueError:  # one damaged record must not hide the rest of the view
                    self.stats.add(unreadable_audit=1)
                    continue
                if isinstance(record, dict):
                    found.append(record)
                else:
                    self.stats.add(unreadable_audit=1)
        return found[:limit]


# -- building a store from a URI -----------------------------------------------------------------------


def s3_config(endpoint: str = "", region: str = "") -> dict[str, object]:
    """Return the keyword arguments for `S3Store`, never a credential (those come from the environment).

    `conditional_put="etag"` is what makes obstore send `If-Match` on S3 (obstore 0.11.1 defaults to it; set
    explicitly so `AWS_CONDITIONAL_PUT` in the environment cannot turn it off). An endpoint is an
    S3-compatible server (MinIO): path-style addressing, and plain http
    only for localhost.
    """
    config: dict[str, object] = {"conditional_put": "etag"}
    if region:
        config["region"] = region
    if endpoint:
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("endpoint must be an http(s) URL")
        if has_credentials_or_query(parts):  # the value is not echoed: it may be `http://KEY:SECRET@host`
            raise ValueError("endpoint must have no credentials, query or fragment")
        config["endpoint"] = endpoint
        config["virtual_hosted_style_request"] = False
        if parts.scheme == "http":
            if not plain_http_is_allowed(parts.hostname):
                raise ValueError("endpoint may use http only for localhost")
            config["client_options"] = {"allow_http": True}
    return config


def open_adapter(uri: str, *, endpoint: str = "", region: str = "") -> ObstoreAdapter:
    """Return the adapter for `s3://bucket/prefix`, `gs://bucket/prefix` or `memory://`."""
    parts = urlsplit(uri)
    prefix = parts.path.strip("/")
    if has_credentials_or_query(parts):  # the value is not echoed: it may be `s3://KEY:SECRET@bucket`
        raise ValueError(
            "a state bucket must have no credentials, query or fragment: the environment's is used"
        )
    if parts.scheme == "memory":
        return _SerialAdapter(obstore.store.MemoryStore())
    if parts.scheme in ("s3", "gs") and parts.hostname:
        if parts.scheme == "s3":
            store: obstore.store.ObjectStore = obstore.store.S3Store(
                parts.hostname,
                prefix=prefix or None,
                **s3_config(endpoint, region),  # type: ignore[arg-type]
            )
        else:
            # Generation preconditions are GCS's native conditional write: nothing to configure.
            store = obstore.store.GCSStore(parts.hostname, prefix=prefix or None)
        return ObstoreAdapter(store)
    raise ValueError("a state bucket is s3://bucket/prefix, gs://bucket/prefix or memory://")


def open_store(
    uri: str, *, endpoint: str = "", region: str = "", retention_days: int = 30, retries: int = _RETRIES
) -> BucketStore:
    return BucketStore(
        open_adapter(uri, endpoint=endpoint, region=region), retention_days=retention_days, retries=retries
    )
