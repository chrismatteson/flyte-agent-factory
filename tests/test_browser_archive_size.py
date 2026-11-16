"""`ops/browser_archive_size.py`: refuses a bad directory; `--synthetic` needs no directory."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
spec = importlib.util.spec_from_file_location("ops_browser_archive_size", OPS / "browser_archive_size.py")
assert spec is not None and spec.loader is not None
size_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(size_tool)


def run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["browser_archive_size.py", *argv])
    size_tool.main()


def test_a_missing_profile_dir_is_refused(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as caught:
        run(monkeypatch, str(tmp_path / "nope"))
    assert caught.value.code == 2
    assert "not a readable directory" in capsys.readouterr().err


def test_synthetic_without_a_dir_uses_a_temp_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(size_tool, "SYNTHETIC", {"Local State": 1000, "Default/Preferences": 2000})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    run(monkeypatch, "--synthetic")
    out = capsys.readouterr().out
    assert "trimmed + encrypted" in out and "profile on disk" in out
    assert list(tmp_path.iterdir()) == []  # the stand-in profile it made is removed again
