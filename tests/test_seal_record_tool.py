"""`ops/seal_record.py`: copy a sealed project's record out of its checkpoint."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"


def _load():
    spec = importlib.util.spec_from_file_location("seal_record", OPS / "seal_record.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seal_record = _load()


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "state"
    role = root / "seal-record" / "roles" / "author"
    role.mkdir(parents=True)
    (root / "seal-record" / "manifest.json").write_text("{}")
    (role / "end.json").write_text("{}")
    (root / "other.txt").write_text("not the record")
    return root


def test_record_dir_is_the_one_the_session_writes():
    from flyte_agent_factory import _session_config

    assert _session_config.SEAL_RECORD.name == seal_record.RECORD_DIR


def test_copy_record_copies_only_the_record(tree, tmp_path):
    dest = tmp_path / "out"
    assert seal_record.copy_record(tree, dest) == dest
    assert (dest / "manifest.json").is_file()
    assert (dest / "roles" / "author" / "end.json").is_file()
    assert not (dest / "other.txt").exists()


def test_copy_record_without_a_record_raises(tmp_path):
    (tmp_path / "state").mkdir()
    with pytest.raises(FileNotFoundError, match="seal-record"):
        seal_record.copy_record(tmp_path / "state", tmp_path / "out")


def test_copy_record_refuses_an_existing_destination(tree, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        seal_record.copy_record(tree, dest)


def _patch_remote(monkeypatch, note, loaded):
    seen = {}

    class Checkpoint:
        def __init__(self, checkpoint_dest, checkpoint_src):
            seen["dest"], seen["src"] = checkpoint_dest, checkpoint_src

        def load_sync(self):
            return loaded

    monkeypatch.setattr(seal_record.remote.Run, "get", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(seal_record._factory_recreate, "_report_note", lambda run: note)
    monkeypatch.setattr(seal_record.flyte, "Checkpoint", Checkpoint)
    return seen


def test_fetch_loads_the_checkpoint_the_report_names(monkeypatch, tree, tmp_path):
    seen = _patch_remote(monkeypatch, {"checkpoint": "s3://bucket/ckpt"}, tree)
    dest = seal_record.fetch("run-1", tmp_path / "out")
    assert seen == {"dest": "s3://bucket/ckpt", "src": "s3://bucket/ckpt"}
    assert (dest / "manifest.json").is_file()


def test_fetch_without_a_checkpoint_location_raises(monkeypatch, tmp_path):
    _patch_remote(monkeypatch, {}, None)
    with pytest.raises(LookupError, match="no checkpoint location"):
        seal_record.fetch("run-1", tmp_path / "out")


def test_fetch_of_an_empty_checkpoint_raises(monkeypatch, tmp_path):
    _patch_remote(monkeypatch, {"checkpoint": "s3://bucket/ckpt"}, None)
    with pytest.raises(LookupError, match="empty or unreadable"):
        seal_record.fetch("run-1", tmp_path / "out")
