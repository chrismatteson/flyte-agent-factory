"""`_app_logs`: newest-N, windowed, attributed app log lines from a fake source. Offline."""

from __future__ import annotations

import ast
import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyteidl2.app import app_logs_payload_pb2 as logs_pb  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.cluster import payload_pb2 as cluster_pb  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.logs.dataplane import payload_pb2 as line_pb  # type: ignore[import-untyped]  # no py.typed marker

from flyte_agent_factory import _app_logs
from flyte_agent_factory._app_logs import (
    AppLogLine,
    ConnectLogSource,
    find_revision_of,
    parse_when,
    read_app_logs,
)

T0 = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
POD_A = "factory-mcp-00003-deployment-6b44b97bd-gcsjq"
POD_B = "factory-mcp-00004-deployment-7c55c98ce-4tf6n"


def line(i: int, replica: str = POD_B, source: str = "live", message: str | None = None) -> AppLogLine:
    return AppLogLine(
        ts=T0 + timedelta(seconds=i),
        replica=replica,
        revision=find_revision_of(replica),
        source=source,  # type: ignore[arg-type]
        originator="user",
        message=message if message is not None else f"line {i}",
    )


class FakeSource:
    """A `LogSource`: the given lines per source, then (when `hang`) a stream that never ends."""

    def __init__(self, live: list[AppLogLine] = (), persisted: list[AppLogLine] = (), hang: bool = False):  # type: ignore[assignment]
        self.lines = {"live": list(live), "persisted": list(persisted)}
        self.hang = hang
        self.asked: list[tuple[str, str | None]] = []
        self.closed: list[str] = []

    async def stream(
        self, app: str, source: Literal["live", "persisted"], replica: str | None
    ) -> AsyncIterator[AppLogLine]:
        self.asked.append((source, replica))
        try:
            for item in self.lines[source]:
                yield item
            if self.hang and source == "live":
                await asyncio.sleep(3600)
        finally:
            self.closed.append(source)


async def test_the_newest_200_of_350_lines_come_back_in_order():
    src = FakeSource(persisted=[line(i, source="persisted") for i in range(350)])
    result = await read_app_logs(src, "factory-mcp", max_lines=200, source="persisted")
    assert [entry["message"] for entry in result["lines"]] == [f"line {i}" for i in range(150, 350)]
    assert result["returned"] == 200
    assert result["truncated"] is True
    assert result["stopped_because"] == "stream_end"


async def test_fewer_lines_than_asked_is_not_truncated():
    src = FakeSource(persisted=[line(i) for i in range(5)])
    result = await read_app_logs(src, "factory-mcp", max_lines=200, source="persisted")
    assert result["returned"] == 5
    assert result["truncated"] is False


async def test_max_lines_is_capped():
    src = FakeSource(persisted=[line(i) for i in range(_app_logs.MAX_LINES_CAP + 50)])
    result = await read_app_logs(src, "factory-mcp", max_lines=10**6, source="persisted")
    assert result["returned"] == _app_logs.MAX_LINES_CAP


async def test_lines_outside_the_window_are_excluded():
    src = FakeSource(persisted=[line(i) for i in range(100)])
    since, until = T0 + timedelta(seconds=20), T0 + timedelta(seconds=30)
    result = await read_app_logs(src, "factory-mcp", source="persisted", since=since, until=until)
    messages = [entry["message"] for entry in result["lines"]]
    assert messages[0] == "line 20"
    assert messages[-1] == "line 29"
    assert len(messages) == 10
    assert result["window"] == {"since": "2026-11-19T09:00:20Z", "until": "2026-11-19T09:00:30Z"}


async def test_every_line_carries_its_replica_and_revision_and_can_be_filtered():
    mixed = [line(1, POD_A), line(2, POD_B), line(3, POD_A), line(4, POD_B)]
    result = await read_app_logs(FakeSource(persisted=mixed), "factory-mcp", source="persisted")
    assert [(e["replica"], e["revision"]) for e in result["lines"]] == [
        (POD_A, "factory-mcp-00003"),
        (POD_B, "factory-mcp-00004"),
        (POD_A, "factory-mcp-00003"),
        (POD_B, "factory-mcp-00004"),
    ]
    assert result["replicas"] == sorted([POD_A, POD_B])
    assert result["revisions"] == ["factory-mcp-00003", "factory-mcp-00004"]

    by_replica = await read_app_logs(
        FakeSource(persisted=mixed), "factory-mcp", source="persisted", replica=POD_A
    )
    assert {e["replica"] for e in by_replica["lines"]} == {POD_A}
    by_revision = await read_app_logs(
        FakeSource(persisted=mixed), "factory-mcp", source="persisted", revision="factory-mcp-00004"
    )
    assert [e["message"] for e in by_revision["lines"]] == ["line 2", "line 4"]


async def test_lines_of_an_app_whose_name_only_starts_with_the_app_are_dropped():
    # Measured live (#49): the tail of `factory-mcp` also streamed `factory-mcp-test-<sha>`'s replicas.
    other = "factory-mcp-test-dde5681-00002-deployment-5d9f8c7b6-x2k4p"
    mixed = [line(1, POD_A), line(2, other), line(3, POD_B), line(4, other)]
    result = await read_app_logs(FakeSource(persisted=mixed), "factory-mcp", source="persisted")
    assert [e["message"] for e in result["lines"]] == ["line 1", "line 3"]
    assert result["replicas"] == sorted([POD_A, POD_B])
    assert result["revisions"] == ["factory-mcp-00003", "factory-mcp-00004"]
    assert result["other_apps_lines_dropped"] == 2

    own = await read_app_logs(FakeSource(persisted=mixed), "factory-mcp-test-dde5681", source="persisted")
    assert [e["message"] for e in own["lines"]] == ["line 2", "line 4"]


async def test_the_replica_filter_is_also_asked_of_the_server():
    src = FakeSource(persisted=[line(1)])
    await read_app_logs(src, "factory-mcp", source="persisted", replica=POD_B)
    assert src.asked == [("persisted", POD_B)]


async def test_system_lines_can_be_left_out():
    system = AppLogLine(T0, POD_B, find_revision_of(POD_B), "live", "system", "Readiness probe failed")
    src = FakeSource(live=[line(1), system])
    kept = await read_app_logs(src, "factory-mcp", source="live", idle_s=0.05)
    assert [e["originator"] for e in kept["lines"]] == ["system", "user"]
    dropped = await read_app_logs(
        FakeSource(live=[line(1), system]), "factory-mcp", source="live", include_system=False
    )
    assert [e["originator"] for e in dropped["lines"]] == ["user"]


async def test_a_live_stream_that_never_ends_is_cut_when_idle_and_closed():
    src = FakeSource(live=[line(1), line(2)], hang=True)
    started = asyncio.get_running_loop().time()
    result = await read_app_logs(src, "factory-mcp", source="live", idle_s=0.1)
    assert asyncio.get_running_loop().time() - started < 2
    assert result["returned"] == 2
    assert result["stopped_because"] == "idle"
    assert src.closed == ["live"]


class CancelledAsConnectError(FakeSource):
    """A live stream cancelled as connectrpc cancels one: as `ConnectError(CANCELED)`, not CancelledError.

    Seen live: `asyncio.wait_for` then raised that error in place of TimeoutError, so every idle cut of a
    live stream was reported as "app logs unavailable" even though all its lines had been read.
    """

    async def stream(
        self, app: str, source: Literal["live", "persisted"], replica: str | None
    ) -> AsyncIterator[AppLogLine]:
        try:
            async for item in super().stream(app, source, replica):
                yield item
        except asyncio.CancelledError as exc:
            raise ConnectError(Code.CANCELED, "Request was cancelled") from exc


async def test_an_idle_cut_is_not_a_gap_when_the_client_turns_cancellation_into_an_error():
    src = CancelledAsConnectError(live=[line(1), line(2)], hang=True)
    result = await read_app_logs(src, "factory-mcp", source="live", idle_s=0.1)
    assert result["returned"] == 2
    assert result["stopped_because"] == "idle"
    assert result["gaps"] == []


async def test_a_deadline_cut_is_not_a_gap_when_the_client_turns_cancellation_into_an_error():
    src = CancelledAsConnectError(live=[], hang=True)
    result = await read_app_logs(src, "factory-mcp", source="live", deadline_s=0.2, idle_s=5, first_line_s=5)
    assert result["stopped_because"] == "deadline"
    assert result["gaps"] == []


async def test_a_real_cancelled_error_from_the_server_is_still_a_gap():
    class ServerCancels(FakeSource):
        async def stream(
            self, app: str, source: Literal["live", "persisted"], replica: str | None
        ) -> AsyncIterator[AppLogLine]:
            yield line(1)
            raise ConnectError(Code.CANCELED, "cancelled by the server")

    result = await read_app_logs(ServerCancels(), "factory-mcp", source="live", idle_s=1)
    assert result["returned"] == 1
    assert any("cancelled by the server" in gap for gap in result["gaps"])


async def test_the_wall_time_is_bounded():
    src = FakeSource(live=[], hang=True)
    result = await read_app_logs(src, "factory-mcp", source="live", deadline_s=0.2, idle_s=5, first_line_s=5)
    assert result["stopped_because"] == "deadline"
    assert src.closed == ["live"]


async def test_both_merges_by_time_and_drops_a_line_seen_in_each_source():
    shared = line(5, message="same")
    live = [line(3, source="live"), shared, line(7, source="live")]
    persisted = [
        line(1, source="persisted"),
        line(5, source="persisted", message="same"),
        line(4, source="persisted"),
    ]
    result = await read_app_logs(FakeSource(live=live, persisted=persisted), "factory-mcp", idle_s=0.05)
    assert [e["message"] for e in result["lines"]] == ["line 1", "line 3", "line 4", "same", "line 7"]
    assert {e["source"] for e in result["lines"]} == {"live", "persisted"}


async def test_both_matches_a_live_line_to_its_persisted_copy_stamped_to_the_millisecond():
    # Seen live: the live stream stamps to the microsecond, the persisted store to the millisecond.
    precise = AppLogLine(
        T0 + timedelta(microseconds=818_958), POD_B, find_revision_of(POD_B), "live", "user", "x"
    )
    stored = AppLogLine(
        T0 + timedelta(milliseconds=818), POD_B, find_revision_of(POD_B), "persisted", "user", "x"
    )
    result = await read_app_logs(FakeSource(live=[precise], persisted=[stored]), "factory-mcp", idle_s=0.05)
    assert result["returned"] == 1
    assert result["lines"][0]["ts"] == "2026-11-19T09:00:00.818Z"


async def test_a_cut_persisted_read_names_the_newest_lines_as_missing(monkeypatch):
    class Slow(FakeSource):
        async def stream(self, app, source, replica):
            async for item in super().stream(app, source, replica):
                await asyncio.sleep(0.01)
                yield item

    src = Slow(persisted=[line(i, source="persisted") for i in range(100)])
    result = await read_app_logs(
        src, "factory-mcp", source="persisted", max_lines=50, deadline_s=0.2, idle_s=5, first_line_s=5
    )
    assert result["stopped_because"] == "deadline"
    last = result["lines"][-1]["ts"]
    assert 0 < result["returned"] < 100
    assert result["truncated"] is True
    (gap,) = result["gaps"]
    assert f"after {last}" in gap
    assert "newer" in gap
    assert "not the newest 50" in gap
    assert "oldest" not in gap


async def test_a_persisted_read_to_its_end_is_not_cut():
    result = await read_app_logs(
        FakeSource(persisted=[line(i, source="persisted") for i in range(5)]),
        "factory-mcp",
        source="persisted",
    )
    assert result["truncated"] is False
    assert result["gaps"] == []


async def test_identical_lines_from_one_source_are_all_kept():
    same = [line(1, message="tick") for _ in range(3)]
    result = await read_app_logs(FakeSource(live=same), "factory-mcp", source="live", idle_s=0.05)
    assert result["returned"] == 3


async def test_repeated_lines_still_merge_across_the_two_sources():
    both = (
        [line(1, source=s, message="tick") for s in ("live",) * 2],
        [line(1, source=s, message="tick") for s in ("persisted",) * 2],
    )
    result = await read_app_logs(FakeSource(live=both[0], persisted=both[1]), "factory-mcp", idle_s=0.05)
    assert result["returned"] == 2


async def test_a_failing_source_returns_a_gap_instead_of_raising():
    class Broken:
        def stream(self, app: str, source: str, replica: str | None) -> AsyncIterator[AppLogLine]:
            raise RuntimeError("boom")

    result = await read_app_logs(Broken(), "factory-mcp", source="persisted")
    assert result["lines"] == []
    assert any("app logs unavailable: RuntimeError: boom" in gap for gap in result["gaps"])
    assert "get_app_status" in result["gaps"][0]


@pytest.mark.parametrize(
    ("pod", "revision"),
    [
        (POD_B, "factory-mcp-00004"),
        ("claude-browser-personal-00001-deployment-86594d5d6f-xczjx", "claude-browser-personal-00001"),
        ("some-other-pod", ""),
        ("", ""),
    ],
)
def test_revision_of(pod, revision):
    assert find_revision_of(pod) == revision


def test_parse_when_takes_iso_and_relative_values():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    assert parse_when(None, now) is None
    assert parse_when("", now) is None
    assert parse_when("15m", now) == now - timedelta(minutes=15)
    assert parse_when("2h", now) == now - timedelta(hours=2)
    assert parse_when("1d", now) == now - timedelta(days=1)
    assert parse_when("2026-11-19T09:00:00Z", now) == datetime(2026, 11, 19, 9, tzinfo=UTC)
    assert parse_when("2026-11-19T09:00:00", now) == datetime(2026, 11, 19, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="not a time"):
        parse_when("yesterday-ish", now)


# --- ConnectLogSource: the two generated clients, faked ---


def tail_response(pod: str, *messages: str, source: Any = line_pb.LOG_LINE_SOURCE_LIVE, system: bool = False):
    stamp_lines = []
    for i, message in enumerate(messages):
        entry = line_pb.LogLine(
            message=message,
            originator=line_pb.SYSTEM if system else line_pb.USER,
            short_pod_name=pod,
        )
        entry.timestamp.FromDatetime(T0 + timedelta(seconds=i))
        stamp_lines.append(entry)
    lines = logs_pb.LogLines(structured_lines=stamp_lines, source=source)
    lines.replica_id.name = pod
    return logs_pb.TailLogsResponse(batches=logs_pb.LogLinesBatch(logs=[lines]))


class FakeClients:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, responses: list[Any]):
        self.cluster_requests: list[Any] = []
        self.tail_requests: list[Any] = []
        self.headers: list[Any] = []
        self.addresses: list[str] = []
        self.closed = False
        outer = self

        class Cluster:
            def __init__(self, address: str, **kwargs: Any):
                outer.addresses.append(address)

            async def select_cluster(self, request: Any, *, headers: Any = None, timeout_ms: Any = None):
                outer.cluster_requests.append(request)
                outer.headers.append(headers)
                return cluster_pb.SelectClusterResponse(cluster_endpoint="dp.example.com:443")

        class Tail:
            def __init__(self, address: str, **kwargs: Any):
                outer.addresses.append(address)

            async def tail_logs(self, request: Any, *, headers: Any = None, timeout_ms: Any = None):
                outer.tail_requests.append(request)
                outer.headers.append(headers)
                try:
                    for response in responses:
                        yield response
                finally:
                    outer.closed = True

        monkeypatch.setattr(_app_logs, "ClusterServiceClient", Cluster)
        monkeypatch.setattr(_app_logs, "AppLogsServiceClient", Tail)


async def test_the_connect_source_selects_the_cluster_then_tails_with_the_callers_headers(monkeypatch):
    fake = FakeClients(monkeypatch, [tail_response(POD_B, "hello", "world")])
    source = ConnectLogSource(
        endpoint="https://union.example.com",
        org="acme",
        project="p",
        domain="d",
        headers={"authorization": "Bearer caller-token"},
        insecure=False,
    )
    got = [item async for item in source.stream("factory-mcp", "persisted", POD_B)]

    select = fake.cluster_requests[0]
    assert select.operation == cluster_pb.SelectClusterRequest.OPERATION_TAIL_LOGS
    assert (select.app_id.org, select.app_id.project, select.app_id.domain, select.app_id.name) == (
        "acme",
        "p",
        "d",
        "factory-mcp",
    )
    tail = fake.tail_requests[0]
    assert tail.source == line_pb.PERSISTED_ONLY
    # A replica is asked for by app and filtered here: the live service refuses a `replica_id` request
    # without a `start_time`, a field this flyteidl2 does not have ("validation error: start_time: value is
    # required").
    assert tail.HasField("app_id") and not tail.HasField("replica_id")
    assert tail.app_id.name == "factory-mcp"
    assert fake.headers == [{"authorization": "Bearer caller-token"}] * 2
    assert fake.addresses == ["https://union.example.com", "https://dp.example.com:443"]
    assert [(g.message, g.replica, g.revision, g.source, g.originator) for g in got] == [
        ("hello", POD_B, "factory-mcp-00004", "live", "user"),
        ("world", POD_B, "factory-mcp-00004", "live", "user"),
    ]
    assert got[1].ts == T0 + timedelta(seconds=1)


async def test_the_connect_source_yields_only_the_asked_replicas_lines(monkeypatch):
    fake = FakeClients(monkeypatch, [tail_response(POD_A, "old pod"), tail_response(POD_B, "new pod")])
    source = ConnectLogSource(
        endpoint="https://u", org="o", project="p", domain="d", headers={}, insecure=False
    )
    got = [item async for item in source.stream("factory-mcp", "persisted", POD_A)]
    assert [(g.replica, g.message) for g in got] == [(POD_A, "old pod")]
    assert not fake.tail_requests[0].HasField("replica_id")


async def test_the_connect_source_asks_for_the_app_when_no_replica_and_the_live_source(monkeypatch):
    fake = FakeClients(monkeypatch, [tail_response(POD_B, "x", system=True)])
    source = ConnectLogSource(
        endpoint="http://localhost:8090", org="", project="p", domain="d", headers={}, insecure=True
    )
    got = [item async for item in source.stream("factory-mcp", "live", None)]
    tail = fake.tail_requests[0]
    assert tail.HasField("app_id")
    assert not tail.HasField("replica_id")
    assert tail.source == line_pb.LIVE_ONLY
    assert got[0].originator == "system"
    assert fake.addresses[1].startswith("http://")


async def test_the_connect_source_drops_the_line_end_so_live_and_persisted_copies_match(monkeypatch):
    # Seen live: the live stream ends every message with "\n" and the persisted one does not, so `both`
    # returned each line twice.
    live = tail_response(POD_B, "INFO hello\n", "INFO crlf\r\n")
    persisted = tail_response(POD_B, "INFO hello", "INFO crlf", source=line_pb.LOG_LINE_SOURCE_PERSISTED)
    FakeClients(monkeypatch, [live, persisted])
    source = ConnectLogSource(
        endpoint="https://u", org="o", project="p", domain="d", headers={}, insecure=False
    )
    got = [item async for item in source.stream("factory-mcp", "live", None)]
    assert [g.message for g in got] == ["INFO hello", "INFO crlf"] * 2

    class Both:
        """Each source streams its own copy, as the live service does."""

        async def stream(self, app: str, which: str, replica: str | None) -> AsyncIterator[AppLogLine]:
            for item in got[:2] if which == "live" else got[2:]:
                yield item

    result = await read_app_logs(Both(), "factory-mcp", idle_s=0.05)
    assert [e["message"] for e in result["lines"]] == ["INFO hello", "INFO crlf"]


async def test_the_connect_source_keeps_inner_and_leading_whitespace(monkeypatch):
    FakeClients(monkeypatch, [tail_response(POD_B, "  │ 72 │ backoff  \n", "a\nb\n")])
    source = ConnectLogSource(
        endpoint="https://u", org="o", project="p", domain="d", headers={}, insecure=False
    )
    got = [item.message async for item in source.stream("factory-mcp", "live", None)]
    assert got == ["  │ 72 │ backoff  ", "a\nb"]


async def test_the_connect_source_closes_its_stream_when_the_reader_stops(monkeypatch):
    fake = FakeClients(monkeypatch, [tail_response(POD_B, "a", "b", "c")])
    source = ConnectLogSource(
        endpoint="https://u", org="o", project="p", domain="d", headers={}, insecure=False
    )
    result = await read_app_logs(source, "factory-mcp", source="live", max_lines=2, idle_s=0.05)
    assert result["returned"] == 2
    assert fake.closed


def test_the_modules_import_nothing_private_from_flyte():
    tree = ast.parse(Path(importlib.import_module("flyte_agent_factory._app_logs").__file__).read_text())  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            for name in names:
                parts = name.split(".")
                assert not (parts[0] == "flyte" and any(p.startswith("_") for p in parts[1:])), name
