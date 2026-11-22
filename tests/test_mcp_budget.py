"""The per-call budget: a slow tool returns `still_working`, an acting one is never repeated (13-15)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from flyte_agent_factory import _mcp_budget
from flyte_agent_factory._mcp_budget import (
    CALL_LOG_MAX,
    LoopLag,
    Operations,
    get_operation,
    note_step,
    run_within_budget,
)
from tests._wait import wait_for


@pytest.fixture(autouse=True)
def short_budget(monkeypatch: pytest.MonkeyPatch) -> Operations:
    ops = Operations()
    monkeypatch.setattr(_mcp_budget, "TOOL_BUDGET_S", 0.2)
    monkeypatch.setattr(_mcp_budget, "OPERATIONS", ops)
    _mcp_budget.CALL_LOG.clear()
    return ops


class Slow:
    """A fake backend: records each time it acts, and takes `delay` seconds.

    `noted` is set once it has acted and noted its step, `finished` once its delay is over, so a test waits
    for exactly that instead of sleeping for a guessed time.
    """

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.acted = 0
        self.noted = asyncio.Event()
        self.finished = asyncio.Event()

    async def run_task(self, name: str = "x", ctx: object = None) -> dict[str, str]:
        self.acted += 1
        note_step("submitted run r1")
        self.noted.set()
        await asyncio.sleep(self.delay)
        self.finished.set()
        return {"run": "r1", "name": name}


async def test_a_slow_call_returns_still_working_within_the_budget() -> None:
    fake = Slow()
    tool = run_within_budget("run_task", fake.run_task, acting=True)
    started = time.monotonic()
    result: Any = await tool(name="a")
    assert time.monotonic() - started < 0.2 + 0.2
    assert result["status"] == "still_working"
    assert result["call_again"] == {
        "tool": "get_operation",
        "arguments": {"operation_id": result["operation_id"]},
    }
    assert result["steps_done"] == ["submitted run r1"]


async def test_get_operation_returns_the_result_without_redoing_the_work() -> None:
    fake = Slow(delay=0.3)
    pending: Any = await run_within_budget("run_task", fake.run_task, acting=False)(name="a")
    done = await get_operation(pending["operation_id"], wait_s=5)
    assert done["status"] == "done"
    assert done["result"] == {"run": "r1", "name": "a"}
    assert fake.acted == 1


async def test_get_operation_reports_progress_while_still_running() -> None:
    fake = Slow(delay=2)
    pending: Any = await run_within_budget("run_task", fake.run_task, acting=False)()
    again = await get_operation(pending["operation_id"], wait_s=0.05)
    assert again["status"] == "still_working"
    assert again["operation_id"] == pending["operation_id"]
    pending_task = _mcp_budget.OPERATIONS.get(pending["operation_id"])
    assert pending_task is not None
    pending_task.task.cancel()


async def test_a_dropped_acting_call_is_joined_not_repeated() -> None:
    fake = Slow(delay=0.35)
    tool = run_within_budget("run_task", fake.run_task, acting=True)
    first: asyncio.Task[Any] = asyncio.ensure_future(tool(name="a"))
    await asyncio.wait_for(fake.noted.wait(), 1)  # the fake has acted and noted its step
    first.cancel()  # the client dropped the request
    with pytest.raises(asyncio.CancelledError):
        await first
    again: Any = await tool(name="a")
    assert fake.acted == 1
    # Joined within the budget and finished (0.35 s > 0.2 s budget, so it reports the step instead)
    assert again["status"] == "still_working"
    assert again["steps_done"] == ["submitted run r1"]
    final: Any = await tool(name="a")
    assert final == {"run": "r1", "name": "a"}
    assert fake.acted == 1


async def test_a_delivered_acting_call_acts_again() -> None:
    fake = Slow(delay=0)
    tool = run_within_budget("run_task", fake.run_task, acting=True)
    assert await tool(name="a") == {"run": "r1", "name": "a"}
    assert await tool(name="a") == {"run": "r1", "name": "a"}
    assert fake.acted == 2


async def test_different_arguments_are_different_operations() -> None:
    fake = Slow(delay=0.5)
    tool = run_within_budget("run_task", fake.run_task, acting=True)
    first: Any = await tool(name="a")
    second: Any = await tool(name="b")
    assert first["operation_id"] != second["operation_id"]
    assert fake.acted == 2


async def test_an_unknown_id_says_so() -> None:
    result = await get_operation("op-nope", wait_s=0.01)
    assert result["status"] == "unknown_operation"
    assert result["operation_id"] == "op-nope"
    assert "server_started_at" in result
    assert "check the effect" in result["hint"]


async def test_an_exception_passes_through_and_is_logged_by_class() -> None:
    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await run_within_budget("boom", boom, acting=False)()
    assert _mcp_budget.CALL_LOG[-1][:2] == ("boom", "error:ValueError")


async def test_in_flight_counts_only_running_operations(short_budget: Operations) -> None:
    fake = Slow(delay=0.4)
    pending: Any = await run_within_budget("run_task", fake.run_task, acting=False)(name="a")
    assert pending["status"] == "still_working" and short_budget.count_in_flight() == 1
    await asyncio.wait_for(fake.finished.wait(), 2)
    await wait_for(lambda: short_budget.count_in_flight() == 0, timeout=2)
    assert short_budget.count_in_flight() == 0 and len(short_budget) == 1


async def test_a_refused_call_makes_no_operation_and_is_logged_as_refused(short_budget: Operations) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    fake = Slow()

    def refuse(name: str = "x", ctx: object = None) -> None:
        raise ToolError("not allowed")

    tool = run_within_budget("run_task", fake.run_task, acting=True, check=refuse)
    with pytest.raises(ToolError, match="not allowed"):
        await tool(name="a")
    assert fake.acted == 0 and len(short_budget) == 0
    assert _mcp_budget.CALL_LOG[-1][:2] == ("run_task", "refused")


async def test_the_wrapper_keeps_the_tool_signature() -> None:
    import inspect

    fake = Slow()
    assert inspect.signature(run_within_budget("run_task", fake.run_task, acting=True)) == inspect.signature(
        fake.run_task
    )


async def test_the_log_line_has_no_argument_values(caplog: pytest.LogCaptureFixture) -> None:
    async def create_secret(name: str, value: str) -> str:
        return "ok"

    with caplog.at_level(logging.INFO, logger=_mcp_budget.logger.name):
        await run_within_budget("create_secret", create_secret, acting=True)(
            name="n-visible", value="hunter2-value"
        )
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("mcp_call")]
    assert len(lines) == 1
    assert lines[0].startswith("mcp_call tool=create_secret duration_ms=")
    assert "outcome=ok" in lines[0]
    assert "hunter2-value" not in lines[0]
    assert "n-visible" not in lines[0]


async def test_the_outcome_ring_buffer_is_capped() -> None:
    async def quick() -> int:
        return 1

    tool = run_within_budget("quick", quick, acting=False)
    for _ in range(CALL_LOG_MAX + 25):
        await tool()
    assert len(_mcp_budget.CALL_LOG) == CALL_LOG_MAX


async def test_finished_operations_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mcp_budget, "OPERATIONS_MAX", 3)
    ops = _mcp_budget.OPERATIONS

    async def quick() -> int:
        return 1

    # Undelivered reads: the ids were returned as `still_working` and the work has since finished.
    for _ in range(6):
        op = ops.start("quick", "k", False, quick())
        await asyncio.wait({op.task})
    ops.get("op-none")  # prunes
    assert len(ops) == 3


async def test_loop_lag_keeps_max_and_p95() -> None:
    lag = LoopLag()
    assert lag.summarise_lag()["samples"] == 0
    for ms in range(1, 101):
        lag.record(ms / 1000)
    snap = lag.summarise_lag()
    assert snap["samples"] == 100
    assert snap["max_ms"] == 100.0
    assert snap["p95_ms"] == 96.0


def test_the_tool_budget_is_a_named_constant_below_the_client_limit_with_margin():
    """Criterion 13: every call ends before the 230 s client limit, with room for the reply to travel."""
    assert _mcp_budget.CLIENT_LIMIT_S == 230
    assert _mcp_budget.TOOL_BUDGET_S < _mcp_budget.CLIENT_LIMIT_S - 20


async def test_an_undelivered_acting_call_survives_more_than_the_cap_of_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mcp_budget, "OPERATIONS_MAX", 5)
    fake = Slow(delay=0.3)
    acting = run_within_budget("run_task", fake.run_task, acting=True)
    # The client cuts the call off: its result is never delivered.
    call = asyncio.ensure_future(acting(name="a"))
    await asyncio.wait_for(fake.noted.wait(), 1)
    call.cancel()
    await asyncio.wait_for(fake.finished.wait(), 2)  # the work is its own task, so it finished anyway
    await wait_for(lambda: _mcp_budget.OPERATIONS.count_in_flight() == 0, timeout=2)

    async def read() -> int:
        return 1

    reading = run_within_budget("read", read, acting=False)
    for _ in range(_mcp_budget.OPERATIONS_MAX + 100):
        await reading()
    assert await acting(name="a") == {"run": "r1", "name": "a"}
    assert fake.acted == 1


async def test_a_delivered_result_is_not_kept() -> None:
    async def read() -> int:
        return 1

    async def fail() -> None:
        raise ValueError("nope")

    assert await run_within_budget("read", read, acting=False)() == 1
    with pytest.raises(ValueError, match="nope"):
        await run_within_budget("fail", fail, acting=False)()
    assert len(_mcp_budget.OPERATIONS) == 0


async def test_an_operation_asked_about_by_id_is_forgotten_once_delivered() -> None:
    fake = Slow(delay=0.3)
    pending: Any = await run_within_budget("run_task", fake.run_task, acting=False)(name="a")
    done = await get_operation(pending["operation_id"], wait_s=5)
    assert done["status"] == "done"
    assert len(_mcp_budget.OPERATIONS) == 0
    assert (await get_operation(pending["operation_id"], wait_s=0.01))["status"] == "unknown_operation"
