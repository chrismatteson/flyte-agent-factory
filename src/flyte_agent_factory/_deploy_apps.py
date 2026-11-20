"""Deploying an app that must be replaced, not rolled over (`deploy_app`), and stopping a dropped one.

Flyte's own `flyte.deploy` of an app updates it in place, and Knative starts the new revision while the old
one still runs. The personal browser holds a single-owner lease on its profile archive that the running pod
renews every 30 s and a new pod waits only 180 s for, so a rolling update can never become ready. The old
revision has to stop first, which is a brief gap in service; this module makes it the only way an app is
updated, and puts the previous spec back if the new one does not come up.

`_deploy.APPS_CHILD` calls it from the engine tree. It is a normal module, so it is tested with a fake
`flyte.remote.App`.

Every app of the factory stage (`factory-mcp`, and `factory-webhook` when the profile configures it) is
deployed by `_deploy.FACTORY_CHILD` with Flyte's own rolling update; a restart of `factory-mcp` drops the chat
connector. Its container command carries the code bundle's `--tgz` and `--version` and the deploy's
`--image-cache`, which change on every deploy, so Flyte's own "spec unchanged, skip" never fires.
`is_unchanged` compares the specs without those three, and each spec carries a digest of the code the app can
run (`_code_closure`, `CODE_DIGEST_ENV`), so a change to a slot's code leaves the apps running and a change to
an app's own code restarts it. `drop_unchanged_apps` leaves the unchanged ones out of the deploy;
`RESTART_APPS_ENV` (`ops/deploy_factory.py --restart-mcp --restart-webhook`) restarts the ones named anyway.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import flyte
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote
from flyteidl2.app import app_definition_pb2  # type: ignore[import-untyped]  # no py.typed marker
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]  # no stubs installed

from . import _code_closure, _deploy
from ._errors import REMOTE_ERRORS

#: Seconds between looks at an app that is starting, and how long it may take: an image pull and a lease wait
#: (180 s at most) fit well inside it.
POLL_SECONDS = 10
ACTIVE_TIMEOUT_SECONDS = 15 * 60
#: How long the old app may take to stop before a replacement: generous against the pod's 90 s grace.
STOP_TIMEOUT_SECONDS = 5 * 60

#: The env var of each factory-stage app that carries `_code_closure.hash_import_closure`: code it runs
#: changing changes its spec. One name for every app; the value differs because the module does.
CODE_DIGEST_ENV = "FACTORY_CODE_DIGEST"
MCP_NAME = "factory-mcp"
WEBHOOK_NAME = "factory-webhook"
#: Comma-separated names of factory-stage apps to restart even when unchanged (a secret's value rotated, say,
#: which the spec does not show): `ops/deploy_factory.py --restart-mcp --restart-webhook`.
RESTART_APPS_ENV = "AGENT_FACTORY_RESTART_APPS"
#: Flags in an app's container command that differ on every deploy, whatever changed: the code bundle's,
#: and the image cache (every environment of the deploy, with build runs; the app's own image is
#: `container.image`).
_PER_DEPLOY_FLAGS = frozenset({"--tgz", "--version", "--image-cache"})

_FAILED = app_definition_pb2.Status.DeploymentStatus.DEPLOYMENT_STATUS_FAILED
#: What a key that may not read the app gets: no poll will get further, so a wait stops on the first.
_REFUSED = frozenset({Code.UNAUTHENTICATED, Code.PERMISSION_DENIED})


class _NotReached(Exception):
    """The app did not reach the state waited for: why, as a sentence, and its latest condition messages."""

    def __init__(self, reason: str, messages: str):
        super().__init__(reason)
        self.reason, self.messages = reason, messages


def _latest_condition_messages(app: Any) -> str:
    """Return the last three distinct condition messages of `app`, redacted: why it is down."""
    distinct: list[str] = []
    for condition in app.pb2.status.conditions:
        if condition.message:
            distinct = [m for m in distinct if m != condition.message] + [condition.message]
    return "; ".join(_deploy.redact(m) for m in distinct[-3:])


def _describe_failure(exc: BaseException) -> str:
    if isinstance(exc, _NotReached):
        return f"{exc.reason}: {exc.messages}" if exc.messages else exc.reason
    return f"deploy failed ({type(exc).__name__}: {_deploy.redact(str(exc))})"


def _wait_until(
    reached: Callable[[Any], bool],
    get: Callable[[], Any],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float,
    *,
    what: str,
    failed_is_fatal: bool,
) -> Any:
    """Return the app when `reached(get())`; `_NotReached` if `timeout` passes, or on FAILED when fatal."""
    deadline = clock() + timeout
    app = None
    while True:
        try:
            app = get()
        except REMOTE_ERRORS as exc:
            # A control-plane blip must not roll back a deploy under way; the deadline still ends this. A
            # refusal is not a blip: the key will not be let in on the next poll either, so say so now.
            if isinstance(exc, ConnectError) and exc.code in _REFUSED:
                reason = f"could not be read ({exc.code.name}: {_deploy.redact(exc.message)})"
                raise _NotReached(reason, "") from None
        else:
            if reached(app):
                return app
            if failed_is_fatal and app.deployment_status == _FAILED:
                raise _NotReached("failed to start", _latest_condition_messages(app))
        if clock() >= deadline:
            messages = _latest_condition_messages(app) if app else ""
            raise _NotReached(f"did not {what} in {timeout:.0f} s", messages)
        sleep(POLL_SECONDS)


def _wait_active(
    get: Callable[[], Any], sleep: Callable[[float], None], clock: Callable[[], float], timeout: float
) -> Any:
    """Return the deployed app when it is active; `_NotReached` if it fails or `timeout` passes."""
    return _wait_until(
        lambda app: app.is_active(), get, sleep, clock, timeout, what="become active", failed_is_fatal=True
    )


def _wait_stopped(
    get: Callable[[], Any], sleep: Callable[[float], None], clock: Callable[[], float], timeout: float
) -> None:
    """Return when the app is stopped; `_NotReached` if `timeout` passes.

    FAILED is not the end of it: a crash-looping app reports FAILED until the controller tears it down.
    """
    _wait_until(
        lambda app: app.is_deactivated(), get, sleep, clock, timeout, what="stop", failed_is_fatal=False
    )


def wait_until_active(
    name: str,
    *,
    app_cls: Any = remote.App,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = ACTIVE_TIMEOUT_SECONDS,
) -> Any:
    """Poll the app `name` until it is active and return it (for its `.endpoint`); DeployError if it is not.

    The SDK's `watch(wait_for="activated")` opens the watch stream, which an API key bound to one project
    and domain is refused (UNAUTHENTICATED / PERMISSION_DENIED on the organization); `App.get` is not.
    The error names the app and carries its own condition messages (`_describe_failure`).
    """
    try:
        return _wait_active(lambda: app_cls.get(name=name), sleep, clock, timeout)
    except _NotReached as exc:
        raise _deploy.DeployError(f"{name} {_describe_failure(exc)}") from None


def _roll_back(name: str, old_spec: Any, app_cls: Any) -> str:
    """Put `old_spec` back and start it, best effort; a sentence for the error."""
    try:
        app_cls.replace(name=name, updated_app_spec=old_spec, reason="restoring the previous spec")
        app_cls.get(name=name).activate(wait=False)
    except REMOTE_ERRORS as exc:  # the caller still has the deploy's own failure to report
        return f"restoring the previous spec failed too ({type(exc).__name__}: {_deploy.redact(str(exc))})"
    return "restored the previous spec"


def deploy_app(
    env: Any,
    *,
    dry_run: bool = False,
    deploy: Callable[..., Any] = flyte.deploy,
    app_cls: Any = remote.App,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = ACTIVE_TIMEOUT_SECONDS,
) -> list[Any]:
    """Deploy the app `env` with replace semantics; the deployments to record.

    The image is built (a dry run) before anything running is touched. A spec equal to the deployed one on
    an active app changes nothing. Otherwise the app is stopped, deployed and waited for; if it does not
    become active, the previous spec is restored and the error carries the app's own condition messages.
    Needs an initialised Flyte client.

    Raises DeployError when the new revision does not come up. A build failure (the dry run), a lookup error
    other than not-found, and a first deploy's remote error escape as the SDK raises them, and reach the
    stage's error through the child's stderr tail.
    """
    name = env.name
    planned = deploy(env, dry_run=True)  # a build failure raises here, with the old app still running
    if dry_run:
        return list(planned)
    new_spec = next(e.deployed_app.pb2.spec for d in planned for e in d.envs.values() if e.get_name() == name)

    try:
        old = app_cls.get(name=name)
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
        old = None

    if old is None:
        deployments = deploy(env)
        wait_until_active(name, app_cls=app_cls, sleep=sleep, clock=clock, timeout=timeout)
        return list(deployments)

    # `App.replace` compares specs this way: the creator is the old one's, whoever deploys.
    comparable = copy.deepcopy(new_spec)
    comparable.creator.CopyFrom(old.pb2.spec.creator)
    if old.is_active() and asyncio.run(app_cls._app_specs_are_equal(old.pb2.spec, comparable)):
        print(f"{name} unchanged", flush=True)
        return list(planned)

    old_spec = copy.deepcopy(old.pb2.spec)
    try:
        # Not the SDK's wait: its watch gives up on a FAILED condition, which a crash-looping app shows.
        old.deactivate(wait=False)
        _wait_stopped(lambda: app_cls.get(name=name), sleep, clock, STOP_TIMEOUT_SECONDS)
        deployments = deploy(env)
        _wait_active(lambda: app_cls.get(name=name), sleep, clock, timeout)
    except (_NotReached, *REMOTE_ERRORS) as exc:  # a failed stop, deploy or start: put the old one back
        raise _deploy.DeployError(
            f"{name} {_describe_failure(exc)}; {_roll_back(name, old_spec, app_cls)}"
        ) from None
    return list(deployments)


def _strip_flags(words: Iterable[str]) -> list[str]:
    kept: list[str] = []
    it = iter(words)
    for word in it:
        if word in _PER_DEPLOY_FLAGS:
            next(it, None)  # its value
        else:
            kept.append(word)
    return kept


def _strip_per_deploy_flags(spec: Any) -> Any:
    """Return a copy of `spec` whose command and args lack the `_PER_DEPLOY_FLAGS` pairs.

    The app runs from `container`, or, with a pod template (the volume state backend), from the containers
    of `pod.pod_spec`. Only the one that is set is touched: writing to the other would clear it (a oneof).
    """
    spec = copy.deepcopy(spec)
    if spec.HasField("container"):
        for field in (spec.container.command, spec.container.args):
            field[:] = _strip_flags(field)
    elif spec.HasField("pod"):
        pod_spec = MessageToDict(spec.pod.pod_spec)
        for container in pod_spec.get("containers", []):
            for key in ("command", "args"):
                if key in container:
                    container[key] = _strip_flags(container[key])
        spec.pod.pod_spec.Clear()
        spec.pod.pod_spec.update(pod_spec)
    return spec


def is_unchanged(env: Any, *, deploy: Callable[..., Any] = flyte.deploy, app_cls: Any = remote.App) -> bool:
    """Return whether the app `env` is deployed, active, and equal to what deploying it now would make.

    Equal as `App.replace` compares (the creator is the deployed one's), except for `--tgz`, `--version` and
    `--image-cache`, which are new on every deploy (the app's own image, `container.image`, is compared). A
    missing app is not unchanged, silently; any other lookup or control-plane error is printed and also
    means "not known to be unchanged": the caller deploys as it always did. Only the errors in
    `REMOTE_ERRORS` are caught, so a build failure in the dry run escapes and deploying would fail the same.
    """
    name = env.name
    try:
        old = app_cls.get(name=name)
        if not old.is_active():
            return False
        planned = deploy(env, dry_run=True)
        new_spec = next(
            e.deployed_app.pb2.spec for d in planned for e in d.envs.values() if e.get_name() == name
        )
        comparable = copy.deepcopy(new_spec)
        comparable.creator.CopyFrom(old.pb2.spec.creator)
        return bool(
            asyncio.run(
                app_cls._app_specs_are_equal(
                    _strip_per_deploy_flags(old.pb2.spec), _strip_per_deploy_flags(comparable)
                )
            )
        )
    except REMOTE_ERRORS as exc:
        if not (isinstance(exc, ConnectError) and exc.code == Code.NOT_FOUND):
            print(
                f"{name}: could not compare with the deployed app, deploying it: {_deploy.redact(str(exc))}"
            )
        return False


def stamp_code_digest(app: Any, package_dir: Path, module: str) -> None:
    """Put the digest of the code `module` can run into `app`'s env vars: a change to it is a spec change."""
    app.env_vars[CODE_DIGEST_ENV] = _code_closure.hash_import_closure(package_dir, module)


def drop_unchanged_apps(
    envs: tuple[Any, ...],
    apps: Iterable[Any],
    *,
    deploy: Callable[..., Any] = flyte.deploy,
    app_cls: Any = remote.App,
) -> tuple[tuple[Any, ...], list[str]]:
    """Return `envs` for a real deploy without the unchanged `apps`, and the names of the ones left out."""
    kept = [app for app in apps if is_unchanged(app, deploy=deploy, app_cls=app_cls)]
    kept_ids = {id(app) for app in kept}
    for app in kept:
        suffix = ", the connector keeps running" if app.name == MCP_NAME else ""
        print(f"{app.name} unchanged: not redeployed{suffix}", flush=True)
    return tuple(env for env in envs if id(env) not in kept_ids), [app.name for app in kept]


def stop_app(name: str, *, app_cls: Any = remote.App) -> bool:
    """Ask the app `name` to stop unless it is missing or already stopped; whether it was asked.

    Whatever state it is in otherwise (running, starting, failed), it is asked. Nothing waits for it: no
    deploy follows a retirement. Raises DeployError.
    """
    try:
        app = app_cls.get(name=name)
        if app.is_deactivated():
            return False
        app.deactivate(wait=False)
    except ConnectError as exc:
        if exc.code == Code.NOT_FOUND:
            return False
        raise _deploy.DeployError(
            f"{name} could not be stopped ({type(exc).__name__}: {_deploy.redact(str(exc))})"
        ) from None
    except REMOTE_ERRORS as exc:
        raise _deploy.DeployError(
            f"{name} could not be stopped ({type(exc).__name__}: {_deploy.redact(str(exc))})"
        ) from None
    return True


def deployed_personal_apps(*, app_cls: Any = remote.App) -> frozenset[str]:
    """Return the names of the deployed personal browser apps: `claude-browser-<x>`, never the shared one.

    An identity dropped from the profile leaves no trace in it, so the apps to stop are found on the server.
    """
    prefix = f"{_deploy.SHARED_BROWSER_APP}-"
    return frozenset(app.name for app in app_cls.listall(limit=1000) if app.name.startswith(prefix))


def retire_apps(names: Iterable[str], *, dry_run: bool = False, app_cls: Any = remote.App) -> list[str]:
    """Ask each app in `names`, which the profile does not enable, to stop; the names asked.

    Stopped, not deleted: a personal browser's profile archive stays, and enabling it again redeploys it.
    """
    stopped = []
    for name in sorted(names):
        if dry_run:
            print(f"would stop {name}: the profile does not enable it", flush=True)
        elif stop_app(name, app_cls=app_cls):
            print(f"{name}: stop requested, the profile does not enable it", flush=True)
            stopped.append(name)
    return stopped
