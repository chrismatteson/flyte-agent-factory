"""Benchmark the factory's state backends under concurrent writers, and print a markdown table.

    PYTHONPATH=src uv run python ops/state_bench.py [--writers 1,4,12] [--seconds 8 | --ops 50] \
        [--backends git,s3,memory,volume] [--git-delay-ms 0]

Every writer (a thread with its own store, so its own git cache and its own S3 client) loops over the mix the
factory makes: a marker (a plain write of a key no one else writes), the dispatch lease (`_queue.take_lease`,
and `release_lease` when it got it) and a reserve read-modify-write (`transact` on one shared counter). Every
call is timed; the table gives the latency of the writes, the retries they lost to another writer, the state
objects written per hour at that rate, failures (a call that raised) and lost updates (the shared counter
ends below the number of increments that returned, which would be a compare-and-swap bug).

The backends, all local, none a real bucket:

- `git`: a bare repository in a temporary directory. `--git-delay-ms` sleeps that long before every fetch and
  push, to mimic the round trip to GitHub (a local remote answers in milliseconds, GitHub in a few hundred);
  it is a model of that latency, not a measurement of GitHub.
- `s3`: moto's `ThreadedMotoServer` on localhost, which honours `If-None-Match` and `If-Match` like S3.
  Moto is one Python process: its latency is a floor for the protocol, not S3's.
- `memory`: obstore's MemoryStore (calls serialised in `_state_bucket`): the floor for the bucket store's
  own logic.
- `volume`: a directory with the `flock` compare-and-swap.

Writers are threads: the state stores' file locks and S3 clients behave as in separate pods, but the GIL
caps the rate at which one process can generate work. Rates here compare backends; they are not a forecast.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/migrate_state.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obstore.exceptions import BaseError as ObstoreError

from flyte_agent_factory import _queue, _state_bucket, _state_store, _state_volume

if TYPE_CHECKING:
    from moto.server import ThreadedMotoServer

BACKENDS = ("git", "s3", "memory", "volume")
QUEUE = f"{_state_store.ROOT}/bench-queue"
_COUNTER = "reserve/counter.json"
#: Enough rounds that a writer under the heaviest contention here does not give up: a failure should mean a
#: bug or an outage, not a benchmark with more writers than the retry budget (as in the contract tests).
_RETRIES = 200
_REGION = "us-east-1"


class _DelayedGitStore(_state_store.StateStore):
    """A git store that waits before each network round trip, as a slow remote would make it."""

    delay = 0.0

    def _fetch(self) -> None:
        time.sleep(self.delay)
        super()._fetch()

    def _push(self, commit: str) -> bool:
        time.sleep(self.delay)
        return super()._push(commit)


@dataclass
class Target:
    """One backend: how to make a writer's store, and what its stores counted."""

    name: str
    new: Callable[[str], _state_store.Store]  # a writer's store, by its name
    stores: list[_state_store.Store] = field(default_factory=list)

    def writer(self, name: str) -> _state_store.Store:
        """Make a writer's store and keep it, so its counters are summed."""
        store = self.new(name)
        self.stores.append(store)
        return store

    def counters(self) -> tuple[int, int, int]:
        """Return (writes, lost races, state objects written) summed over this target's stores."""
        writes = lost = objects = 0
        for store in self.stores:
            if isinstance(store, _state_store.StateStore):
                writes, lost, objects = writes + store.pushes, lost + store.lost_races, objects + store.pushes
            else:
                assert isinstance(store, _state_bucket.BucketStore)
                s = store.stats
                writes, lost, objects = writes + s.writes, lost + s.lost_races, objects + s.objects_written
        return writes, lost, objects


@dataclass
class Result:
    """One run: a backend with a number of writers."""

    backend: str
    writers: int
    calls: int
    failures: int
    lost_updates: int
    p50: float
    p95: float
    max: float
    lost_per_write: float
    objects_per_hour: int
    list_ms: float
    keys: int
    errors: list[str]


def percentiles(latencies: list[float]) -> tuple[float, float, float]:
    """Return (p50, p95, max); the inclusive method keeps both inside the observed range."""
    if not latencies:
        return 0.0, 0.0, 0.0
    if len(latencies) == 1:
        return latencies[0], latencies[0], latencies[0]
    cuts = statistics.quantiles(latencies, n=100, method="inclusive")
    return cuts[49], cuts[94], max(latencies)


def _increment(snap: _state_store.SnapshotView) -> tuple[Mapping[str, bytes | None], int]:
    body = snap.read(_COUNTER)
    value = (json.loads(body)["n"] if body else 0) + 1
    return {_COUNTER: json.dumps({"n": value}).encode()}, value


def _run_writer(
    store: _state_store.Store, name: str, deadline: float, ops: int, latencies: list[float], errors: list[str]
) -> int:
    """Loop the factory's mix until `deadline` (or `ops` cycles); return how many increments returned."""
    local.store = store
    increments = 0
    cycle = 0

    def timed(call: Callable[[], object]) -> bool:
        started = time.perf_counter()
        try:
            call()
        # A failed call is a result (counted and reported), not a reason to stop. Only what a backend raises:
        # anything else is a bug in the harness and should crash.
        except (_state_store.StateError, OSError, ObstoreError) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[-120:]}")
            return False
        latencies.append(time.perf_counter() - started)
        return True

    while cycle < ops if ops else time.monotonic() < deadline:
        cycle += 1
        key = f"markers/{name}/{cycle}"
        timed(partial(store.write, {key: b"x"}, f"marker {key}"))
        if timed(lambda: _take_and_release(name)) is False:
            continue
        if timed(lambda: store.transact(_increment, f"reserve {name}")):
            increments += 1
    return increments


def _take_and_release(holder: str) -> None:
    if _queue.take_lease(QUEUE, holder) is None:
        _queue.release_lease(QUEUE, holder)


#: `_queue` finds its store through `_state_store.current()`, one per process; each writer thread has its own.
local = threading.local()


def _current_of_this_thread(*, branch: str = "") -> _state_store.Store:
    store: _state_store.Store = local.store
    return store


def run_target(target: Target, writers: int, seconds: float, ops: int) -> Result:
    """Run `writers` threads against a fresh store of `target` and summarise the calls they made."""
    target.stores.clear()
    stores = [target.writer(f"w{i}") for i in range(writers)]
    results: list[list[float]] = [[] for _ in stores]
    errors: list[list[str]] = [[] for _ in stores]
    increments = [0] * writers
    deadline = time.monotonic() + seconds

    def work(i: int) -> None:
        increments[i] = _run_writer(stores[i], f"w{i}", deadline, ops, results[i], errors[i])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(writers)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    latencies = [x for r in results for x in r]
    all_errors = [e for r in errors for e in r]
    writes, lost, objects = target.counters()
    final = stores[0].read(_COUNTER, fresh=True)
    counted = json.loads(final)["n"] if final else 0
    started_list = time.perf_counter()
    keys = len(stores[0].list("", fresh=True))
    list_ms = (time.perf_counter() - started_list) * 1000
    p50, p95, peak = percentiles(latencies)
    return Result(
        backend=target.name,
        writers=writers,
        calls=len(latencies) + len(all_errors),
        failures=len(all_errors),
        lost_updates=max(0, sum(increments) - counted),
        p50=p50,
        p95=p95,
        max=peak,
        lost_per_write=lost / writes if writes else 0.0,
        objects_per_hour=round(objects / elapsed * 3600),
        list_ms=list_ms,
        keys=keys,
        errors=all_errors,
    )


def _git_target(work: Path, delay_ms: float) -> Target:
    remote = work / f"remote-{uuid.uuid4().hex[:8]}.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    store_class = type("DelayedGit", (_DelayedGitStore,), {"delay": delay_ms / 1000})

    def new(writer: str) -> _state_store.Store:
        cache = work / f"cache-{remote.stem}-{writer}.git"
        return store_class(str(remote), "factory-state/bench", cache, ttl=0.0, retries=_RETRIES)  # type: ignore[no-any-return]

    return Target("git", new)


@contextlib.contextmanager
def make_target(name: str, git_delay_ms: float, work: Path) -> Iterator[Target]:
    """Yield a fresh, empty `Target` for `name`; for `s3` a local server runs while it is in use."""
    if name == "git":
        yield _git_target(work, git_delay_ms)
    elif name == "memory":
        adapter = _state_bucket.open_adapter("memory://")
        yield Target("memory", lambda _writer: _state_bucket.BucketStore(adapter, retries=_RETRIES))
    elif name == "volume":
        root = str(work / f"volume-{uuid.uuid4().hex[:8]}")
        yield Target("volume", lambda _writer: _state_volume.open_store(root, retries=_RETRIES))
    elif name == "s3":
        saved = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
        server, endpoint = _start_moto()
        try:
            # Dummy values moto accepts; the benchmark never holds a real credential.
            os.environ["AWS_ACCESS_KEY_ID"] = os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
            uri = _make_bucket(endpoint)
            yield Target(
                "s3",
                lambda _writer: _state_bucket.open_store(
                    uri, endpoint=endpoint, region=_REGION, retries=_RETRIES
                ),
            )
        finally:
            server.stop()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    else:
        raise SystemExit(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")


def _start_moto() -> tuple[ThreadedMotoServer, str]:
    # Imported here: moto is a dev dependency and `--help` must not need it.
    from moto.server import ThreadedMotoServer

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # one line per request otherwise
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    return server, f"http://127.0.0.1:{port}"


def _make_bucket(endpoint: str) -> str:
    import boto3  # type: ignore[import-untyped]

    bucket = f"bench-{uuid.uuid4().hex[:12]}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    client.create_bucket(Bucket=bucket)
    return f"s3://{bucket}/factory"


def format_table(results: list[Result]) -> str:
    """Return the results as a markdown table, one row per backend and writer count."""
    lines = [
        "| backend | writers | calls | failures | lost updates | p50 s | p95 s | max s "
        "| lost races / write | objects / hour | list ms (keys) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.backend} | {r.writers} | {r.calls} | {r.failures} | {r.lost_updates} "
            f"| {r.p50:.3f} | {r.p95:.3f} | {r.max:.3f} | {r.lost_per_write:.2f} "
            f"| {r.objects_per_hour:,} | {r.list_ms:.1f} ({r.keys}) |"
        )
    return "\n".join(lines)


def writer_counts(text: str) -> list[int]:
    """Parse `--writers`; a count below 1 would leave `run_target` with no store to read back from."""
    try:
        counts = [int(w) for w in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a comma separated list of whole numbers") from None
    if any(c < 1 for c in counts):
        raise argparse.ArgumentTypeError("each writer count must be at least 1")
    return counts


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark; return 1 when any call failed or an update was lost."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--writers",
        type=writer_counts,
        default="1,4,12",
        help="concurrent writers, comma separated (12 = the slots)",
    )
    parser.add_argument("--seconds", type=float, default=8.0, help="how long each run lasts (default 8)")
    parser.add_argument(
        "--ops", type=int, default=0, help="cycles of the mix per writer instead of --seconds"
    )
    parser.add_argument("--backends", default=",".join(BACKENDS), help=f"any of {', '.join(BACKENDS)}")
    parser.add_argument(
        "--git-delay-ms",
        type=float,
        default=0.0,
        help="sleep before each git fetch and push, to mimic GitHub",
    )
    args = parser.parse_args(argv)

    # `_queue` takes its store from `current()`; give each writer thread its own, as each pod has.
    real_current = _state_store.current
    _state_store.current = _current_of_this_thread
    results: list[Result] = []
    try:
        with tempfile.TemporaryDirectory(prefix="state-bench-") as tmp:
            for backend in args.backends.split(","):
                for writers in args.writers:
                    # A fresh target per run: each starts from an empty store, so the key count is comparable.
                    with make_target(backend, args.git_delay_ms, Path(tmp)) as target:
                        results.append(run_target(target, writers, args.seconds, args.ops))
    finally:
        _state_store.current = real_current
    print(format_table(results))
    failures = [r for r in results if r.failures or r.lost_updates]
    for r in failures:
        print(
            f"\n{r.backend} x{r.writers}: {r.failures} failures, {r.lost_updates} lost updates",
            file=sys.stderr,
        )
        for message in sorted(set(r.errors))[:3]:
            print(f"  {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
