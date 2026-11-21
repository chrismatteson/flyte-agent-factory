"""What a workspace project's gates measure from when its import of an upstream copy is only on its branch.

`promote.py import` (or a session, by hand) commits an upstream copy into `projects/<name>/`. Until that
commit is on `origin/main`, the merge-base with `origin/HEAD` has no project folder, and every upstream file
would count as the project's own new code (upstream issue #54). The import is laid over that merge-base
(`_gate_start._start_tree`) when it can be trusted: the operator made it before the project's recorded base,
or its files are checked byte for byte against the upstream commit its provenance file names.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from . import _delivery, _gate_base, _gate_start, _vendored


def measure_from(
    workspace: Path, name: str, layout: str, folder: str, captured: str
) -> _gate_start.WorkspaceStart | None:
    """Return a workspace project's start (`find_workspace_start`), None for any other layout.

    `captured` is the supervisor's in-process base (`_gates._captured_base`): only it may make an import the
    baseline unchecked. The session can rewrite the file, so without it the import is verified instead.
    """
    if layout != "workspace":
        return None
    return find_workspace_start(workspace, _gate_base.project_rel(name, layout, folder), captured)


def find_workspace_start(workspace: Path, rel: str, captured_base: str) -> _gate_start.WorkspaceStart | None:
    """Return what a workspace project is measured from, or None without an `origin/HEAD` merge-base.

    An import the merge-base already holds changes nothing. Otherwise the operator's import
    (`_delivery.import_commit`) is taken whole when `captured_base` (recorded at project creation, after the
    import) contains it and no agent identity authored it: a commit the session made later must never become
    the baseline. `captured_base` must be the supervisor's in-process record (`_gates._captured_base`), never
    the session-writable file; "" (that record was lost to a restart) trusts nothing unchecked, and every
    upstream commit is then fetched rather than read from the workspace. Any other import, a session's
    included, counts only for the files identical to the upstream commit `promote.toml` or a whole-folder
    `vendored.toml` tree names, fetched and checked as `_vendored` checks a vendored tree; the import tool's
    own files count with it only when no agent identity authored it and the supervisor's record is absent or
    contains it. An import that cannot be trusted leaves the merge-base alone, and the result says why.
    """
    upstream = _gate_start.find_upstream_start(workspace)
    if not upstream:
        return None
    project = workspace / rel
    plain = _gate_start.WorkspaceStart(upstream, rel)
    try:
        if (project / _delivery.FILE).is_file():
            imported = _delivery.import_commit(project)
            if _gate_start._is_ancestor(workspace, imported, upstream):
                return plain
            # The import tool's own files (promote.toml, checks.sh, SUMMARY.md) are the tool's only when no
            # agent identity made the import and the supervisor's record, when it has one, contains it: a
            # promote.toml committed after the base stays the project's, for `scope`, whatever its mail.
            tool_wrote_it = not _is_agent_commit(workspace, imported) and (
                not captured_base or _gate_start._is_ancestor(workspace, imported, captured_base)
            )
            if captured_base and tool_wrote_it:
                return _gate_start.WorkspaceStart(upstream, rel, imported)
            declared = _read_promote(workspace, imported, rel)
        elif (project / _vendored.MANIFEST).is_file():
            imported = _find_first_commit_adding(workspace, f"{rel}/{_vendored.MANIFEST}")
            if _gate_start._is_ancestor(workspace, imported, upstream):
                return plain
            declared = _read_whole_folder_tree(workspace, imported, rel)
            tool_wrote_it = False
        else:
            return plain
        return _verify(
            workspace, project, upstream, rel, imported, declared, captured_base, promoted=tool_wrote_it
        )
    except (_delivery.DeliveryError, _gate_start._GateError, OSError) as exc:
        return _gate_start.WorkspaceStart(upstream, rel, ignored=str(exc).strip()[:300])


def _is_agent_commit(workspace: Path, sha: str) -> bool:
    """Tell whether `sha` carries an agent's author identity (a `localhost` mail): no operator import."""
    email = _gate_start._git_bytes(["log", "-1", "--format=%ae", sha], workspace).decode().strip().lower()
    return email.endswith(_gate_base.AGENT_MAIL_SUFFIXES)


def _find_first_commit_adding(workspace: Path, path: str) -> str:
    log = _gate_start._git_bytes(
        ["log", "--diff-filter=A", "--reverse", "--format=%H", "--", path], workspace
    )
    first = next(iter(log.decode().split()), None)
    if not first:
        raise _gate_start._GateError(f"{path} is not committed")
    return first


def _read_promote(workspace: Path, imported: str, rel: str) -> tuple[str, str, str]:
    """Return the repository, commit and folder (workspace-relative) `promote.toml` names at the import."""
    data = _read_toml_at(workspace, imported, f"{rel}/{_delivery.FILE}")
    repo, commit, path = data.get("repo"), data.get("base"), data.get("path", ".")
    if not (isinstance(repo, str) and isinstance(commit, str) and isinstance(path, str)):
        raise _gate_start._GateError(f"import {imported[:12]}: {_delivery.FILE} names no upstream commit")
    return repo, commit, str(PurePosixPath(rel) / _delivery.relative_path(path))


def _read_whole_folder_tree(workspace: Path, imported: str, rel: str) -> tuple[str, str, str]:
    """Return the `vendored.toml` tree at the import that is the whole project folder (`.` or `rel`)."""
    data = _read_toml_at(workspace, imported, f"{rel}/{_vendored.MANIFEST}")
    trees = data.get("tree", [])
    for tree in trees if isinstance(trees, list) else []:
        path = tree.get("path") if isinstance(tree, dict) else None
        if isinstance(path, str) and PurePosixPath(path) in {PurePosixPath("."), PurePosixPath(rel)}:
            repo, commit = tree.get("repo"), tree.get("commit")
            if isinstance(repo, str) and isinstance(commit, str):
                return repo, commit, rel
    raise _gate_start._GateError(
        f"import {imported[:12]}: {_vendored.MANIFEST} names no tree for the whole folder"
    )


def _read_toml_at(workspace: Path, commit: str, path: str) -> dict[str, object]:
    text = _gate_start._git_bytes(["show", f"{commit}:{path}"], workspace).decode(errors="replace")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _gate_start._GateError(f"import {commit[:12]}: {path} is not valid TOML: {exc}") from exc


def _verify(
    workspace: Path,
    project: Path,
    upstream: str,
    rel: str,
    imported: str,
    declared: tuple[str, str, str],
    captured_base: str,
    *,
    promoted: bool,
) -> _gate_start.WorkspaceStart:
    """Return the start holding only the import's files identical to the declared upstream commit.

    `promoted`, set only after a restart (no in-process record) for a `promote.toml` import no agent authored,
    also keeps the import tool's own files at the folder's top.
    """
    repo, commit, folder = declared
    if not repo.startswith(tuple(f"{scheme}://" for scheme in _vendored._ALLOWED_SCHEMES)):
        raise _gate_start._GateError(
            f"import {imported[:12]}: upstream {repo!r} is not a URL the gate fetches"
        )
    if not _vendored._COMMIT.fullmatch(commit):
        raise _gate_start._GateError(f"import {imported[:12]}: upstream commit {commit!r} is not a full sha")
    _, _, blobs = _vendored.read_upstream_tree(project, repo, commit.lower(), captured_base)
    listing = _gate_start._git_bytes(["ls-tree", "-r", "-z", "--full-tree", imported, "--", rel], workspace)
    only, harness = set(), set()
    for entry in filter(None, listing.split(b"\0")):
        meta, _, raw = entry.partition(b"\t")
        _, kind, sha = meta.decode().split()
        try:
            path = raw.decode()
        except UnicodeDecodeError:
            continue  # not a path the gates can name: it stays the project's
        pure = PurePosixPath(path)
        if kind != "blob":
            continue
        # The tool's files are the import's only when `promoted`; `_delivery.import_commit` has already
        # refused a promote.toml that differs from the import's.
        if promoted and pure.parent == PurePosixPath(rel) and pure.name in _delivery.HARNESS_NAMES:
            harness.add(path)
        elif pure.is_relative_to(folder) and blobs.get(str(pure.relative_to(folder)), ("",))[0] == sha:
            only.add(path)
    return _gate_start.WorkspaceStart(
        upstream,
        rel,
        imported,
        verified_against=f"{repo} @ {commit[:12]}",
        only=frozenset(only | harness),
        harness=frozenset(harness),
    )
