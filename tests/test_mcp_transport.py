"""A dropped request on the server's real streamable-HTTP transport joins its acting call (C11, C41).

The server runs under uvicorn on a loopback port inside the test's own event loop, so the fake's events
are awaited directly: no sleeps, no guessed timings. The drop is a TCP close after the request was sent.
Each test runs twice: on the stock transport with only the disconnect watch, and under the middleware the
factory's app installs (`_starlette_middleware`: the watch, the stock pass-through auth, the grant page).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp

from flyte_agent_factory import _factory_mcp as factory
from flyte_agent_factory import _mcp_budget
from flyte_agent_factory._mcp_budget import CancelOnDisconnect, Operations
from tests._wait import wait_for

TOKEN = "test-" + "bearer"
HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "authorization": f"Bearer {TOKEN}",
}
CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "run_task", "arguments": {"name": "a"}},
}


class Acting:
    """A fake `run_task`: acts at once, then takes a while; `noted` is set once it has acted."""

    def __init__(self) -> None:
        self.acted = 0
        self.noted = asyncio.Event()
        self.release = asyncio.Event()

    async def run_task(self, name: str = "x") -> dict[str, str]:
        self.acted += 1
        self.noted.set()
        await self.release.wait()
        return {"run": f"run-{self.acted}", "name": name}


@pytest.fixture
def isolated_budget(monkeypatch: pytest.MonkeyPatch) -> Operations:
    ops = Operations()
    monkeypatch.setattr(_mcp_budget, "OPERATIONS", ops)
    monkeypatch.setattr(_mcp_budget, "TOOL_BUDGET_S", 30.0)
    _mcp_budget.CALL_LOG.clear()
    return ops


def _wrap_as_the_factory_does(app: ASGIApp) -> ASGIApp:
    for middleware in reversed(factory.app._starlette_middleware()):
        app = middleware.cls(app, *middleware.args, **middleware.kwargs)
    return app


@pytest.fixture(params=["disconnect-watch", "factory-middleware"])
async def served(
    request: pytest.FixtureRequest, isolated_budget: Operations
) -> AsyncIterator[tuple[str, int, Acting]]:
    """The factory's served layer on a stateless JSON streamable-HTTP server, as the factory configures it."""
    fake = Acting()
    probe = FastMCP("probe", stateless_http=True, json_response=True)
    probe.add_tool(factory._serve("run_task", fake.run_task), name="run_task")
    inner = probe.streamable_http_app()
    watched = request.param == "disconnect-watch"
    app = CancelOnDisconnect(inner) if watched else _wrap_as_the_factory_does(inner)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    await wait_for(lambda: server.started or serving.done(), timeout=30, what="uvicorn started")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp", port, fake
    finally:
        fake.release.set()
        server.should_exit = True
        await serving


async def _drop_a_request_after_it_acted(port: int, fake: Acting) -> None:
    """Send the call on a raw connection and close it once the fake has acted: the client went away."""
    body = json.dumps(CALL).encode()
    head = (
        f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: {HEADERS['accept']}\r\n"
        f"Content-Type: application/json\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head + body)
    await writer.drain()
    await asyncio.wait_for(fake.noted.wait(), 30)
    writer.close()
    await writer.wait_closed()
    del reader


def _text(response: httpx.Response) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(response.json()["result"]["content"][0]["text"])
    return result


async def test_a_request_the_client_drops_joins_its_acting_call_over_streamable_http(
    served: tuple[str, int, Acting],
) -> None:
    url, port, fake = served
    await _drop_a_request_after_it_acted(port, fake)
    await wait_for(
        lambda: _mcp_budget.CALL_LOG and _mcp_budget.CALL_LOG[-1][1] == "dropped", what="dropped logged"
    )
    assert len(_mcp_budget.OPERATIONS) == 1  # open: nobody was told the result
    async with httpx.AsyncClient() as client:
        repeat = asyncio.ensure_future(client.post(url, headers=HEADERS, json=CALL, timeout=30))
        await wait_for(lambda: _mcp_budget.OPERATIONS.count_in_flight() == 1, what="repeat joined")
        fake.release.set()
        assert _text(await repeat) == {"run": "run-1", "name": "a"}
    assert fake.acted == 1
    assert _mcp_budget.CALL_LOG[-1][:2] == ("run_task", "joined")
    assert len(_mcp_budget.OPERATIONS) == 0


async def test_a_delivered_call_repeated_acts_again_over_streamable_http(
    served: tuple[str, int, Acting],
) -> None:
    url, _, fake = served
    fake.release.set()
    async with httpx.AsyncClient() as client:
        first = _text(await client.post(url, headers=HEADERS, json=CALL, timeout=30))
        second = _text(await client.post(url, headers=HEADERS, json=CALL, timeout=30))
    assert (first["run"], second["run"], fake.acted) == ("run-1", "run-2", 2)
    assert [outcome for _, outcome, _, _ in _mcp_budget.CALL_LOG] == ["ok", "ok"]
