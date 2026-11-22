"""What exists only because the backend is OSS Flyte: following a run when the stock tools come up empty.

Which functions check `is_oss` (and are the identity on Union: they return None, the input unchanged, or
the stock tool) and which do not:

- Check `is_oss`: `find_run_by_label`, `wrap_get_logs`, `find_openable_url`, `fix_urls`; the callers in
  `_factory` check it before `read_stored_report`, `report_unavailable` and `list_open_questions`.
- Profile-agnostic: `is_oss` itself, `resolve_real_attempt`, `report_unavailable_reason`,
  `read_report_via_storage`, and the private helpers.

On OSS (`[backend] kind = "oss"`) the server differs:

- It resets an action's attempt count to 0 once the action starts a child, so the stock `get_logs` and
  `get_report` ask for attempt 0 and get "not found". The real attempt is in `ActionDetails.pb2.attempts`.
- It has no log aggregation: logs exist only while the pod does, and a missing stream is a bare error. The
  wrapper turns that into a gap result, but a run or action that does not exist stays a "not found" error.
- On the devbox its signed report links point at `localhost`, which a pod cannot reach; the report object can
  still be read from the object store with the pod's own credentials.
- A run URL built in a pod carries the in-cluster host, which no browser can open.
- It rejects label filters on a run listing, and a listing's runs carry no labels (`Run.pb2.labels` is empty):
  only `RunDetails.pb2.run_spec.labels` has them. A lookup of a project's run by its label needs the details.

Kubernetes pod logs through the API server were rejected: they need RBAC in the user's cluster and the
`kubernetes` package, which is deploy-only. The gap result names `kubectl logs` for the operator instead.

SDK proposals: gaps 9 (`get_logs` attempt) and 15 (URLs) in docs/mcp-sdk-requirements.md; the report
fallback and the run lookup stay factory-side (the table there).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import ipaddress
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import flyte.storage
import httpx
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote
from flyteidl2.common import identifier_pb2  # type: ignore[import-untyped]  # no py.typed marker
from obstore.exceptions import BaseError as ObstoreError

from flyte_agent_factory._errors import REMOTE_ERRORS

if TYPE_CHECKING:
    from flyte_agent_factory._profile import Profile

Reason = Literal["state_store", "permission", "missing_report", "unreachable"]

NO_URL_NOTE = "no openable URL: set [backend] console_host in the profile to get console links"
NO_URL_TEXT = "<no openable URL>"
_INTERNAL_SUFFIXES = (".svc", ".cluster.local", ".local", ".internal", ".localdomain", ".localhost")
# Namespaces a `<service>.<namespace>` host can end in. A two-label host cannot be told from a public
# domain without a public-suffix list (a new dependency), so only these names, and the namespace of the
# profile's own `internal_endpoint`, count; any other `a.b` host is kept as public.
_NAMESPACES = frozenset({"flyte", "flyte-system", "union", "default", "kube-system", "kube-public"})
_URL_KEYS = frozenset({"url", "endpoint", "link", "uri", "href"})
_URL_KEY_SUFFIXES = ("_url", "_uri", "_link", "_href", "_endpoint")
# http(s) URLs inside prose; trailing sentence punctuation is not part of the URL.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"
_OPENERS = {")": "(", "]": "[", "}": "{"}
_PERMISSION_WORDS = ("accessdenied", "access denied", "forbidden", "permission", "unauthorized")


def is_oss(profile: Profile | None) -> bool:
    """Whether the profile points at an OSS Flyte backend rather than Union."""
    return profile is not None and profile.backend.kind == "oss"


#: How many of the newest runs `find_run_by_label` reads the details of, and how long it may take.
_LABEL_LOOKUP_RUNS = 30
_LABEL_LOOKUP_SECONDS = 20.0


def find_run_by_label(
    profile: Profile | None,
    labels: Mapping[str, str],
    *,
    max_runs: int = _LABEL_LOOKUP_RUNS,
    budget_s: float = _LABEL_LOOKUP_SECONDS,
) -> Any:  # a `remote.Run`, the unreachable dict or None; Any keeps `.name` usable where a run was found
    """Return the newest of the last `max_runs` runs that carries all of `labels`, or None; OSS only.

    When the listing itself fails, return `{"why": "unreachable", "error": ...}` instead of None: a server
    that could not be asked is not the same as a project that has no run.

    One `GetRunDetails` per candidate, newest first, so the time is bounded by both the count and `budget_s`.
    A project whose run is older than that is not found: the caller says "not found" rather than scan on.
    """
    if not is_oss(profile):
        return None
    deadline = time.monotonic() + budget_s
    try:
        candidates = remote.Run.listall(limit=max_runs, sort_by=("created_at", "desc"))
        for run in candidates:
            if time.monotonic() >= deadline:
                break
            try:
                have = remote.RunDetails.get(name=run.name).pb2.run_spec.labels.values
            except REMOTE_ERRORS:
                continue  # one run that vanished or cannot be read does not end the search
            if all(have.get(k) == v for k, v in labels.items()):
                return run
    except REMOTE_ERRORS:
        return {"why": "unreachable", "error": "The Flyte server could not be reached to look up the run"}
    return None


def _build_action_id(run: str, action: str | None, org: str, project: str, domain: str) -> Any:
    return identifier_pb2.ActionIdentifier(
        run=identifier_pb2.RunIdentifier(org=org, project=project, domain=domain, name=run),
        name=action or "a0",
    )


def _find_newest_attempt(details: Any) -> Any | None:
    """Return the attempt with the highest number in `details.pb2.attempts`, or None."""
    return max(details.pb2.attempts, key=lambda a: a.attempt, default=None)


def _find_pod_of(attempt: Any) -> dict[str, str]:
    context = attempt.log_context
    pods = list(context.pods)
    pod = next((p for p in pods if p.pod_name == context.primary_pod_name), pods[0] if pods else None)
    if pod is None:
        return {}
    return {"pod": pod.pod_name, "namespace": pod.namespace, "container": pod.primary_container_name}


async def resolve_real_attempt(
    run: str, action: str | None, *, org: str, project: str, domain: str
) -> tuple[int, dict[str, str]]:
    """Return the newest attempt number of `action` (a0 if None) and its pod; `(0, {})` if none."""
    details = await remote.ActionDetails.get_details.aio(_build_action_id(run, action, org, project, domain))
    newest = _find_newest_attempt(details)
    return (0, {}) if newest is None else (newest.attempt, _find_pod_of(newest))


def _build_gap_result(
    run: str, action: str | None, attempt: int | None, pod: dict[str, str], cause: str
) -> dict[str, Any]:
    action = action or "a0"
    where = (
        f" -n {pod['namespace']} {pod['pod']} -c {pod['container']}"
        if pod
        else " -n <namespace> <pod> -c <container>"
    )
    shown = f" attempt {attempt}" if attempt else ""
    result: dict[str, Any] = {
        "run": run,
        "action": action,
        "attempt": attempt,
        "lines": [],
        "gap": (
            f"This Flyte cluster served no log stream for {run}/{action}{shown} "
            "(no log aggregation reachable from the server; logs exist only while the pod does)."
        ),
        "alternatives": [
            f"kubectl logs{where}",
            f"get_action(run_name='{run}', action_name='{action}') for error_info",
            "factory.status for the run report and open questions",
        ],
    }
    if cause:
        result["cause"] = cause
    return {**result, **{k: v for k, v in pod.items() if k != "container"}}


def _describe_target(run: str, action: str | None) -> str:
    """Name the run, or the action in its run, the way a "not found" error says it."""
    return f"action {action} in run {run}" if action else f"run {run}"


def wrap_get_logs(
    stock: Callable[..., Awaitable[dict[str, Any]]], profile: Profile | None
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Return `stock` itself on Union; on OSS a `get_logs` with the real attempt and a gap result."""
    if not is_oss(profile):
        return stock
    assert profile is not None
    # Here, not at the top: `_factory` imports this module, and the factory's task image has no `mcp` package
    # (measured on the devbox: every factory task failed to load with ModuleNotFoundError).
    from mcp.server.fastmcp.exceptions import ToolError

    signature = inspect.signature(stock)

    @functools.wraps(stock)
    async def get_logs(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        given = bound.arguments
        run, action, attempt = given["run_name"], given.get("action_name"), given.get("attempt")
        backend = profile.backend
        pod: dict[str, str] = {}
        if attempt is None:
            try:
                found, pod = await resolve_real_attempt(
                    run,
                    action,
                    org=backend.org,
                    project=given.get("project") or backend.project,
                    domain=given.get("domain") or backend.domain,
                )
            except ConnectError as exc:
                if exc.code == Code.NOT_FOUND:  # the run or action does not exist: no stream to be missing
                    raise ToolError(f"{_describe_target(run, action)} not found") from exc
                found = 0  # the stock call below may still work, and the gap names its own cause
            except REMOTE_ERRORS:
                found = 0
            if found:
                attempt = given["attempt"] = found
        cause = ""
        try:
            result = await stock(*bound.args, **bound.kwargs)
        except ConnectError as exc:
            if exc.code == Code.NOT_FOUND:
                raise ToolError(f"{_describe_target(run, action)} not found") from exc
            # The message may hold a signed URL, so only the class and the code are kept.
            result, cause = {}, f"{type(exc).__name__}:{exc.code.name}"
        except (*REMOTE_ERRORS, ToolError) as exc:
            result, cause = {}, type(exc).__name__
        # The stock's answer stands when it has lines or says itself why there are none: not running yet
        # (it names the phase and to retry or wait) or out of time (newer lines may exist). The gap is for a
        # stream that served nothing.
        if isinstance(result, dict) and (
            result.get("logs")
            or result.get("lines")
            or result.get("available") is False
            or result.get("stopped") == "time_limit"
        ):
            return result
        if isinstance(result, dict):
            # The stock tool's own words ("No log lines were returned."); `_serve` redacts every result.
            words = result.get("error") or result.get("message")
            cause = words if isinstance(words, str) and words else cause
        return _build_gap_result(run, action, attempt, pod, cause)

    return get_logs


def report_unavailable_reason(exc: BaseException) -> Reason:
    """Classify why a run report could not be read; never uses the message beyond keywords."""
    text = str(exc).lower()
    if isinstance(exc, ConnectError):
        if exc.code in (Code.PERMISSION_DENIED, Code.UNAUTHENTICATED):
            return "permission"
        if exc.code in (Code.UNAVAILABLE, Code.DEADLINE_EXCEEDED):
            return "unreachable"
        if exc.code == Code.NOT_FOUND:
            return "missing_report"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return "permission"
    if any(word in text for word in _PERMISSION_WORDS):
        return "permission"
    if isinstance(exc, (httpx.HTTPError, OSError, ObstoreError)):
        return "state_store"
    return "missing_report"


async def read_report_via_storage(report_uri: str) -> str | None:
    """Read the report object with the pod's own storage credentials; None if it cannot be read.

    The signed link the server hands out is unreachable from a devbox pod (it points at localhost), the
    object store is not.
    """
    if not report_uri:
        return None
    try:
        chunks = [chunk async for chunk in flyte.storage.get_stream(report_uri)]
    except REMOTE_ERRORS:  # the caller reports why the report is unavailable
        return None
    return b"".join(chunks).decode("utf-8", errors="replace") or None


def read_stored_report(run: remote.Run, profile: Profile) -> str | None:
    """Return the report page of `run`'s newest root attempt from the object store; None if unavailable."""
    backend = profile.backend
    try:
        details = remote.ActionDetails.get_details(
            _build_action_id(run.name, None, backend.org, backend.project, backend.domain)
        )
        newest = _find_newest_attempt(details)
        uri = newest.outputs.report_uri if newest is not None else ""
        # `status` runs inside an event loop, so the read gets a loop of its own.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, read_report_via_storage(uri)).result()
    except REMOTE_ERRORS:
        return None


def list_open_questions(run: remote.Run, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return the run's open questions in the one OSS shape `{condition, phase, text, timed_out}`.

    From the report's own list (`extra`) when the report was readable, else from the paused conditions.
    Fields not known are None.
    """
    if extra is not None:
        return [
            {
                "condition": q.get("condition"),
                "phase": q.get("phase"),
                "text": q.get("text"),
                "timed_out": q.get("timed_out"),
            }
            for q in extra
        ]
    try:
        conditions = list(remote.Condition.listall(run_name=run.name))
    except REMOTE_ERRORS:  # conditions are the fallback; without them the questions are simply empty
        conditions = []
    return [
        {"condition": c.name, "phase": str(c.phase), "text": c.prompt or "", "timed_out": None}
        for c in conditions
        if "PAUSED" in str(c.phase)
    ]


def report_unavailable(run: remote.Run, exc: BaseException) -> dict[str, Any]:
    """Return the status fields for an unreadable report: the reason class and the open questions."""
    reason = report_unavailable_reason(exc)
    return {
        "error": f"Run report unavailable: {reason.replace('_', ' ')}; get_run_io and list_conditions work",
        "reason": reason,
        "open_questions": list_open_questions(run),
    }


def recover_report(
    run: remote.Run, profile: Profile, exc: BaseException
) -> tuple[str | None, dict[str, Any]]:
    """Return the report page of a run whose report link failed with `exc`, or None and the status fields.

    On OSS the signed link can be unreachable from a pod, so the page is read from the object store, and when
    that fails too the fields say why. Elsewhere there is no fallback.
    """
    if not is_oss(profile):
        return None, {"error": "Run report unavailable; open the run URL for details"}
    stored = read_stored_report(run, profile)
    return (stored, {}) if stored is not None else (None, report_unavailable(run, exc))


def explain_missing_report(run: remote.Run, profile: Profile) -> dict[str, Any]:
    """Return the status fields for a run that has published no report page yet.

    On OSS they say why there is no page, and still give the questions, which come from conditions.
    """
    error = "Run report is not available yet"
    if not is_oss(profile):
        return {"error": error}
    return {
        "error": f"{error}: the run has published no report page (not started, or it publishes none)",
        "reason": "missing_report",
        "open_questions": list_open_questions(run),
    }


def pick_open_questions(run: remote.Run, data: Mapping[str, Any], profile: Profile) -> list[dict[str, Any]]:
    """Return a readable report's open questions: in the one OSS shape on OSS, else as the report has them."""
    if is_oss(profile):
        return list_open_questions(run, data.get("needs_you") or [])
    needs_you: list[dict[str, Any]] = data.get("needs_you", [])
    return needs_you


def _is_internal(host: str, profile: Profile) -> bool:
    host = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host.partition("%")[0])  # `fe80::1%eth0` carries a zone id
    except ValueError:
        pass
    else:
        ip = getattr(ip, "ipv4_mapped", None) or ip
        # Not global: private, loopback, link-local, unspecified and carrier-grade NAT ranges alike.
        return not ip.is_global
    internal = (urlsplit(profile.backend.internal_endpoint).hostname or "").lower()
    labels = host.split(".")
    return (
        # `localhost` is openable on the devbox host, but the chat client is not on that host.
        host in ("", "localhost", "localhost.localdomain")
        or len(labels) == 1  # also `2130706433`, an IPv4 address written as one number
        or host.endswith(_INTERNAL_SUFFIXES)
        or ".svc." in host
        or labels[-1].isdigit()  # no public TLD is numeric: `10.1`, `0x7f.1` style IPv4 spellings
        or (
            len(labels) == 2
            and labels[1] in _NAMESPACES | ({internal.partition(".")[2].split(".")[0]} - {""})
        )
        # A pod's SDK shortens the service to `<service>.<namespace>`, so the same service matches by name.
        or (bool(internal) and labels[0] == internal.split(".")[0])
    )


def find_openable_url(url: str, profile: Profile | None) -> tuple[str | None, str | None]:
    """Return `(url, note)`: the URL itself, its console equivalent, or None and a note saying why not.

    `localhost` counts as internal: the stock devbox run URL opens only on the host that runs the devbox.
    With `console_host` set it is rewritten like any other in-cluster URL.
    """
    if not is_oss(profile) or not url:
        return url, None
    assert profile is not None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:  # an unparseable authority is never worth returning
        parts, host = urlsplit(""), ""
    if not _is_internal(host, profile):
        return url, None
    run = parts.path.partition("/runs/")[2].split("/")[0]
    if profile.console_base and run:
        return f"{profile.console_base}/runs/{run}", None
    return None, NO_URL_NOTE


def pick_url_fields(url: str, profile: Profile | None) -> dict[str, Any]:
    """Return a status's `url`, and a `url_note` when there is none to open (`find_openable_url`).

    On OSS the run URL is the pod's in-cluster address; it is swapped for the console's or dropped.
    """
    openable, note = find_openable_url(url, profile)
    return {"url": openable, **({"url_note": note} if note else {})}


def _is_url_key(key: object) -> bool:
    return isinstance(key, str) and (key.lower() in _URL_KEYS or key.lower().endswith(_URL_KEY_SUFFIXES))


def _fix_text(text: str, profile: Profile) -> str:
    """Return `text` with each internal http(s) URL in it made its console URL, or `<no openable URL>`."""

    def replace(match: re.Match[str]) -> str:
        found = match.group(0)
        url = found.rstrip(_TRAILING_PUNCTUATION)
        # A closing bracket ends the URL unless it closes one inside it (`http://[::1]:80/x`).
        while url[-1:] in (")", "]", "}") and url.count(_OPENERS[url[-1]]) < url.count(url[-1]):
            url = url[:-1].rstrip(_TRAILING_PUNCTUATION)
        fixed, _ = find_openable_url(url, profile)
        return (fixed or NO_URL_TEXT) + found[len(url) :]

    return _URL_IN_TEXT.sub(replace, text)


def fix_urls(result: object, profile: Profile | None) -> object:
    """Return `result` with every internal http(s) URL made openable; `result` itself on Union.

    Link fields (`url`, `endpoint`, `link`, `uri`, `href` and `*_url`, `*_uri`, `*_link`, `*_href`,
    `*_endpoint`) holding a URL become the URL, its console equivalent, or None plus a `url_note`. Any other
    string has the URLs inside it replaced in place. `s3://` and `gs://` URIs are not links and stay as they
    are; a scheme-less `host:port` is not recognised as a URL.
    """
    if not is_oss(profile):
        return result
    assert profile is not None
    if isinstance(result, str):
        return _fix_text(result, profile)
    if isinstance(result, dict):
        fixed: dict[Any, Any] = {}
        for key, value in result.items():
            if (
                _is_url_key(key)
                and isinstance(value, str)
                and value.lower().startswith(("http://", "https://"))
            ):
                fixed[key], note = find_openable_url(value.strip(), profile)
                if note:
                    fixed["url_note"] = note
            else:
                fixed[key] = fix_urls(value, profile)
        return fixed
    if isinstance(result, (list, tuple)):
        return type(result)(fix_urls(item, profile) for item in result)
    return result
