"""The app tools of the factory's MCP server: summary, status history, logs, restart, health.

The stock `get_app`, `activate_app` and `deactivate_app` return the whole App proto, environment-variable
values included, and there is no stock tool for an app's logs, failure reason or restart. These replace the
first three and add the rest. Every tool looks the app up in the factory's project and domain only.

The process environment is read only for the cluster endpoint, the org and the server's own names, never for a
value that could be a secret; an app's environment names come from its spec and are listed, never their
values. Redaction of the results is the registration layer's job (`_mcp_redact`); the one exception is a
message this module cuts to a cap, which it redacts first (`shorten_after_redaction`), since a cut secret no
longer matches any rule. `on_step` and the health inputs are parameters, so it does not import `_mcp_budget`;
the one time format comes from `_timefmt`, which needs no `mcp`.

SDK proposals: gaps 7 (status history) and 10 (no env values) in docs/mcp-sdk-requirements.md replace
`get_app_status` and `summarise_app`; `restart_app` and `factory_mcp_health` stay factory-side.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import flyte.remote
from flyteidl2.app import app_definition_pb2  # type: ignore[import-untyped]  # no py.typed marker
from mcp.server.fastmcp import (
    Context as MCPContext,
)  # annotations below are strings; FastMCP resolves them here
from mcp.server.fastmcp.exceptions import ToolError

from . import _app_logs
from ._errors import REMOTE_ERRORS
from ._factory import DOMAIN, PROJECT
from ._mcp_redact import shorten_after_redaction
from ._timefmt import format_utc as _iso

FailureClass = Literal["crash", "oom", "probe_failed", "image_pull", "secret_mount", "unknown"]
OnStep = Callable[[str], Awaitable[None] | None]

#: The MCP app's own name, set in its `env_vars` at deploy. It is how a separately named test app knows
#: itself, so `restart_app` can refuse it. Unset means the production name.
OWN_APP_ENV = "AGENT_FACTORY_MCP_APP"
DEFAULT_OWN_APP = "factory-mcp"

#: How long a deactivate or an activate may take to show in `App.get`. Restart gets half of this for each of
#: its two transitions, so it stays inside one call budget.
APP_TRANSITION_CAP_S = 150.0
POLL_INTERVAL_S = 3.0
#: Longest condition message kept in a result; the full text is rarely more than a Kubernetes event.
_MESSAGE_CAP = 600
#: Condition history the API keeps per app.
_HISTORY_API_CAP = 40
_ERROR_MARKERS = ("ERROR", "Traceback", "CRITICAL")
#: A replaced replica is matched to a failure condition when the condition is this close after its last line.
_RESTART_MATCH_WINDOW = timedelta(minutes=10)

# One bare `dict` return type and a bare `MCPContext` on purpose, as in `_factory_mcp.get_run_io`: FastMCP
# finds its Context argument by `issubclass`, and `dict[str, Any]` would make it add a second, structured
# content block.

_FAILURE_SUBSTATES = {"CRASH_LOOP", "OOM_KILLED", "IMAGE_PULL_ERROR", "SECRET_MOUNT_ERROR", "WEBHOOK_ERROR"}
_DEPLOYMENT_STATUS_PREFIX = "DEPLOYMENT_STATUS_"


def classify_failure(substate: str, message: str, system_lines: Sequence[str] = ()) -> FailureClass:
    """Name the kind of failure a condition (and, for probes, the SYSTEM log lines) describes.

    The raw reason stays in the result next to the class, so `unknown` loses nothing.
    """
    text = " ".join([substate, message, *system_lines]).lower()
    # 137 is SIGKILL, which for a container is the kernel's OOM killer unless something else killed it.
    if any(k in text for k in ("oom_killed", "oomkilled", "exit code 137")):
        return "oom"
    image = (
        "image_pull_error",
        "errimagepull",
        "imagepullbackoff",
        "errimageneverpull",
        "invalidimagename",
        "back-off pulling image",
    )
    if any(k in text for k in image):
        return "image_pull"
    if "secret_mount_error" in text:
        return "secret_mount"
    if "probe failed" in text:
        return "probe_failed"
    crash = ("crash_loop", "crashloopbackoff", "exit code", "back-off restarting failed container")
    if any(k in text for k in crash):
        return "crash"
    return "unknown"


# --- looking an app up -------------------------------------------------------------------------------------


async def _get_app(name: str) -> Any:
    """Return the factory's app `name`; a seam the tests replace with a fake control plane."""
    return await flyte.remote.App.get.aio(name, project=PROJECT, domain=DOMAIN)


def _refuse_foreign(tool: str, project: str | None, domain: str | None) -> None:
    """Refuse a project or domain other than the factory's, before any remote call.

    The registration layer's scope wrapper does the same for every tool by parameter name; doing it here too
    keeps these tools safe when called on their own.
    """
    for argument, given, own in (("project", project, PROJECT), ("domain", domain, DOMAIN)):
        if given and given != own:
            raise ToolError(
                f"{tool}: {argument} {given!r} is not allowed; this server is limited to {own!r}."
            )


def read_own_app_name() -> str:
    """Return the name of the app this server runs as, from its environment or the default."""
    return os.environ.get(OWN_APP_ENV) or DEFAULT_OWN_APP


def _refuse_own_app(tool: str, name: str) -> None:
    if name == read_own_app_name():
        raise ToolError(
            f"{tool}: {name!r} is this server's own app. Stopping it ends this very call before it can be "
            "started again, which would leave the server down; ask for it from outside."
        )


def _read_pb_time(pb_time: Any) -> datetime | None:
    return pb_time.ToDatetime(tzinfo=UTC) if pb_time.seconds or pb_time.nanos else None


def _format_status_name(value: int) -> str:
    name: str = app_definition_pb2.Status.DeploymentStatus.Name(value)
    return name.removeprefix(_DEPLOYMENT_STATUS_PREFIX)


def _format_substate_name(value: int) -> str:
    name: str = app_definition_pb2.Status.Substate.Name(value)
    return name


# --- the summary ------------------------------------------------------------------------------------------


def summarise_app(app: Any) -> dict[str, Any]:
    """Summarise an app: where it runs and what it runs, naming its env and secrets but never their values."""
    pb = app.pb2
    spec, status = pb.spec, pb.status
    replicas = spec.autoscaling.replicas
    secrets = [f"{s.group}/{s.key}" if s.group else s.key for s in spec.security_context.secrets]
    return {
        "name": pb.metadata.id.name,
        "project": pb.metadata.id.project,
        "domain": pb.metadata.id.domain,
        "status": _format_status_name(app.deployment_status),
        "url": status.ingress.public_url,
        "image": spec.container.image,
        "version": spec.runtime_metadata.version,
        "revision": pb.metadata.revision,
        "replicas": {"current": status.current_replicas, "min": replicas.min, "max": replicas.max},
        "last_started_at": _iso(_read_pb_time(status.last_started_at)),
        "env_names": sorted(entry.key for entry in spec.container.env),
        "secret_names": sorted(secrets),
    }


def _list_conditions_oldest_first(app: Any) -> list[dict[str, Any]]:
    """List the app's conditions oldest first, each with its failure class (None when it is not a failure)."""
    ordered = sorted(
        app.pb2.status.conditions,
        key=lambda c: (c.last_transition_time.seconds, c.last_transition_time.nanos),
    )
    found = []
    for condition in ordered:
        substate = _format_substate_name(condition.substate)
        message = shorten_after_redaction(condition.message, _MESSAGE_CAP)
        failure_class = classify_failure(substate, message)
        failed = (
            substate in _FAILURE_SUBSTATES
            or _format_status_name(condition.deployment_status) == "FAILED"
            or failure_class != "unknown"
        )
        found.append(
            {
                "at": _iso(_read_pb_time(condition.last_transition_time)),
                "deployment_status": _format_status_name(condition.deployment_status),
                "substate": substate,
                "class": failure_class if failed else None,
                "message": message,
            }
        )
    return found


async def get_app(
    name: str,
    project: str | None = None,
    domain: str | None = None,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Get an app's summary: status, URL, image, version, revision, replicas, env and secret names.

    Environment variable values and secret contents are never returned, only their names.
    """
    _refuse_foreign("get_app", project, domain)
    return summarise_app(await _get_app(name))


# --- logs -------------------------------------------------------------------------------------------------


class LogsUnavailable(Exception):
    """The server cannot reach app logs; the message names what is missing."""


def _build_caller_headers(ctx: Any) -> dict[str, str]:
    """Return the Authorization and Cookie of the MCP request: the caller's own credentials, never a pod's."""
    try:
        headers = ctx.request_context.request.headers
    except (AttributeError, ValueError):  # no context, or called outside a request
        return {}
    return {name: headers[name] for name in ("authorization", "cookie") if name in headers}


def _open_log_source(ctx: Any) -> _app_logs.LogSource:
    """Build the log source for this call; a seam the tests replace with a fake."""
    endpoint = os.environ.get("_U_EP_OVERRIDE")
    if not endpoint:
        raise LogsUnavailable("_U_EP_OVERRIDE is not set in this server, so the cluster endpoint is unknown")
    return _app_logs.ConnectLogSource(
        endpoint=endpoint,
        org=os.environ.get("_U_ORG_NAME", ""),
        project=PROJECT,
        domain=DOMAIN,
        headers=_build_caller_headers(ctx),
        insecure=os.environ.get("_U_INSECURE", "").lower() in ("1", "true", "yes"),
    )


async def _read_logs(ctx: Any, name: str, **options: Any) -> dict[str, Any]:
    """Read logs, turning a missing endpoint into the same kind of gap a failed read gives."""
    try:
        source = _open_log_source(ctx)
    except LogsUnavailable as exc:
        return {"app": name, "lines": [], "returned": 0, "replicas": [], "revisions": [], "gaps": [str(exc)]}
    return await _app_logs.read_app_logs(source, name, **options)


async def get_app_logs(
    name: str,
    max_lines: int = _app_logs.DEFAULT_MAX_LINES,
    since: str | None = None,
    until: str | None = None,
    replica: str | None = None,
    revision: str | None = None,
    source: str = "both",
    include_system: bool = True,
    project: str | None = None,
    domain: str | None = None,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Get an app's newest log lines, newest last, each with the replica and revision it came from.

    `max_lines` is capped at 1000; below 1 it reads 1, like the stock `get_logs`.
    `since` and `until` are an ISO-8601 time or an age (`15m`, `2h`, `1d`).
    `replica` and `revision` filter; `source` is `live`, `persisted` (reaches replicas that have died) or
    `both`. `include_system` keeps pod events such as failed probes. A persisted history can be long to read,
    so a read is bounded in time and says when it stopped early.
    """
    _refuse_foreign("get_app_logs", project, domain)
    if source not in ("live", "persisted", "both"):
        raise ToolError(f"get_app_logs: source {source!r} is not live, persisted or both.")
    now = datetime.now(UTC)
    try:
        window = (_app_logs.parse_when(since, now), _app_logs.parse_when(until, now))
    except ValueError as exc:
        raise ToolError(f"get_app_logs: {exc}") from None
    result = await _read_logs(
        ctx,
        name,
        max_lines=max_lines,
        since=window[0],
        until=window[1],
        replica=replica,
        revision=revision,
        source=source,
        include_system=include_system,
    )
    return {"project": PROJECT, "domain": DOMAIN, **result}


# --- status history and the last failure -----------------------------------------------------------------


async def get_app_status(
    name: str,
    project: str | None = None,
    domain: str | None = None,
    history: int = 20,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Get an app's status history (oldest first) and why it last failed, classified.

    The class is `crash`, `oom`, `probe_failed`, `image_pull`, `secret_mount` or `unknown`; the raw message is
    kept. A failed probe appears only in the app's system log lines, so those are read when no condition
    failed.
    """
    _refuse_foreign("get_app_status", project, domain)
    app = await _get_app(name)
    conditions = _list_conditions_oldest_first(app)
    gaps = []
    if len(conditions) >= _HISTORY_API_CAP:
        gaps.append(
            f"Union keeps only the newest {_HISTORY_API_CAP} conditions, so older history is not available"
        )
    failures = [c for c in conditions if c["class"]]
    last_failure: dict[str, Any] | None = None
    if failures:
        newest = failures[-1]
        last_failure = {**newest, "evidence": "condition"}
    else:
        probe = await _last_probe_failure(ctx, name, gaps)
        if probe:
            last_failure = probe
    return {
        "app": name,
        "summary": summarise_app(app),
        "history": conditions[-max(1, history) :],
        "last_failure": last_failure,
        "gaps": gaps,
    }


async def _last_probe_failure(ctx: Any, name: str, gaps: list[str]) -> dict[str, Any] | None:
    """Return the newest failed-probe SYSTEM line of the last hour as a failure; probes show only there."""
    result = await _read_logs(
        ctx,
        name,
        max_lines=_app_logs.MAX_LINES_CAP,
        since=datetime.now(UTC) - timedelta(hours=1),
        source="live",
    )
    gaps.extend(result.get("gaps", []))
    for entry in reversed(result["lines"]):
        if entry["originator"] == "system" and "probe failed" in entry["message"].lower():
            return {
                "at": entry["ts"],
                "class": "probe_failed",
                "substate": "",
                "message": shorten_after_redaction(entry["message"], _MESSAGE_CAP),
                "replica": entry["replica"],
                "evidence": "logs",
            }
    return None


# --- activate, deactivate, restart -------------------------------------------------------------------------


async def _record_step(acted: list[str], on_step: OnStep | None, message: str) -> None:
    """Record a step done and tell the caller, so a call that is cut off still shows how far it got."""
    acted.append(message)
    if on_step is not None:
        outcome = on_step(message)
        if inspect.isawaitable(outcome):
            await outcome


async def _wait_until(name: str, reached: Callable[[Any], bool], cap_s: float) -> Any | None:
    """Poll `App.get` until `reached(app)`; None when `cap_s` is spent. Polling avoids the denied Watch."""
    deadline = time.monotonic() + cap_s
    while True:
        app = await _get_app(name)
        if reached(app):
            return app
        if time.monotonic() + POLL_INTERVAL_S > deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL_S)


async def _run_transition(
    tool: str,
    name: str,
    stop: bool,
    start: bool,
    on_step: OnStep | None,
) -> dict[str, Any]:
    """Run a deactivate (`stop`), an activate (`start`) or both in turn, polling after each, and report it."""
    began = time.monotonic()
    requested_at = datetime.now(UTC)
    app = await _get_app(name)
    before = summarise_app(app)
    acted: list[str] = []
    outcome = "restarted" if (stop and start) else "done"
    error = ""
    # Each transition gets its share of the cap, so the whole call stays inside the call budget.
    cap = APP_TRANSITION_CAP_S / (2 if (stop and start) else 1)
    try:
        stopped = True
        if stop:
            await app.deactivate.aio(wait=False)
            await _record_step(acted, on_step, "deactivate requested")
            stopped = await _wait_until(name, lambda a: a.is_deactivated(), cap) is not None
            await _record_step(acted, on_step, "stopped" if stopped else "stop not seen in time")
            if not stopped:
                outcome = "timed_out"
        # A restart asks for the app back even when the stop was not seen in time: the desired state is then
        # active again, so a slow stop never leaves the app down.
        if start:
            app = await _get_app(name)
            await app.activate.aio(wait=False)
            await _record_step(acted, on_step, "activate requested")
            if await _wait_until(name, lambda a: a.is_active(), cap) is None:
                outcome = "timed_out"
            else:
                await _record_step(acted, on_step, "active")
                if not stopped:
                    # Active again, but the stop was never seen: it may not have restarted at all.
                    outcome = "restart_unconfirmed"
    except REMOTE_ERRORS as exc:
        outcome, error = "failed", f"{type(exc).__name__}: {exc}"
    result: dict[str, Any] = {
        "app": name,
        "tool": tool,
        "requested_at": _iso(requested_at),
        "acted": acted,
        "revision_before": before["revision"],
        "replicas_before": before["replicas"]["current"],
        "outcome": outcome,
        "elapsed_s": round(time.monotonic() - began, 1),
    }
    if error:
        result["error"] = error
    try:
        after = summarise_app(await _get_app(name))
    except REMOTE_ERRORS as exc:
        result["gaps"] = [f"could not read the app afterwards: {type(exc).__name__}: {exc}"]
    else:
        result.update(
            summary=after, revision_after=after["revision"], replicas_after=after["replicas"]["current"]
        )
    if outcome in ("timed_out", "restart_unconfirmed"):
        result["next"] = f"call get_app_status for {name!r}; activate_app starts it if it is left stopped"
    return result


async def restart_app(
    name: str,
    project: str | None = None,
    domain: str | None = None,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
    on_step: OnStep | None = None,
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Restart an app by deactivating then activating it (never by deleting it), and say what was done.

    Reports the revision and replicas before and after, and each step done. The app is unreachable for a
    while. This server's own app is refused: stopping it would end this call before it could start it again.
    """
    _refuse_foreign("restart_app", project, domain)
    _refuse_own_app("restart_app", name)
    return await _run_transition("restart_app", name, stop=True, start=True, on_step=on_step)


async def activate_app(
    name: str,
    project: str | None = None,
    domain: str | None = None,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
    on_step: OnStep | None = None,
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Start an app and wait (bounded) for it to be active; the result never includes environment values."""
    _refuse_foreign("activate_app", project, domain)
    return await _run_transition("activate_app", name, stop=False, start=True, on_step=on_step)


async def deactivate_app(
    name: str,
    project: str | None = None,
    domain: str | None = None,
    ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
    on_step: OnStep | None = None,
) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
    """Stop an app and wait (bounded) for it to be stopped. This server's own app is refused."""
    _refuse_foreign("deactivate_app", project, domain)
    _refuse_own_app("deactivate_app", name)
    return await _run_transition("deactivate_app", name, stop=True, start=False, on_step=on_step)


# --- health -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthInputs:
    """What only the registration layer knows; each is a callable so this module imports nothing of it."""

    calls: Callable[[], Mapping[str, Any]] | None = (
        None  # {"count", "still_working", "dropped", "p95_ms", "tool_errors"}
    )
    loop_lag: Callable[[], Mapping[str, Any]] | None = None  # {"max_5m", "p95_5m"}
    operations_in_flight: Callable[[], int] | None = None
    budget_s: float | None = None
    started_at: datetime | None = None  # the server's own start clock, so health and calls agree


#: The container's cgroup (v2), read for the health tool's `process` field.
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read_cgroup_value(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:  # not cgroup v2, or not in a container
        return None
    return int(text) if text.isdigit() else None


def _read_cgroup_fields(path: Path, *keys: str) -> dict[str, int]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    pairs = (line.split() for line in lines)
    return {p[0]: int(p[1]) for p in pairs if len(p) == 2 and p[0] in keys and p[1].isdigit()}


def read_process_usage(cgroup: Path = CGROUP_ROOT) -> dict[str, Any]:
    """Read this process's CPU time and memory, and its container's limits, throttling and OOM kills.

    It tells a starved or memory-squeezed pod from a slow backend: `cpu_throttled_s` growing under load means
    a CPU limit, `memory_current_mb` near `memory_max_mb` (or `oom_kills` > 0) a memory limit. A field the
    container does not expose is left out.
    """
    times = os.times()
    usage: dict[str, Any] = {"cpu_s": round(times.user + times.system, 2)}
    try:
        rss_pages = int(Path("/proc/self/statm").read_text().split()[1])
        usage["rss_mb"] = round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
    except (OSError, ValueError, IndexError):
        pass
    for key, name in (("memory_current_mb", "memory.current"), ("memory_peak_mb", "memory.peak")):
        value = _read_cgroup_value(cgroup / name)
        if value is not None:
            usage[key] = round(value / 2**20, 1)
    limit = _read_cgroup_value(cgroup / "memory.max")  # "max" (no limit) is not a number and is left out
    if limit is not None:
        usage["memory_max_mb"] = round(limit / 2**20, 1)
    cpu = _read_cgroup_fields(cgroup / "cpu.stat", "nr_periods", "nr_throttled", "throttled_usec")
    if "throttled_usec" in cpu:
        usage["cpu_throttled_s"] = round(cpu["throttled_usec"] / 1e6, 2)
        usage["cpu_throttled_periods"] = cpu.get("nr_throttled", 0)
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()
        usage["cpu_limit"] = None if quota == "max" else round(int(quota) / int(period), 2)
    except (OSError, ValueError):
        pass
    events = _read_cgroup_fields(cgroup / "memory.events", "oom", "oom_kill")
    if "oom_kill" in events:
        usage["oom_kills"] = events["oom_kill"]
    return usage


#: When this process started: the module is imported once, as the server comes up.
_STARTED_AT = datetime.now(UTC)


def _match_restarts(
    conditions: list[dict[str, Any]], replicas: Mapping[str, datetime], current: str, since: datetime
) -> list[dict[str, Any]]:
    """Combine failure conditions and replaced replicas into restart entries, newest last.

    A replica that is not the current one was replaced. It is matched to the failure condition that follows
    its last log line; a failure with no replica is a container restarted in place, which keeps its pod.
    """
    failures = [c for c in conditions if c["class"] and c["at"] and c["at"] >= _iso(since)]
    # Union can keep one kill as two identical conditions (seen live: OOM_KILLED twice at one second, one
    # message), so the same time and message is one event.
    unused = list({(c["at"], c["message"]): c for c in failures}.values())
    entries: list[dict[str, Any]] = []
    for replica, last_seen in sorted(replicas.items(), key=lambda item: item[1]):
        if replica == current:
            continue
        match = next(
            (
                c
                for c in unused
                if last_seen - timedelta(minutes=1)
                <= datetime.fromisoformat(c["at"].replace("Z", "+00:00"))
                <= last_seen + _RESTART_MATCH_WINDOW
            ),
            None,
        )
        if match:
            unused.remove(match)
        entries.append(
            {
                "at": match["at"] if match else _iso(last_seen),
                "replica": replica,
                "revision": _app_logs.find_revision_of(replica),
                "class": match["class"] if match else "unknown",
                "reason": match["message"] if match else "replaced (no condition)",
            }
        )
    entries.extend(
        {"at": c["at"], "replica": "", "revision": "", "class": c["class"], "reason": c["message"]}
        for c in unused
    )
    return sorted(entries, key=lambda e: e["at"] or "")


def make_factory_mcp_health(inputs: HealthInputs) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build the health tool around what the registration layer can measure."""

    async def factory_mcp_health(
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
        """Report this server's health: revision, uptime, restarts (24 h) and errors (last hour).

        The restart reasons and error samples come from the app's conditions and its logs, so they show what
        happened to earlier replicas too. `process` is this replica's CPU time and memory and its container's
        limits, CPU throttling and OOM kills. `gaps` names anything that could not be read.

        `errors_last_hour.count` is the log error lines among the newest lines of the hour that were read,
        plus the tool errors, which also have their own field, `tool_errors`.
        """
        name = read_own_app_name()
        now = datetime.now(UTC)
        started_at = inputs.started_at or _STARTED_AT
        gaps: list[str] = []
        pod = os.environ.get("HOSTNAME", "")
        try:
            app = await _get_app(name)
        except REMOTE_ERRORS as exc:
            app = None
            gaps.append(f"app record unavailable: {type(exc).__name__}: {exc}")
        summary = summarise_app(app) if app else {}
        conditions = _list_conditions_oldest_first(app) if app else []
        oldest = conditions[0]["at"] if conditions else None
        if len(conditions) >= _HISTORY_API_CAP and oldest and oldest > _iso(now - timedelta(hours=24)):
            gaps.append(
                f"Union keeps only the newest {_HISTORY_API_CAP} conditions, which reach back to {oldest}; "
                "restarts before that are not counted"
            )

        # Two reads, each up to its own wall-time bound: run side by side so the tool keeps to one bound.
        day, recent = await asyncio.gather(
            _read_logs(
                ctx,
                name,
                max_lines=_app_logs.MAX_LINES_CAP,
                since=now - timedelta(hours=24),
                source="persisted",
            ),
            _read_logs(ctx, name, max_lines=_app_logs.MAX_LINES_CAP, since=now - timedelta(hours=1)),
        )
        gaps.extend(day.get("gaps", []))
        last_seen: dict[str, datetime] = {}
        for entry in day["lines"]:
            when = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
            last_seen[entry["replica"]] = max(when, last_seen.get(entry["replica"], when))
        restarts = _match_restarts(conditions, last_seen, pod, now - timedelta(hours=24))
        if day.get("truncated") and day["stopped_because"] == "stream_end":
            # Read to its end, so the newest lines were kept and the older ones dropped.
            gaps.append(
                "the 24 h of persisted logs was longer than one read; the oldest replicas may be missing"
            )
        elif day.get("truncated"):
            # Cut: the persisted stream is oldest first, so it is the newest lines that are missing.
            gaps.append(
                "the 24 h of persisted logs was cut before its end; replicas that appear only in the newest "
                "lines may be missing"
            )

        gaps.extend(g for g in recent.get("gaps", []) if g not in gaps)
        # A live stream never ends, so "idle" is the normal stop; only a cap or the deadline loses errors.
        if recent.get("truncated"):
            gaps.append(
                f"errors_last_hour is counted over the newest {_app_logs.MAX_LINES_CAP} lines only; "
                "older errors in the hour are not counted"
            )
        elif recent.get("stopped_because") == "deadline":
            gaps.append(
                "the hour's log read hit its deadline before the streams were drained; "
                "errors it did not reach are not counted"
            )
        errors = [e["message"] for e in recent["lines"] if any(m in e["message"] for m in _ERROR_MARKERS)]
        calls = dict(inputs.calls()) if inputs.calls else {}
        return {
            "app": name,
            "revision": _app_logs.find_revision_of(pod) or str(summary.get("revision", "")),
            "replica": pod,
            "started_at": _iso(started_at),
            "uptime_s": int((now - started_at).total_seconds()),
            "image": summary.get("image", ""),
            "version": summary.get("version", ""),
            "restarts": {"count_24h": len(restarts), "recent": restarts[-10:]},
            "errors_last_hour": {
                "count": len(errors) + int(calls.get("tool_errors", 0)),
                "samples": [shorten_after_redaction(message, 300) for message in errors[-5:]],
                "tool_errors": int(calls.get("tool_errors", 0)),
            },
            "calls_last_hour": {
                k: calls[k] for k in ("count", "still_working", "dropped", "p95_ms") if k in calls
            },
            "loop_lag_ms": dict(inputs.loop_lag()) if inputs.loop_lag else {},
            "process": read_process_usage(),
            "budget_s": inputs.budget_s,
            "operations_in_flight": inputs.operations_in_flight() if inputs.operations_in_flight else 0,
            "gaps": gaps,
        }

    return factory_mcp_health
