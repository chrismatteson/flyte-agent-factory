"""The pre-review steps (`_pre_review`) carry the capacity outcome of `checks.sh` as the inline block did."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _capacity,
    _gate_tests,
    _gates,
    _lane_shards,
    _load_clock,
    _pre_review,
    _session_config,
    _supervise_support,
    _timeline,
)


def _check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str, left: dict[str, Any]
) -> _pre_review.PreReview:
    """Run `_pre_review.check` with a `checks.sh` that returned `problem` and left `left` on the timeline."""
    checks = tmp_path / _session_config._checks_rel(_session_config._project_name, _session_config._layout())
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text("exit 1\n")
    timeline = _timeline._Timeline()

    async def run_declared_checks(workspace: Path, timeline_: Any, junit_dir: Path | None) -> str:
        timeline_.checks = left
        return problem

    monkeypatch.setattr(_supervise_support, "_run_declared_checks", run_declared_checks)
    monkeypatch.setattr(_session_config, "warm_vendored_cache", lambda: None)
    monkeypatch.setattr(_session_config, "_applicable_standards", lambda: {})
    monkeypatch.setattr(_gates, "run", lambda *args: "")
    checked = _pre_review.PreReview(_gate_tests.GateWork(junit_dir=tmp_path / "junit"))
    asyncio.run(_pre_review.check(checked, tmp_path, timeline, "DONE"))
    return checked


def test_checks_without_capacity_are_not_a_problem_and_the_reason_is_kept(tmp_path, monkeypatch):
    problem = _capacity.ChecksNoCapacity("`checks.sh` failed: no capacity", "slow lane: no room")

    checked = _check(tmp_path, monkeypatch, problem, {"timed_out": "the node was busy"})

    assert checked.problems == [] and not checked.check_failed
    assert checked.checks_capacity == "slow lane: no room"
    # Cut off while its lanes found no room: the gate's own run is not refused for it.
    assert checked.work.checks_timed_out == ""


def test_checks_that_timed_out_on_their_own_are_a_problem_and_are_not_rerun(tmp_path, monkeypatch):
    left = {"timed_out": "the checks themselves are slow", "load": "load 2.0", "image": "img"}

    checked = _check(tmp_path, monkeypatch, "`checks.sh` failed with exit code 124", left)

    assert checked.problems == ["`checks.sh` failed with exit code 124"] and checked.check_failed
    assert checked.checks_capacity == ""
    assert checked.work.checks_timed_out == "the checks themselves are slow"
    assert checked.work.checks_load == "load 2.0" and checked.work._image == "img"


@pytest.mark.parametrize(
    ("cpu_max", "quota", "workers"), [("250000 100000\n", 2.5, 3), ("max 100000\n", 4, 4)]
)
def test_the_load_clock_and_the_worker_count_read_one_quota(tmp_path, monkeypatch, cpu_max, quota, workers):
    (tmp_path / "cpu.max").write_text(cpu_max)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(_load_clock, "_CGROUP_CPU_MAX", tmp_path / "cpu.max")

    assert _load_clock.cpu_allowance() == _lane_shards.read_cpu_quota(tmp_path / "cpu.max") == quota
    assert _lane_shards.read_cpu_allowance(tmp_path / "cpu.max") == workers
