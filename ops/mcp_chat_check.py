"""Do what the operator does from chat, against a deployed factory MCP server, and keep the evidence.

    PYTHONPATH=src uv run python ops/mcp_chat_check.py --endpoint <connector URL> --test-app <name>

Over MCP, as a chat client would (this pod's Union token as `Authorization: Bearer`):

1. `get_app_logs` for the production factory MCP app: its newest 200 lines;
2. `get_app_status` for it: the history and the last restart reason;
3. `factory_mcp_health`;
4. `restart_app` of the test app on the test app's own server, which must refuse (it would end its own call).

Then, in this pod, the same served layer (`_factory_mcp.app.mcp.call_tool`, which goes through
`_serve`) restarts the test app (`ops/live_mcp_test_app.py` deployed it), following `still_working`
with `get_operation`.
Production `factory-mcp` is only read, never restarted; the restart refuses any name without the test prefix.

The evidence keeps counts, timings, replicas, revisions and a few log samples, redacted and with the
deployment's names replaced by placeholders (`ops/app_logs_probe.scrub`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
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

from flyte_agent_factory import _app_tools, _union_identity  # noqa: E402 -- after select_from_argv
from ops.app_logs_probe import make_placeholders, read_bearer_token, scrub  # noqa: E402 -- after select_from_argv
from ops.live_mcp_test_app import PREFIX as TEST_PREFIX  # noqa: E402 -- after select_from_argv
from ops.mcp_soak import read_tool_result  # noqa: E402 -- after select_from_argv

REPO = Path(__file__).resolve().parent.parent
SAMPLE_LINES = 5
CLIENT_LIMIT_S = 230.0


def _digest_logs(result: dict[str, Any]) -> dict[str, Any]:
    lines = result.get("lines") or []
    return {
        **{k: v for k, v in result.items() if k != "lines"},
        "line_count": len(lines),
        "newest_samples": lines[-SAMPLE_LINES:],
    }


async def check_over_mcp(url: str, token: str, production: str, test_app: str) -> dict[str, Any]:
    """Call the read tools about `production`, and the refused self-restart, on the server at `url`."""
    out: dict[str, Any] = {}
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(CLIENT_LIMIT_S)
        ) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        calls: list[tuple[str, str, dict[str, Any]]] = [
            ("get_app_logs_newest_200", "get_app_logs", {"name": production, "max_lines": 200}),
            ("get_app_status", "get_app_status", {"name": production}),
            ("factory_mcp_health", "factory_mcp_health", {}),
            ("restart_own_app", "restart_app", {"name": test_app}),
        ]
        for label, tool, arguments in calls:
            began = time.monotonic()
            result = await session.call_tool(tool, arguments)
            seconds = round(time.monotonic() - began, 3)
            body: Any = read_tool_result(result)
            if label.startswith("get_app_logs") and isinstance(body, dict):
                body = _digest_logs(body)
            out[label] = {"seconds": seconds, "is_error": bool(result.isError), "result": body}
    return out


async def restart_in_process(test_app: str) -> dict[str, Any]:
    """Restart `test_app` through the served layer here, following `still_working` to the end."""
    from flyte_agent_factory import _factory_mcp

    server = _factory_mcp.app.mcp
    assert server is not None
    began = time.monotonic()
    steps: list[dict[str, Any]] = []

    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        t0 = time.monotonic()
        result = await server.call_tool(tool, arguments)
        content: Any = result[0] if isinstance(result, tuple) else result  # (blocks, structured) or blocks
        body = json.loads(content[0].text) if content and hasattr(content[0], "text") else content
        steps.append({"tool": tool, "seconds": round(time.monotonic() - t0, 3), "result": body})
        return body

    body = await call("restart_app", {"name": test_app})
    while isinstance(body, dict) and body.get("status") in ("still_working", "running"):
        body = await call("get_operation", {"operation_id": body["operation_id"]})
    return {"total_seconds": round(time.monotonic() - began, 3), "calls": steps}


def main() -> int:
    """Run the check and write the evidence."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile (read before argparse runs)")
    parser.add_argument("--endpoint", required=True, help="the test app's connector URL")
    parser.add_argument("--test-app", required=True)
    parser.add_argument("--production", default=_app_tools.read_own_app_name())
    parser.add_argument("--out", type=Path, default=REPO / "evidence" / "union-chat-check.json")
    args = parser.parse_args()
    if not args.test_app.startswith(TEST_PREFIX):
        raise SystemExit(f"{args.test_app!r} is not a test app ({TEST_PREFIX}*); refusing to restart it")

    profile = _profile.require()
    _union_identity.init_for_script(profile, key_env=_union_identity.KEY_ENV)
    token = read_bearer_token()
    evidence = {
        "began_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "production": args.production,
        "test_app": args.test_app,
        "over_mcp": asyncio.run(check_over_mcp(args.endpoint, token, args.production, args.test_app)),
        "restart_in_process": asyncio.run(restart_in_process(args.test_app)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    text = scrub(json.dumps(evidence, indent=2, default=str), make_placeholders(args.endpoint), token)
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; not overwritten")
    args.out.write_text(text + "\n")
    print(text[:4000])
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
