"""Helpers that run in a separate process for the volume backend's contract tests."""

from __future__ import annotations

from flyte_agent_factory import _queue, _state_store, _state_volume


def take_lease(path: str, holder: str) -> str:
    """Race the real `_queue.take_lease` on the volume at `path`; return who holds the lease afterwards."""
    _state_store.configure(_state_volume.open_store(path))
    won = _queue.take_lease(f"{_state_store.ROOT}/queue", holder)
    return holder if won is None else won


def increment(path: str, key: str, times: int) -> int:
    """Add one to the counter at `key`, `times` times, each a transaction; return `times`."""
    store = _state_volume.open_store(path, retries=200)

    def decide(snap):
        return {key: str(int(snap.read(key) or b"0") + 1).encode()}, None

    for _ in range(times):
        store.transact(decide, "increment")
    return times
