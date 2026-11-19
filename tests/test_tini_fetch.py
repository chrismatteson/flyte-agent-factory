"""tests/_tini.py: a stuck fetch is bounded and leaves PATH alone, so the tests that need tini fail loudly."""

import os
import time

from tests import _tini


def _write_tool(bin_dir, name, body):
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)


def test_an_unpack_that_hangs_is_given_up_and_path_is_left_alone(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_tool(bin_dir, "apt-get", ": > tini_0_amd64.deb")
    _write_tool(bin_dir, "dpkg-deb", "exec /bin/sleep 1000")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_tini, "UNPACK_TIMEOUT_S", 1)
    monkeypatch.setattr(_tini, "FETCH_FAILURE", None)
    start = time.monotonic()

    _tini.ensure_on_path()

    assert time.monotonic() - start < 30
    assert _tini.FETCH_FAILURE is not None and "TimeoutExpired" in _tini.FETCH_FAILURE
    assert os.environ["PATH"] == str(bin_dir)


def test_a_download_that_fails_says_why(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_tool(bin_dir, "apt-get", "echo 'E: Unable to locate package tini' >&2; exit 100")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_tini, "FETCH_FAILURE", None)

    _tini.ensure_on_path()

    assert _tini.FETCH_FAILURE is not None and "Unable to locate package tini" in _tini.FETCH_FAILURE
    assert os.environ["PATH"] == str(bin_dir)
