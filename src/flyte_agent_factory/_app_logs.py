"""Read an app's log lines into a bounded, attributed, newest-last list.

The SDK has no public call that returns app log lines (`App.show_logs` prints), so this talks to the two
generated `flyteidl2` connect clients the SDK itself uses: `ClusterService.SelectCluster` finds the data plane
that holds the app's logs, and `AppLogsService.TailLogs` streams them from there. Neither client is
private. The caller's own request headers authenticate both, so the factory reads logs as the caller, as every
other tool does.

`TailLogsRequest` has no line count, time window or revision, so all three are applied here, on the stream.
A live stream never ends and a persisted one can run for minutes on a large history, so every read is bounded
in wall time and an idle stream is cut and closed.

Redaction is not done here: the registration layer redacts every tool result (`_mcp_redact`).

SDK proposal: gap 6 in docs/mcp-sdk-requirements.md (`App.get_logs`), which removes this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from flyteidl2.app import app_definition_pb2, app_logs_payload_pb2  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.app.app_logs_service_connect import AppLogsServiceClient  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.cluster import payload_pb2 as cluster_pb2  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.cluster.service_connect import ClusterServiceClient  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.logs.dataplane import payload_pb2 as line_pb2  # type: ignore[import-untyped]  # no py.typed marker

from ._errors import REMOTE_ERRORS
from ._timefmt import format_utc as _iso
from ._timefmt import format_utc_ms

Source = Literal["live", "persisted"]

#: What one call may return, whatever `max_lines` asks for: a result this size is already near what a chat
#: client can show.
MAX_LINES_CAP = 1000
DEFAULT_MAX_LINES = 200
#: The wall time of one read. The MCP call budget is 200 s and a read is one step of a tool, so it stays well
#: inside it.
DEFAULT_DEADLINE_S = 60.0
#: After the first line, a stream silent this long has delivered its backlog, which arrives in one burst.
DEFAULT_IDLE_S = 2.0
#: Before the first line, how long to wait: the persisted store took 5-7 s to start answering when measured.
DEFAULT_FIRST_LINE_S = 15.0

_REVISION = re.compile(r"^(?P<revision>.+-\d+)-deployment-")
_RELATIVE = re.compile(r"^(?P<n>\d+)(?P<unit>[mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


@dataclass(frozen=True)
class AppLogLine:
    ts: datetime
    replica: str
    revision: str
    source: Source
    originator: Literal["user", "system", "unknown"]
    message: str


class LogSource(Protocol):
    """Where lines come from: `ConnectLogSource` in production, a fake in tests."""

    def stream(self, app: str, source: Source, replica: str | None) -> AsyncIterator[AppLogLine]: ...


def find_revision_of(pod_name: str) -> str:
    """`<app>-<rev>-deployment-<hash>-<id>` is a Knative pod name; its revision is `<app>-<rev>`, else ''.

    `ReplicaMeta.revision` exists in the API but the server never sends it, so the pod name is the source.
    """
    match = _REVISION.match(pod_name)
    return match["revision"] if match else ""


def find_app_of_revision(revision: str) -> str:
    """`<app>-<rev>` without its `-<rev>`: the app a revision belongs to, else ''."""
    head, _, number = revision.rpartition("-")
    return head if number.isdigit() else ""


def parse_when(value: str | None, now: datetime) -> datetime | None:
    """Parse an ISO-8601 time (UTC when it has no offset) or an age such as `15m`, `2h`, `1d` before `now`."""
    if not value:
        return None
    relative = _RELATIVE.match(value.strip())
    if relative:
        return now - timedelta(**{_UNITS[relative["unit"]]: int(relative["n"])})
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{value!r} is not a time: use ISO-8601 (2026-11-19T09:00:00Z) or an age (15m, 2h, 1d)"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _classify_origin(originator: Any) -> Literal["user", "system", "unknown"]:
    if originator == line_pb2.USER:
        return "user"
    if originator == line_pb2.SYSTEM:
        return "system"
    return "unknown"


def _build_codec_argument() -> dict[str, Any]:
    """Return the binary-protobuf codec when connectrpc has `compat`; older releases default to it."""
    try:
        codec = importlib.import_module("connectrpc.compat").google_protobuf_binary_codec
    except (ImportError, AttributeError):
        return {}
    return {"codec": codec()}


class ConnectLogSource:
    """Stream an app's log lines through the generated `ClusterService` and `AppLogsService` clients."""

    def __init__(
        self,
        *,
        endpoint: str,
        org: str,
        project: str,
        domain: str,
        headers: Mapping[str, str],
        insecure: bool,
    ) -> None:
        self.endpoint = endpoint
        self.org, self.project, self.domain = org, project, domain
        self.headers = dict(headers)
        self.insecure = insecure

    def _build_address(self, endpoint: str) -> str:
        if "://" in endpoint:
            return endpoint
        return f"{'http' if self.insecure else 'https'}://{endpoint.removeprefix('dns:///')}"

    async def stream(self, app: str, source: Source, replica: str | None) -> AsyncIterator[AppLogLine]:
        app_id = app_definition_pb2.Identifier(
            org=self.org, project=self.project, domain=self.domain, name=app
        )
        selected = await ClusterServiceClient(
            self._build_address(self.endpoint), **_build_codec_argument()
        ).select_cluster(
            cluster_pb2.SelectClusterRequest(
                operation=cluster_pb2.SelectClusterRequest.OPERATION_TAIL_LOGS, app_id=app_id
            ),
            headers=self.headers,
            timeout_ms=15_000,
        )
        wanted = line_pb2.LIVE_ONLY if source == "live" else line_pb2.PERSISTED_ONLY
        # Always by app, with a replica filtered below: the service refuses a `replica_id` request without a
        # `start_time` ("validation error: start_time: value is required"), a field this flyteidl2's
        # `TailLogsRequest` does not have. The app's streams carry its dead replicas' lines too.
        request = app_logs_payload_pb2.TailLogsRequest(app_id=app_id, source=wanted)
        responses = AppLogsServiceClient(
            self._build_address(selected.cluster_endpoint or self.endpoint), **_build_codec_argument()
        ).tail_logs(request, headers=self.headers)
        try:
            async for response in responses:
                batches = [response.log_lines] if response.HasField("log_lines") else []
                if response.HasField("batches"):
                    batches = list(response.batches.logs)
                for batch in batches:
                    for item in self._parse_lines(batch, source):
                        if not replica or item.replica == replica:
                            yield item
        finally:
            # An idle stream is abandoned mid-flight; closing it here is what stops connectrpc logging
            # "Task exception was never retrieved" for it.
            close = getattr(responses, "aclose", None)
            if close:
                await close()

    @staticmethod
    def _parse_lines(batch: Any, requested: Source) -> list[AppLogLine]:
        origin: Source
        if batch.source == line_pb2.LOG_LINE_SOURCE_LIVE:
            origin = "live"
        elif batch.source == line_pb2.LOG_LINE_SOURCE_PERSISTED:
            origin = "persisted"
        else:
            origin = requested
        replica_of_batch = batch.replica_id.name if batch.HasField("replica_id") else ""
        found = []
        for entry in batch.structured_lines:
            replica = replica_of_batch or entry.short_pod_name
            found.append(
                AppLogLine(
                    ts=entry.timestamp.ToDatetime(tzinfo=UTC),
                    replica=replica,
                    revision=find_revision_of(replica),
                    source=origin,
                    originator=_classify_origin(entry.originator),
                    # The live stream ends each message with a line end and the persisted one does not;
                    # without this, `both` keeps the two copies of every line as different lines.
                    message=entry.message.rstrip("\r\n"),
                )
            )
        if not batch.structured_lines:
            # Plain lines carry no timestamp; the time they arrived is the best that can be said, and a
            # `since`/`until` window then applies to that.
            received = datetime.now(UTC)
            for text in batch.lines:
                found.append(
                    AppLogLine(
                        received,
                        replica_of_batch,
                        find_revision_of(replica_of_batch),
                        origin,
                        "unknown",
                        text.rstrip("\r\n"),
                    )
                )
        return found


@dataclass
class _Reading:
    """What one source's reader leaves behind."""

    stopped_because: Literal["stream_end", "idle", "deadline"] = "stream_end"
    gap: str = ""
    #: The newest timestamp fed to `keep`; on an oldest-first stream, how far the read got.
    last_ts: datetime | None = None


async def _read_one(
    src: LogSource,
    app: str,
    source: Source,
    replica: str | None,
    keep: Callable[[AppLogLine], None],
    *,
    deadline: float,
    idle_s: float,
    first_line_s: float,
) -> _Reading:
    """Feed `keep` (called with each line) from one stream until it ends, goes idle or hits `deadline`."""
    reading = _Reading()
    stream: Any = None
    got_any = False
    try:
        stream = src.stream(app, source, replica)
        iterator = stream.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reading.stopped_because = "deadline"
                break
            patience = idle_s if got_any else first_line_s
            # Not `asyncio.wait_for`: connectrpc turns the cancellation of a pending read into
            # `ConnectError(CANCELED)`, which `wait_for` raises in place of TimeoutError, so every idle cut
            # looked like a transport failure. Waiting on the read and cancelling it here keeps "we stopped
            # reading" apart from "the read failed".
            step = asyncio.ensure_future(iterator.__anext__())
            try:
                await asyncio.wait({step}, timeout=min(patience, remaining))
            finally:
                cut = not step.done()
                if cut:
                    step.cancel()
                    with contextlib.suppress(asyncio.CancelledError, *REMOTE_ERRORS):
                        await step
            if cut:
                reading.stopped_because = "deadline" if patience >= remaining else "idle"
                break
            try:
                item = step.result()
            except StopAsyncIteration:
                break
            got_any = True
            reading.last_ts = item.ts if reading.last_ts is None else max(reading.last_ts, item.ts)
            keep(item)
    except REMOTE_ERRORS as exc:  # a transport failure is a result, not a crash of the tool
        reading.gap = f"app logs unavailable: {type(exc).__name__}: {exc}; try get_app_status for conditions"
    finally:
        close = getattr(stream, "aclose", None)
        if close:
            await close()
    return reading


def _format_entry(item: AppLogLine) -> dict[str, str]:
    return {
        "ts": format_utc_ms(item.ts),
        "replica": item.replica,
        "revision": item.revision,
        "source": item.source,
        "originator": item.originator,
        "message": item.message,
    }


async def read_app_logs(
    src: LogSource,
    app: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    since: datetime | None = None,
    until: datetime | None = None,
    replica: str | None = None,
    revision: str | None = None,
    source: Literal["live", "persisted", "both"] = "both",
    include_system: bool = True,
    deadline_s: float = DEFAULT_DEADLINE_S,
    idle_s: float = DEFAULT_IDLE_S,
    first_line_s: float = DEFAULT_FIRST_LINE_S,
) -> dict[str, Any]:
    """Return the newest `max_lines` lines of `app` in `[since, until)`, newest last, each attributed.

    `both` reads the live and the persisted stream at once, merges them by time and keeps one of a line that
    both hold. `LIVE_OR_PERSISTED` is not used: its ordering was never measured.

    `truncated` is True when `lines` may not be the newest `max_lines` of the window: more lines matched than
    were returned (the older ones were dropped), or the persisted read was cut by the deadline or an idle
    stream, which delivers oldest first and so loses the newest lines (named in `gaps`).
    """
    max_lines = max(1, min(max_lines, MAX_LINES_CAP))
    sources: list[Source] = ["live", "persisted"] if source == "both" else [source]
    # Newest N over streams that arrive oldest-first. Two streams are merged and deduplicated, so a plain
    # deque will not do; instead the dict is trimmed to the newest N whenever it reaches twice that.
    newest: dict[tuple[str, datetime, str, int], AppLogLine] = {}
    # How often each line has come from one source: the live and the persisted copy of a line share an
    # occurrence number and merge, while a line the app genuinely logged twice keeps both.
    seen: dict[tuple[str, datetime, str, Source], int] = {}
    seen_replicas: set[str] = set()
    seen_revisions: set[str] = set()
    matched = 0
    other_apps = 0

    def keep(item: AppLogLine) -> None:
        nonlocal matched, other_apps
        # Measured live: a tail of `factory-mcp` also streams `factory-mcp-test-<sha>`'s pods, so the stream
        # matches app names by prefix. A line whose pod belongs to another app is dropped and counted.
        if item.revision and find_app_of_revision(item.revision) != app:
            other_apps += 1
            return
        if (since and item.ts < since) or (until and item.ts >= until):
            return
        if replica and item.replica != replica:
            return
        if revision and item.revision != revision:
            return
        if not include_system and item.originator == "system":
            return
        # To the millisecond: the persisted store keeps no finer time than that, the live stream does.
        ms_ts = item.ts.replace(microsecond=item.ts.microsecond // 1000 * 1000)
        occurrence = (item.replica, ms_ts, item.message, item.source)
        seen[occurrence] = seen.get(occurrence, 0) + 1
        key = (item.replica, ms_ts, item.message, seen[occurrence])
        if key in newest:
            return
        matched += 1
        seen_replicas.add(item.replica)
        if item.revision:
            seen_revisions.add(item.revision)
        newest[key] = item
        if len(newest) >= 2 * max_lines:
            for old in sorted(newest, key=lambda k: newest[k].ts)[:-max_lines]:
                del newest[old]

    deadline = time.monotonic() + deadline_s
    readings = await asyncio.gather(
        *(
            _read_one(
                src, app, one, replica, keep, deadline=deadline, idle_s=idle_s, first_line_s=first_line_s
            )
            for one in sources
        )
    )

    kept = sorted(newest.values(), key=lambda item: item.ts)[-max_lines:]
    gaps = [reading.gap for reading in readings if reading.gap]
    persisted_cut = False
    for one, reading in zip(sources, readings, strict=True):
        if one == "persisted" and reading.stopped_because != "stream_end" and not reading.gap:
            persisted_cut = True
            # The persisted stream runs oldest first, so a cut loses the newest lines, not the oldest.
            last = format_utc_ms(reading.last_ts) if reading.last_ts else None
            after = f"after {last}" if last else "before any line was read"
            gaps.append(
                f"the persisted history was cut ({reading.stopped_because}) {after}; lines newer than that "
                f"may be missing, so these are not the newest {max_lines}"
            )
    stopped = "stream_end"
    for reading in readings:
        if reading.stopped_because == "deadline":
            stopped = "deadline"
        elif reading.stopped_because == "idle" and stopped != "deadline":
            stopped = "idle"
    return {
        "app": app,
        "source": source,
        "window": {"since": _iso(since), "until": _iso(until)},
        "lines": [_format_entry(item) for item in kept],
        "returned": len(kept),
        "truncated": persisted_cut or matched > len(kept),
        "replicas": sorted(seen_replicas - {""}),
        "revisions": sorted(seen_revisions),
        "other_apps_lines_dropped": other_apps,
        "stopped_because": stopped,
        "gaps": gaps,
    }
