import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _seal_snapshot

MARKER = "SEAL_FIX_MARKER_7f3a"


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin", "HOME": str(cwd)}  # fmt: skip
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def seeded(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "mod.py").write_text("def f():\n    return 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    base = _git(origin, "rev-parse", "HEAD")
    (origin / "mod.py").write_text(f"def f():\n    return 2  # {MARKER}\n")
    _git(origin, "commit", "-qam", "fix")
    fix = _git(origin, "rev-parse", "HEAD")
    _git(origin, "tag", "v-fixed")
    _git(origin, "branch", "fix-branch")
    return origin, base, fix


@pytest.mark.parametrize("as_url", [False, True])
def test_snapshot_holds_only_the_base_commit(seeded, tmp_path, as_url):
    origin, base, fix = seeded
    source = origin.as_uri() if as_url else str(origin)
    dest = tmp_path / "snap"
    assert _seal_snapshot.snapshot_task_repo(source, base, dest) == dest
    assert (dest / "mod.py").read_text() == "def f():\n    return 1\n"
    assert _git(dest, "log", "--all", "--format=%H").split() == [base]
    assert _git(dest, "rev-parse", "HEAD") == base
    assert subprocess.run(["git", "cat-file", "-e", fix], cwd=dest).returncode != 0
    grep = subprocess.run(["git", "grep", MARKER], cwd=dest, capture_output=True)
    assert grep.returncode == 1
    assert _git(dest, "remote") == ""
    assert not (dest / ".git" / "FETCH_HEAD").exists()
    assert _git(dest, "for-each-ref") == ""
    assert not (dest / ".git" / "logs").exists()
    assert _git(dest, "tag") == ""
    assert not any(MARKER in p.read_text(errors="ignore") for p in (dest / ".git").rglob("*") if p.is_file())


def test_snapshot_refuses_unknown_sha(seeded, tmp_path):
    origin, _, _ = seeded
    with pytest.raises(RuntimeError, match="fetch"):
        _seal_snapshot.snapshot_task_repo(str(origin), "0" * 40, tmp_path / "snap")


def test_snapshot_refuses_non_empty_destination(seeded, tmp_path):
    origin, base, _ = seeded
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(RuntimeError, match="not empty"):
        _seal_snapshot.snapshot_task_repo(str(origin), base, dest)


def test_verify_rejects_extra_commit(seeded, tmp_path):
    origin, base, _ = seeded
    dest = tmp_path / "snap"
    _seal_snapshot.snapshot_task_repo(str(origin), base, dest)
    (dest / "new").write_text("n")
    _git(dest, "add", ".")
    _git(dest, "commit", "-qm", "extra")
    with pytest.raises(RuntimeError, match="base_sha"):
        _seal_snapshot.verify_snapshot(dest, base)
