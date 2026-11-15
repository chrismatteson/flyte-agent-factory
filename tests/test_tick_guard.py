"""Scheduled ticks skip themselves while an earlier run of the same task is live (`_tick_guard`)."""

import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import flyte
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import (
    _bench_flow,
    _board,
    _deploy,
    _deployer,
    _factory,
    _factory_base,
    _factory_recreate,
    _loop_flow,
    _profile,
    _pull_requests,
    _reconcile,
    _tick_guard,
)
from tests.test_deployer import make_profile

TASK = "factory.sweep"


def _run(name, phase="RUNNING"):
    return SimpleNamespace(name=name, phase=phase)


@pytest.fixture
def listing(monkeypatch):
    """Fake the run listing, oldest first as asked for; `.runs` is what it returns, `.calls` its filters."""
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="me")))
    state = SimpleNamespace(runs=[], calls=[], error=None)

    async def listall(**kwargs):
        state.calls.append(kwargs)
        if state.error:
            raise state.error
        for run in state.runs:
            yield run

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listall))
    return state


async def _blocker():
    return await _tick_guard.blocking_run(TASK, project="p", domain="d")


async def test_an_earlier_live_run_blocks_the_tick(listing):
    listing.runs = [_run("earlier", "WAITING_FOR_RESOURCES"), _run("me")]
    assert await _blocker() == ("earlier", "WAITING_FOR_RESOURCES")
    (call,) = listing.calls
    assert call["task_name"] == TASK and call["project"] == "p" and call["domain"] == "d"
    assert call["in_phase"] == _deploy.LIVE_PHASES
    assert call["sort_by"] == ("created_at", "asc")


async def test_no_live_run_or_only_its_own_lets_the_tick_run(listing):
    assert await _blocker() is None
    listing.runs = [_run("me")]
    assert await _blocker() is None


async def test_a_run_that_ended_does_not_block(listing):
    # The server filters by phase; the listing of a terminal run is empty.
    listing.runs = []
    assert await _tick_guard.skip_reason(TASK, project="p", domain="d") is None


async def test_a_later_live_run_does_not_block_an_earlier_one(listing):
    listing.runs = [_run("me"), _run("later")]
    assert await _blocker() is None


async def test_the_skip_names_the_blocking_run_and_is_logged(listing, capsys):
    listing.runs = [_run("earlier", "RUNNING"), _run("me")]
    reason = await _tick_guard.skip_reason(TASK, project="p", domain="d")
    assert reason is not None
    assert json.loads(reason) == {"skipped": "previous run earlier is RUNNING"}
    assert "previous run earlier is RUNNING" in capsys.readouterr().out


async def test_the_phase_is_named_without_its_enum_prefix(listing):
    listing.runs = [_run("earlier", "ActionPhase.QUEUED")]
    assert await _blocker() == ("earlier", "QUEUED")


@pytest.mark.parametrize(
    "error", [RuntimeError("down"), OSError("offline"), ConnectError(Code.UNAVAILABLE, "x")]
)
async def test_a_failing_listing_runs_the_tick_with_a_warning(listing, caplog, error):
    listing.error = error
    with caplog.at_level(logging.WARNING):
        assert await _blocker() is None
    assert "running this tick anyway" in caplog.text


@pytest.mark.parametrize("code", [Code.UNAUTHENTICATED, Code.PERMISSION_DENIED])
async def test_bad_credentials_are_not_swallowed(listing, code):
    listing.error = ConnectError(code, "no")
    with pytest.raises(ConnectError):
        await _blocker()


async def test_outside_a_task_there_is_nothing_to_guard(monkeypatch):
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    assert await _blocker() is None


# --- every scheduled task asks first -------------------------------------------------------------

SKIP = json.dumps({"skipped": "previous run earlier is RUNNING"})


@pytest.fixture
def blocked(monkeypatch):
    """`skip_reason` reports a blocker; returns the (task, project, domain) it was asked about."""
    asked = []

    async def skip_reason(task_name, *, project, domain):
        asked.append((task_name, project, domain))
        return SKIP

    monkeypatch.setattr(_tick_guard, "skip_reason", skip_reason)
    return asked


def _no_work(monkeypatch, module, *names):
    for name in names:

        def work(*args, _name=name, **kwargs):
            raise AssertionError(f"{_name} ran although a previous tick is live")

        monkeypatch.setattr(module, name, work)


async def test_sweep_skips(blocked, monkeypatch):
    _no_work(monkeypatch, _factory_recreate, "recreate_aborted")
    _no_work(monkeypatch, _factory, "dispatch", "wake_planner_for_free_slot")
    assert await _factory.sweep.func() == SKIP
    assert blocked == [(_deploy.SWEEP_TASK, _factory.PROJECT, _factory.DOMAIN)]


async def test_garden_weekly_skips(blocked, monkeypatch):
    _no_work(monkeypatch, _factory_base, "_labelled_runs")
    _no_work(monkeypatch, _factory, "list_targets", "_spot_first")
    assert await _factory.garden_weekly.func() == SKIP
    assert blocked == [(_deploy.GARDEN_TASK, _factory.PROJECT, _factory.DOMAIN)]


async def test_the_scheduled_reconcile_skips(blocked, monkeypatch):
    _no_work(monkeypatch, _board, "take_token")
    _no_work(monkeypatch, _reconcile, "project_runs")
    assert await _reconcile.reconcile.func() == SKIP
    assert blocked == [(_deploy.RECONCILE_TASK, _factory.PROJECT, _factory.DOMAIN)]


async def test_an_event_reconcile_is_never_skipped(blocked, monkeypatch):
    # It heals one completion's runs; skipping it would lose that work.
    done = SimpleNamespace(phase="SUCCEEDED")
    monkeypatch.setattr(remote.Run, "get", lambda name: done)
    monkeypatch.setattr(_board, "take_token", lambda secret: "")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda secret: "")
    monkeypatch.setattr(_reconcile, "PROFILE", dataclasses.replace(_reconcile.PROFILE, board=None))
    await _reconcile.reconcile.func(completion_run="done-run")
    assert blocked == []


async def test_deploy_if_changed_skips_and_says_so_in_its_report(blocked, monkeypatch):
    reports = []

    async def replace(html, do_flush):
        reports.append(html)

    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="me")))
    monkeypatch.setattr(flyte.report.replace, "aio", replace)
    _no_work(monkeypatch, _deployer, "run_once", "_app")
    assert await _deployer.deploy_if_changed.func() == SKIP
    assert blocked == [(_deploy.DEPLOYER_TASK, _deployer.PROJECT, _deployer.DOMAIN)]
    assert "previous run earlier is RUNNING" in reports[0]


async def test_generate_backlog_skips(blocked, monkeypatch):
    _no_work(monkeypatch, _loop_flow, "_generate_backlog", "_app")
    assert await _loop_flow.generate_backlog.func() == SKIP
    assert blocked == [(_deploy.BACKLOG_TASK, _factory.PROJECT, _factory.DOMAIN)]


async def test_collect_labels_skips(blocked, monkeypatch):
    _no_work(monkeypatch, _bench_flow, "_collect_labels")
    assert await _bench_flow.collect_labels.func() == 0
    assert blocked == [(_deploy.COLLECT_LABELS_TASK, _bench_flow.PROJECT, _bench_flow.DOMAIN)]


def test_the_guarded_task_names_are_the_deployed_ones():
    for name, module in (
        (_deploy.SWEEP_TASK, _factory),
        (_deploy.GARDEN_TASK, _factory),
        (_deploy.BACKLOG_TASK, _loop_flow),
        (_deploy.COLLECT_LABELS_TASK, _bench_flow),
        (_deploy.RECONCILE_TASK, _reconcile),
        (_deploy.DEPLOYER_TASK, _deployer),
    ):
        assert name in module.env.tasks


# --- the deployer's schedule follows `[deploy] auto` -----------------------------------------------


def test_the_schedule_is_declared_only_with_auto():
    assert _deployer._triggers(make_profile("file:///unused", auto=True)) == [_deployer.TRIGGER]
    assert _deployer._triggers(make_profile("file:///unused", auto=False)) == []
    assert _deployer.TRIGGER.name == _deploy.DEPLOY_TRIGGER
    # The test profile has auto off, so the task as imported carries none.
    assert _deployer.PROFILE.deploy.auto is False
    assert not _deployer.deploy_if_changed.triggers


@pytest.mark.parametrize("auto", [False, True])
def test_a_deploy_deletes_the_deployer_schedule_only_without_auto(monkeypatch, auto):
    deleted = []
    monkeypatch.setattr(remote.Trigger, "delete", lambda name, task_name: deleted.append((name, task_name)))
    profile = dataclasses.replace(_profile.require(), deploy=_profile.Deploy(auto=auto))
    _deploy.delete_retired_triggers(profile)
    deploy_on_merge = ("deploy-on-merge", "factory-deployer.deploy_if_changed")
    assert (deploy_on_merge in deleted) is (not auto)
    assert set(_deploy.RETIRED_TRIGGERS) <= set(deleted)


async def test_a_run_stuck_live_for_longer_than_any_tick_runs_no_longer_blocks(listing):
    await _blocker()
    (call,) = listing.calls
    after = call["created_at"].after
    assert _tick_guard.STALE_AFTER >= timedelta(hours=2)
    assert abs(datetime.now(timezone.utc) - _tick_guard.STALE_AFTER - after) < timedelta(minutes=1)
    assert call["created_at"].before is None
