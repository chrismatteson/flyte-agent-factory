"""Replay on a test app the parallel bursts that timed out on production; read its health around them.

    PYTHONPATH=src uv run python ops/mcp_burst.py --endpoint <URL> --app <app> --status-name <p> --dry-run
    PYTHONPATH=src uv run python ops/mcp_burst.py --endpoint <URL> --app <app> --status-name <p>

The bursts are the "before" measurements of `evidence/reliability-measurements.md` (8 parallel `whoami`, of
which 7 took 226.6 s on 2026-11-19 ~17:47Z; 8 parallel `tools/list`; 4 parallel `get_app`) plus the
operator's case, 4 parallel `factory.status` through `run_task`. Each burst shares one MCP session, as a
chat client's parallel tool calls do. `factory_mcp_health` is read before the first burst and after the
last, so the app's own CPU time, memory, cgroup limits, CPU throttling, OOM kills and event-loop lag under
that load are measured in the pod rather than inferred. Read-only apart from the `factory.status` runs it
starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

# Run under `uv run`, which does not install this project (`package = false`), as in ops/deploy_status.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flyte_agent_factory import _profile

_profile.select_from_argv()

import httpx  # noqa: E402 -- after select_from_argv, as every ops script
from mcp import ClientSession  # noqa: E402 -- after select_from_argv
from mcp.client.streamable_http import streamable_http_client  # noqa: E402 -- after select_from_argv

from flyte_agent_factory import _factory, _mcp_budget  # noqa: E402 -- after select_from_argv
from ops.app_logs_probe import make_placeholders, read_bearer_token, scrub  # noqa: E402 -- after select_from_argv
from ops.mcp_soak import CALL_ERRORS, Soak, classify_error_kind, measure_latency  # noqa: E402 -- after select_from_argv

REPO = Path(__file__).resolve().parent.parent
#: (label, tool, arguments, parallel calls); `None` as the tool is `tools/list`.
BURSTS: list[tuple[str, str | None, dict[str, Any], int]] = [
    ("whoami x8", "whoami", {}, 8),
    ("tools/list x8", None, {}, 8),
    ("get_app x4", "get_app", {"name": "<app>"}, 4),
    ("factory.status x4", "run_task", {"name": "factory.status"}, 4),
]


def pick_distinct(arguments: dict[str, Any], i: int) -> dict[str, Any]:
    """Return `arguments` for parallel call `i`; a `run_task` call gets its own `rows` and so its own run."""
    # Identical arguments would join into ONE run: the server does that on purpose (a retried acting call
    # must not act twice). Four parallel calls therefore differ in `rows`: 20, 21, 22, 23.
    if "inputs" not in arguments:
        return arguments
    return {**arguments, "inputs": {**arguments["inputs"], "rows": arguments["inputs"]["rows"] + i}}


async def run_burst(
    soak: Soak, session: ClientSession, index: int, tool: str | None, arguments: dict[str, Any], n: int
) -> float:
    """Make `n` calls at once; return the wall time of the whole burst."""
    began = time.monotonic()
    if tool is None:
        await asyncio.gather(*(soak.list_tools(session, index, set()) for _ in range(n)))
    else:
        calls = (soak.call(session, index, tool, pick_distinct(arguments, i)) for i in range(n))
        await asyncio.gather(*calls)
    return round(time.monotonic() - began, 3)


async def read_health(session: ClientSession) -> dict[str, Any]:
    """Read the test app's `factory_mcp_health`, the fields that describe load."""
    out = await session.call_tool(
        "factory_mcp_health", {}, read_timeout_seconds=timedelta(seconds=_mcp_budget.CLIENT_LIMIT_S)
    )
    result = json.loads(out.content[0].text) if out.content and hasattr(out.content[0], "text") else {}
    keys = ("revision", "replica", "uptime_s", "process", "loop_lag_ms", "calls_last_hour", "restarts")
    return {k: result.get(k) for k in keys}


async def run_bursts(url: str, token: str, app: str, status_name: str, soak: Soak) -> dict[str, Any]:
    """Open one session, read health, run every burst, read health again."""
    scope = {"project": _factory.PROJECT, "domain": _factory.DOMAIN}
    record: dict[str, Any] = {"bursts": []}
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(_mcp_budget.CLIENT_LIMIT_S, connect=30.0),
        ) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        record["health_before"] = await read_health(session)
        for index, (label, tool, arguments, n) in enumerate(BURSTS):
            args = {k: (app if v == "<app>" else v) for k, v in arguments.items()}
            if tool == "run_task":
                args = {**args, "inputs": {"name": status_name, "rows": 20}, **scope}
            wall = await run_burst(soak, session, index, tool, args, n)
            calls = [c for c in soak.calls if c.round == index]
            record["bursts"].append(
                {
                    "burst": label,
                    "wall_s": wall,
                    "latency": measure_latency([c.seconds for c in calls]),
                    "outcomes": sorted({c.outcome for c in calls}),
                    "over_client_limit": sum(c.seconds > _mcp_budget.CLIENT_LIMIT_S for c in calls),
                }
            )
            print(json.dumps(record["bursts"][-1]), flush=True)
        record["health_after"] = await read_health(session)
    return record


def main() -> int:
    """Run the bursts and write the evidence."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile (read before argparse runs)")
    parser.add_argument("--endpoint", required=True, help="the connector URL: <app endpoint>/flyte-mcp/mcp")
    parser.add_argument("--app", required=True, help="the test app (the health tool reports on it)")
    parser.add_argument("--status-name", required=True, help="the project factory.status reports on")
    parser.add_argument("--out", type=Path, default=REPO / "evidence" / "mcp-burst.json")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; call nothing")
    args = parser.parse_args()

    if args.dry_run:
        for label, tool, arguments, n in BURSTS:
            print(f"{label}: {n} x {tool or 'tools/list'} {arguments} in parallel, one session")
        print(f"factory_mcp_health before and after; evidence to {args.out}")
        return 0

    token = read_bearer_token()
    pairs = make_placeholders(args.endpoint)
    soak = Soak(lambda text: scrub(text, pairs, token))
    began = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        record = asyncio.run(run_bursts(args.endpoint, token, args.app, args.status_name, soak))
    except CALL_ERRORS as exc:  # the session itself failed: kept as the result
        record = {"session_error": classify_error_kind(exc), "error": scrub(repr(exc), pairs, token)[:300]}
    evidence = {
        "began_at": began,
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": args.endpoint,
        "app": args.app,
        **record,
        "calls": [asdict(c) for c in soak.calls],
    }
    text = scrub(json.dumps(evidence, indent=2, default=str), pairs, token)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n")
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
