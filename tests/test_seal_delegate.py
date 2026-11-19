"""A sealed project's delegate workers: the task repo goes out in the tarball and the edits come back."""

import dataclasses
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from flyte_agent_factory import _delegation, _seal_session
from flyte_agent_factory._delegation import Delegations
from tests._seal_bench import git
from tests._seal_fakes import SPEC
from tests.test_seal_session import _clone

pytest_plugins = ["tests._seal_bench"]

TASK_DIR = Path("projects/x/repo")


@pytest.fixture
def task(tmp_path):
    """A seeded task repo: a base commit and a later `fix` commit that a snapshot must not carry."""
    path = tmp_path / "task"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "mod.py").write_text("x = 1\n")
    (path / "other.py").write_text("y = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    base = git(path, "rev-parse", "HEAD")
    (path / "mod.py").write_text("x = FIXED\n")
    git(path, "commit", "-qam", "fix")
    return path, base, git(path, "rev-parse", "HEAD")


@pytest.fixture
def sealed_workspace(tmp_path, bench, store, task):
    path, base, _ = task
    spec = dataclasses.replace(SPEC, task_repo=str(path), base_sha=base)
    workspace = _clone(bench[0], tmp_path, store)
    git(workspace, "fetch", "--prune", "origin")
    _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")
    return workspace


def _unpack(target: Path, into: Path) -> Path:
    with tarfile.open(target / "repo.tar.gz") as archive:
        archive.extractall(into, filter="data")
    return into / "repo"


def test_a_sealed_snapshot_carries_the_task_repo_at_its_base_commit_and_nothing_else(
    sealed_workspace, tmp_path, task, bench
):
    _, base, fix = task
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(sealed_workspace, target, TASK_DIR)
    copied = _unpack(target, tmp_path / "unpacked")
    nested = copied / TASK_DIR
    assert (nested / "mod.py").read_text() == "x = 1\n"
    assert git(nested, "rev-list", "--all").split() == [base]
    assert git(nested, "cat-file", "-e", fix, check=False).returncode != 0
    # The outer repo neither tracks the task dir (no gitlink) nor holds another project's work.
    assert not any(line.startswith("projects/x/repo") for line in git(copied, "ls-files").splitlines())
    assert not any("other" in ref for ref in git(copied, "for-each-ref", "--format=%(refname)").splitlines())
    assert git(copied, "cat-file", "-e", bench[1], check=False).returncode != 0


def test_an_unsealed_snapshot_leaves_the_git_excluded_task_dir_out(sealed_workspace, tmp_path):
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(sealed_workspace, target)
    assert not (_unpack(target, tmp_path / "unpacked") / TASK_DIR).exists()
    assert not (target / "task-base").exists()


async def _delegate(workspace: Path, root: Path, edit, task_dir=TASK_DIR) -> Path:
    """Run one job whose fake worker unpacks the tarball, runs `edit(repo)` and writes the outer patch."""

    async def worker(target, brief, engine, *, answer_only=False, on_engine=None):
        repo = _unpack(target, target / "result")
        edit(repo)
        base = (target / "base").read_text().strip()
        subprocess.run(["git", "-C", str(repo), "add", "-N", "."], check=True, capture_output=True)
        patch = subprocess.run(
            ["git", "-C", str(repo), "diff", "--binary", base],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        (target / "result" / "factory.patch").write_bytes(patch)
        return "done"

    async def engine():
        return "claude"

    (root / "requests").mkdir(parents=True, exist_ok=True)
    (root / "answers").mkdir(parents=True, exist_ok=True)
    delegations = Delegations(root, workspace, worker, engine, lambda *_: None, task_dir=task_dir)
    await delegations.service({"id": "delegate-1", "brief": "work"})
    return delegations.jobs / "delegate-1"


def _edit(repo: Path) -> None:
    (repo / TASK_DIR / "mod.py").write_text("x = 2\n")
    (repo / TASK_DIR / "new.py").write_text("z = 1\n")
    (repo / "projects" / "x" / "outer.txt").write_text("outer\n")


async def test_the_workers_edits_inside_the_task_dir_reach_the_author(sealed_workspace, tmp_path):
    task_dir = sealed_workspace / TASK_DIR
    (task_dir / "other.py").write_text("y = AUTHOR\n")  # the author's own uncommitted edit goes out too
    (sealed_workspace / "projects" / "x").mkdir(exist_ok=True)
    job = await _delegate(sealed_workspace, tmp_path / "bridge", _edit)
    answer = json.loads((tmp_path / "bridge" / "answers" / "delegate-1.json").read_text())
    assert answer["state"] == "done", answer
    patch = Path(answer["task_patch"])
    assert patch == job / "task.patch"
    text = patch.read_text()
    assert "x = 2" in text and "new.py" in text
    assert "AUTHOR" not in text  # only what the worker did, not what the author already had
    assert "outer.txt" not in text and "projects/x/repo" not in text
    assert "outer.txt" in (job / "factory.patch").read_text()
    assert "mod.py" not in (job / "factory.patch").read_text()
    # What the coordinator does: apply it in the task dir.
    git(task_dir, "apply", "--3way", str(patch))
    assert (task_dir / "mod.py").read_text() == "x = 2\n"
    assert (task_dir / "new.py").read_text() == "z = 1\n"
    assert (task_dir / "other.py").read_text() == "y = AUTHOR\n"


async def test_no_task_edits_means_no_task_patch(sealed_workspace, tmp_path):
    (sealed_workspace / "projects" / "x").mkdir(exist_ok=True)
    job = await _delegate(sealed_workspace, tmp_path / "bridge", lambda repo: None)
    answer = json.loads((tmp_path / "bridge" / "answers" / "delegate-1.json").read_text())
    assert answer["state"] == "done", answer
    assert "task_patch" not in answer
    assert not (job / "task.patch").exists()


async def test_a_worker_that_wrote_no_patch_still_hands_back_its_task_edits(sealed_workspace, tmp_path):
    """The outer fallback (`work_diff`) disarms every nested `.git`: the task diff has to come first."""
    (sealed_workspace / "projects" / "x").mkdir(exist_ok=True)

    async def worker(target, brief, engine, *, answer_only=False, on_engine=None):
        _edit(_unpack(target, target / "result"))
        return "done"

    async def engine():
        return "claude"

    root = tmp_path / "bridge"
    (root / "requests").mkdir(parents=True)
    (root / "answers").mkdir()
    delegations = Delegations(root, sealed_workspace, worker, engine, lambda *_: None, task_dir=TASK_DIR)
    await delegations.service({"id": "delegate-1", "brief": "work"})
    answer = json.loads((root / "answers" / "delegate-1.json").read_text())
    assert answer["state"] == "done", answer
    assert "x = 2" in Path(answer["task_patch"]).read_text()
