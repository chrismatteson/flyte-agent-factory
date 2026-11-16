"""A StateStore over a fresh local bare repository, for tests of anything built on `_state_store`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from flyte_agent_factory import _queue, _state_store

BRANCH = "factory-state/test-dev"


def make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    return remote


def make_store(
    tmp_path: Path,
    *,
    remote: Path | None = None,
    name: str = "pod",
    ttl: float = 0.0,
    retries: int = 10,
) -> _state_store.StateStore:
    """Return a store over `remote` (default: a new bare repository in `tmp_path`) with its own cache."""
    remote = remote or make_remote(tmp_path)
    return _state_store.StateStore(
        str(remote), BRANCH, tmp_path / f"cache-{name}.git", ttl=ttl, retries=retries
    )


def take_lease(remote: str, cache: str, holder: str) -> str:
    """Take the lease if it is absent; return who holds it afterwards. Run in a separate process."""
    store = _state_store.StateStore(remote, BRANCH, Path(cache), ttl=0.0, retries=50)

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], str]:
        held = snap.read("queue/.dispatch-lease.json")
        if held is not None:
            return {}, json.loads(held)["holder"]
        return {"queue/.dispatch-lease.json": json.dumps({"holder": holder}).encode()}, holder

    return store.transact(decide, f"lease {holder}")


def take_production_lease(remote: str, base: str, holder: str) -> str:
    """Race the real `_queue.take_lease`; return who holds the lease afterwards. Run in a separate process.

    The process selects its store the way a pod does, through the environment; `base` gives it its own cache.
    """
    os.environ["AGENT_FACTORY_STATE_REMOTE"] = remote
    os.environ["AGENT_FACTORY_STATE_BRANCH"] = BRANCH
    os.environ["AGENT_FACTORY_BASE"] = base
    won = _queue.take_lease(f"{_state_store.ROOT}/queue", holder)
    return holder if won is None else won


def rival_wins_first_push(
    monkeypatch, store: _state_store.StateStore, tmp_path: Path, changes: dict[str, bytes | None]
) -> None:
    """Make `store` lose its first push: a second store (own cache, same remote) commits `changes` first.

    The deterministic form of two writers that both read the same tip.
    """
    rival = make_store(tmp_path, remote=Path(store.remote), name="rival")
    real_push = _state_store.StateStore._push
    pushed: list[str] = []

    def push(self, commit):
        if self is store and not pushed:  # the rival's own push goes through untouched
            pushed.append(commit)
            rival.write(changes, "rival")
        return real_push(self, commit)

    monkeypatch.setattr(_state_store.StateStore, "_push", push)


def write_in_process(remote: str, cache: str, prefix: str, writes: int) -> int:
    """Write `writes` distinct keys under `prefix`, one commit each; return `writes`.

    Run in a separate process.
    """
    store = _state_store.StateStore(remote, BRANCH, Path(cache), ttl=0.0)
    for i in range(writes):
        store.write({f"{prefix}/{i}": b"x"}, f"{prefix} {i}")
    return writes
