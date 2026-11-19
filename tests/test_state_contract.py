"""One contract for every state backend: git, an object store (obstore memory and a local S3 fake), a volume.

Whatever `Store` is behind `_state_store.configure`, the queue's dispatch lease must have one holder, and
concurrent writers must lose nothing. The S3 case runs against moto's server, which honours both
`If-None-Match` and `If-Match` on PutObject (checked in `test_moto_honours_conditional_writes`), so it
exercises the same conditional writes a real bucket gets.
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import multiprocessing
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import obstore
import pytest
from moto.s3.models import S3Backend
from moto.s3.responses import S3Response
from moto.server import ThreadedMotoServer

from flyte_agent_factory import _queue, _state_bucket, _state_store, _state_volume
from tests.state_git import make_remote, make_store
from tests.state_volume import increment, take_lease

OPS = Path(__file__).resolve().parents[1] / "ops"
QUEUE = f"{_state_store.ROOT}/queue"
RETRIES = 200


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _one_at_a_time(put: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap moto's PutObject handler so its precondition check and its write happen as one step."""
    lock = threading.Lock()

    @functools.wraps(put)
    def put_atomically(*args: Any, **kwargs: Any) -> Any:
        with lock:
            return put(*args, **kwargs)

    return put_atomically


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    port = _free_port()
    with pytest.MonkeyPatch.context() as patch:
        # S3 decides a conditional PutObject atomically: of two writers on one etag, one wins. moto 5.2 checks
        # If-Match / If-None-Match and then writes, and its server is threaded, so under load both could win.
        patch.setattr(S3Response, "put_object", _one_at_a_time(S3Response.put_object))
        server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
        server.start()
        yield f"http://127.0.0.1:{port}"
        server.stop()


@pytest.fixture
def s3_uri(moto_endpoint, monkeypatch) -> str:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # moto accepts any; never a real credential
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    bucket = f"state-{uuid.uuid4().hex[:12]}"
    boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1").create_bucket(Bucket=bucket)
    return f"s3://{bucket}/factory"


def test_moto_honours_conditional_writes(s3_uri, moto_endpoint):
    """The premise of the S3 case: create-only and if-match writes are refused when they should be."""
    adapter = _state_bucket.open_adapter(s3_uri, endpoint=moto_endpoint, region="us-east-1")
    assert adapter.create("k", b"1")
    assert not adapter.create("k", b"2")  # If-None-Match
    tag = adapter.head("k")
    assert tag is not None
    assert adapter.replace("k", b"3", tag)
    assert not adapter.replace("k", b"4", tag)  # If-Match with a stale etag
    found = adapter.get("k")
    assert found is not None
    assert found[0] == b"3"


def test_moto_lets_one_of_many_racing_if_match_writes_win(s3_uri, moto_endpoint, monkeypatch):
    """As on S3: writers racing with the same etag, exactly one replaces the object; the rest are refused."""
    adapter = _state_bucket.open_adapter(s3_uri, endpoint=moto_endpoint, region="us-east-1")
    # moto runs in this process. A pause between its If-Match check and its write is what a loaded machine
    # does at random; here it happens every time, so a check-then-write that is not one step lets two win.
    write = S3Backend.put_object

    def slow_write(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.02)
        return write(*args, **kwargs)

    monkeypatch.setattr(S3Backend, "put_object", slow_write)
    _race_if_match_writes(adapter)


def _race_if_match_writes(adapter: _state_bucket.ObstoreAdapter) -> None:
    """Race 16 If-Match writers on one etag, 5 times; exactly one must win each time."""
    for round_ in range(5):
        key = f"k{round_}"
        assert adapter.create(key, b"0")
        tag = adapter.head(key)
        assert tag is not None
        start = threading.Barrier(16)

        def replace(
            i: int, key: str = key, tag: _state_bucket.Tag = tag, start: threading.Barrier = start
        ) -> bool:
            start.wait()
            # Each body differs from "0": equal bytes keep the etag, so a later If-Match would rightly pass.
            return adapter.replace(key, f"w{i}".encode(), tag)

        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            won = list(pool.map(replace, range(16)))
        assert won.count(True) == 1, (round_, won)


class Backend:
    """A way to get stores on one shared backing: `new()` is another writer (own cache, same data)."""

    def __init__(self, new: Callable[[], _state_store.Store], volume: Path | None = None) -> None:
        self.new = new
        self.volume = volume


@pytest.fixture(params=["git", "memory", "s3", "volume"])
def backend(request, tmp_path) -> Backend:
    return _backend(request.param, request, tmp_path)


def _backend(name: str, request: pytest.FixtureRequest, tmp_path: Path) -> Backend:
    if name == "git":
        remote = make_remote(tmp_path)
        count = iter(range(1000))
        return Backend(lambda: make_store(tmp_path, remote=remote, name=f"w{next(count)}", retries=RETRIES))
    if name == "memory":
        adapter = _state_bucket.open_adapter("memory://")
        return Backend(lambda: _state_bucket.BucketStore(adapter, retries=RETRIES))
    if name == "s3":
        uri = request.getfixturevalue("s3_uri")
        endpoint = request.getfixturevalue("moto_endpoint")
        return Backend(
            lambda: _state_bucket.open_store(uri, endpoint=endpoint, region="us-east-1", retries=RETRIES)
        )
    root = tmp_path / "volume"
    return Backend(lambda: _state_volume.open_store(str(root), retries=RETRIES), volume=root)


@pytest.fixture
def store(backend) -> Iterator[_state_store.Store]:
    store = backend.new()
    _state_store.configure(store)
    yield store
    _state_store.configure(None)


@pytest.fixture(params=["memory", "s3", "volume"])
def audited_store(request, tmp_path) -> Iterator[_state_bucket.BucketStore]:
    """The backends that keep audit records (git's history is its log)."""
    store = _backend(request.param, request, tmp_path).new()
    assert isinstance(store, _state_bucket.BucketStore)
    _state_store.configure(store)
    yield store
    _state_store.configure(None)


def test_read_your_write(store):
    assert store.read("a/b") is None
    assert not store.exists("a/b")
    assert store.write({"a/b": b"one"}, "write a/b")
    assert store.read("a/b") == b"one"
    assert store.exists("a/b")
    assert store.snapshot().read("a/b") == b"one"
    assert store.snapshot().read_many(["a/b", "missing"]) == {"a/b": b"one"}
    assert store.write({"a/b": b"two"}, "overwrite")
    assert store.read("a/b") == b"two"


def test_list_prefix_matches_whole_path_segments(store):
    # No key is also a directory ("queue/a" and "queue/a/x"): a git tree and a file system cannot hold both.
    store.write({k: b"x" for k in ("queue/a/x", "queue/a/y", "queue/ab/x", "other")}, "files")
    assert store.list("queue/a") == ["queue/a/x", "queue/a/y"]
    assert store.list("queue/a/") == ["queue/a/x", "queue/a/y"]
    assert store.list("queue/a/x") == ["queue/a/x"]  # a prefix may name one file
    assert store.list("queue") == ["queue/a/x", "queue/a/y", "queue/ab/x"]
    assert store.list() == ["other", "queue/a/x", "queue/a/y", "queue/ab/x"]
    assert store.snapshot().list("queue/ab") == ["queue/ab/x"]


def test_state_keys_do_not_include_audit_records(store):
    store.write({"k": b"x"}, "one")
    assert store.list() == ["k"]


def test_delete(store):
    store.write({"a": b"1", "b": b"2"}, "two")
    assert store.write({"a": None}, "delete a")
    assert store.read("a") is None
    assert store.list() == ["b"]
    assert not store.write({"a": None}, "delete again")  # already gone: nothing to do
    store.fs().rm(f"{_state_store.ROOT}/b")
    assert store.list() == []
    with pytest.raises(FileNotFoundError):
        store.fs().rm(f"{_state_store.ROOT}/b")


def test_a_write_that_changes_nothing_writes_nothing(store):
    assert store.write({"k": b"v"}, "first")
    before = store.recent_changes(100)
    assert not store.write({"k": b"v"}, "same bytes")
    assert not store.write({}, "no keys")
    assert store.recent_changes(100) == before


def test_invalid_keys_are_refused(store):
    for key in ("", "/a", "a/../b", "a//b"):
        with pytest.raises(ValueError):
            store.write({key: b"x"}, "bad")


def test_transact_decides_again_when_its_read_went_stale(store, backend):
    rival = backend.new()
    calls = []

    def decide(snap):
        calls.append(snap.read("k"))
        if len(calls) == 1:
            rival.write({"k": b"rival"}, "rival wins the first round")
        return {"k": (snap.read("k") or b"") + b"+me"}, len(calls)

    assert store.transact(decide, "transact") == 2
    assert calls == [None, b"rival"]
    assert store.read("k") == b"rival+me"


def test_transact_that_changes_nothing_returns_its_result(store):
    store.write({"k": b"v"}, "first")
    assert store.transact(lambda snap: ({}, snap.read("k")), "read only") == b"v"
    assert store.transact(lambda snap: ({"k": b"v"}, "same"), "same bytes") == "same"
    assert len(store.recent_changes(100)) == 1


def test_transact_gives_up_after_its_retries(backend):
    store = backend.new()
    rival = backend.new()
    store.retries = 3
    n = iter(range(100))

    def decide(snap):
        rival.write({"k": str(next(n)).encode()}, "rival")  # a different value every round: always stale
        return {"k": b"mine"}, None

    with pytest.raises(_state_store.StateError):
        store.transact(decide, "never wins")


def test_lease_has_one_holder_expires_and_is_retaken_by_its_holder(store):
    assert _queue.take_lease(QUEUE, "a", now=1000) is None
    assert _queue.take_lease(QUEUE, "b", now=1001) == "a"
    assert _queue.take_lease(QUEUE, "a", now=1002) is None  # the holder takes it again
    assert _queue.take_lease(QUEUE, "b", now=1002 + _queue.LEASE_TTL + 1) is None  # a's expired
    _queue.release_lease(QUEUE, "a")  # a no longer holds it: b's lease stays
    assert _queue.take_lease(QUEUE, "a", now=1003 + _queue.LEASE_TTL) == "b"
    _queue.release_lease(QUEUE, "b")
    assert _queue.take_lease(QUEUE, "a", now=1004 + _queue.LEASE_TTL) is None  # released: free at once


def _race(workers: int, work: Callable[[int], object]) -> list[object]:
    # `result()` re-raises a worker's exception here, in the test's thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, i) for i in range(workers)]
        return [future.result(timeout=180) for future in futures]


def test_concurrent_takers_leave_one_holder(backend):
    # One store per taker, as in separate pods. `current()` is process-wide, so the takers use `transact` on
    # their own store through the production decide: `_queue.take_lease` with the store configured is below.
    stores = [backend.new() for _ in range(6)]
    key = _state_store.key_of(f"{QUEUE}/{_queue._LEASE}")

    def take(i: int) -> str | None:
        def decide(snap):
            lease = _queue._parse_lease(snap.read(key))
            if lease and lease["holder"] != f"h{i}" and lease["expires"] > 1000:
                return {}, lease["holder"]
            return {key: json.dumps({"holder": f"h{i}", "expires": 2000}).encode()}, None

        holder: str | None = stores[i].transact(decide, f"lease h{i}")
        return holder

    results = _race(6, take)
    assert results.count(None) == 1
    winner = f"h{results.index(None)}"
    assert set(results) - {None} == {winner}


def test_concurrent_take_lease_through_the_queue(store):
    results = _race(6, lambda i: _queue.take_lease(QUEUE, f"h{i}", now=1000))
    assert results.count(None) == 1
    winner = f"h{results.index(None)}"
    assert set(results) - {None} == {winner}


def test_concurrent_writers_of_different_keys_lose_nothing(backend):
    stores = [backend.new() for _ in range(6)]

    def write(i: int) -> None:
        for j in range(4):
            stores[i].write({f"w{i}/{j}": b"x"}, f"w{i} {j}")

    _race(6, write)
    assert backend.new().list() == sorted(f"w{i}/{j}" for i in range(6) for j in range(4))


def test_concurrent_increments_of_one_key_lose_none(backend):
    stores = [backend.new() for _ in range(4)]

    def add(i: int) -> None:
        def decide(snap):
            return {"n": str(int(snap.read("n") or b"0") + 1).encode()}, None

        for _ in range(4):
            stores[i].transact(decide, "increment")

    _race(4, add)
    assert backend.new().read("n") == b"16"


def test_recent_changes_lists_the_newest_first(store):
    store.write({"a": b"1"}, "first")
    store.write({"b": b"2", "c": b"3"}, "second")
    store.write({"a": None}, "third")
    changes = store.recent_changes(2)
    assert [c["message"] for c in changes] == ["third", "second"]
    assert changes[1]["keys"] == ["b", "c"]
    assert all(isinstance(c["at"], float) and c["at"] > 0 for c in changes)
    assert [c["message"] for c in store.recent_changes(10)] == ["third", "second", "first"]
    assert store.recent_changes(0) == []


def test_transact_appears_in_recent_changes(store):
    store.transact(lambda snap: ({"k": b"v"}, None), "by transact")
    assert store.recent_changes(1)[0]["message"] == "by transact"


def test_a_malformed_audit_record_is_skipped_not_fatal(audited_store):
    store = audited_store
    store.write({"a": b"1"}, "first")
    store.write({"b": b"2"}, "second")
    day = store.adapter.list_dirs("audit/")[-1]
    store.adapter.put(f"{day}zzz-damaged.json", b"{not json")
    store.adapter.put(f"{day}zzy-not-a-record.json", b"[1]")
    assert [c["message"] for c in store.recent_changes(10)] == ["second", "first"]
    assert store.stats.unreadable_audit == 2


def test_the_overview_still_shows_changes_beside_a_malformed_audit_record(audited_store):
    store = audited_store
    import importlib.util

    spec = importlib.util.spec_from_file_location("operate_overview", OPS / "operate.py")
    assert spec is not None and spec.loader is not None
    operate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operate)
    store.write({"a": b"1"}, "first")
    store.adapter.put(f"{store.adapter.list_dirs('audit/')[-1]}zzz.json", b"{not json")
    assert [c["message"] for c in operate.recent_state_changes()] == ["first"]


def _set_key(key: str, snap: object) -> tuple[dict[str, bytes | None], None]:
    return {key: b"1"}, None


def test_volume_keys_named_like_the_lock_or_a_temp_file_are_refused(tmp_path):
    store = _state_volume.open_store(str(tmp_path / "volume"))
    for key in ("q/.lock", ".lock", "q/.tmp-x", "q/.tmp-x/y"):
        with pytest.raises(ValueError, match="may not have a path part"):
            store.write({key: b"1"}, "reserved")
        with pytest.raises(ValueError, match="may not have a path part"):
            store.transact(functools.partial(_set_key, key), "reserved")
    assert store.list("") == []
    store.write({"q/lock": b"1", "q/tmp-x": b"2"}, "near misses are fine")
    assert store.list("q") == ["q/lock", "q/tmp-x"]


def test_volume_processes_take_the_lease_once(tmp_path):
    root = str(tmp_path / "volume")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        jobs = [pool.apply_async(take_lease, (root, f"holder{i}")) for i in range(4)]
        holders = [job.get(timeout=120) for job in jobs]
    assert len(set(holders)) == 1
    assert holders[0].startswith("holder")


def test_volume_processes_increment_without_loss(tmp_path):
    root = str(tmp_path / "volume")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        jobs = [pool.apply_async(increment, (root, "n", 10)) for _ in range(4)]
        assert [job.get(timeout=120) for job in jobs] == [10] * 4
    assert _state_volume.open_store(root).read("n") == b"40"


def test_a_key_named_409_is_not_mistaken_for_a_conflict(monkeypatch):
    from obstore.exceptions import GenericError

    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise GenericError("Error performing PUT http://h/b/state/queue/issue-409/x: NoSuchBucket")

    monkeypatch.setattr(obstore, "put", boom)
    adapter = _state_bucket.open_adapter("memory://")
    with pytest.raises(GenericError):
        adapter.create("state/queue/issue-409/x", b"{}")
    assert len(calls) == 1


def test_a_real_conditional_conflict_is_a_lost_race(monkeypatch):
    from obstore.exceptions import GenericError

    for text in (
        "Client error with status 409 Conflict: <Code>ConditionalRequestConflict</Code>",
        "Server returned non-2xx status code: 409 Conflict",
    ):
        monkeypatch.setattr(
            obstore, "put", lambda *a, text=text, **k: (_ for _ in ()).throw(GenericError(text))
        )
        assert _state_bucket.open_adapter("memory://").create("state/k", b"{}") is False
