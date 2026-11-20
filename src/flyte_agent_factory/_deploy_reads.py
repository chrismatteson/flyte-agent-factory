"""What a deploy reads from the direction repository, and whether a range of commits touches any of it.

The direction repo is also where the operator writes `operator/STATE.md` many times an hour and where
`projects/**` lands; none of that is read by a deploy, which reads only the profile file and the standards it
captures (`_profile._with_tree` -> `_standards.capture`). `explain_skip` lets the deployer leave such commits
alone. Anything it cannot establish is a deploy: a skip is only ever the answer to a positive proof.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

# Deploy tools also load `_deployer` by path, without a package context.
if TYPE_CHECKING:
    from . import _github, _profile, _standards
    from ._errors import REMOTE_ERRORS
else:
    from flyte_agent_factory import _github, _profile, _standards
    from flyte_agent_factory._errors import REMOTE_ERRORS

_TREE_MODE = "040000"
_LINK_MODE = "120000"


def list_read_paths(profile: _profile.Profile) -> tuple[str, ...] | None:
    """Repo-relative POSIX paths and directory prefixes the deploy reads, or None when that is not knowable.

    A path is a read when it equals an entry or lies below it. Both places `_standards._resolve` may pick
    (beside the profile, else the repo root) are listed: which one wins depends on what exists at the commit.
    "." is the whole repository.
    """
    direction = profile.direction
    if direction is None:
        return None
    standards = profile.standards
    values = [
        direction.profile,
        standards.dir or _standards.DEFAULT_DIR,
        *([standards.brand] if standards.brand else []),
    ]
    # The loader hands a `..` to the filesystem as it is, so which place exists can turn on a directory the
    # read set does not name.
    if any(v.startswith(("/", "~")) or ".." in v.split("/") for v in values):
        return None
    beside = posixpath.dirname(posixpath.normpath(direction.profile))
    paths = [posixpath.normpath(direction.profile)]
    for value in values[1:]:
        paths += [posixpath.normpath(posixpath.join(beside, value)), posixpath.normpath(value)]
    return tuple(dict.fromkeys(paths))


def _is_read(path: str, reads: tuple[str, ...]) -> bool:
    return any(r == "." or path == r or path.startswith(r + "/") for r in reads)


def _fetch_tree(
    reader: _github.GithubApp, repo: str, sha: str, cache: dict[str, list[_github.Json]]
) -> list[_github.Json]:
    if sha not in cache:
        cache[sha] = reader.list_tree_entries(repo, sha)
    return cache[sha]


def _read_path_object(
    reader: _github.GithubApp, repo: str, commit: str, path: str, cache: dict[str, list[_github.Json]]
) -> tuple[str, str] | None:
    """Git `(mode, sha)` of `path` at `commit`, or None when it is absent there.

    One tree request per path component, none for a tree already fetched (`cache`, keyed by sha, so the two
    commits share every subtree they have in common). Equal objects are equal content, whatever else moved.
    A link met on the way is returned as the object, so a link that is or contains the path shows as one.
    """
    entry: _github.Json = {"sha": commit}
    for name in path.split("/"):
        if entry.get("mode") == _LINK_MODE:
            break
        if entry.get("type", "tree") != "tree":
            return None  # a file where a directory should be
        found = [e for e in _fetch_tree(reader, repo, entry["sha"], cache) if e.get("path") == name]
        if not found:
            return None
        entry = found[0]
    return str(entry["mode"]), str(entry["sha"])


def _may_hold_link(reader: _github.GithubApp, repo: str, obj: tuple[str, str] | None) -> bool:
    """Whether the read object `obj` is a link, or a tree with a link in it or too large to list whole.

    A deploy follows links (the profile is resolved, standards are read with `read_text`), so what it reads
    through one is a path the read set does not name, and a comparison cannot clear it. Only the read path's
    own subtree is listed, so the cost is the size of the read set, not of the repository.
    """
    if obj is None:
        return False
    mode, sha = obj
    if mode == _LINK_MODE:
        return True
    if mode != _TREE_MODE:
        return False
    tree = reader.get_recursive_tree(repo, sha)
    entries = tree.get("tree")
    if tree.get("truncated") or not isinstance(entries, list):
        return True
    return any(e.get("mode") == _LINK_MODE for e in entries)


def explain_skip(
    reader: _github.GithubApp, repo: str, deployed: str, head: str, profile: _profile.Profile
) -> str | None:
    """Why moving the direction repo from `deployed` to `head` needs no deploy, or None to deploy as usual."""
    reads = list_read_paths(profile)
    if reads is None:
        return None
    try:
        comparison = reader.compare_commits(repo, deployed, head)
        files = comparison.get("files")
        # A force-push (diverged, behind) or an empty comparison proves nothing about what is read.
        if comparison.get("status") != "ahead" or not isinstance(files, list):
            return None
        if "." in reads:
            return None  # the whole repository is read, so any change may be one the deploy reads
        cache: dict[str, list[_github.Json]] = {}
        shown = []
        for path in reads:
            at_head = _read_path_object(reader, repo, head, path, cache)
            if _read_path_object(reader, repo, deployed, path, cache) != at_head:
                return None
            if _may_hold_link(reader, repo, at_head):
                return None
            # A file (the profile, a brand) is named as it is; a directory, or what may become one, with /...
            shown.append(path if at_head and at_head[0] != _TREE_MODE else f"{path}/...")
    except (_github.GithubError, *REMOTE_ERRORS):
        return None  # an unreadable comparison (a rewritten history, say) is not proof of anything
    # GitHub gives no total, and the count is only for the message: the proof above does not use the list.
    count = (
        f"{len(files)} file{'s' * (len(files) != 1)}"
        if len(files) < _github.PER_PAGE
        else f"{_github.PER_PAGE} or more files"
    )
    return (
        f"direction {head} changes nothing the deploy reads since {deployed}: {count} changed, "
        f"none of {', '.join(shown)}; not deploying (force: deploy_if_changed(force=True))"
    )
