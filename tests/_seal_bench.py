"""Fixtures for the sealed-run tests that need a git remote: a benchmark workspace repo and a push token.

Loaded with `pytest_plugins` (a fixture imported into a test module and used as a parameter trips ruff F811).
"""

import os
import subprocess

import pytest

from flyte_agent_factory import _git_https

IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def git(cwd, *args, check=True):
    done = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **IDENTITY}, capture_output=True, text=True, check=check
    )
    return done.stdout.strip() if check else done


@pytest.fixture
def bench(tmp_path):
    """A bare benchmark workspace repo: `main` with `projects/`, and another project's branch."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "projects").mkdir()
    (seed / "projects" / ".gitkeep").write_text("")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "template")
    git(seed, "checkout", "-qb", "factory/other")
    (seed / "projects" / "other.txt").write_text("OTHER-ARMS-WORK")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "other project")
    foreign = git(seed, "rev-parse", "HEAD")
    git(seed, "checkout", "-q", "main")
    bare = tmp_path / "bench.git"
    git(tmp_path, "clone", "-q", "--bare", seed.as_uri(), str(bare))
    return bare, foreign


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "push-credentials"
    _git_https.write_token(path, "ghs_firsttoken")
    return path
