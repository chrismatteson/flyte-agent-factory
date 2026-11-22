"""Soak a deployed factory MCP server with the check-in pattern chat uses, over real MCP, and write JSON.

    PYTHONPATH=src uv run python ops/mcp_soak.py --endpoint <URL> --app <app> --status-name <p> --dry-run
    PYTHONPATH=src uv run python ops/mcp_soak.py --endpoint <URL> --app <app> --status-name <p> --rounds 20

`--endpoint` is the connector URL (`<app endpoint>/flyte-mcp/mcp`), for example the one
`ops/live_mcp_test_app.py` prints. Every call carries this pod's Union token as `Authorization: Bearer`, as
the connector does, and goes through the MCP client's streamable-HTTP transport. Each round opens its own
session and makes these calls:

1. `tools/list`, compared with the tools the engine serves (`_factory_mcp.SERVED_TOOLS`)
2. four parallel `run_task(name="factory.status")`, the status check-in (it is a task, not a tool)
3. `wait_for_run` on one of those runs, then `get_run_io` and `get_logs` on it
4. the app tools: `get_app_logs` (200 lines of `--app`), `get_app_status` and `factory_mcp_health`

A `still_working` answer is followed with `get_operation` until it is done, as a client should, and every
follow-up is recorded as its own call. The JSON keeps, per call, its round, latency and outcome (`ok`,
`still_working`, `tool_error`, `http_<status>`, `timeout` or the exception's class) and the first part of any
error, scrubbed of the token and the deployment's names; results themselves are not kept. The summary has
count, p50, p95 and max latency per tool and overall, the still_working count and errors by kind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

# Run under `uv run`, which does not install this project (`package = false`), as in ops/deploy_status.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flyte_agent_factory import _profile

_profile.select_from_argv()

import anyio  # noqa: E402 -- after select_from_argv, as every ops script
import httpx  # noqa: E402 -- after select_from_argv, as every ops script
from mcp import ClientSession  # noqa: E402 -- after select_from_argv
from mcp.client.streamable_http import streamable_http_client  # noqa: E402 -- after select_from_argv
from mcp.shared.exceptions import McpError  # noqa: E402 -- after select_from_argv
from mcp.types import CallToolResult  # noqa: E402 -- after select_from_argv

from flyte_agent_factory import _factory, _mcp_budget  # noqa: E402 -- after select_from_argv
from ops.app_logs_probe import PROBE_ERRORS, make_placeholders, read_bearer_token, scrub  # noqa: E402 -- after select_from_argv

REPO = Path(__file__).resolve().parent.parent
STATUS_CALLS = 4
#: `rows` of the first of the parallel factory.status calls; call i sends STATUS_ROWS + i, see run_round.
STATUS_ROWS = 20
LOG_LINES = 50
APP_LOG_LINES = 200
#: Follow-ups of one still_working answer before the soak gives up on that operation and records it.
MAX_FOLLOW_UPS = 5
#: What a call over MCP raises: an MCP error (a client read timeout among them), a transport failure, which
#: anyio wraps in the session's task group, or a closed stream.
CALL_ERRORS = (*PROBE_ERRORS, McpError, ExceptionGroup, anyio.ClosedResourceError, anyio.BrokenResourceError)
ERROR_CHARS = 300


@dataclass
class Call:
    """One MCP request and how it went."""

    round: int
    tool: str
    seconds: float
    outcome: str
    error: str = ""
    #: The `inputs` of a `run_task` call (which `rows` it asked for); empty for every other tool.
    inputs: dict[str, Any] = field(default_factory=dict)


class Soak:
    """Makes the calls of each round and keeps a record of each one."""

    def __init__(self, scrubbed: Any) -> None:
        """Record calls; `scrubbed` cleans an error's text before it is kept."""
        self.scrubbed = scrubbed
        self.calls: list[Call] = []
        self.tool_lists: list[dict[str, Any]] = []

    def _record_call(
        self,
        rnd: int,
        tool: str,
        began: float,
        outcome: str,
        error: str = "",
        inputs: dict[str, Any] | None = None,
    ) -> None:
        text = self.scrubbed(error)[:ERROR_CHARS] if error else ""
        self.calls.append(Call(rnd, tool, round(time.monotonic() - began, 3), outcome, text, inputs or {}))

    async def call(
        self, session: ClientSession, rnd: int, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Call `tool`; follow a still_working answer to its end. Return the final result, None on error."""
        result = await self._call_once(session, rnd, tool, arguments)
        for _ in range(MAX_FOLLOW_UPS):
            if not (result and result.get("status") == "still_working"):
                break
            follow = result.get("call_again") or {}
            result = await self._call_once(
                session, rnd, follow.get("tool", "get_operation"), follow.get("arguments", {})
            )
        if result and result.get("status") == "done":
            done = result.get("result")
            return done if isinstance(done, dict) else {"result": done}
        return result

    async def _call_once(
        self, session: ClientSession, rnd: int, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        began = time.monotonic()
        inputs = arguments.get("inputs", {}) if tool == "run_task" else {}
        try:
            out = await session.call_tool(
                tool, arguments, read_timeout_seconds=timedelta(seconds=_mcp_budget.CLIENT_LIMIT_S)
            )
        except CALL_ERRORS as exc:  # a soak records each failure by kind and goes on
            self._record_call(rnd, tool, began, classify_error_kind(exc), repr(exc), inputs)
            return None
        result = read_tool_result(out)
        if out.isError:
            self._record_call(rnd, tool, began, "tool_error", json.dumps(result, default=str), inputs)
            return None
        still = result.get("status") == "still_working"
        self._record_call(rnd, tool, began, "still_working" if still else "ok", inputs=inputs)
        return result

    async def list_tools(self, session: ClientSession, rnd: int, expected: set[str]) -> None:
        """List the served tools and compare them with `expected`."""
        began = time.monotonic()
        try:
            listed = await session.list_tools()
        except CALL_ERRORS as exc:  # recorded like any call
            self._record_call(rnd, "tools/list", began, classify_error_kind(exc), repr(exc))
            return
        self._record_call(rnd, "tools/list", began, "ok")
        served = {tool.name for tool in listed.tools}
        self.tool_lists.append(
            {
                "round": rnd,
                "matches": served == expected,
                "missing": sorted(expected - served),
                "extra": sorted(served - expected),
            }
        )

    async def run_round(
        self, url: str, token: str, rnd: int, app: str, status_name: str, expected: set[str]
    ) -> None:
        """Run one check-in round in its own MCP session."""
        scope = {"project": _factory.PROJECT, "domain": _factory.DOMAIN}
        began = time.monotonic()
        try:
            async with (
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(_mcp_budget.CLIENT_LIMIT_S, connect=30.0),
                ) as client,
                streamable_http_client(url, http_client=client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                self._record_call(rnd, "initialize", began, "ok")
                await self.list_tools(session, rnd, expected)

                # Identical arguments would join into ONE run (the server does that on purpose: a retried
                # call must not act twice), so each call asks for its own `rows` and starts its own run.
                started = await asyncio.gather(
                    *(
                        self.call(
                            session,
                            rnd,
                            "run_task",
                            {
                                "name": "factory.status",
                                "inputs": {"name": status_name, "rows": STATUS_ROWS + i},
                                **scope,
                            },
                        )
                        for i in range(STATUS_CALLS)
                    )
                )
                run = next((s["name"] for s in started if s and s.get("name")), None)
                if run:
                    await self.call(
                        session,
                        rnd,
                        "wait_for_run",
                        {"name": run, "timeout_s": _mcp_budget.WAIT_CAP_S, **scope},
                    )
                    await self.call(session, rnd, "get_run_io", {"name": run, "outputs_only": True, **scope})
                    await self.call(
                        session, rnd, "get_logs", {"run_name": run, "max_lines": LOG_LINES, **scope}
                    )

                await self.call(
                    session, rnd, "get_app_logs", {"name": app, "max_lines": APP_LOG_LINES, **scope}
                )
                await self.call(session, rnd, "get_app_status", {"name": app, **scope})
                await self.call(session, rnd, "factory_mcp_health", {})
        except CALL_ERRORS as exc:  # a session that would not open is this round's result
            self._record_call(rnd, "session", began, classify_error_kind(exc), repr(exc))


def classify_error_kind(exc: BaseException) -> str:
    """Name a failed call's kind: the HTTP status, a client-side timeout, or the exception's class."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    # The MCP client reports its own read timeout as an McpError with code 408 (REQUEST_TIMEOUT).
    if isinstance(exc, McpError) and exc.error.code == httpx.codes.REQUEST_TIMEOUT:
        return "timeout"
    # anyio wraps a transport failure inside the session's task group.
    if isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        return classify_error_kind(exc.exceptions[0])
    return type(exc).__name__


def read_tool_result(out: CallToolResult) -> dict[str, Any]:
    """Return a tool's answer as a dict: its structured content, else its text parsed as JSON."""
    if out.structuredContent is not None:
        structured = out.structuredContent
        inner = structured.get("result") if set(structured) == {"result"} else structured
        return inner if isinstance(inner, dict) else {"result": inner}
    text = "".join(getattr(block, "text", "") for block in out.content)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def measure_latency(seconds: list[float]) -> dict[str, Any]:
    """Return count, p50, p95 and max of `seconds`."""
    if not seconds:
        return {"count": 0}
    if len(seconds) == 1:
        return {"count": 1, "p50_s": seconds[0], "p95_s": seconds[0], "max_s": seconds[0]}
    cuts = statistics.quantiles(seconds, n=100, method="inclusive")
    return {
        "count": len(seconds),
        "p50_s": round(cuts[49], 3),
        "p95_s": round(cuts[94], 3),
        "max_s": max(seconds),
    }


def summarize_calls(calls: list[Call], tool_lists: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the numbers the soak reports: latency per tool and overall, still_working, errors by kind."""
    tools = sorted({c.tool for c in calls})
    errors = Counter(c.outcome for c in calls if c.outcome not in ("ok", "still_working"))
    return {
        "overall": measure_latency([c.seconds for c in calls]),
        "per_tool": {t: measure_latency([c.seconds for c in calls if c.tool == t]) for t in tools},
        "still_working": sum(1 for c in calls if c.outcome == "still_working"),
        "errors_by_kind": dict(errors),
        "tools_list_matches_every_round": bool(tool_lists) and all(t["matches"] for t in tool_lists),
    }


def read_expected_tools() -> set[str]:
    """Return the tools the server should list: the stock ones the engine enables, plus the factory's."""
    from flyte_agent_factory import _factory_mcp

    return set(_factory_mcp.SERVED_TOOLS)


def describe_plan(args: argparse.Namespace) -> list[str]:
    """Return what a real run would do, one step per line."""
    return [
        f"{args.rounds} rounds against {args.endpoint}, this pod's token as bearer, one session each",
        "  tools/list, compared with _factory_mcp.SERVED_TOOLS",
        f"  {STATUS_CALLS} x run_task factory.status(name={args.status_name}, "
        f"rows={STATUS_ROWS}..{STATUS_ROWS + STATUS_CALLS - 1}), parallel, distinct inputs",
        f"  wait_for_run(timeout_s={_mcp_budget.WAIT_CAP_S:g}), get_run_io, get_logs(max_lines={LOG_LINES})",
        f"  get_app_logs({args.app}, max_lines={APP_LOG_LINES}), get_app_status, factory_mcp_health",
        f"  each call cut by the client at {_mcp_budget.CLIENT_LIMIT_S:g} s; still_working followed with"
        f" get_operation up to {MAX_FOLLOW_UPS} times",
        f"write {args.out}",
    ]


def main() -> int:
    """Run the soak and write its evidence."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile (read before argparse runs)")
    parser.add_argument("--endpoint", required=True, help="the connector URL: <app endpoint>/flyte-mcp/mcp")
    parser.add_argument("--app", required=True, help="the app the app tools read (the test app)")
    parser.add_argument("--status-name", required=True, help="the project factory.status reports on")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--out", type=Path, default=REPO / "evidence" / "mcp-soak.json")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; call nothing")
    args = parser.parse_args()

    if args.dry_run:
        print("\n".join(describe_plan(args)))
        return 0

    token = read_bearer_token()
    pairs = make_placeholders(args.endpoint)
    soak = Soak(lambda text: scrub(text, pairs, token))
    expected = read_expected_tools()
    began = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for rnd in range(args.rounds):
        asyncio.run(soak.run_round(args.endpoint, token, rnd, args.app, args.status_name, expected))
        print(f"round {rnd}: {len([c for c in soak.calls if c.round == rnd])} calls", flush=True)

    evidence = {
        "began_at": began,
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": args.endpoint,
        "app": args.app,
        "rounds": args.rounds,
        "summary": summarize_calls(soak.calls, soak.tool_lists),
        "tools_list": soak.tool_lists,
        "calls": [asdict(c) for c in soak.calls],
    }
    text = scrub(json.dumps(evidence, indent=2), pairs, token)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n")
    print(json.dumps(evidence["summary"], indent=2))
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
