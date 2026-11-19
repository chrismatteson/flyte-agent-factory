"""The sealed task checkout: the task repository at its base commit and nothing else.

A fetched clone carries the fix as later history, tags and packed objects, so the checkout is built by
fetching exactly one commit, then everything that could lead to another commit is removed and the
result is verified object by object. Runs outside the seal: this is the step that needs the network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ._secret_env import GIT_SAFE_CONFIG

_GIT_TIMEOUT_SECONDS = 600


def _git(repo: Path, *args: str, check: bool = True) -> str:
    # protocol.file.allow: a task repo given as a local path or file:// URL (tests, operators' mirrors).
    command = ["git", *GIT_SAFE_CONFIG, "-c", "protocol.file.allow=always", *args]
    done = subprocess.run(
        command, cwd=repo, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed in {repo}: {done.stderr.strip()}")
    return done.stdout


def snapshot_task_repo(url_or_path: str, base_sha: str, dest: Path) -> Path:
    """Create `dest` as a repository holding only commit `base_sha` of `url_or_path`, checked out detached.

    Raise `RuntimeError` if the commit cannot be fetched or the result holds anything else.
    """
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise RuntimeError(f"snapshot destination {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q")
    try:
        _git(dest, "fetch", "-q", "--depth", "1", "--no-tags", url_or_path, base_sha)
    except RuntimeError as error:
        raise RuntimeError(f"could not fetch {base_sha} from the task repository: {error}") from None
    _git(dest, "checkout", "-q", "--detach", base_sha)
    _strip_to_head(dest)
    verify_snapshot(dest, base_sha)
    return dest


def _strip_to_head(repo: Path) -> None:
    git_dir = repo / ".git"
    for remote in _git(repo, "remote").split():
        _git(repo, "remote", "remove", remote)
    (git_dir / "FETCH_HEAD").unlink(missing_ok=True)
    shutil.rmtree(git_dir / "logs", ignore_errors=True)
    # `fetch <url> <sha>` writes no refs, but a task repo's own packed-refs or a future git might.
    for line in _git(repo, "for-each-ref", "--format=%(refname)").split():
        _git(repo, "update-ref", "-d", line)
    (git_dir / "packed-refs").unlink(missing_ok=True)
    # A depth-1 fetch records the base as a shallow boundary: that is correct and kept, its parents are not
    # in the repository. Expire anything else so gc can drop it.
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "-q", "--prune=now")


def verify_snapshot(repo: Path, base_sha: str) -> None:
    """Raise `RuntimeError` unless `base_sha` is the only reachable commit and the only commit object."""
    reachable = _git(repo, "rev-list", "--all").split()
    if reachable != [base_sha]:
        raise RuntimeError(f"snapshot reaches {reachable}, expected only the base_sha {base_sha}")
    objects = _git(repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    commits = [line.split()[0] for line in objects if line.split()[1] == "commit"]
    if commits != [base_sha]:
        raise RuntimeError(f"snapshot holds commit objects {commits}, expected only the base_sha {base_sha}")
