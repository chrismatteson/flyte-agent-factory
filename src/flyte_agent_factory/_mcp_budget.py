"""A per-call time budget for the factory's MCP tools, and the operations that outlive a call.

The claude.ai connector cuts a call at about 230 s (measured on 2026-11-19, see REPORT.md "Do the 502s and
timeouts come from the MCP app, the ingress, or redeploys during calls?"), and a cut call surfaces as a 502
with no hint of what the server did. `run_within_budget` runs the tool as its own task and waits for it only
for `TOOL_BUDGET_S`; past that it returns a `still_working` result naming an operation the client can
ask about with `get_operation`. Because the work is its own task, a dropped request does not cancel it, and
`CancelOnDisconnect` makes the transport's disconnect cancel the *call*, so the operation stays open; an
acting tool that is called again with the same arguments joins the operation that has not been delivered yet
instead of acting twice.

One replica and one worker serve the MCP app, so the operations live in process memory. After a restart an id
is `unknown_operation` and the caller is told to look at the effect directly.

SDK proposals: gaps 8 (call budget, still_working) and 11 (call log) in docs/mcp-sdk-requirements.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from mcp.server.fastmcp.exceptions import ToolError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ._timefmt import format_utc as _iso

logger = logging.getLogger(__name__)

# The claude.ai connector's observed cut, measured 2026-11-19 (REPORT.md, "Do the 502s ...")
CLIENT_LIMIT_S = 230.0
TOOL_BUDGET_S = 200.0  # 30 s margin: ingress + queue-proxy + TLS + serialisation + client clock skew
WAIT_CAP_S = 170.0  # wait_for_run's timeout_s is clamped to this (budget minus 30 s for outputs/IO)
OPERATION_TTL_S = 1800.0
OPERATIONS_MAX = 200
CALL_LOG_MAX = 500

SERVER_STARTED_AT = datetime.now(UTC)


@dataclass
class Operation:
    id: str
    tool: str
    key: str
    started_at: datetime
    acting: bool
    task: asyncio.Task[object]
    steps: list[str] = field(default_factory=list)


#: The operation whose task is running, so `note_step` needs no handle passed through the tool.
_CURRENT: contextvars.ContextVar[Operation] = contextvars.ContextVar("mcp_current_operation")


def note_step(step: str) -> None:
    """Record what an acting tool just did. Outside a budgeted call it does nothing."""
    op = _CURRENT.get(None)
    if op is not None:
        op.steps.append(step)


def _observe_exception(task: asyncio.Task[object]) -> None:
    # A failed operation nobody asks about again would log "exception was never retrieved" when collected.
    if not task.cancelled():
        task.exception()


class Operations:
    """The calls in flight or finished but not yet forgotten. One per process."""

    def __init__(self) -> None:
        self._by_id: dict[str, Operation] = {}

    def start(self, tool: str, key: str, acting: bool, coro: Awaitable[object]) -> Operation:
        """Run the call as a background task and record it as a new operation."""

        async def run() -> object:
            _CURRENT.set(op)  # the task has its own context copy, so this does not leak into the caller
            return await coro

        task = asyncio.ensure_future(run())
        task.add_done_callback(_observe_exception)
        op = Operation(f"op-{secrets.token_hex(6)}", tool, key, datetime.now(UTC), acting, task)
        self._by_id[op.id] = op
        self._prune()
        return op

    def __len__(self) -> int:
        return len(self._by_id)

    def count_in_flight(self) -> int:
        """How many operations are still running, for the health tool."""
        return sum(1 for op in self._by_id.values() if not op.task.done())

    def find_open(self, tool: str, key: str) -> Operation | None:
        """Return the undelivered operation for the same tool and arguments, if one is open."""
        self._prune()
        for op in self._by_id.values():
            if op.tool == tool and op.key == key:
                return op
        return None

    def get(self, op_id: str) -> Operation | None:
        """Return the operation with this id, if it has not expired."""
        self._prune()
        return self._by_id.get(op_id)

    def forget(self, op: Operation) -> None:
        """Drop `op`: its result was delivered, so nobody can ask about it again."""
        self._by_id.pop(op.id, None)

    def _prune(self) -> None:
        """Drop finished operations past the TTL, then the oldest finished reads past the cap.

        The store holds in-flight operations and the ones whose id was returned as `still_working`.
        A running operation is never dropped: its task would go on, but nobody could ask about it. Nor is an
        open acting one before its TTL: repeating the call must join it, not act again. Only
        non-acting finished operations count against `OPERATIONS_MAX`.
        """
        now = datetime.now(UTC)
        for op_id, op in list(self._by_id.items()):
            if op.task.done() and (now - op.started_at).total_seconds() > OPERATION_TTL_S:
                del self._by_id[op_id]
        finished_reads = [op_id for op_id, op in self._by_id.items() if op.task.done() and not op.acting]
        # dicts keep insertion order: oldest first
        for op_id in finished_reads[: max(len(finished_reads) - OPERATIONS_MAX, 0)]:
            del self._by_id[op_id]


OPERATIONS = Operations()

#: (tool, outcome, duration_ms, ended at `time.monotonic()`) of the last calls, for the health tool.
#: Never holds arguments.
CALL_LOG: deque[tuple[str, str, int, float]] = deque(maxlen=CALL_LOG_MAX)


def _record(tool: str, started: float, outcome: str, op_id: str) -> None:
    ended = time.monotonic()
    duration_ms = int((ended - started) * 1000)
    CALL_LOG.append((tool, outcome, duration_ms, ended))
    # Arguments are not logged: `create_secret` carries values.
    logger.info("mcp_call tool=%s duration_ms=%d outcome=%s op=%s", tool, duration_ms, outcome, op_id)


def _build_still_working(op: Operation) -> dict[str, Any]:
    return {
        "status": "still_working",
        "operation_id": op.id,
        "tool": op.tool,
        "started_at": _iso(op.started_at),
        "elapsed_s": round((datetime.now(UTC) - op.started_at).total_seconds(), 1),
        "acting": op.acting,
        "steps_done": list(op.steps),
        "call_again": {"tool": "get_operation", "arguments": {"operation_id": op.id}},
        "note": "The work continues on the server. Do not repeat the call; repeating it joins it.",
    }


def run_within_budget(
    name: str,
    fn: Callable[..., Awaitable[object]],
    *,
    acting: bool,
    budget_s: float | None = None,
    ops: Operations | None = None,
    check: Callable[..., object] | None = None,
) -> Callable[..., Awaitable[object]]:
    """`fn` under the call budget. The wrapper keeps `fn`'s signature, so the client's schema is unchanged.

    `check` gets the call's arguments first; a `ToolError` from it is a refusal: logged as `refused`, and no
    operation is made, so a foreign project never starts work that `get_operation` could then report.

    `budget_s` and `ops` default to the module's `TOOL_BUDGET_S` and `OPERATIONS` *at call time*, so a test
    can patch them after the tool is wrapped.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> object:
        started = time.monotonic()
        if check is not None:
            try:
                check(*args, **kwargs)
            except ToolError:
                _record(name, started, "refused", "-")
                raise
        operations = ops if ops is not None else OPERATIONS
        arguments = {k: v for k, v in kwargs.items() if k != "ctx"}
        key = json.dumps([args, arguments], sort_keys=True, default=str)
        op = operations.find_open(name, key) if acting else None
        joined = op is not None
        if op is None:
            op = operations.start(name, key, acting, fn(*args, **kwargs))
        timeout = (TOOL_BUDGET_S if budget_s is None else budget_s) - (time.monotonic() - started)
        try:
            await asyncio.wait({op.task}, timeout=max(timeout, 0.0))
        except asyncio.CancelledError:
            # The request was dropped (`CancelOnDisconnect`): the work goes on as its own task, the operation
            # stays open for an identical call to join, and the drop is counted.
            _record(name, started, "dropped", op.id)
            raise
        if not op.task.done():
            _record(name, started, "still_working", op.id)
            return _build_still_working(op)
        # Delivered inline, so the id was never handed out and nothing can join or ask about it.
        operations.forget(op)
        try:
            result = op.task.result()
        except BaseException as exc:
            _record(name, started, f"error:{type(exc).__name__}", op.id)
            raise
        _record(name, started, "joined" if joined else "ok", op.id)
        return result

    return wrapper


async def get_operation(
    operation_id: str, wait_s: float = 120.0, *, budget_share: float = 1.0
) -> dict[str, Any]:
    """Ask about a call that returned `still_working`: its result once done, else its progress.

    Waits up to `wait_s` seconds for the operation to finish, and never past the call budget.
    `budget_share` is the part of `TOOL_BUDGET_S` the wait may use, so its answer cannot race the budget's.
    """
    op = OPERATIONS.get(operation_id)
    if op is None:
        return {
            "status": "unknown_operation",
            "operation_id": operation_id,
            "server_started_at": _iso(SERVER_STARTED_AT),
            "hint": "The server restarted or the operation expired; check the effect directly "
            "(get_run / get_app_status) before acting again.",
        }
    await asyncio.wait({op.task}, timeout=max(min(wait_s, budget_share * TOOL_BUDGET_S), 0.0))
    if not op.task.done():
        return _build_still_working(op)
    OPERATIONS.forget(op)
    # A failed operation raises here as the original call would have.
    return {"status": "done", "operation_id": op.id, "tool": op.tool, "result": op.task.result()}


# Generic so that a list of ints gives an int, as the call-stats' millisecond counts need.
def pick_percentile[T: float](sorted_values: Sequence[T], fraction: float) -> T:
    """Return the value at `fraction` (0.95 for p95) of `sorted_values`: ascending, and not empty."""
    return sorted_values[min(int(len(sorted_values) * fraction), len(sorted_values) - 1)]


class LoopLag:
    """How late the event loop wakes from a 1 s sleep, kept for 5 minutes: a blocked loop shows as lag."""

    INTERVAL_S = 1.0
    WINDOW_S = 300.0

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()  # (monotonic time, lag seconds)

    def record(self, lag_s: float) -> None:
        """Keep one lag sample and drop those older than the window."""
        now = time.monotonic()
        self._samples.append((now, lag_s))
        while self._samples and now - self._samples[0][0] > self.WINDOW_S:
            self._samples.popleft()

    async def run(self) -> None:
        """Measure how late each sleep wakes, for as long as the server runs."""
        while True:
            before = time.monotonic()
            await asyncio.sleep(self.INTERVAL_S)
            self.record(max(time.monotonic() - before - self.INTERVAL_S, 0.0))

    def summarise_lag(self) -> dict[str, Any]:
        """Return the sample count, maximum and 95th percentile of the lag in the window, in ms."""
        lags = sorted(lag for _, lag in self._samples)
        if not lags:
            return {"samples": 0, "max_ms": 0.0, "p95_ms": 0.0}
        p95 = pick_percentile(lags, 0.95)
        return {"samples": len(lags), "max_ms": round(lags[-1] * 1000, 1), "p95_ms": round(p95 * 1000, 1)}


class CancelOnDisconnect:
    """ASGI middleware: a request the client drops cancels the call it was making, and nothing else.

    The stock streamable-HTTP transport (stateless, JSON responses) reads the request body and then never
    touches the ASGI `receive` channel again, so a client that goes away is not noticed: the handler runs to
    the end, writes its result to a closed socket, and `run_within_budget` forgets the operation as
    delivered. With the disconnect watched, the handler is cancelled instead; the work is its own task and
    goes on, the operation stays open, and an identical call joins it. This makes "a dropped call is a
    cancelled handler", which the budget's tests model, true on the server's real transport. It goes
    outermost, so the cancellation reaches every layer below it, and the ASGI server, which knows the client
    is gone, logs nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, which gets every request; one of them is cancelled only when its client has gone."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, cancelling it when `receive` reports `http.disconnect` before the response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body_read = anyio.Event()
        response_sent = False

        async def receive_and_note() -> Message:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                body_read.set()
            return message

        async def send_and_note(message: Message) -> None:
            nonlocal response_sent
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_sent = True
            await send(message)

        async with anyio.create_task_group() as tasks:

            async def cancel_when_dropped() -> None:
                # Only after the body: the app reads that itself, and `receive` must not be shared.
                await body_read.wait()
                while (await receive())["type"] != "http.disconnect":
                    pass
                if not response_sent:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(cancel_when_dropped)
            await self.app(scope, receive_and_note, send_and_note)
            tasks.cancel_scope.cancel()  # served: the watcher is done
