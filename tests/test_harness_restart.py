"""The harness re-seeding `DESIGN.md` after a pod restart is never reported as uncommitted work."""

import asyncio
import hashlib
import os
import subprocess

import pytest

from flyte_agent_factory import _design_seed, _gate_base, _gates, _session_config, _session_setup, _timeline

SEED = _design_seed.SEED
OLD_SEED = "# Design guidance\n\nAn older harness version's seed text.\n"


@pytest.fixture
def repo(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    _gates.capture_base(tmp_path, "proj")
    git("config", "user.email", "proj@agent-factory.localhost")
    return tmp_path, (tmp_path / ".claude-flyte/proj/base-commit").read_text(), git


def _scope(workspace, base):
    rows = {}
    _gates._judge_project(
        workspace,
        "proj",
        base,
        "May edit: src",
        "",
        "DONE",
        [],
        lambda r, c, o: rows.update({r: (c, o)}),
        "root",
    )
    return rows["scope"]


def _uncommitted(workspace):
    return _gates._uncommitted(workspace, "proj", "root")


def test_untracked_seeded_design_is_not_reported_or_judged(repo):
    workspace, base, git = repo
    (workspace / "DESIGN.md").write_text(SEED)
    assert _uncommitted(workspace) == []
    (workspace / "src/lib.py").write_text("x = 2\n")
    git("add", ".")
    git("commit", "-m", "work")
    status, output = _scope(workspace, base)
    assert status is _gate_base.Status.PASS, output


def test_committing_the_seed_anyway_does_not_fail_scope(repo):
    workspace, base, git = repo
    (workspace / "DESIGN.md").write_text(SEED)
    (workspace / "src/lib.py").write_text("x = 2\n")
    git("add", "-A")
    git("commit", "-m", "work")
    status, output = _scope(workspace, base)
    assert status is _gate_base.Status.PASS, output


def test_untracked_edited_design_is_reported(repo):
    workspace, _, _ = repo
    (workspace / "DESIGN.md").write_text(SEED + "\nMy own rule.\n")
    assert _uncommitted(workspace) == ["DESIGN.md"]


def test_tracked_design_with_edits_is_reported(repo):
    workspace, _, git = repo
    (workspace / "DESIGN.md").write_text("# Mine\n")
    git("add", "DESIGN.md")
    git("commit", "-m", "design")
    (workspace / "DESIGN.md").write_text(SEED)  # even byte-identical to the seed: it is tracked
    assert _uncommitted(workspace) == ["DESIGN.md"]


def test_old_seed_recorded_by_hash_is_not_reported(repo):
    workspace, _, _ = repo
    (workspace / "DESIGN.md").write_text(OLD_SEED)
    assert _uncommitted(workspace) == ["DESIGN.md"]  # unknown text is the project's
    hashes = workspace / _design_seed.HASHES_FILE
    hashes.parent.mkdir(parents=True, exist_ok=True)
    hashes.write_text(hashlib.sha256(OLD_SEED.encode()).hexdigest() + "\n")
    assert _uncommitted(workspace) == []


def test_a_seed_from_before_hashes_were_recorded_is_not_reported(repo, monkeypatch):
    # Seeded 2026-10-13..11-04 by a harness that kept no hashes file; a restart never re-seeds it.
    workspace, _, _ = repo
    monkeypatch.setattr(
        _design_seed, "EARLIER_SEEDS", frozenset({hashlib.sha256(OLD_SEED.encode()).hexdigest()})
    )
    (workspace / "DESIGN.md").write_text(OLD_SEED)
    assert not (workspace / _design_seed.HASHES_FILE).exists()
    assert _uncommitted(workspace) == []
    (workspace / "DESIGN.md").write_text(OLD_SEED + "mine\n")
    assert _uncommitted(workspace) == ["DESIGN.md"]


def test_seeding_records_the_hash_once_and_keeps_older_ones(tmp_path):
    hashes = tmp_path / _design_seed.HASHES_FILE
    hashes.parent.mkdir(parents=True)
    hashes.write_text("abc\n")
    for _ in range(2):
        (tmp_path / "DESIGN.md").write_text(SEED)
        _session_setup._seed_team(tmp_path)
    assert hashes.read_text().split() == ["abc", hashlib.sha256(SEED.encode()).hexdigest()]


def test_an_edited_design_is_not_recorded(tmp_path):
    (tmp_path / "DESIGN.md").write_text(SEED + "mine\n")
    _session_setup._seed_team(tmp_path)
    assert not (tmp_path / _design_seed.HASHES_FILE).exists()


def _prepare(monkeypatch, workspace, resuming):
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    asyncio.run(
        _session_setup._prepare_workspace(
            brief="Do the work", repo="", branch="", resuming=resuming, timeline=_timeline._Timeline()
        )
    )


def test_restart_then_done_reports_no_harness_file(repo, monkeypatch):
    workspace, base, git = repo
    _prepare(monkeypatch, workspace, resuming=False)
    (workspace / "src/lib.py").write_text("x = 2\n")
    git("add", "src")
    git("commit", "-m", "work")
    assert _uncommitted(workspace) == []

    (workspace / "DESIGN.md").unlink()  # the pod restarted from a checkpoint without it
    _prepare(monkeypatch, workspace, resuming=True)

    assert (workspace / "DESIGN.md").read_text() == SEED
    assert _uncommitted(workspace) == []
    status, output = _scope(workspace, base)
    assert status is _gate_base.Status.PASS, output
    _, porcelain = _gate_base._run(["git", "status", "--porcelain", "--untracked-files=no"], workspace)
    assert porcelain == ""


def test_restart_never_overwrites_a_tracked_design(repo, monkeypatch):
    workspace, _, git = repo
    (workspace / "DESIGN.md").write_text("# Project's own\n")
    git("add", "DESIGN.md")
    git("commit", "-m", "design")
    _prepare(monkeypatch, workspace, resuming=True)
    assert (workspace / "DESIGN.md").read_text() == "# Project's own\n"
    assert _uncommitted(workspace) == []
