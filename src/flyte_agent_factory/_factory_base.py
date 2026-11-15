"""What the factory modules share: the deployment profile, and a run's name, phase and labels."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from itertools import islice
from typing import Any

import flyte
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import _deploy, _git, _profile, _queue, _state_store

PROFILE = _profile.require()


SLOTS = PROFILE.tasks


# What the server accepts for `in_phase`; LIVE also names WAITING for the phase strings readers compare.
SERVER_LIVE = _deploy.LIVE_PHASES
LIVE = ("RUNNING", "QUEUED", "WAITING", "WAITING_FOR_RESOURCES", "INITIALIZING", "PAUSED")


#: Every run the queue starts carries these, so a project's newest run is found on the server by name.
LABEL_PROJECT = "factory-project"
LABEL_GEN = "factory-gen"
#: The engine a shared-environment launch was admitted for, so the per-engine caps can count live projects.
LABEL_ENGINE = "factory-engine"
#: Abort reasons are for the humans reading the console. The server does not hand them back
#: (`abort_info.reason` reads None), so nothing may use one as state: outcomes go in queue markers.
CONTENTION_REASON = "Concurrent factory launches selected the same slot"
DUPLICATE_REASON = "Another run of the same launch request is older"
CANCEL_REASON = "The launch request was cancelled"


def cap() -> int:
    wanted = int(os.environ.get("OPERATE_CAP", str(PROFILE.cap)))
    # One shared environment holds many projects; with slots each holds one.
    return wanted if PROFILE.shared else min(wanted, len(SLOTS))


def _phase(run: remote.Run | remote.Action) -> str:
    return str(run.phase).split(".")[-1]


def _task_name(run: remote.Run) -> str:
    try:
        name: str = run.to_dict()["action"]["metadata"]["task"]["id"]["name"]
        return name
    except (KeyError, TypeError, AttributeError):
        return ""


def report_html(run: remote.Run) -> str:
    return str(run.get_report()) if hasattr(run, "get_report") else ""


def state_secrets(profile: _profile.Profile) -> list[flyte.Secret]:
    """Return the deploy key a task environment mounts to push to the state branch (`[repo] project`).

    Every environment whose tasks read or write the state store needs it: the store pushes with
    `_git.install_deploy_key`, which finds no key where none is mounted.
    """
    if not profile.secrets.deploy_key:
        return []
    return [flyte.Secret(key=profile.secrets.deploy_key, as_env_var=_git.GIT_DEPLOY_KEY_ENV_VAR)]


def queue_root() -> str:
    return _queue.root(_state_store.ROOT)


def run_labels(run: remote.Run) -> dict[str, str]:
    """Return a run's labels. `flyte.remote.Run` has no `.labels`; they are on its protobuf."""
    return dict(run.pb2.labels)


# --- label-filtered run listings ---------------------------------------------------------------------------
# Union filters `Run.listall(with_labels=..., with_label_keys=...)` on the server. Open-source Flyte 2 rejects
# both with `failed to convert filters: invalid filter field: labels...`; then the listing is repeated without
# the label filters and the labels are matched here, on each run's protobuf.

#: The SDK pages in 100s and stops at `limit`; the fallback must not be cut short by its default of 100.
_UNBOUNDED = 1_000_000

#: `Run.listall`'s own default, which the fallback keeps for a caller that names no `limit`.
_SDK_LIMIT = 100

_REJECTED = "invalid filter field: labels"


def _rejects_labels(exc: Exception) -> bool:
    # The run service is called directly, so the server's error reaches us as a ConnectError. Match the
    # message too: any other ConnectError (auth, unavailable) is not this and must propagate.
    return isinstance(exc, ConnectError) and _REJECTED in str(exc)


def _has_labels(run: remote.Run, labels: Mapping[str, str], keys: Sequence[str] = ()) -> bool:
    have = run_labels(run)
    return all(have.get(k) == v for k, v in labels.items()) and all(k in have for k in keys)


def _build_label_filters(labels: Mapping[str, str], keys: Sequence[str]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if labels:
        filters["with_labels"] = dict(labels)
    if keys:
        filters["with_label_keys"] = list(keys)
    return filters


def _build_limit(limit: int | None) -> dict[str, int]:
    return {} if limit is None else {"limit": limit}


def _cap_of(limit: int | None) -> int:
    """Return how many runs a listing returns at most: `limit`, or the SDK's default when there is none."""
    return _SDK_LIMIT if limit is None else limit


def list_runs(
    labels: Mapping[str, str],
    /,
    *,
    keys: Sequence[str] = (),
    limit: int | None = None,
    **kwargs: Any,
) -> list[remote.Run]:
    """Return runs carrying all of `labels` and every label in `keys`, in server order, at most `limit`.

    `kwargs` are `Run.listall`'s other filters; `limit=None` means the SDK's own default of 100 either way.
    """
    found: list[remote.Run] = []
    try:
        listed = remote.Run.listall(**_build_label_filters(labels, keys), **_build_limit(limit), **kwargs)
        # Stop at the cap rather than ask the iterator for one more page, which it would fetch at a multiple
        # of the page size.
        for run in islice(listed, _cap_of(limit)):
            found.append(run)
        return found
    except ConnectError as exc:
        # The server refuses before the first page; an error after one is not this one.
        if found or not _rejects_labels(exc):
            raise
    cap = _cap_of(limit)
    if cap == 0:
        return found
    # `limit` applies after the filter. Stop reading once it is met: the SDK fetches a page only when asked.
    for run in remote.Run.listall(**kwargs, limit=_UNBOUNDED):
        if _has_labels(run, labels, keys):
            found.append(run)
            if len(found) >= cap:
                break
    return found


async def iter_runs_aio(
    labels: Mapping[str, str],
    /,
    *,
    keys: Sequence[str] = (),
    limit: int | None = None,
    **kwargs: Any,
) -> AsyncIterator[remote.Run]:
    """Yield `list_runs`'s runs one by one, for a caller that stops early and should not read every page."""
    yielded = False
    try:
        async for run in remote.Run.listall.aio(
            **_build_label_filters(labels, keys), **_build_limit(limit), **kwargs
        ):
            yielded = True
            yield run
        return
    except ConnectError as exc:
        # The server refuses before the first page; an error after a yield is not this one.
        if yielded or not _rejects_labels(exc):
            raise
    cap = _cap_of(limit)
    count = 0
    if cap == 0:
        return
    async for run in remote.Run.listall.aio(**kwargs, limit=_UNBOUNDED):
        if _has_labels(run, labels, keys):
            count += 1
            yield run
            if count >= cap:
                return


async def list_runs_aio(
    labels: Mapping[str, str],
    /,
    *,
    keys: Sequence[str] = (),
    limit: int | None = None,
    **kwargs: Any,
) -> list[remote.Run]:
    """Return `list_runs`'s result, asynchronously."""
    return [run async for run in iter_runs_aio(labels, keys=keys, limit=limit, **kwargs)]


def map_live_projects(runs: Sequence[remote.Run]) -> dict[str, str]:
    """Return the project name of each of `runs` that the queue started, with its run name."""
    labelled = ((run_labels(r).get(LABEL_PROJECT), r.name) for r in runs)
    return {project: run for project, run in labelled if project}


def _labelled_runs(name: str, gen: int | None = None, **kwargs: Any) -> list[remote.Run]:
    """Return runs of project `name` (of generation `gen` if given), newest first.

    The server filters on the labels where it can; `list_runs` filters here where it cannot.
    """
    labels = {LABEL_PROJECT: name, **({LABEL_GEN: str(gen)} if gen is not None else {})}
    return list_runs(labels, sort_by=("created_at", "desc"), **kwargs)


def _live_labelled(name: str, gen: int) -> list[remote.Run]:
    return [r for r in _labelled_runs(name, gen, in_phase=SERVER_LIVE) if _phase(r) in LIVE]


def _oldest_first(runs: list[remote.Run]) -> list[remote.Run]:
    """Return `runs` as the server listed them (newest created first), oldest first.

    Creation order, not `action.start_time`: a run that has not started yet reads as the epoch, which
    would rank the newest, unstarted duplicate first. Every dispatcher sees the same server order.
    """
    return list(reversed(runs))
