"""`ops/migrate_state.py`: the one-time copy of the old bucket's state onto the git branch."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from tests.state_git import make_store, rival_wins_first_push

OPS = Path(__file__).resolve().parents[1] / "ops"


def _load():
    spec = importlib.util.spec_from_file_location("ops_migrate_state", OPS / "migrate_state.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate_state = _load()

SOURCE = {
    "queue/alpha/0001.json": b'{"name": "alpha"}',
    "reserve.json": b'{"claude": 5.0}',
    "gardening/alpha.json": b"{}",
    "github/intake/1.json": b"{}",
    "loop/backlog/a.json": b"{}",
    "checkpoints/alpha-run.json": b'{"location": "s3://b/alpha-run/checkpoint", "app": "k"}',
}
SKIPPED = {
    "waits.json": b"{}",
    "board/x.json": b"{}",
    "github/outbox/1.json": b"{}",
    "github/outbox-seen.json": b"{}",
    "deployer/x.json": b"{}",
    "queue/.dispatch-lease.json": b'{"holder": "old"}',
}


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path, name="operator")


def _commits(store) -> int:
    out = subprocess.run(
        ["git", "--git-dir", str(store.remote), "rev-list", "--count", store.branch],
        capture_output=True,
        text=True,
    )
    return int(out.stdout) if out.returncode == 0 else 0


def _read(store, key):
    return store.snapshot(fresh=True).read(key)


def test_copies_the_state_layout_and_skips_the_rest(store):
    result = migrate_state.migrate({**SOURCE, **SKIPPED}, store)
    assert result["counts"] == {"copied": len(SOURCE), "conflict": 0, "skipped": len(SKIPPED)}
    assert sorted(result["skipped"]) == sorted(SKIPPED)
    assert _commits(store) == 1  # one commit for everything
    for key, body in SOURCE.items():
        assert _read(store, key) == body
    for key in SKIPPED:
        assert _read(store, key) is None


def test_a_second_run_finds_nothing_new_and_commits_nothing(store):
    migrate_state.migrate(SOURCE, store)
    again = migrate_state.migrate(SOURCE, store)
    assert again["counts"]["copied"] == 0
    assert again["counts"]["same"] == len(SOURCE)
    assert _commits(store) == 1


def test_a_differing_key_is_a_conflict_and_is_not_overwritten(store):
    store.write({"reserve.json": b'{"claude": 9.0}'}, "set")
    result = migrate_state.migrate(SOURCE, store)
    assert result["conflicts"] == ["reserve.json"]
    assert result["counts"]["conflict"] == 1
    assert result["counts"]["copied"] == len(SOURCE) - 1
    assert _read(store, "reserve.json") == b'{"claude": 9.0}'


def test_overwrite_replaces_a_differing_key(store):
    store.write({"reserve.json": b'{"claude": 9.0}'}, "set")
    result = migrate_state.migrate(SOURCE, store, overwrite=True)
    assert result["conflicts"] == []
    assert result["counts"]["copied"] == len(SOURCE)
    assert _read(store, "reserve.json") == SOURCE["reserve.json"]


def test_dry_run_reports_and_writes_nothing(store):
    result = migrate_state.migrate(SOURCE, store, dry_run=True)
    assert result["counts"]["copied"] == len(SOURCE)
    assert _commits(store) == 0
    assert _read(store, "reserve.json") is None


def test_nothing_on_the_branch_is_deleted(store):
    store.write({"queue/beta/0001.json": b"{}"}, "other")
    migrate_state.migrate(SOURCE, store)
    assert _read(store, "queue/beta/0001.json") == b"{}"


def test_reads_a_bucket_prefix_and_only_that_prefix(tmp_path, store, capsys, monkeypatch):
    src = tmp_path / "bucket" / "prefix"
    for key, body in {**SOURCE, **SKIPPED}.items():
        path = src / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    elsewhere = tmp_path / "bucket" / "prefix-other" / "reserve.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"not ours")

    found = migrate_state.read_bucket(f"file://{src}")
    assert found == {**SOURCE, **SKIPPED}

    monkeypatch.setattr(migrate_state._state_store, "current", lambda: store)
    assert migrate_state.main(["--from", f"file://{src}"]) == 0
    assert f"copied {len(SOURCE)}, same 0, conflict 0, skipped {len(SKIPPED)}" in capsys.readouterr().out
    assert migrate_state.main(["--from", f"file://{src}"]) == 0
    assert "nothing to migrate" in capsys.readouterr().out
    assert _commits(store) == 1


def test_a_source_with_no_state_keys_is_refused(tmp_path, store, capsys, monkeypatch):
    """An empty or wrong prefix must not read as the cutover's "nothing to migrate" stop condition."""
    monkeypatch.setattr(migrate_state._state_store, "current", lambda: store)
    empty = tmp_path / "empty"
    empty.mkdir()
    wrong = tmp_path / "wrong"
    for key in ("board/x", "waits.json"):
        (wrong / key).parent.mkdir(parents=True, exist_ok=True)
        (wrong / key).write_bytes(b"{}")
    for source in (empty, wrong, tmp_path / "missing"):
        assert migrate_state.main(["--from", f"file://{source}"]) != 0
        captured = capsys.readouterr()
        assert "nothing to migrate" not in captured.out + captured.err
        assert "no factory state under" in captured.err
        assert _commits(store) == 0


# --- a pass after the deploy: the new code has written to the branch since ----------------------------

OLD_REQUEST = b'{"gen": 1, "name": "gamma", "brief": "old"}'
NEW_REQUEST = b'{"gen": 1, "name": "gamma", "brief": "new"}'


def test_a_bucket_request_colliding_with_a_new_one_is_renumbered_with_its_markers(store):
    """A launch the new code queued as gen 1 keeps its place, and the bucket's gen 1 moves up whole."""
    store.write({"queue/gamma/0001.json": NEW_REQUEST}, "enqueue on the new code")
    bucket = {"queue/gamma/0001.json": OLD_REQUEST, "queue/gamma/0001.started": b'{"run": "old-run"}'}
    result = migrate_state.migrate(bucket, store)
    assert result["renumbered"] == {"queue/gamma/0001": 2}
    assert result["conflicts"] == []
    assert _read(store, "queue/gamma/0001.json") == NEW_REQUEST
    assert _read(store, "queue/gamma/0001.started") is None, "the new request must still wait"
    assert json.loads(_read(store, "queue/gamma/0002.json")) == {"gen": 2, "name": "gamma", "brief": "old"}
    assert _read(store, "queue/gamma/0002.started") == b'{"run": "old-run"}'

    # A marker the old code adds later follows its request; nothing is renumbered twice.
    bucket["queue/gamma/0001.cancelled"] = b"{}"
    again = migrate_state.migrate(bucket, store)
    assert again["renumbered"] == {}
    assert _read(store, "queue/gamma/0002.cancelled") == b"{}"
    assert _read(store, "queue/gamma/0001.cancelled") is None
    assert migrate_state.migrate(bucket, store)["counts"]["copied"] == 0


def test_the_renumbered_generation_is_free_on_both_sides(store):
    store.write({"queue/gamma/0001.json": NEW_REQUEST}, "new")
    bucket = {"queue/gamma/0001.json": OLD_REQUEST, "queue/gamma/0002.json": b'{"gen": 2, "name": "gamma"}'}
    result = migrate_state.migrate(bucket, store)
    assert result["renumbered"] == {"queue/gamma/0001": 3}
    assert _read(store, "queue/gamma/0002.json") == bucket["queue/gamma/0002.json"]


def test_a_key_the_bucket_changed_is_updated_while_the_branch_has_the_copy(store):
    key = "checkpoints/alpha-run.json"
    migrate_state.migrate({key: b'{"at": 1}'}, store)
    migrate_state.migrate({key: b'{"at": 2}'}, store)
    assert _read(store, key) == b'{"at": 2}'


def test_a_key_the_new_code_changed_since_the_copy_is_kept(store):
    migrate_state.migrate({"reserve.json": b'{"claude": 1.0}'}, store)
    store.write({"reserve.json": b'{"claude": 7.0}'}, "set on the new code")
    result = migrate_state.migrate({"reserve.json": b'{"claude": 2.0}'}, store)
    assert result["conflicts"] == ["reserve.json"]
    assert _read(store, "reserve.json") == b'{"claude": 7.0}'


def test_a_marker_the_new_code_removed_is_not_brought_back(store):
    bucket = {"queue/alpha/0001.json": b"{}", "queue/alpha/0001.started": b'{"run": "r"}'}
    migrate_state.migrate(bucket, store)
    store.write({"queue/alpha/0001.started": None}, "reopen on the new code")
    assert migrate_state.migrate(bucket, store)["counts"]["copied"] == 0
    assert _read(store, "queue/alpha/0001.started") is None


def test_a_started_marker_the_old_code_removed_is_removed_on_the_branch(store):
    bucket = {"queue/alpha/0001.json": b"{}", "queue/alpha/0001.started": b'{"run": "r"}'}
    migrate_state.migrate(bucket, store)
    reopened = {"queue/alpha/0001.json": b"{}", "queue/alpha/0001.attempt-1": b"{}"}
    result = migrate_state.migrate(reopened, store)
    assert result["counts"]["deleted"] == 1
    assert _read(store, "queue/alpha/0001.started") is None
    assert _read(store, "queue/alpha/0001.attempt-1") == b"{}"


def test_a_started_marker_the_new_code_rewrote_is_not_removed(store):
    bucket = {"queue/alpha/0001.json": b"{}", "queue/alpha/0001.started": b'{"run": "r"}'}
    migrate_state.migrate(bucket, store)
    store.write({"queue/alpha/0001.started": b'{"run": "new"}'}, "dispatched on the new code")
    migrate_state.migrate({"queue/alpha/0001.json": b"{}"}, store)
    assert _read(store, "queue/alpha/0001.started") == b'{"run": "new"}'


def test_an_empty_source_deletes_nothing(store):
    migrate_state.migrate({"queue/alpha/0001.json": b"{}", "queue/alpha/0001.started": b"{}"}, store)
    assert migrate_state.migrate({}, store)["counts"].get("deleted", 0) == 0
    assert _read(store, "queue/alpha/0001.started") == b"{}"


def test_a_write_the_new_code_makes_during_the_pass_is_not_overwritten(store, monkeypatch, tmp_path):
    """The new code queues gamma 0002 between the pass's read and its push: the pass decides again."""
    store.write({"queue/gamma/0001.json": NEW_REQUEST}, "new")
    mine = b'{"gen": 2, "name": "gamma", "brief": "new code"}'
    rival_wins_first_push(monkeypatch, store, tmp_path, {"queue/gamma/0002.json": mine})
    result = migrate_state.migrate({"queue/gamma/0002.json": b'{"gen": 2, "name": "gamma"}'}, store)
    assert _read(store, "queue/gamma/0002.json") == mine
    assert result["renumbered"] == {"queue/gamma/0002": 3}
    assert json.loads(_read(store, "queue/gamma/0003.json")) == {"gen": 3, "name": "gamma"}


def test_a_pass_from_the_wrong_prefix_reopens_nothing(store):
    bucket = {"queue/alpha/0001.json": b"{}", "queue/alpha/0001.started": b'{"run": "r"}'}
    migrate_state.migrate(bucket, store)
    result = migrate_state.migrate({"reserve.json": b"{}"}, store, dry_run=True)
    assert result["deleted"] == []
    assert migrate_state.migrate({"reserve.json": b"{}"}, store)["deleted"] == []
    assert _read(store, "queue/alpha/0001.started") == b'{"run": "r"}'


def test_the_pass_records_the_bucket_generations_for_the_cutover_floor(store):
    migrate_state.migrate({"queue/gamma/0003.json": b"{}", "queue/alpha/0001.json": b"{}"}, store)
    assert json.loads(_read(store, "migration.json"))["gens"] == {"alpha": 1, "gamma": 3}
