"""A deploy works when the temp dir is reached through a symlink (macOS: /var -> /private/var).

The deploy child runs with `cwd` set to the tree's `src`, so the modules it imports get their resolved
path (`getcwd()` is always resolved, on Linux too), while the root it hands Flyte's code bundler came from
argv. Both must be the same spelling of the directory, or the bundler's `relative_to` fails.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from flyte_agent_factory import _deploy, _profile
from tests._git import git


@pytest.fixture
def symlinked_tmp(tmp_path, monkeypatch):
    """Point `tempfile` at a symlink to a real directory, as macOS's $TMPDIR is."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    return link


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    (path / "README").write_text("tree\n")
    git(path, "add", "README")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return path


def test_worktree_is_resolved_under_a_symlinked_temp_dir(symlinked_tmp, repo):
    with _deploy.worktree(repo, git(repo, "rev-parse", "HEAD"), "deploy-test-") as tree:
        assert tree.is_dir()
        assert tree == tree.resolve()


def test_fetched_tree_is_resolved_under_a_symlinked_temp_dir(symlinked_tmp, repo):
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    with _deploy.fetched_tree(str(repo), git(repo, "rev-parse", "HEAD")) as tree:
        assert (tree / "README").is_file()
        assert tree == tree.resolve()


def test_deploy_child_names_modules_under_a_symlinked_root(tmp_path):
    """The child's root and its modules' paths agree, so Flyte can name a task's module (the real failure).

    `_run_child` is given the symlinked spelling, as a worktree under an unresolved temp dir would give it.
    """
    real = tmp_path / "real-src"
    (real / "probe_pkg").mkdir(parents=True)
    (real / "probe_pkg" / "__init__.py").write_text("")
    (real / "probe_pkg" / "tasks.py").write_text("def work():\n    return 1\n")
    link = tmp_path / "link-src"
    link.symlink_to(real, target_is_directory=True)
    script = _deploy._CHILD_START + (
        "from flyte._module import extract_obj_module\n"
        "from probe_pkg import tasks\n"
        "print(extract_obj_module(tasks.work, root))\n"
    )
    tail: list[str] = []
    code = _deploy._run_child(
        _profile.require(), script, link, dict(os.environ), dry_run=True, stderr_tail=tail
    )
    assert code == 0, tail
