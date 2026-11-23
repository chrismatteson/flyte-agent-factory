"""`factory-deployer`: merged factory changes deploy themselves.

On a signed push, or a periodic reconcile, `deploy_if_changed` compares the deploy repo's
(`[deploy] repo`, default `[repo] factory`) default-branch head with the SHA recorded at the last
successful deploy. If they differ and its CI passed, it deploys that SHA: the work slots, the factory
environments and itself, then the apps. It is on by `[deploy] auto` in the profile and is otherwise a no-op.

Status and source come through the profile's GitHub App (`_github`, read-only on code) when it is installed on
the deploy repo, else anonymously (a public repository); the source is a shallow fetch of exactly the SHA over
HTTPS. The one thing it writes is its deploy lease, on the state branch (`_acquire_lease`), with the workspace
deploy key `[secrets] deploy_key`, which `_child_env` keeps from every deploy child. Projects that are running
keep the version they started with, so a deploy never interrupts one.

A commit to the direction repo that changes nothing the deploy reads (`_deploy_reads`: the profile file and
the standards it captures; everything else there, such as `operator/STATE.md` and `projects/**`, is not read)
is not deployed, when the engine sha is the deployed one. The tick says so and why. To deploy anyway, run
`deploy_if_changed(force=True)`, or the manual `ops/deploy_factory.py` / `ops/deploy_work.py`, which never
skip.

Release history and attempt counts live in labelled Flyte runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, TypeVar, TypeVarTuple, Unpack, cast

import flyte
import flyte.report
from flyte import remote

# Deploy tools also load this file by path, without a package context.
if TYPE_CHECKING:
    from . import (
        _deploy,
        _deploy_reads,
        _factory_base,
        _git,
        _github,
        _github_mode,
        _isolation_canary,
        _profile,
        _profile_oss,
        _pull_requests,
        _resources,
        _standards,
        _state_store,
        _tick_guard,
        _union_identity,
    )
    from ._errors import REMOTE_ERRORS
    from ._images import agent_image
else:
    from flyte_agent_factory import (
        _deploy,
        _deploy_reads,
        _factory_base,
        _git,
        _github,
        _github_mode,
        _isolation_canary,
        _profile,
        _profile_oss,
        _pull_requests,
        _resources,
        _standards,
        _state_store,
        _tick_guard,
        _union_identity,
    )
    from flyte_agent_factory._errors import REMOTE_ERRORS
    from flyte_agent_factory._images import agent_image

PROFILE = _profile.require()
PROJECT = os.environ.get(
    "FLYTE_MCP_PROJECT", os.environ.get("FLYTE_INTERNAL_PROJECT", PROFILE.backend.project)
)
DOMAIN = os.environ.get("FLYTE_MCP_DOMAIN", os.environ.get("FLYTE_INTERNAL_DOMAIN", PROFILE.backend.domain))
MAX_RUNTIME = timedelta(minutes=60)
MAX_ATTEMPTS = 3
#: How much of a failed run's error the tick repeats; the run has the rest.
_ERROR_SHOWN = 400
#: Deploys one `deploy_pair` run makes when the heads move under it; a fourth head waits for a tick.
MAX_DEPLOYS_PER_RUN = 3
SETTLE = 10
# No live result yet. Keep the CAS fallback until ops/probes/run_listing_order.py passes on the tenant.
RUN_LISTING_CONSISTENT = False
_LIVE = _deploy.LIVE_PHASES
_TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED")
_FAILED = ("FAILED", "TIMED_OUT", "ABORTED")


def _tier_mounts(profile: _profile.Profile) -> dict[str, str]:
    """Union secret name -> the variable it is mounted under, one entry per distinct secret.

    The deployer's own key is `KEY_ENV`, as in every tier. Each other tier's key is mounted under
    `<KEY_ENV>_<TIER>` for reporting client ids and for the isolation canary; a tier that shares a secret
    with an earlier one is not mounted twice.
    """
    if _profile_oss.keyless(profile):
        return {}  # no keys to mount
    mounts = {profile.union.key_for("deployer"): _union_identity.KEY_ENV}
    for tier in ("project", "session", "queue"):
        mounts.setdefault(profile.union.key_for(tier), f"{_union_identity.KEY_ENV}_{tier.upper()}")
    return mounts


def tier_secrets_and_env(profile: _profile.Profile) -> tuple[list[flyte.Secret], dict[str, str]]:
    """Return the secrets for every tier key, and the setting that points children at the deployer's own."""
    secrets, env_vars = _union_identity.pod_spec(profile, "deployer")
    if _profile_oss.keyless(profile):
        return secrets, env_vars  # no key to point children at: `init_for_script` uses the endpoint
    secrets += [
        flyte.Secret(key=secret, as_env_var=var)
        for secret, var in _tier_mounts(profile).items()
        if var != _union_identity.KEY_ENV
    ]
    # Deploy children act as the deployer's key (`init_for_script`), never the key Union injects.
    return secrets, {**env_vars, _deploy.DEPLOY_KEY_ENV: _union_identity.KEY_ENV}


def identities(profile: _profile.Profile) -> list[dict[str, str]]:
    """`[{tier, secret, client_id}]` for every tier, read from the mounted keys; never a key."""
    mounts = _tier_mounts(profile)
    found = []
    for row in _union_identity.describe(profile):
        key = os.environ.get(mounts.get(row["secret"], ""), "")
        try:
            client_id = _union_identity.client_id_of(key) if key else "unknown"
        except ValueError:  # reporting must not fail a deploy
            client_id = "unknown"
        found.append({**row, "client_id": client_id})
    return found


def _child_env(profile: _profile.Profile) -> dict[str, str]:
    """Return this pod's environment for a child process, without any credential it must not pass on.

    That is the App's credentials, the key Union injects into every pod, the workspace deploy key (the
    deploy lease's, which can write the state branch) and any tier key but the deployer's own (`KEY_ENV`),
    under either of its names.
    """
    hidden = {
        *_pull_requests.credential_env_vars(profile.github.private_key_secret, os.environ),
        *_union_identity.INJECTED_VARS,
        # `install_deploy_key` moves the key out of its variables into a file that GIT_SSH_COMMAND names,
        # which would hand the key to anything that runs ssh; the mounted variables (`GIT_DEPLOY_KEY` and
        # the per-repo ones) and Flyte's prefixed copy are hidden in case it has not run yet.
        *(name for name in os.environ if name.startswith(_git.GIT_DEPLOY_KEY_ENV_VAR)),
        _pull_requests.prefixed_key_env_var(profile.secrets.deploy_key, os.environ),
        "GIT_SSH_COMMAND",
    }
    for secret, var in _tier_mounts(profile).items():
        hidden.add(_pull_requests.prefixed_key_env_var(secret, os.environ))
        if var != _union_identity.KEY_ENV:
            hidden.add(var)
    return {k: v for k, v in os.environ.items() if k not in hidden}


_IDENTITY_SECRETS, _IDENTITY_ENV = tier_secrets_and_env(PROFILE)

env = flyte.TaskEnvironment(
    name="factory-deployer",
    # It imports the factory-mcp app to deploy it, so it needs flyte's `mcp` extra (pins as pyproject).
    # `kubernetes` is what Flyte imports to serialize the personal browser's pod template: the deployer
    # deploys that app (`_deploy.deploy_apps`), and the agent image it is built from does not carry it.
    image=agent_image().with_pip_packages(
        "mcp>=1.30.0,<2",
        "starlette",
        "fastapi>=0.115,<1",
        "uvicorn",
        "regex>=2024.5.15",
        "kubernetes>=31.0.0",
    ),
    resources=_resources.for_component(PROFILE, "deployer").to_flyte(),
    # Spot: a preempted tick records nothing, and the next one retries.
    interruptible=True,
    secrets=[
        *(
            [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV)]
            if PROFILE.github.private_key_secret
            else []
        ),
        *_IDENTITY_SECRETS,
        # The deploy lease is a file on the state branch, pushed with this key (`_acquire_lease`).
        *_factory_base.state_secrets(PROFILE),
    ],
    env_vars={
        "FLYTE_MCP_PROJECT": PROJECT,
        "FLYTE_MCP_DOMAIN": DOMAIN,
        **_profile.pod_env(),
        **_IDENTITY_ENV,
    },
)

# The isolation measurement (`_isolation_canary`): the session-tier launcher overrides this task's secrets
# with the App key's (`_CANARY_LAUNCH`), replacing the deployer's tier keys, so it needs no environment
# of its own (which the deploy would have to allow). It only reports whether the key reached it.
CANARY_TASK = "factory-deployer.isolation_canary"


@env.task
def isolation_canary(secret: str) -> dict[str, bool]:
    return _isolation_canary.probe(secret)


_reconcile_minutes = _deploy.reconcile_minutes


TRIGGER = flyte.Trigger(
    name=_deploy.DEPLOY_TRIGGER,
    automation=flyte.FixedRate(_reconcile_minutes(PROFILE)),
    description="Deploy the deploy repo's default branch when it has changed and its CI has passed",
    auto_activate=True,
)


def _triggers(profile: _profile.Profile) -> list[flyte.Trigger]:
    """Declare the schedule only with `[deploy] auto`; without it a tick every few minutes does nothing.

    The webhook submits `deploy_if_changed` directly, so it works without one. A deploy with auto off
    deletes a schedule an earlier version left (`_deploy.delete_retired_triggers`).
    """
    return [TRIGGER] if profile.deploy.auto else []


def _say(message: str) -> str:
    print(f"[deploy] auto {message}", flush=True)
    return message


def _label_hash(text: str) -> str:
    # Run labels may become pod labels, whose values are at most 63 characters of [A-Za-z0-9_.-]:
    # a full sha256 is 64, and a repo or profile name may hold '/' or '+'.
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def release_labels(profile: _profile.Profile) -> dict[str, str]:
    # A rename must not inherit the old repository's success or give-up history.
    repo = _deploy_repo(profile).lower()
    return {"deploy-repo": _label_hash(repo), "deploy-profile": _label_hash(profile.name)}


def _deploy_repo(profile: _profile.Profile) -> str:
    if not profile.deploy.repo:
        raise _deploy.DeployError("[deploy] auto needs a [deploy] repo or a [repo] factory in the profile")
    return _github.parse_repo(profile.deploy.repo)


def _deploy_reader(app: _github.GithubApp, repo: str) -> _github.GithubApp | _github.PublicGithub:
    """Return the App where it is installed on `repo`, else anonymous reads (the deploy repo is public)."""
    try:
        # Contents-read, the scope the reads and the clone use: the probe mints no broader token.
        app.installation_token(repo, _github.CONTENTS_READ)
    except _github.GithubError as exc:
        if exc.status != 404:
            raise
        return _github.PublicGithub()
    return app


def _clone_url(app: _github.GithubApp, repo: str) -> str:
    """Return the deploy repo's fetch URL: with the App's token where it is installed, else anonymous."""
    if _deploy_reader(app, repo) is app:
        return _app_clone_url(app, repo)
    return f"https://github.com/{repo}.git"


def _pair_labels(
    profile: _profile.Profile, sha: str, direction_sha: str, move_sha: str = ""
) -> dict[str, str]:
    """Return the labels an attempt is counted on; a [direction] move's add the new repo's head (#180)."""
    labels = {**release_labels(profile), "deploy-sha": sha, "deploy-direction": direction_sha or "none"}
    return {**labels, "deploy-move": move_sha} if move_sha else labels


class _RunFilters(TypedDict):
    labels: NotRequired[dict[str, str]]
    in_phase: NotRequired[tuple[str, ...]]
    sort_by: NotRequired[tuple[str, Literal["asc", "desc"]]]
    limit: NotRequired[int]


class DeployOutput(TypedDict):
    deployed: bool
    envs: NotRequired[list[str]]
    slots: NotRequired[list[str]]
    sha: NotRequired[str]
    direction_sha: NotRequired[str]
    standards: NotRequired[dict[str, bool | str | None]]
    #: Secret name -> environments that need it: nothing was deployed because they do not exist yet.
    waiting_for_secrets: NotRequired[dict[str, list[str]]]
    #: Why a [direction] move was not deployed (`_MoveNotReady`): not a failure, so nothing else records it.
    refused: NotRequired[str]
    #: `AGENT_FACTORY_GITHUB_PUSH` the slots were deployed with, and what the isolation canary said.
    github_push: NotRequired[str]
    isolation: NotRequired[list[str]]


async def _runs(profile: _profile.Profile, **kwargs: Unpack[_RunFilters]) -> list[remote.Run]:
    return await _factory_base.list_runs_aio(
        kwargs.pop("labels", release_labels(profile)),
        task_name=_deploy.DEPLOY_PAIR,
        project=PROJECT,
        domain=DOMAIN,
        **kwargs,
    )


async def deployed_pair(profile: _profile.Profile) -> tuple[remote.Run, tuple[str, str]] | None:
    """Newest successful deployment and the (sha, direction_sha) it left deployed, or None.

    The pair is the run's output, not its labels: a run that followed a moved head deployed a later
    pair than the one it was submitted for. A successful stand-down has no deployed pair, and any
    number of them may follow a release, so this pages until it finds one (labels are fixed at
    submission, so a deploy cannot be told from a stand-down by label).
    """
    async for run in _factory_base.iter_runs_aio(
        release_labels(profile),
        task_name=_deploy.DEPLOY_PAIR,
        project=PROJECT,
        domain=DOMAIN,
        in_phase=("SUCCEEDED",),
        sort_by=("created_at", "desc"),
        limit=1000000,
    ):
        try:
            (output,) = await run.outputs.aio()
            if (
                isinstance(output, dict)
                and output.get("deployed") is True
                and isinstance(output.get("envs"), list)
                and output["envs"]
                and isinstance(output.get("sha"), str)
                and isinstance(output.get("direction_sha"), str)
            ):
                return run, (output["sha"], output["direction_sha"])
        except (*REMOTE_ERRORS, ValueError, TypeError, KeyError, AttributeError):
            # An unreadable output must not hide the preceding release.
            _say(f"ignoring unreadable outputs of {run.name}")
    return None


def _started(run: remote.Run) -> bool:
    # A pair that waited out `max_queued_time` for a spot node ends TIMED_OUT without ever starting;
    # it ran nothing, so it is not an attempt. An unstarted run's start time reads as the epoch.
    status = run.pb2.action.status
    # Generated protobuf bindings do not type HasField.
    return cast(bool, status.HasField("start_time") and status.start_time.seconds > 0)


@dataclass(frozen=True)
class _Failures:
    count: int  # started runs of the pair that failed since the newest release, up to MAX_ATTEMPTS
    newest: remote.Run | None  # the most recent of them


async def _failed_attempts(
    profile: _profile.Profile, labels: dict[str, str], last: remote.Run | None
) -> _Failures:
    """Return the started runs of this pair that failed since the newest release."""
    # The SDK's Run protobuf omits creation time. Traverse server creation order to the
    # success boundary instead of substituting start_time (queued runs can start out of order).
    failures = 0
    newest = None
    async for candidate in _factory_base.iter_runs_aio(
        release_labels(profile),
        task_name=_deploy.DEPLOY_PAIR,
        project=PROJECT,
        domain=DOMAIN,
        sort_by=("created_at", "desc"),
        limit=1000000,
    ):
        if last and candidate.name == last.name:
            break
        if (
            str(candidate.phase).split(".")[-1] in _FAILED
            and all(candidate.pb2.labels.get(k) == v for k, v in labels.items())
            and _started(candidate)
        ):
            failures += 1
            newest = newest or candidate
            if failures == MAX_ATTEMPTS:
                break
    return _Failures(failures, newest)


#: What a failed attempt's error says when the cause is a credential or a permission, which a retry cannot
#: fix: (pattern, what the owner changes). The first that matches names the fix.
_CREDENTIAL_FAILURES = (
    (
        re.compile(r"access_tokens: HTTP (401|403|422)|permissions requested are not granted", re.I),
        "the GitHub App is not allowed what the deployer asked of it: check the App's permissions and that "
        "it is installed on the repositories named in [repo], [deploy] and [direction]",
    ),
    (
        # What GitHub's ssh says to a key it does not accept, or a key that cannot write; not git's generic
        # "Could not read from remote repository", which follows a timeout or a DNS failure as well.
        re.compile(
            r"Permission denied \(publickey|Permission to \S+ denied to|marked as read only"
            r"|Host key verification failed",
            re.I,
        ),
        "the state remote refused the deploy key: [secrets] deploy_key must be a write-enabled deploy key of "
        "the [repo] project repository",
    ),
)


# GitHub answers a rate limit, primary or secondary, with 403 (or 429) on any endpoint, the token one too:
# that passes on its own, so it keeps the retries an ordinary failure gets.
_RATE_LIMITED = re.compile(r"rate limit|abuse detection", re.I)


def _find_credential_fix(error: str) -> str:
    """Return what to fix when `error` is a credential or permission failure, else ""."""
    if _RATE_LIMITED.search(error):
        return ""
    return next((fix for pattern, fix in _CREDENTIAL_FAILURES if pattern.search(error)), "")


async def _read_run_failure(run: remote.Run) -> str:
    """Return the redacted, one-line error of a failed run, or why it cannot be read."""
    try:
        failure = await run.first_failure.aio()
        error = failure.error_message if failure else ""
    except REMOTE_ERRORS as exc:
        error = f"(error unreadable: {type(exc).__name__})"
    error = " ".join(_deploy.redact(error).split())
    return error[:_ERROR_SHOWN] + ("..." if len(error) > _ERROR_SHOWN else "") or "(no error recorded)"


def _heads(profile: _profile.Profile, app: _github.GithubApp) -> tuple[str, str, str, str]:
    """(repo, sha, direction_repo, direction_sha) of the default branches now; direction ones "" without."""
    repo = _deploy_repo(profile)
    # `git ls-remote` is not REST: a tick whose head is already deployed spends none of an anonymous
    # reader's 60 calls an hour. A repository it cannot read (private) falls back to the App.
    sha = _deploy.public_head(repo)
    contents = app.scoped(_github.CONTENTS_READ)
    if not sha:
        reader = _deploy_reader(app, repo)
        _, sha = (contents if reader is app else reader).default_branch_sha(repo)
    direction_repo = direction_sha = ""
    if profile.direction:
        direction_repo = _github.parse_repo(profile.direction.repo)
        try:
            _, direction_sha = contents.default_branch_sha(direction_repo, profile.direction.ref)
        except _github.GithubError as exc:
            raise _deploy.DeployError(
                f"direction repo {direction_repo} cannot be read, nothing deployed: {exc}"
            ) from exc
    return repo, sha, direction_repo, direction_sha


def _shown(sha: str, direction_sha: str) -> str:
    return f"{sha} with direction {direction_sha}" if direction_sha else sha


async def _not_deployable(
    profile: _profile.Profile,
    app: _github.GithubApp,
    heads: tuple[str, str, str, str],
    last: remote.Run | None,
    *,
    retry_credentials: bool = False,
) -> tuple[str | None, dict[str, str], str]:
    """Why the tick's checks refuse these heads (given up on, or CI not green), or None to deploy.

    Also return the labels an attempt at these heads is counted on, and, when an earlier attempt failed, what
    it failed on (else ""): the tick says it, whether it submits again or not. An attempt that failed on a
    credential or permission is not retried, however few attempts it was, unless `retry_credentials`: a
    retry cannot fix it, and three silent ones only hide it for 45 minutes.
    """
    repo, sha, direction_repo, direction_sha = heads
    shown = _shown(sha, direction_sha)
    targets = [(repo, sha, "")]
    if direction_repo:
        targets.append((direction_repo, direction_sha, "direction "))
    for target, head, prefix in targets:
        reader = _deploy_reader(app, target) if target == repo else app
        refused = _unhealthy(reader, target, head, prefix, needs_ci=target == repo)
        if refused:
            return refused, _pair_labels(profile, sha, direction_sha), ""
    move_repo, move_sha = _move_head(profile, app, direction_sha)
    labels = _pair_labels(profile, sha, direction_sha, move_sha)
    if move_repo:
        # Judged here, like the direction repo's own CI: a check that stays pending must not cost a deploy run
        # every tick. Optional, as there; the attempt judges it again before it deploys.
        refused = _unhealthy(app, move_repo, move_sha, "direction ", needs_ci=False)
        if refused:
            return refused, labels, ""
    failures = await _failed_attempts(profile, labels, last)
    if failures.newest is None:
        return None, labels, ""
    detail = await _read_run_failure(failures.newest)
    failed = f"{failures.newest.name}: {detail}"
    shown += f" moving to {move_repo} at {move_sha}" if move_repo else ""
    fix = _find_credential_fix(detail)
    if failures.count >= MAX_ATTEMPTS:
        # Before the credential stop: force does not lift the attempt limit, so it is not offered here.
        msg = f"giving up on {shown} after {MAX_ATTEMPTS} attempts; waiting for a new commit"
        cause = f" The latest was a credential or permission problem: {fix}." if fix else ""
        return f"{msg}.{cause} Latest failure, run {failed}", labels, ""
    if fix and not retry_credentials:
        return (
            f"not deploying {shown}: a credential or permission problem, which a retry will not fix; {fix}. "
            "Not submitting again until a head moves or `deploy_if_changed(force=True)`. "
            f"Latest failure, run {failed}",
            labels,
            "",
        )
    return None, labels, f"; the last attempt failed, run {failed}"


def _move_head(profile: _profile.Profile, app: _github.GithubApp, direction_sha: str) -> tuple[str, str]:
    """(repo, head) of the repository the direction profile at `direction_sha` moves [direction] to.

    ("", "") when it names the same [direction], or when anything on the way cannot be read: the attempt then
    resolves the move itself and, refusing it, says why in its report.
    """
    direction = profile.direction
    if direction is None or not direction_sha:
        return "", ""
    contents = app.scoped(_github.CONTENTS_READ)
    try:
        # The one file through the contents API, not a clone: the attempt fetches the trees it deploys from.
        text = contents.read_file(_github.parse_repo(direction.repo), direction.profile, ref=direction_sha)
        shipped = _profile.parse(text, direction.profile, tolerate_unknown=True)
        if shipped.direction is None or _same_direction(shipped.direction, direction):
            return "", ""
        new_repo = _github.parse_repo(shipped.direction.repo)
        _, new_sha = contents.default_branch_sha(new_repo, shipped.direction.ref)
    except (_profile.ProfileError, _github.GithubError):
        return "", ""
    return new_repo, new_sha


def _unhealthy(
    reader: _github.GithubApp | _github.PublicGithub, repo: str, head: str, prefix: str, *, needs_ci: bool
) -> str | None:
    """Why CI refuses this commit (failing, pending, or none when `needs_ci`), or None, saying it deploys."""
    # The App reads CI with a CI-only token; an anonymous reader has no token to scope.
    if isinstance(reader, _github.GithubApp):
        reader = reader.scoped(_github.CI_READ)
    health, detail = reader.commit_health(repo, head)
    if health in ("failing", "pending"):
        retry = "; retrying next tick" if health == "pending" else ""
        return f"skipped {prefix}{head}: {detail}{retry}"
    if health == "none" and needs_ci:
        # Deploying code nothing tested is the bug a CI-less repository would silently reintroduce.
        return f"no CI reported on {head}: not deployed; the deploy repo must run CI"
    _say(f"deploying {prefix}{head} (CI: {health}, {detail})")
    return None


async def _read_secrets_last_waited_for(
    profile: _profile.Profile, labels: dict[str, str]
) -> dict[str, list[str]]:
    """Return the secrets the newest finished attempt at these heads waited for; none if it did not wait."""
    for run in await _runs(
        profile, labels=labels, in_phase=("SUCCEEDED",), sort_by=("created_at", "desc"), limit=1
    ):
        try:
            (output,) = await run.outputs.aio()
            waiting = output.get("waiting_for_secrets")
            if isinstance(waiting, dict):
                return waiting
        except (*REMOTE_ERRORS, TypeError, AttributeError):
            _say(f"ignoring unreadable outputs of {run.name}")
    return {}


def _list_missing_secrets(waiting: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    # A tick that cannot look falls back to submitting an attempt, never to a silent hold: those names come
    # back second, so the attempt can say what it may wait for again.
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    missing, unchecked = [], []
    for name in waiting:
        try:
            remote.Secret.get(name)
        except (ConnectError, *REMOTE_ERRORS) as exc:
            if isinstance(exc, ConnectError) and exc.code == Code.NOT_FOUND:
                missing.append(name)
            else:
                unchecked.append(name)
                _say(f"could not check secret {name}; submitting anyway, and the attempt may wait for it")
    return missing, unchecked


async def _submit(task_name: str, *, labels: dict[str, str] | None = None, **inputs: str) -> remote.Run:
    task = remote.Task.get(name=task_name, auto_version="latest", project=PROJECT, domain=DOMAIN)
    return await flyte.with_runcontext(
        mode="remote", project=PROJECT, domain=DOMAIN, labels=labels or {}
    ).run.aio(task, **inputs)


def _hash_lock_name(profile: _profile.Profile) -> str:
    """Name of this deployer's lock, the same for the lease and for the legacy ref an older engine wrote."""
    return hashlib.sha256(f"{PROJECT}/{DOMAIN}/{profile.name}".encode()).hexdigest()


def _build_lease_key(profile: _profile.Profile) -> str:
    return f"deployer-lock/{_hash_lock_name(profile)}"


def _open_lease_store(profile: _profile.Profile) -> _state_store.Store:
    """Return the git store of the lease: `[repo] project`'s state branch, whatever `[state] backend` is.

    Not `_state_store.current`: the owner's rule is that the lease adds no state to an object store (or a
    volume). The deploy key is mounted for every profile (`[secrets] deploy_key` is required), so this
    works on every backend.
    """
    branch = profile.state.branch or _state_store.default_branch(
        profile.backend.project, profile.backend.domain
    )
    return _state_store.git_store(profile.repo, branch)


#: How old a legacy lock ref can be and still have a live holder; twice the longest a deploy_pair runs.
_LEGACY_MAX_AGE = 2 * MAX_RUNTIME


def _parse_lock_holder(record: str | bytes) -> str:
    """Return the run a lock record names; a record that is not one needs the operator, like a foreign one."""
    try:
        run = json.loads(record)["run"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _deploy.DeployError("unrecognized deployer lock; operator resolution required") from exc
    if not isinstance(run, str) or not run:
        raise _deploy.DeployError("unrecognized deployer lock; operator resolution required")
    return run


def _parse_taken_at(commit: dict[str, Any]) -> datetime | None:
    """When a legacy lock commit was made (GitHub's committer date), or None when it does not say."""
    try:
        taken = datetime.fromisoformat((commit.get("committer") or {})["date"])
    except (KeyError, TypeError, ValueError):
        return None
    return taken if taken.tzinfo else taken.replace(tzinfo=timezone.utc)


def _is_live(run_name: str) -> bool:
    # Only an authoritative terminal phase frees a holder: a failed lookup raises, so admission fails closed.
    return str(remote.Run.get(name=run_name).phase).split(".")[-1] not in _TERMINAL


def _find_legacy_holder(profile: _profile.Profile, app: _github.GithubApp, run_name: str) -> str | None:
    """Return the live run, other than `run_name`, that holds a lock ref written by an older deployer, if any.

    Older deployers kept the lock as a git ref `heads/factory-deployer-lock/<key>` in the deploy repo (where
    the App was installed) or in [direction], written with the App's contents:write. It is only read here,
    with contents:read, and never written: a deploy_pair that was running when the engine upgraded still
    holds it. A ref older than any deploy_pair can run (`MAX_RUNTIME` from its start, and it takes the lock
    after it starts) is not looked up, so a holder Flyte has since forgotten cannot block every later deploy.
    Remove this once no install has a live legacy holder.
    """
    repos = []
    deploy = _deploy_repo(profile)
    if _deploy_reader(app, deploy) is app:
        repos.append(deploy)
    if profile.direction:
        repos.append(_github.parse_repo(profile.direction.repo))
    contents = app.scoped(_github.CONTENTS_READ)
    ref = f"heads/factory-deployer-lock/{_hash_lock_name(profile)}"
    for repo in dict.fromkeys(repos):
        try:
            sha = contents._call(repo, "GET", f"/repos/{repo}/git/ref/{ref}")["object"]["sha"]
            commit = contents._call(repo, "GET", f"/repos/{repo}/git/commits/{sha}")
        except _github.GithubError as exc:
            if exc.status != 404:  # no ref, or the App is not installed on this repo: nothing held here
                raise
            continue
        taken = _parse_taken_at(commit)
        if taken and datetime.now(timezone.utc) - taken > _LEGACY_MAX_AGE:
            continue
        holder = _parse_lock_holder(commit["message"])
        if holder != run_name and _is_live(holder):
            return str(holder)
    return None


def _acquire_lease(profile: _profile.Profile, app: _github.GithubApp, run_name: str) -> bool:
    """Take the deploy lease: a file on the state branch, created or replaced by one non-forced push.

    `StateStore.transact` is the compare-and-swap: of two acquirers that read the same tip only one push
    lands, and the other re-reads the winner's record and stands down. The record is replaced, never deleted,
    when its holder is finished, so a dead holder's successor has nothing to race with but the push.
    A holder that cannot be looked up never expires: that raises, failing closed until its Flyte phase is
    authoritative.
    """
    legacy = _find_legacy_holder(profile, app, run_name)
    if legacy:
        _say(f"lease held by {legacy} (legacy lock)")
        return False
    key = _build_lease_key(profile)
    scope = f"{PROJECT}/{DOMAIN}"

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], bool]:
        # May run again on a newer tip after a lost push, so it only looks; the lookup is repeated.
        held = snap.read(key)
        if held is not None:
            _parse_lock_holder(held)  # a record that is not JSON naming a run needs the operator
            owner = json.loads(held)
            if (
                not isinstance(owner, dict)
                or owner.get("profile") != profile.name
                or owner.get("scope") != scope
            ):
                raise _deploy.DeployError("unrecognized deployer lock; operator resolution required")
            if owner["run"] == run_name:
                return {}, True
            if _is_live(owner["run"]):
                _say(f"lease held by {owner['run']}")
                return {}, False
        record = json.dumps({"run": run_name, "profile": profile.name, "scope": scope}).encode()
        return {key: record}, True

    return _open_lease_store(profile).transact(decide, f"deployer lease {run_name}")


def _preflight_lease(profile: _profile.Profile) -> None:
    """Refuse at the tick, where the owner reads it, when a deploy_pair could not take the lease."""
    if not _git.install_deploy_key(profile.repo):
        raise _deploy.DeployError(
            f"nothing is deployed: the deploy lock is written to the state branch of {profile.repo} with the "
            f"deploy key, and {_git.GIT_DEPLOY_KEY_ENV_VAR} is not mounted in {env.name}. Set "
            "[secrets] deploy_key to a write-enabled deploy key of that repository and redeploy"
        )
    try:
        _open_lease_store(profile).snapshot(fresh=True)
    except _state_store.StateError as exc:
        raise _deploy.DeployError(
            f"nothing is deployed: the state branch of {profile.repo}, where the deploy lock lives, "
            f"cannot be read with the deploy key: {exc}"
        ) from exc


async def _admitted(profile: _profile.Profile, app: _github.GithubApp, run_name: str) -> bool:
    if not RUN_LISTING_CONSISTENT:
        return await asyncio.to_thread(_acquire_lease, profile, app, run_name)
    await asyncio.sleep(SETTLE)
    # Include every live pair, not just this SHA: moving heads cannot deploy concurrently.
    runs = await _runs(profile, in_phase=_LIVE, sort_by=("created_at", "asc"), limit=1000000)
    if not any(run.name == run_name for run in runs):
        raise _deploy.DeployError(f"deploy run {run_name} is missing from the live listing")
    if runs[0].name != run_name:
        _say(f"lease held by {runs[0].name}")
        return False
    return True


@dataclass(frozen=True)
class _ResolvedDirection:
    """The direction profile to deploy: `profile` at `sha` of `repo`; `moved_from` is a move's old repo."""

    profile: _profile.Profile
    direction: _profile.Direction  # where `profile` was read: its repo, path and ref
    repo: str
    sha: str
    moved_from: str = ""


def _same_direction(a: _profile.Direction, b: _profile.Direction) -> bool:
    # The repo by owner/name, so an https spelling of the same repo is the same [direction]; GitHub names are
    # case-insensitive, so `Acme/Direction` is it too. The path and the ref are git's, which are not.
    same_repo = _github.parse_repo(a.repo).lower() == _github.parse_repo(b.repo).lower()
    return same_repo and (a.profile, a.ref) == (b.profile, b.ref)


def _app_clone_url(app: _github.GithubApp, repo: str) -> str:
    # Source is read, never written: the token that reaches git (and a child's checkout) is contents-read.
    return _github.clone_url(repo, app.installation_token(repo, _github.CONTENTS_READ))


def _read_direction(app: _github.GithubApp, direction: _profile.Direction, sha: str) -> _profile.Profile:
    """Read the direction repo's profile at `sha`: DeployError if unfetchable, ProfileError if unusable."""
    repo = _github.parse_repo(direction.repo)
    with _deploy.fetched_tree(_app_clone_url(app, repo), sha) as tree:
        # Unknown top-level keys may be the deployed engine's: `_deploy_sha` has it check them.
        return _profile.load_from_direction(tree, direction.profile, tolerate_unknown=True)


def _check_direction_name(
    shipped: _profile.Profile,
    profile: _profile.Profile,
    direction: _profile.Direction,
    sha: str,
    note: str,
    error: type[_deploy.DeployError] = _deploy.DeployError,
) -> None:
    if shipped.name != profile.name:  # the release history and deployed envs are keyed on it
        repo = _github.parse_repo(direction.repo)
        raise error(
            f"{direction.profile} at {sha} of {repo} is profile {shipped.name!r}, not {profile.name!r}{note}"
        )


def _resolve_direction(profile: _profile.Profile, app: _github.GithubApp, sha: str) -> _ResolvedDirection:
    """Resolve the profile to deploy from the direction repo; `sha` is its head, per the deployed [direction].

    Never falls back: an unreadable one fails the deploy, before anything is deployed.

    The deployer redeploys itself with this profile, so its [direction] decides where the next deployer reads.
    The profile at `sha` of the deployed repo ("old") either names that same [direction] (steady) or names
    another one ("new"): a move, which is two-sided. The new repo's head is resolved with the GitHub App and
    its profile must name the new repo itself, so the move is deployed only when both repos agree. The new
    repo is read only because the old one names it: the deployer never follows a repo the current direction
    did not name. A move the new repo refuses raises `_MoveNotReady`, which is not a failed attempt: the fix
    is in the new repo, whose commits do not change the pair attempts are counted on.
    """
    # Only called after the caller has checked profile.direction.
    direction = cast(_profile.Direction, profile.direction)
    repo = _github.parse_repo(direction.repo)
    try:
        shipped = _read_direction(app, direction, sha)
    except _profile.ProfileError as exc:
        raise _deploy.DeployError(f"{direction.profile} at {sha} of {repo} is not usable: {exc}") from exc
    _check_direction_name(shipped, profile, direction, sha, "")
    if shipped.direction is None:
        # Without it the next deployer would stop reading the direction repo, silently.
        raise _deploy.DeployError(
            f"{direction.profile} at {sha} of {repo} must keep a [direction]: removing it would stop the "
            f"deployer reading {repo}; nothing deployed"
        )
    if _same_direction(shipped.direction, direction):
        return _adopt(shipped, direction, repo, sha)
    return _resolve_move(profile, app, direction, sha, shipped.direction)


def _resolve_move(
    profile: _profile.Profile,
    app: _github.GithubApp,
    old: _profile.Direction,
    old_sha: str,
    new: _profile.Direction,
) -> _ResolvedDirection:
    old_repo, new_repo = _github.parse_repo(old.repo), _github.parse_repo(new.repo)
    try:
        _, new_sha = app.scoped(_github.CONTENTS_READ).default_branch_sha(new_repo, new.ref)
        shipped = _read_direction(app, new, new_sha)
    except _profile.ProfileError as exc:
        raise _MoveNotReady(
            f"{new.profile} at {new_sha} of {new_repo} is not usable: {exc} (new direction {new_repo})"
        ) from exc
    except (_github.GithubError, _deploy.DeployError) as exc:
        raise _MoveNotReady(
            f"[direction] move to {new_repo} refused: {new_repo} cannot be read ({exc}); nothing deployed. "
            f"Install the GitHub App on {new_repo} or fix [direction] in {old_repo}."
        ) from exc
    _check_direction_name(shipped, profile, new, new_sha, f" (new direction {new_repo})", _MoveNotReady)
    if shipped.direction is None or not _same_direction(shipped.direction, new):
        names = (
            "no [direction]"
            if shipped.direction is None
            else f"{_github.parse_repo(shipped.direction.repo)}, {shipped.direction.profile}, "
            f"ref {shipped.direction.ref or 'default'}"
        )
        raise _MoveNotReady(
            f"{old.profile} at {old_sha} of {old_repo} moves [direction] to {new_repo}, but {new.profile} at "
            f"{new_sha} of {new_repo} names {names}: a move needs the new profile to name its own repo; "
            "nothing deployed"
        )
    return _adopt(shipped, new, new_repo, new_sha, moved_from=old_repo)


def _adopt(
    shipped: _profile.Profile, direction: _profile.Direction, repo: str, sha: str, moved_from: str = ""
) -> _ResolvedDirection:
    _say(f"profile: {direction.profile} from {repo} at {sha}")
    captured = _standards.decode(shipped.standards_payload)
    if captured:
        _say(f"direction standards captured: {len(captured)} files from {repo}, {direction.profile} at {sha}")
    return _ResolvedDirection(shipped, direction, repo, sha, moved_from)


class _MoveNotReady(_deploy.DeployError):
    """The new repo of a [direction] move does not allow it yet: nothing deployed, and not an attempt.

    It cannot be read, its profile is unusable or does not name the new repo itself, the engine being deployed
    refuses its profile, or its CI is failing or pending. Each is fixed in the new repo, and a commit there
    does not change the pair `_failed_attempts` counts on: counted, an owner who committed the old side first
    would see the deployer give up on that pair and never deploy the new side's commit that completes the
    move (#180). So `run_pair` reports the refusal and leaves it for the next tick, as it does pending CI.
    """


def _direction_profile(profile: _profile.Profile, app: _github.GithubApp, sha: str) -> _profile.Profile:
    """Return the profile at `sha` of the direction repo (or of the repo a commit there moves it to)."""
    return _resolve_direction(profile, app, sha).profile


def _deploy_sha(
    profile: _profile.Profile,
    app: _github.GithubApp,
    repo: str,
    sha: str,
    direction_sha: str = "",
    push: str = "deploy_key",
    isolation: Sequence[str] = (),
) -> tuple[list[str], _profile.Profile, dict[str, bool | str | None], _ResolvedDirection | None]:
    """Deploy `sha` of `repo`: slots, factory, then apps.

    Return its environments, profile and standards diagnostics.

    With `[direction]` the profile is the one at `direction_sha` of the direction repo, or of the repo a
    commit there moves `[direction]` to (the fourth item returned says so). Otherwise it is
    the merged tree's `profiles/<name>.toml` when it has one, else `profile`, the one this
    pod started with: the children take their profile text from here, so a pod profile alone would
    ship every later change to it (and to the standards) only by hand, and the deployer would
    redeploy itself with stale text. Raises on the first stage that fails.

    This process is the previous release. It reads the profile only for what it acts on itself (the
    name and `[direction]`; every table it knows is still validated) and lets through top-level keys
    it does not know. The tree's own code (`_deploy_child`) then reads the profile strictly, and
    decides and deploys every environment: a key released with the code that reads it deploys in one
    step, and a key that code does not know either (a typo) fails the check, naming it, before
    anything is deployed.
    """
    # The children import and run the deployed code: they get neither the App key nor a token, nor the key
    # Union injects, nor any tier key but the deployer's. `push` is what the slots are to run with.
    base = {
        **_child_env(profile),
        _github_mode.PUSH_ENV_VAR: push,
        **({_github_mode.ISOLATION_ENV_VAR: "; ".join(isolation)} if isolation else {}),
        _profile.IDENTITIES_ENV: json.dumps(identities(profile)),
    }
    with _deploy.fetched_tree(_clone_url(app, repo), sha) as tree:
        direction = None
        if profile.direction:
            # The direction's profile carries its own standards, captured from the direction repository: the
            # children must not capture them again from the engine tree.
            direction = _resolve_direction(profile, app, direction_sha)
            if direction.moved_from:
                # The direction repo is read with the App, like the old one; its CI is optional, as there.
                refused = _unhealthy(app, direction.repo, direction.sha, "direction ", needs_ci=False)
                if refused:
                    raise _MoveNotReady(refused)
            text = direction.profile.deployment_text()
            where, standards = f"{direction.direction.profile} at {direction.sha}", None
        else:
            try:
                shipped = _profile.load_from_tree(tree, profile.name, tolerate_unknown=True)
            except _profile.ProfileError as exc:
                raise _deploy.DeployError(
                    f"profiles/{profile.name}.toml at {sha} is not usable: {exc}"
                ) from exc
            if shipped:
                _say(f"profile: profiles/{profile.name}.toml from {sha}")
                text, where, standards = shipped.text, f"profiles/{profile.name}.toml at {sha}", tree
            else:
                _say(f"no profiles/{profile.name}.toml in {sha}; using the deployed profile")
                # It already carries its standards: they are not the tree's.
                text, where, standards = profile.deployment_text(), "the deployed profile", None
        src = tree / "src"
        diagnostics: dict[str, bool | str | None] = {
            "configured": None,
            "warning": "standards metadata unavailable (older engine)",
        }
        try:
            planned = _deploy.check_in_tree(
                src, text, where, tree=standards, base_env=base, diagnostics=diagnostics
            )
        except _deploy.MissingSecrets:
            raise  # not about the text: the owner creates them, and run_pair reports it as waiting
        except _deploy.DeployError as exc:
            if direction is None or not direction.moved_from:
                raise
            # The refused text is the new repo's: a commit there fixes it, so this is not a counted attempt.
            raise _MoveNotReady(f"{exc} (new direction {direction.repo}); nothing deployed") from exc
        _say(f"the engine at {sha} accepts {where}; deploying {', '.join(planned)}")
        deployed = _deploy.deploy_in_tree(
            src, text, where, tree=standards, project=PROJECT, domain=DOMAIN, ref=sha, base_env=base
        )
    missing = sorted(set(planned) - set(deployed))
    if missing:  # a success must not claim an environment that was not deployed
        raise _deploy.DeployError(f"deploy of {sha} did not deploy {', '.join(missing)}")
    return deployed, _profile.parse(text, where, tolerate_unknown=True), diagnostics, direction


# Runs as the session tier's key, in a subprocess so the deployer's own process never holds it as its
# identity. argv carries no secret: the SDK logs command lines.
_CANARY_LAUNCH = """
import json, sys
from connectrpc.code import Code
from connectrpc.errors import ConnectError
import flyte
import flyte.remote
from flyte_agent_factory import _profile
from flyte_agent_factory._pull_requests import KEY_ENV_VAR
from flyte_agent_factory._union_identity import KEY_ENV, init_for_script
project, domain, task_name, secret = sys.argv[1:5]
init_for_script(_profile.require(), key_env=KEY_ENV, project=project, domain=domain)
try:
    task = flyte.remote.Task.get(name=task_name, auto_version="latest", project=project, domain=domain)
    task = task.fetch()
    task = task.override(secrets=[flyte.Secret(key=secret, as_env_var=KEY_ENV_VAR)])
    run = flyte.with_runcontext(mode="remote", project=project, domain=domain).run(task, secret=secret)
    run.wait()
    (answer,) = run.outputs()
except ConnectError as exc:
    if exc.code != Code.PERMISSION_DENIED:
        raise
    print("REFUSED PERMISSION_DENIED")
else:
    print(json.dumps(answer))
"""
CANARY_TIMEOUT_S = 900  # a cold pod and an image pull, then a no-op


def launch_canary(secret: str) -> dict[str, object]:
    """Launch `isolation-canary` (which declares `secret`) as the session tier; its answer.

    Raises `LaunchRefused` when Union refuses the launch for lack of permission. Anything else that goes
    wrong raises `SubprocessError`, which the canary reads as "not isolated". Only the exit code is
    reported: the child's stderr may carry a token.
    """
    session = PROFILE.union.key_for("session")
    var = _tier_mounts(PROFILE)[session]
    key = os.environ.get(var, "")
    if not key:
        raise _union_identity.IdentityMissing(
            f"the session tier's key (secret {session}) is not mounted as {var}: isolation cannot be measured"
        )
    child_env = {
        **_child_env(PROFILE),
        _union_identity.KEY_ENV: key,
        _union_identity.TIER_ENV: "session",
        _union_identity.SECRET_ENV: session,
    }
    done = subprocess.run(
        [sys.executable, "-c", _CANARY_LAUNCH, PROJECT, DOMAIN, CANARY_TASK, secret],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=CANARY_TIMEOUT_S,
    )
    if done.returncode:
        raise subprocess.SubprocessError(f"the canary launch exited {done.returncode}")
    lines = done.stdout.strip().splitlines()
    if lines and lines[-1].startswith("REFUSED"):
        raise _isolation_canary.LaunchRefused(lines[-1])
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise subprocess.SubprocessError("the canary launch printed no answer")
    return answer


def _push_mode(profile: _profile.Profile) -> tuple[str, list[str]]:
    """`AGENT_FACTORY_GITHUB_PUSH` for the slots, and why: `app` only if asked for and isolation held.

    With an App configured the canary runs whatever `push` is, so `factory.status` can say whether sessions
    can reach the App key; without one there is nothing to measure. A canary that fails is a reason, never
    a failed deploy.
    """
    if not profile.github.configured:
        return "deploy_key", []
    if _profile_oss.keyless(profile):
        # The canary launches a task as the session tier's key; OSS has no keys to measure with.
        return "deploy_key", ["OSS backend: sessions have no separate identity, so isolation is not measured"]
    try:
        verdict = _isolation_canary.check(profile, launch_canary)
    except (_union_identity.IdentityMissing, *_DEPLOY_ERRORS) as exc:
        return "deploy_key", [str(exc)]
    for reason in verdict.reasons:
        _say(f"isolation canary: {reason}")
    push = "app" if verdict.isolated and profile.github.push == "app" else "deploy_key"
    return push, ([_github_mode.ISOLATED, *verdict.reasons] if verdict.isolated else verdict.reasons)


#: The whole dispatch wake, app discovery included: a stalled control-plane read must not hold the deploy.
WAKE_TIMEOUT_S = 15


#: What a deploy step raises: control-plane and GitHub errors, git failing, and a refusal (a SystemExit).
_DEPLOY_ERRORS = (*REMOTE_ERRORS, subprocess.SubprocessError, _deploy.Refused)

_Ts = TypeVarTuple("_Ts")
_T = TypeVar("_T")


def _on_daemon_thread(fn: Callable[[Unpack[_Ts]], _T], *args: Unpack[_Ts]) -> asyncio.Future[_T]:
    """Run `fn(*args)` on a daemon thread, awaitable.

    Unlike `asyncio.to_thread`, a call that outlives its timeout cannot hold the deployer pod open:
    interpreter exit waits for executor threads, not daemons.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def settle(callback: Callable[[], object]) -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:  # the loop closed after the wake gave up on this call
            pass

    def run() -> None:
        try:
            result = fn(*args)
        # Anything else is a bug: the thread's excepthook shows it and the awaiting wake times out.
        except REMOTE_ERRORS as exc:
            error = exc  # `exc` is unbound when the except block ends; the callback runs later
            settle(lambda: future.done() or future.set_exception(error))  # type: ignore[func-returns-value]  # Short-circuit skips completed futures.
        else:
            settle(lambda: future.done() or future.set_result(result))  # type: ignore[func-returns-value]  # Short-circuit skips completed futures.

    threading.Thread(target=run, name="dispatch-wake", daemon=True).start()
    return future


async def _wake_dispatch(profile: _profile.Profile) -> None:
    try:
        if profile.planner.admission == "planner":
            from . import _planner_runtime

            key = os.environ[_deploy.GITHUB_KEY_ENV]
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                await _on_daemon_thread(_planner_runtime.notify, profile, key)
        else:
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                await _submit("factory.sweep")
    except (*REMOTE_ERRORS, TimeoutError) as exc:
        _say(f"dispatch wake failed ({type(exc).__name__}); the safety net will retry")


def _explain_unread_only_skip(
    profile: _profile.Profile, app: _github.GithubApp, deployed: tuple[str, str], sha: str, direction_sha: str
) -> str | None:
    """Why `direction_sha` needs no deploy over the deployed pair (engine unchanged), or None to deploy."""
    deployed_sha, deployed_direction = deployed
    if sha != deployed_sha or not deployed_direction or deployed_direction == direction_sha:
        return None
    assert profile.direction is not None  # a direction sha is only ever set with [direction]
    return _deploy_reads.explain_skip(
        app.scoped(_github.CONTENTS_READ),
        _github.parse_repo(profile.direction.repo),
        deployed_direction,
        direction_sha,
        profile,
    )


async def run_once(
    profile: _profile.Profile,
    run_name: str,
    app: _github.GithubApp | None,
    now: float | None = None,
    *,
    force: bool = False,
) -> str:
    """Resolve current heads, check CI and submit one labelled attempt."""
    if not profile.deploy.auto:
        return _say("off: [deploy] auto is false in the profile")
    if app is None:
        raise _deploy.DeployError("[deploy] auto needs the GitHub App")
    try:
        heads = _heads(profile, app)
        _, sha, _, direction_sha = heads
        shown = _shown(sha, direction_sha)
        release = await deployed_pair(profile)
        if release and release[1] == (sha, direction_sha):
            return _say(f"up to date at {shown}")
        if release and not force:
            unread = await asyncio.to_thread(
                _explain_unread_only_skip, profile, app, release[1], sha, direction_sha
            )
            if unread:
                return _say(unread)
        await asyncio.to_thread(_preflight_lease, profile)
        refused, labels, earlier = await _not_deployable(
            profile, app, heads, release and release[0], retry_credentials=force
        )
        if refused:
            return _say(refused)
        if await _runs(profile, in_phase=_LIVE, limit=1):
            return _say("deploy in flight")
        waiting = await _read_secrets_last_waited_for(profile, labels)
        still_missing, unchecked = await asyncio.to_thread(_list_missing_secrets, waiting)
        if still_missing:
            # The labels are per-heads, so a moved head finds no waiting attempt and submits.
            return _say(
                f"waiting for secrets at {shown}: {', '.join(sorted(still_missing))} "
                "(create them with `flyte create secret <name>`); "
                "not submitting until they exist or a head moves"
            )
        run = await _submit(_deploy.DEPLOY_PAIR, labels=labels, sha=sha, direction_sha=direction_sha)
        note = f"; may still be waiting for secrets: {', '.join(sorted(unchecked))}" if unchecked else ""
        return _say(f"submitted {shown}: {run.name}{note}{earlier}")
    except _DEPLOY_ERRORS as exc:  # task boundary redacts remote errors
        raise _deploy.DeployError(_deploy.redact(str(exc))) from None


def _describe_refusal(refused: str) -> str:
    # `_unhealthy` already says a pending commit is retried next tick; saying it twice reads as two retries.
    return refused if refused.endswith("retrying next tick") else f"{refused}; leaving it for the next tick"


async def run_pair(
    profile: _profile.Profile, app: _github.GithubApp, run_name: str, sha: str, direction_sha: str
) -> DeployOutput:
    """Deploy the pair this run was submitted for, then follow heads that moved meanwhile.

    While this run is live every tick says "deploy in flight", so a push that lands during the
    deploy is this run's to pick up, holding the lock: up to MAX_DEPLOYS_PER_RUN deploys, each
    after the tick's own checks. A failure in a follow-up fails the run, so its earlier deploy is
    not recorded and the next tick redeploys the current heads; that errs towards deploying.
    """
    shown = _shown(sha, direction_sha)
    try:
        # Re-resolve before using attempt inputs. A queued attempt from before a rename or newer
        # push must never query CI or fetch source at a stale commit under the current repository.
        heads = _heads(profile, app)
        repo = heads[0]
        if (sha, direction_sha) != (heads[1], heads[3]):
            _say(f"{shown} is no longer the head; standing down")
            # Best effort: the tick may still see this run live; the periodic reconcile backs it.
            try:
                await _submit(_deploy.DEPLOYER_TASK)
            except REMOTE_ERRORS as exc:  # the periodic tick recovers
                _say(f"could not submit follow-up tick: {_deploy.redact(str(exc))}")
            return {"deployed": False}
        if not await _admitted(profile, app, run_name):
            return {"deployed": False}
        # An older pair for the same heads may have deployed and finished before this one was
        # placed; admission only says no other deploy is live.
        release = await deployed_pair(profile)
        if release and release[1] == (sha, direction_sha):
            _say(f"up to date at {shown}")
            return {"deployed": False}
        previous_slots = {s.env for s in profile.slots}
        if release:
            (previous,) = await release[0].outputs.aio()
            # Older releases did not record slots; the pod profile is their deployed configuration.
            previous_slots = set(previous.get("slots", previous_slots))
        output: DeployOutput = {"deployed": False}
        # Before any slot is deployed, and once per run: it launches a pod.
        push, isolation = await asyncio.to_thread(_push_mode, profile)
        # What a follow-up is judged on: the read set of the profile its predecessor deployed.
        deployed_profile = profile
        for deploy in range(MAX_DEPLOYS_PER_RUN):
            # Every deploy, the first included, is judged on CI as it is now: the tick that queued this
            # run may have seen green minutes ago (lock wait, queue time).
            heads = _heads(profile, app)
            if deploy:
                if (heads[1], heads[3]) == (sha, direction_sha):
                    break
                unread = await asyncio.to_thread(
                    _explain_unread_only_skip, deployed_profile, app, (sha, direction_sha), heads[1], heads[3]
                )
                if unread:
                    _say(unread)
                    break
                sha, direction_sha = heads[1], heads[3]
                shown = _shown(sha, direction_sha)
            elif (heads[1], heads[3]) != (sha, direction_sha):
                # Moved while waiting for the lock; the next tick judges the new heads.
                _say(f"{shown} is no longer the head; leaving it for the next tick")
                break
            # The tick that submitted this pair already judged its credential failures (and was forced past
            # them, or saw none); a follow-up's heads are new and judged here.
            refused, _, _ = await _not_deployable(
                profile, app, heads, release and release[0], retry_credentials=not deploy
            )
            if refused:
                _say(_describe_refusal(refused))
                if not output["deployed"]:  # the run succeeds: its report is where the reason is read
                    output = {**output, "refused": refused}
                break
            try:
                envs, deployed_profile, standards, resolved = await asyncio.to_thread(
                    _deploy_sha, profile, app, repo, sha, direction_sha, push, isolation
                )
            except _MoveNotReady as exc:
                # The run succeeds, so the refusal goes in its output as well as its log: nothing else has it.
                _say(_describe_refusal(str(exc)))
                output = {**output, "refused": str(exc)}
                break
            except _deploy.MissingSecrets as exc:
                # Nothing was deployed and the release is fine: not a failed attempt (which counts against
                # MAX_ATTEMPTS) and not a release. The next tick resubmits, and deploys once they exist.
                _say(str(exc))
                return {**output, "waiting_for_secrets": exc.secrets}
            slots = {s.env for s in deployed_profile.slots}
            if slots != previous_slots:
                await _wake_dispatch(deployed_profile)
            previous_slots = slots
            moved = resolved is not None and bool(resolved.moved_from)
            if moved:
                direction_sha = cast(_ResolvedDirection, resolved).sha
                shown = _shown(sha, direction_sha)
            _say(f"deployed {shown}: {', '.join(envs)}")
            output = {
                "deployed": True,
                "envs": envs,
                "slots": sorted(slots),
                "sha": sha,
                "direction_sha": direction_sha,
                "standards": standards,
                "github_push": push,
                "isolation": isolation,
            }
            if moved:
                _say(
                    f"direction moved: {cast(_ResolvedDirection, resolved).moved_from} -> "
                    f"{cast(_ResolvedDirection, resolved).repo} at {direction_sha}"
                )
                # The redeployed deployer reads the new repo, where this is its direction head. Following
                # the heads with this (old) profile would resolve the old repo's head and deploy it again.
                break
        return output
    except _DEPLOY_ERRORS as exc:  # task boundary redacts remote errors
        raise _deploy.DeployError(f"deploy {shown} failed: {_deploy.redact(str(exc))}") from None


def _app() -> _github.GithubApp:
    if _deploy.GITHUB_KEY_ENV not in os.environ:
        raise _deploy.DeployError(f"[deploy] auto needs the {_deploy.GITHUB_KEY_ENV} secret mounted")
    return _github.GithubApp(PROFILE.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])


def _report_html(message: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"<h3>factory-deployer</h3><p>{html.escape(message)}</p><p><small>{stamp}</small></p>"


def _describe_outcome(output: DeployOutput) -> str:
    """Describe an attempt for its report: what it deployed, why not, or which secrets it is waiting for."""
    deployed = f"deployed {', '.join(output['envs'])}" if output.get("deployed") else "nothing deployed"
    warning = output.get("standards", {}).get("warning")
    if warning:
        deployed += f"; {warning}"
    if "github_push" in output:
        deployed += "; " + _github_mode.describe(
            PROFILE,
            {_github_mode.PUSH_ENV_VAR: output["github_push"]},
            "; ".join(output.get("isolation", [])),
        )
    refused = output.get("refused")
    if refused:
        deployed += f"; {_describe_refusal(refused)}"
    waiting = output.get("waiting_for_secrets")
    if not waiting:
        return deployed
    needs = "; ".join(f"{name} (needed by {', '.join(envs)})" for name, envs in sorted(waiting.items()))
    return f"{deployed}; waiting for secrets, create them with `flyte create secret <name>`: {needs}"


@env.task(
    report=True,
    timeout=flyte.Timeout(max_runtime=MAX_RUNTIME, max_queued_time=timedelta(minutes=5)),
)
async def deploy_pair(sha: str, direction_sha: str) -> dict[str, Any]:
    # Flyte serializes TypedDict through Pydantic, adding defaults for absent keys. Keep its
    # existing untyped STRUCT wire format here; run_pair carries the precise shape internally.
    # Remote tasks always have a run name.
    try:
        _union_identity.establish()
        output = await run_pair(PROFILE, _app(), cast(str, flyte.ctx().action.run_name), sha, direction_sha)
    except Exception as exc:
        await flyte.report.replace.aio(_report_html(f"failed: {_deploy.redact(str(exc))}"), do_flush=True)
        raise
    who = identities(PROFILE)
    shown = ", ".join(f"{i['tier']}: {i['secret']} ({i['client_id']})" for i in who)
    await flyte.report.replace.aio(
        _report_html(f"{_describe_outcome(output)}; identities {shown}"), do_flush=True
    )
    return {**cast(dict[str, Any], output), "identities": who}


@env.task(
    report=True,
    # The SDK accepts lists at runtime, but its annotation only admits tuples.
    triggers=cast(tuple[flyte.Trigger, ...], _triggers(PROFILE)),
    timeout=flyte.Timeout(max_runtime=MAX_RUNTIME, max_queued_time=timedelta(minutes=5)),
)
async def deploy_if_changed(force: bool = False) -> str:
    """Deploy the factory repo's default branch if it moved since the last deploy.

    `force` deploys a direction repo change that touches nothing the deploy reads (`_deploy_reads`), and
    retries heads whose last attempt failed on a credential or permission (once fixed); it does not lift the
    MAX_ATTEMPTS limit.
    """
    # This task only runs within a named remote action.
    run_name = cast(str, flyte.ctx().action.run_name)
    try:
        _union_identity.establish()  # before the run listing below, which acts as this identity
        skipped = await _tick_guard.skip_reason(_deploy.DEPLOYER_TASK, project=PROJECT, domain=DOMAIN)
        if skipped:
            await flyte.report.replace.aio(_report_html(skipped), do_flush=True)
            return skipped
        app = None
        if PROFILE.deploy.auto:
            app = _app()
        message = await run_once(PROFILE, run_name, app, force=force)
    except Exception as exc:
        await flyte.report.replace.aio(_report_html(f"failed: {_deploy.redact(str(exc))}"), do_flush=True)
        raise
    await flyte.report.replace.aio(_report_html(message), do_flush=True)
    return message
