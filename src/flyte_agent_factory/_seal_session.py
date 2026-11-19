"""A sealed project's author session: its role, its workspace clone and its task snapshot.

`project` calls `begin` once the author engine is chosen and `end` when the project ends. In between,
`_author` asks `role()` for the launcher to start the session through, `_session_setup` clones the
benchmark workspace through `clone_workspace` and places the task repo with `place_task_repo`, and the DONE
path calls `close_record` before review. Sub-agents are sealed separately (`_seal_role.register_subagent`).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import flyte.errors

from . import (
    _bridge,
    _gate_base,
    _git_https,
    _pull_requests,
    _push_guard,
    _seal_audit,
    _seal_git,
    _seal_guard,
    _seal_role,
    _session_config,
)
from ._seal_snapshot import snapshot_task_repo
from ._seal_spec import SealSpec
from ._workspace import run_git

#: The audit of the record at the session's latest DONE, kept beside the record (not in it) for the operator.
AUDIT_AT_DONE = "seal-audit-at-done.json"

_stack: contextlib.ExitStack | None = None
_role: _seal_role.SealedRole | None = None
_config_dir: Path | None = None


def role() -> _seal_role.SealedRole | None:
    """Return the author's sealed role, or None for an ordinary project."""
    return _role


def begin(seal: str, engine: str, push_store: Path) -> _seal_role.SealedRole:
    """Seal the author: set the project's spec, start its proxy and open its record.

    `push_store` is the credential file the push token is kept in; the proxy reads it, so the session's
    clone holds no push credential of its own to use. Refuses (not retried) when this pod cannot seal.
    """
    global _stack, _role, _config_dir
    end()
    spec = SealSpec.from_json(seal)
    _config_dir = _session_config.CODEX_HOME_DIR if engine == "codex" else _session_config.CONFIG_DIR
    stack = contextlib.ExitStack()
    try:
        _role = stack.enter_context(
            _seal_role.sealed_role(
                spec,
                _session_config.SEAL_RECORD,
                "author",
                "author",
                engine,
                workdir_roots=[_session_config.WORKSPACE],
                push_auth=_git_https.push_auth(push_store),
                config_dir=_config_dir,
                # `flyte-note`, `ask-human`, `delegate` and DONE reach the supervisor through these files.
                extra_writable=[_bridge.BRIDGE_DIR],
            )
        )
    except RuntimeError as exc:
        # Nothing about a retry will make namespaces available.
        raise flyte.errors.NonRecoverableError(str(exc)) from exc
    _stack = stack
    _session_config._seal = spec
    return _role


def settings_extra(base: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return `base` (the session's own settings) with the guard's hook merged in when sealed."""
    return _seal_guard.merge_settings(base, _role.settings_extra) if _role is not None else base


def end() -> None:
    """Close the author's role (collect its transcripts, write `end.json`, stop the proxy); idempotent."""
    global _stack, _role
    stack, _stack, _role = _stack, None, None
    _session_config._seal = None
    _seal_git.disarm_all()
    if stack is not None:
        stack.close()


def close_record() -> None:
    """Bring the author's record up to date; the DONE path calls it before review.

    The session is still running, so this records what exists now; `audit_record` reads it next.
    """
    if _config_dir is not None:
        _seal_role.close_author_record(_session_config.SEAL_RECORD, _config_dir, _role)


def audit_record() -> str:
    """Audit the record as it stands at DONE; write the verdict beside it and return one line for the report.

    Without the gold (a run never holds it), so `reached` waits for the operator's audit; every other reason
    is checked. It does not bounce the DONE: nothing the author does now can make a void run clean (it is
    excluded and rerun under a new name), and telling it so would only change what it does next. "" when the
    project is not sealed.
    """
    spec = _session_config._seal
    if spec is None:
        return ""
    root = _session_config.SEAL_RECORD
    verdict = _seal_audit.audit(root, task_project=spec.task_project, forbidden=spec.forbidden_paths)
    (root.parent / AUDIT_AT_DONE).write_text(verdict.to_json() + "\n")
    attempts = f"{len(verdict.attempts)} blocked attempt(s) recorded"
    if verdict.verdict == "clean":
        return f"seal audit at DONE (no gold): clean, {verdict.counts.get('roles', 0)} role(s), {attempts}"
    first = verdict.reasons[0]
    return (
        f"seal audit at DONE (no gold): VOID, {len(verdict.reasons)} reason(s); first: {first.kind} "
        f"{first.role_id} {first.file}:{first.line} {first.evidence[:160]}; {attempts}"
    )


def require_https_push(slug: str) -> None:
    """Refuse a sealed project whose push is not by the GitHub App's HTTPS token.

    The seal leaves no route to ssh, so a deploy key could never push; and the proxy's push route
    authenticates with that token.
    """
    if not slug:
        raise flyte.errors.NonRecoverableError(
            "a sealed project pushes through the seal's proxy with a GitHub App token, and none is armed "
            'for this repo: set [github] push = "app" and AGENT_FACTORY_GITHUB_PUSH=app on the slot'
        )


def arm_workspace(checkout: Path, remote_url: str, store: Path) -> None:
    """Make the harness's git on the session's clone hooks-off and config-sanitised (`_seal_git`).

    Call it before the harness's first git call on the clone. `remote_url` is the one the harness clones
    from and `store` the push token's credential file: the harness's own fetch and push authenticate with
    it through the environment (`_seal_git.harness_env`), as the sanitised `.git/config` has no helper.
    """
    _seal_git.arm(checkout, remote_url, push_route=_seal_role.PUSH_ROUTE, store=store)


def _default_branch(checkout: Path) -> str:
    return (
        run_git(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        .strip()
        .removeprefix("origin/")
    )


def claim_branch(checkout: Path, project: str) -> bool:
    """Create `factory/<project>` on the remote, at the default branch's tip, unless it is there; best effort.

    The harness does this before the session starts, outside the seal. The session's first push then
    updates `refs/remotes/origin/factory/<project>`, so `@{u}` resolves straight after `git push -u`
    (which `git rev-list --count @{u}..HEAD`, and the DONE preflight, need). Git updates a tracking ref
    only through a fetch refspec of the remote, and `limit_fetch` lists the branch's refspec only once the
    remote has the branch: a refspec for a missing branch makes every `git fetch origin` fail, and the
    harness runs one. Rejected: a pattern refspec for the branch, which also matches other projects'
    branches (`factory/<project>-r2`), the exact thing the clone must never fetch. Costs an empty branch
    on the benchmark repo for a project that never pushes: it equals the default branch, so it holds no work.
    Returns False when the branch could not be created; `refresh_upstream` then covers the first push.
    Call it before `route_pushes`, which would send this push to the proxy route.
    """
    branch = _pull_requests.branch_name(project)
    try:
        if run_git(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
            return True
        default = _default_branch(checkout)
        run_git(checkout, "push", "origin", f"refs/remotes/origin/{default}:refs/heads/{branch}")
    except RuntimeError:
        return False
    return True


def refresh_upstream(checkout: Path, project: str) -> None:
    """Re-list the project branch's refspec and fetch, so `@{u}` resolves after the session's first push.

    The harness runs this before the DONE preflight, outside the seal (the session cannot fetch). It covers
    a branch `claim_branch` could not create. A fetch that fails changes nothing the preflight then judges.
    """
    try:
        limit_fetch(checkout, project)
        run_git(checkout, "fetch", "--prune", "origin")
    except RuntimeError:
        pass


def limit_fetch(checkout: Path, project: str) -> None:
    """Make `git fetch origin` in the clone bring only the default branch and `factory/<project>`.

    The harness runs `git fetch --prune origin` here, and the delegate workers' tarballs carry the clone's
    `.git`: with the clone's one branch and these two refspecs, no other project's work (an earlier attempt,
    another arm) is ever in the session's reach. The project branch is listed only once the remote has it,
    because a refspec for a missing branch makes the whole fetch fail (`claim_branch` makes it exist).
    """
    default = _default_branch(checkout)
    branch = _pull_requests.branch_name(project)
    refspecs = [_refspec(default)]
    run_git(checkout, "config", "--replace-all", "remote.origin.fetch", refspecs[0])
    try:
        listed = run_git(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    except RuntimeError:  # the remote cannot be asked: the clone's own branch is enough to work from
        listed = ""
    if listed.strip():
        refspecs.append(_refspec(branch))
        run_git(checkout, "config", "--add", "remote.origin.fetch", refspecs[1])
    # The sanitiser writes these back whatever the role puts in `.git/config` before the next fetch.
    _seal_git.set_fetch(checkout, refspecs)


def _refspec(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def route_pushes(checkout: Path, remote_url: str) -> None:
    """Send the clone's pushes to `remote_url` through the proxy's push-only route.

    Git rewrites the push URL only (`pushInsteadOf`), so the harness's fetches keep the real URL. The
    pre-push hook is installed without `_push_guard.install`'s own rewrites, which would send the route's
    http:// URL to a refused directory: the proxy is the only way out of the seal and admits a push and
    nothing else, but the hook still scans the pushed commits for credentials (`_push_guard.scan_main`),
    which no proxy rule can do.
    """
    run_git(checkout, "config", "--replace-all", f"url.{_seal_role.PUSH_ROUTE}.pushInsteadOf", remote_url)
    _push_guard.install_hook(checkout, _seal_role.PUSH_ROUTE)


def task_dir_rel(spec: SealSpec | None) -> Path | None:
    """Return the task repo's path relative to the workspace for a sealed project, else None."""
    if spec is None:
        return None
    project = _gate_base.project_rel(
        _session_config._project_name, _session_config._layout(), _session_config._project_folder
    )
    return Path(project) / spec.task_dir


def place_task_repo(workspace: Path, spec: SealSpec, name: str, layout: str, folder: str) -> Path:
    """Put the task snapshot at `<project folder>/<task_dir>` (outside the seal), out of the clone's git.

    Skipped when it is already there. A resumed project may lack it, because the checkpoint leaves out the
    ignored files of the workspace, so existence rather than "first start" decides.
    """
    if layout != "workspace":
        raise flyte.errors.NonRecoverableError(
            "the sealed workspace repo must be a workspace (a `projects/` directory on its default branch): "
            f"the task repo goes in projects/{name}/{spec.task_dir}"
        )
    dest = _gate_base.project_dir(workspace, name, layout, folder) / spec.task_dir
    if not (dest / ".git").exists():
        snapshot_task_repo(spec.task_repo, spec.base_sha, dest)
    _seal_git.arm(dest)  # no remote: the snapshot has none, so any the role adds is dropped
    exclude = Path(run_git(workspace, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else workspace / exclude
    entry = f"/{dest.relative_to(workspace).as_posix()}/"
    # The role owns `.git`: a link at `exclude` is neither read nor written through, it is replaced.
    plain = exclude.is_file() and not exclude.is_symlink()
    lines = exclude.read_text().splitlines() if plain else []
    if entry not in lines or not plain:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(f"{line}\n" for line in [*lines, *([] if entry in lines else [entry])])
        _seal_git.replace_file(exclude, text.encode(), 0o644, strict_parent=True)
    return dest
