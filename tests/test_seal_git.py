"""Harness git on a sealed role's repositories runs nothing the role planted and fetches from its own URL."""

import asyncio
import dataclasses
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _checkpoint,
    _delegation,
    _judge_inputs,
    _push_guard,
    _seal_git,
    _seal_role,
    _seal_session,
    _secret_env,
    _workspace,
)
from tests._seal_bench import git
from tests._seal_fakes import SPEC
from tests.test_seal_session import _clone

pytest_plugins = ["tests._seal_bench"]

TASK_DIR = Path("projects/x/repo")


@pytest.fixture(autouse=True)
def _disarmed():
    yield
    _seal_git.disarm_all()


@pytest.fixture
def sealed(tmp_path, bench, store):
    """A sealed project's armed workspace clone and nested task repo, and the repo the role will point at."""
    task = tmp_path / "task"
    task.mkdir()
    git(task, "init", "-q", "-b", "main")
    (task / "mod.py").write_text("x = 1\n")
    git(task, "add", ".")
    git(task, "commit", "-qm", "base")
    spec = dataclasses.replace(SPEC, task_repo=str(task), base_sha=git(task, "rev-parse", "HEAD"))
    bare = bench[0]
    workspace = _clone(bare, tmp_path, store)
    _seal_session.arm_workspace(workspace, bare.as_uri(), store)
    git(workspace, "fetch", "--prune", "origin")
    _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")
    # What the role will point origin at: the same repository with the reference fix on `main`.
    fix = tmp_path / "fix.git"
    git(tmp_path, "clone", "-q", "--bare", bare.as_uri(), str(fix))
    scratch = tmp_path / "fix-work"
    git(tmp_path, "clone", "-q", fix.as_uri(), str(scratch))
    (scratch / "FIX").write_text("THE-REFERENCE-FIX")
    git(scratch, "add", ".")
    git(scratch, "commit", "-qm", "the fix")
    git(scratch, "push", "-q", "origin", "main")
    return workspace, bare, fix, git(fix, "rev-parse", "main")


def _script(path: Path, marker: Path, body: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho ran >> {marker}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _plant(repo: Path, tmp_path: Path, marker: Path, origin: str | None = None) -> None:
    """What a sealed role can do to a repository it owns, as plain files and `git config` (no seal needed)."""
    hooks = repo / ".git" / "hooks"
    for name in ("reference-transaction", "post-checkout", "pre-commit", "post-commit", "post-merge"):
        _script(hooks / name, marker)
    other_hooks = tmp_path / f"hooks-{repo.name}"
    for name in ("reference-transaction", "pre-commit", "post-checkout"):
        _script(other_hooks / name, marker)
    monitor = _script(tmp_path / f"fsmonitor-{repo.name}", marker)
    clean = _script(tmp_path / f"clean-{repo.name}", marker, "cat\n")
    included = tmp_path / f"included-{repo.name}"
    included.write_text(f"[core]\n\tfsmonitor = {monitor}\n\thooksPath = {other_hooks}\n")
    if origin is not None:
        git(repo, "remote", "set-url", "origin", origin)
        git(repo, "config", "url." + origin + ".insteadOf", "https://github.com/")
        git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(
            repo,
            "config",
            "remote.origin.uploadpack",
            str(_script(tmp_path / "up", marker, 'exec git-upload-pack "$@"\n')),
        )
    git(repo, "config", "core.fsmonitor", str(monitor))
    git(repo, "config", "core.hooksPath", str(other_hooks))
    git(repo, "config", "core.sshCommand", str(_script(tmp_path / f"ssh-{repo.name}", marker)))
    git(repo, "config", "include.path", str(included))
    git(repo, "config", "credential.helper", f"!{_script(tmp_path / f'cred-{repo.name}', marker)}")
    git(repo, "config", "filter.x.clean", str(clean))
    git(repo, "config", "filter.x.smudge", str(clean))
    git(repo, "config", "diff.x.textconv", str(clean))
    (repo / ".gitattributes").write_text("* filter=x diff=x\n")
    (repo / "dirty.txt").write_text("changed\n")


def _harness_git_calls(workspace: Path, tmp_path: Path) -> None:
    """Every kind of harness git call on the workspace and the task repo, as the supervisor makes them."""
    _seal_session.limit_fetch(workspace, "x")
    _seal_session.claim_branch(workspace, "x")
    _seal_session.refresh_upstream(workspace, "x")
    for args in (
        ("rev-parse", "--git-dir"),
        ("status", "--porcelain", "--untracked-files=no"),
        ("remote",),
        ("rev-list", "--count", "@{u}..HEAD"),
        ("rev-parse", "--abbrev-ref", "HEAD"),
    ):
        asyncio.run(_checkpoint._git_out(workspace, *args))
    asyncio.run(_checkpoint._git_head(workspace))
    _judge_inputs._worktree_clean_sync(workspace)
    _workspace.run_git(workspace, "status", "--porcelain")
    _workspace.run_git(workspace / TASK_DIR, "status", "--porcelain")
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(workspace, target, TASK_DIR)


def test_the_harness_runs_nothing_the_role_planted_and_fetches_from_its_own_url(sealed, tmp_path):
    workspace, bare, fix, fix_sha = sealed
    marker = tmp_path / "MARKER"
    _plant(workspace, tmp_path, marker, origin=fix.as_uri())
    _plant(workspace / TASK_DIR, tmp_path, marker)
    (workspace / TASK_DIR / "dirty.txt").write_text("task edit\n")

    _harness_git_calls(workspace, tmp_path)

    assert not marker.exists(), marker.read_text()  # no hook, fsmonitor, filter, helper or uploadpack ran
    refs = git(workspace, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    assert not any(fix_sha in ref for ref in refs)  # the fix never reached an `origin/*` ref
    assert git(workspace, "cat-file", "-e", fix_sha, check=False).returncode != 0
    assert git(workspace, "config", "--get", "remote.origin.url") == bare.as_uri()
    allowed = {"core", "user", "remote", "branch", "url", "push"}
    for repo in (workspace, workspace / TASK_DIR):
        names = git(repo, "config", "--local", "--list", "--name-only").splitlines()
        assert {name.split(".")[0] for name in names} <= allowed, names
        assert not [n for n in names if n.startswith(("include", "filter", "credential", "diff"))], names
        assert "core.fsmonitor" not in names and "core.hookspath" not in names
    assert git(workspace / TASK_DIR, "config", "--local", "--get-regexp", "^remote", check=False).returncode


def test_the_snapshot_copy_and_its_commit_carry_no_planted_config_and_run_no_hook(sealed, tmp_path):
    workspace, bare, fix, _ = sealed
    marker = tmp_path / "MARKER"
    _plant(workspace, tmp_path, marker, origin=fix.as_uri())
    _plant(workspace / TASK_DIR, tmp_path, marker)
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(workspace, target, TASK_DIR)
    assert not marker.exists()
    import tarfile

    with tarfile.open(target / "repo.tar.gz") as archive:
        archive.extractall(tmp_path / "unpacked", filter="data")
    copied = tmp_path / "unpacked" / "repo"
    for repo in (copied, copied / TASK_DIR):
        names = git(repo, "config", "--local", "--list", "--name-only").splitlines()
        assert not [n for n in names if n.startswith(("include", "filter", "credential", "core.hookspath"))]
        assert "core.fsmonitor" not in names
    assert git(copied, "config", "--get", "remote.origin.url") == bare.as_uri()
    # The commit the snapshot made ("Delegation base") exists although the pre-commit hook was planted.
    assert git(copied, "log", "-1", "--format=%s") == "Delegation base"


def test_sanitize_keeps_what_the_harness_and_the_role_legitimately_set(sealed, tmp_path):
    workspace, bare, _, _ = sealed
    _seal_session.route_pushes(workspace, bare.as_uri())
    git(workspace, "config", "user.name", "Role")
    git(workspace, "config", "user.email", "role@example.com")
    git(workspace, "config", "branch.main.remote", "origin")
    git(workspace, "config", "push.default", "current")
    git(workspace, "config", "include.path", "/etc/passwd")
    _workspace.run_git(workspace, "status")
    assert git(workspace, "config", "--get", "user.name") == "Role"
    assert git(workspace, "config", "--get", "user.email") == "role@example.com"
    assert git(workspace, "config", "--get", "branch.main.remote") == "origin"
    assert git(workspace, "config", "--get", "push.default") == "current"
    assert git(workspace, "config", "--get", f"url.{_seal_role.PUSH_ROUTE}.pushInsteadOf") == bare.as_uri()
    assert git(workspace, "config", "--get", "include.path", check=False).returncode != 0


def test_the_harness_sets_the_fetch_refspecs_whatever_the_role_wrote(sealed):
    workspace, *_ = sealed
    git(workspace, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(workspace, "config", "--add", "remote.origin.fetch", "+refs/heads/factory/other:refs/x/y")
    _workspace.run_git(workspace, "status")
    assert git(workspace, "config", "--get-all", "remote.origin.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/origin/main"
    ]
    _seal_session.limit_fetch(workspace, "x")
    git(
        workspace,
        "config",
        "--add",
        "remote.origin.fetch",
        "+refs/heads/factory/other:refs/remotes/origin/other",
    )
    _workspace.run_git(workspace, "status")
    assert "other" not in git(workspace, "config", "--get-all", "remote.origin.fetch")


def test_the_role_keeps_the_pre_push_hook_the_harness_installed(sealed):
    workspace, bare, *_ = sealed
    _seal_session.route_pushes(workspace, bare.as_uri())
    hook = workspace / ".git" / "hooks" / "pre-push"
    assert hook.is_file() and os.access(hook, os.X_OK)
    _workspace.run_git(workspace, "status")
    assert hook.is_file()


def test_a_git_directory_that_is_not_a_plain_directory_is_refused(sealed, tmp_path):
    workspace, *_ = sealed
    moved = tmp_path / "elsewhere.git"
    (workspace / ".git").rename(moved)
    (workspace / ".git").symlink_to(moved)
    with pytest.raises(_seal_git.UnsafeRepo):
        _workspace.run_git(workspace, "status")


def test_an_unarmed_repository_is_left_alone(tmp_path):
    repo = tmp_path / "ordinary"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "include.path", "/nonexistent")
    git(repo, "config", "alias.zz", "status")
    before = (repo / ".git" / "config").read_text()
    _workspace.run_git(repo, "status")
    assert (repo / ".git" / "config").read_text() == before
    assert _seal_git.config_args() == ()
    assert _seal_git.harness_env(repo) is None


def test_the_harness_env_names_the_credential_and_turns_off_the_rest(sealed, store):
    workspace, *_ = sealed
    env = _seal_git.harness_env(
        workspace, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    )
    assert env is not None
    pairs = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]
    assert pairs[0] == ("a.b", "c")  # a caller's own entry stays
    assert ("core.hooksPath", "/dev/null") in pairs and ("core.fsmonitor", "false") in pairs
    assert ("credential.helper", f"store --file={store}") in pairs
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull


# --- escapes a role can make with plain file operations in the `.git` it owns ---------------------------


def _clean_filter(marker: Path) -> str:
    return f'[filter "x"]\n\tclean = "touch {marker}; cat"\n'


def _tracked_file_with_stale_stat(workspace: Path) -> None:
    """A tracked file that differs from the index at the same size, so `status` must run its clean filter."""
    (workspace / "a.txt").write_text("hello\n")
    git(workspace, "add", "a.txt")
    git(workspace, "commit", "-qm", "a")
    (workspace / ".gitattributes").write_text("* filter=x\n")
    (workspace / "a.txt").write_text("jello\n")
    os.utime(workspace / "a.txt", ns=(1, 1))


def test_a_config_rewritten_in_place_with_the_old_size_and_mtime_is_sanitised_again(sealed, tmp_path):
    workspace, *_ = sealed
    marker = tmp_path / "PWNED"
    _workspace.run_git(workspace, "status")  # sanitises and remembers what it left
    config = workspace / ".git" / "config"
    seen = config.stat()
    original = config.read_bytes()
    evil = _clean_filter(marker).encode()
    evil += b"#" * (len(original) - len(evil) - 1) + b"\n"
    with open(config, "r+b") as handle:  # the same inode and size
        handle.write(evil)
    os.utime(config, ns=(seen.st_atime_ns, seen.st_mtime_ns))  # the same mtime
    _tracked_file_with_stale_stat(workspace)
    _workspace.run_git(workspace, "status", "--porcelain")
    assert not marker.exists()
    assert "filter" not in config.read_text()


def test_a_planted_config_harness_symlink_is_not_written_through(sealed, tmp_path):
    workspace, *_ = sealed
    victim = tmp_path / "victim_bashrc"
    victim.write_text("# the harness user's own file\n")
    (workspace / ".git" / "config.harness").symlink_to(victim)
    git(workspace, "config", "include.path", "/nonexistent")  # dropped, so the config is rewritten
    git(workspace, "config", "user.name", "$(touch PWNED2)")
    _workspace.run_git(workspace, "status")
    assert victim.read_text() == "# the harness user's own file\n"
    assert git(workspace, "config", "--get", "user.name") == "$(touch PWNED2)"  # kept, as ever


def test_a_config_harness_directory_does_not_stop_the_sanitiser(sealed):
    workspace, *_ = sealed
    (workspace / ".git" / "config.harness").mkdir()
    git(workspace, "config", "include.path", "/nonexistent")
    _workspace.run_git(workspace, "status")
    assert git(workspace, "config", "--get", "include.path", check=False).returncode != 0


@pytest.mark.parametrize("mode", ["missing", "emptydir"])
def test_a_bare_repository_at_the_worktree_root_is_never_what_the_harness_runs_git_in(sealed, tmp_path, mode):
    workspace, bare, *_ = sealed
    git_dir = workspace / ".git"
    for name in ("HEAD", "objects", "refs"):
        (git_dir / name).rename(workspace / name)
    for leftover in git_dir.iterdir():
        leftover.unlink() if not leftover.is_dir() else shutil.rmtree(leftover)
    if mode == "missing":
        git_dir.rmdir()
    marker = tmp_path / "PWNED3"
    (workspace / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
        f'\tsshCommand = "touch {marker}; false"\n'
        f'[remote "origin"]\n\turl = {bare.as_uri()}\n'
        f'\tuploadpack = "touch {marker}; git-upload-pack"\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )
    with pytest.raises(_seal_git.UnsafeRepo):
        _workspace.run_git(workspace, "fetch", "origin")
    with pytest.raises(_seal_git.UnsafeRepo):
        _secret_env.git_argv("status")  # every other harness path asks the sanitiser first
    # Even a call that does get as far as git does not use the root as a repository.
    env = {**os.environ, "GIT_CONFIG_COUNT": "0"}
    argv = ["git", *_seal_git.config_args(), "fetch", "origin"]
    assert subprocess.run(argv, cwd=workspace, env=env, capture_output=True).returncode != 0
    assert not marker.exists()


def test_harness_git_runs_in_the_named_git_dir_and_still_works_from_a_subdirectory(sealed):
    workspace, *_ = sealed
    env = _seal_git.harness_env(workspace)
    assert env is not None
    assert (env["GIT_DIR"], env["GIT_WORK_TREE"]) == (str(workspace / ".git"), str(workspace))
    copy_env = _seal_git.harness_env(workspace, root=workspace / "elsewhere")
    assert copy_env is not None and copy_env["GIT_DIR"] == str(workspace / "elsewhere" / ".git")
    assert (
        _workspace.run_git(workspace, "-C", "projects", "rev-parse", "--show-prefix").strip() == "projects/"
    )
    assert _workspace.run_git(workspace, "status", "--porcelain") == ""


def _nested_repo_with_clean_filter(workspace: Path, marker: Path, *, stage: bool = True) -> Path:
    sub = workspace / "sub"
    git(workspace, "init", "-q", str(sub))
    (sub / "f").write_text("aaaa\n")
    git(sub, "add", "f")
    git(sub, "commit", "-qm", "s")
    if stage:
        git(workspace, "add", "sub")  # the role stages a gitlink
    (workspace / ".gitmodules").write_text(
        '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = none\n'
    )
    git(sub, "config", "filter.x.clean", f"touch {marker}; cat")
    (sub / ".gitattributes").write_text("* filter=x\n")
    (sub / "f").write_text("bbbb\n")
    os.utime(sub / "f", ns=(1, 1))
    return sub


@pytest.mark.parametrize("which", ["status", "diff", "judge"])
def test_a_nested_repository_the_role_added_is_not_entered_by_status_or_diff(sealed, tmp_path, which):
    workspace, *_ = sealed
    marker = tmp_path / "PWNED4"
    _nested_repo_with_clean_filter(workspace, marker)
    if which == "status":
        _workspace.run_git(workspace, "status", "--porcelain")
    elif which == "diff":
        _workspace.run_git(workspace, "diff", "HEAD")
    else:
        _judge_inputs._worktree_clean_sync(workspace)
    assert not marker.exists()


def test_the_snapshot_does_not_enter_a_nested_repository_the_role_added(sealed, tmp_path):
    workspace, *_ = sealed
    marker = tmp_path / "PWNED4"
    sub = _nested_repo_with_clean_filter(workspace, marker, stage=False)  # an untracked nested `.git`
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(workspace, target, TASK_DIR)
    assert sub.is_dir() and not marker.exists()


def test_the_snapshot_ships_nothing_a_symlink_in_git_points_at(sealed, tmp_path):
    workspace, *_ = sealed
    secret = tmp_path / "harness_secret_token"
    secret.write_text("SECRET-TOKEN-1234\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "harness_token").write_text("DIR-SECRET\n")
    task_git = workspace / TASK_DIR / ".git"
    for git_dir in (workspace / ".git", task_git):
        (git_dir / "description.bak").symlink_to(secret)
        (git_dir / "info" / "loot").symlink_to(secrets)
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(workspace, target, TASK_DIR)
    with tarfile.open(target / "repo.tar.gz") as archive:
        names = archive.getnames()
        assert not [n for n in names if "description.bak" in n or "/loot" in n], names
        for member in archive.getmembers():
            if member.isfile():
                data = archive.extractfile(member)
                assert (
                    data is not None
                    and b"SECRET-TOKEN" not in data.read()
                    and b"DIR-SECRET" not in data.read()
                )


def test_an_exclude_file_replaced_by_a_symlink_is_replaced_not_written_through(sealed, tmp_path):
    workspace, *_ = sealed
    victim = tmp_path / "victim"
    victim.write_text("important=1\n")
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.unlink(missing_ok=True)
    exclude.symlink_to(victim)
    spec = dataclasses.replace(SPEC, task_repo="unused", base_sha="0" * 40)
    _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")  # the task repo is already there
    assert victim.read_text() == "important=1\n"
    assert not exclude.is_symlink()
    assert f"/{(workspace / TASK_DIR).relative_to(workspace).as_posix()}/" in exclude.read_text().splitlines()


def test_a_pre_push_symlink_is_replaced_not_written_through(sealed, tmp_path):
    workspace, *_ = sealed
    victim = tmp_path / "victim.py"
    victim.write_text("print('harness code')\n")
    victim.chmod(0o644)
    hook = workspace / ".git" / "hooks" / "pre-push"
    hook.unlink(missing_ok=True)
    hook.symlink_to(victim)
    _push_guard.install_hook(workspace, "http://route/x")
    assert victim.read_text() == "print('harness code')\n" and stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert not hook.is_symlink() and os.access(hook, os.X_OK)


def test_a_fifo_config_is_replaced_without_waiting_for_it(sealed):
    workspace, *_ = sealed
    config = workspace / ".git" / "config"
    config.unlink()
    os.mkfifo(config)
    _workspace.run_git(workspace, "status")  # a read of the FIFO would block
    assert stat.S_ISREG(config.lstat().st_mode)
    assert git(workspace, "config", "--get", "remote.origin.url")


def test_alternates_a_clone_of_the_harness_never_had_are_removed(sealed, tmp_path):
    workspace, *_ = sealed
    info = workspace / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    for name in ("alternates", "http-alternates"):
        (info / name).write_text(f"{tmp_path}/role-objects\n")
    _workspace.run_git(workspace, "status")
    assert not (info / "alternates").exists() and not (info / "http-alternates").exists()
