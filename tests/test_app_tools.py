"""`_app_tools`: app summary, status, failure class, logs, restart and health, on a fake control plane."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import flyte.remote
import pytest
from flyteidl2.app import app_definition_pb2 as pb  # type: ignore[import-untyped]  # no py.typed marker
from mcp.server.fastmcp.exceptions import ToolError

from flyte_agent_factory import _app_tools, _mcp_redact
from flyte_agent_factory._app_logs import MAX_LINES_CAP, AppLogLine, find_revision_of
from flyte_agent_factory._app_tools import HealthInputs, classify_failure
from flyte_agent_factory._factory import DOMAIN, PROJECT
from flyte_agent_factory._mcp_redact import collect_secret_values_from_env
from tests.test_mcp_redact import POD_ENV, UNION_KEY

Status = pb.Status
SECRET_VALUE = "sk-ant-api03-SECRETSECRETSECRET"
POD_OLD = "demo-00003-deployment-6b44b97bd-gcsjq"
POD_NEW = "demo-00004-deployment-7c55c98ce-4tf6n"
NOW = datetime.now(UTC)


def make_pb(
    name: str = "demo",
    status: Any = Status.DEPLOYMENT_STATUS_ACTIVE,
    conditions: list[tuple[Any, Any, str, datetime]] = (),  # type: ignore[assignment]
    revision: int = 4,
    replicas: int = 1,
) -> Any:
    app = pb.App()
    app.metadata.id.project, app.metadata.id.domain, app.metadata.id.name = PROJECT, DOMAIN, name
    app.metadata.revision = revision
    app.spec.container.image = "registry.example.com/demo@sha256:abc123"
    app.spec.runtime_metadata.version = "v-9f8e7d"
    app.spec.autoscaling.replicas.min, app.spec.autoscaling.replicas.max = 1, 3
    env = app.spec.container.env.add()
    env.key, env.value = "API_TOKEN", SECRET_VALUE
    env = app.spec.container.env.add()
    env.key, env.value = "MODE", "prod"
    secret = app.spec.security_context.secrets.add()
    secret.key = "queue-key"
    app.status.current_replicas = replicas
    app.status.ingress.public_url = "https://demo.example.com"
    app.status.last_started_at.FromDatetime(NOW - timedelta(hours=1))
    for deployment_status, substate, message, at in conditions:
        condition = app.status.conditions.add()
        condition.deployment_status, condition.substate, condition.message = (
            deployment_status,
            substate,
            message,
        )
        condition.last_transition_time.FromDatetime(at)
    if not conditions:
        condition = app.status.conditions.add()
        condition.deployment_status = status
    return app


class FakeApp:
    """The part of `flyte.remote.App` the tools use, over a proto and a shared fake control plane."""

    def __init__(self, plane: FakePlane, proto: Any):
        self.plane, self.pb2 = plane, proto

    @property
    def deployment_status(self) -> Any:
        return self.pb2.status.conditions[-1].deployment_status

    def is_active(self) -> bool:
        return self.deployment_status in (Status.DEPLOYMENT_STATUS_ACTIVE, Status.DEPLOYMENT_STATUS_STARTED)

    def is_deactivated(self) -> bool:
        return self.deployment_status in (
            Status.DEPLOYMENT_STATUS_STOPPED,
            Status.DEPLOYMENT_STATUS_UNASSIGNED,
        )

    @property
    def deactivate(self) -> Any:
        return SimpleNamespace(aio=self.plane.deactivate)

    @property
    def activate(self) -> Any:
        return SimpleNamespace(aio=self.plane.activate)


class FakePlane:
    """One app that takes `lag` reads to reach a requested state; counts every call."""

    def __init__(self, proto: Any, lag: int = 1):
        self.proto, self.lag = proto, lag
        self.calls: list[str] = []
        self.pending: tuple[Any, int] | None = None

    async def get(self, name: str) -> FakeApp:
        self.calls.append(f"get:{name}")
        if self.pending:
            target, reads = self.pending
            if reads <= 0:
                self.proto.status.conditions[-1].deployment_status = target
                self.proto.metadata.revision += target == Status.DEPLOYMENT_STATUS_ACTIVE
                self.pending = None
            else:
                self.pending = (target, reads - 1)
        return FakeApp(self, self.proto)

    async def deactivate(self, wait: bool = False) -> None:
        self.calls.append(f"deactivate wait={wait}")
        self.pending = (Status.DEPLOYMENT_STATUS_STOPPED, self.lag)

    async def activate(self, wait: bool = False) -> None:
        self.calls.append(f"activate wait={wait}")
        self.pending = (Status.DEPLOYMENT_STATUS_ACTIVE, self.lag)

    async def delete(self) -> None:  # the restart must never get here
        self.calls.append("delete")


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> FakePlane:
    fake = FakePlane(make_pb())
    monkeypatch.setattr(_app_tools, "_get_app", fake.get)
    monkeypatch.setattr(_app_tools, "POLL_INTERVAL_S", 0)
    monkeypatch.delenv(_app_tools.OWN_APP_ENV, raising=False)
    return fake


class FakeLogs:
    def __init__(self, lines: list[AppLogLine]):
        self.lines = lines

    async def stream(
        self, app: str, source: Literal["live", "persisted"], replica: str | None
    ) -> AsyncIterator[AppLogLine]:
        for item in self.lines:
            if item.source == source:
                yield item


def log(
    replica: str, message: str, ago: timedelta, source: str = "persisted", system: bool = False
) -> AppLogLine:
    return AppLogLine(
        NOW - ago,
        replica,
        find_revision_of(replica),
        source,  # type: ignore[arg-type]
        "system" if system else "user",
        message,
    )


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[AppLogLine]:
    lines: list[AppLogLine] = []
    monkeypatch.setattr(_app_tools, "_open_log_source", lambda ctx: FakeLogs(lines))
    return lines


# --- classification (criterion 4) ---


@pytest.mark.parametrize(
    ("substate", "message", "expected"),
    [
        ("CRASH_LOOP", 'Container "demo" is in CrashLoopBackOff (exit code 1)', "crash"),
        ("RUNNING", "back-off restarting failed container, exit code 2", "crash"),
        ("OOM_KILLED", 'Container "demo" was OOMKilled (exit code 137, restart count 5)', "oom"),
        ("UNKNOWN", "OOMKilled", "oom"),
        (
            "RUNNING",
            'Readiness probe failed: Get "http://10.0.0.1:8012/": context deadline exceeded',
            "probe_failed",
        ),
        ("RUNNING", "Liveness probe failed: HTTP probe failed with statuscode: 500", "probe_failed"),
        ("IMAGE_PULL_ERROR", "ErrImagePull: pull access denied", "image_pull"),
        ("PULLING_IMAGE", "Back-off pulling image: ImagePullBackOff", "image_pull"),
        ("RUNNING", "Container demo terminated (exit code 137)", "oom"),
        ("RUNNING", "Back-off restarting failed container demo in pod demo-0", "crash"),
        ("PULLING_IMAGE", "ErrImageNeverPull: image not present with pull policy Never", "image_pull"),
        ("PULLING_IMAGE", "InvalidImageName: couldn't parse image reference", "image_pull"),
        ("PULLING_IMAGE", 'Back-off pulling image "ghcr.io/x/y:1"', "image_pull"),
        ("SECRET_MOUNT_ERROR", "secret not found", "secret_mount"),
        ("RUNNING", "Service is ready", "unknown"),
        ("WEBHOOK_ERROR", "admission webhook denied", "unknown"),
    ],
)
def test_classify_failure(substate, message, expected):
    assert classify_failure(substate, message) == expected


def test_a_probe_failure_is_found_in_the_system_lines():
    assert classify_failure("RUNNING", "", ["Readiness probe failed: timeout"]) == "probe_failed"


@pytest.mark.parametrize(
    ("substate", "message", "expected"),
    [
        (Status.CRASH_LOOP, "CrashLoopBackOff exit code 1", "crash"),
        (Status.OOM_KILLED, "was OOMKilled", "oom"),
        (Status.IMAGE_PULL_ERROR, "ErrImagePull", "image_pull"),
        (Status.SECRET_MOUNT_ERROR, "no such secret", "secret_mount"),
        (
            Status.INITIALIZING,
            "Liveness probe failed: HTTP probe failed with statuscode: 503",
            "probe_failed",
        ),
        (Status.WEBHOOK_ERROR, "admission webhook denied the request: quota exceeded", "unknown"),
    ],
)
async def test_the_last_failure_of_a_status_is_classified_with_the_raw_message(
    plane, substate, message, expected
):
    plane.proto = make_pb(
        status=Status.DEPLOYMENT_STATUS_FAILED,
        conditions=[
            (Status.DEPLOYMENT_STATUS_STARTED, Status.RUNNING, "Service is ready", NOW - timedelta(hours=2)),
            (Status.DEPLOYMENT_STATUS_FAILED, substate, message, NOW - timedelta(minutes=5)),
            (
                Status.DEPLOYMENT_STATUS_STARTED,
                Status.RUNNING,
                "Service is ready",
                NOW - timedelta(minutes=1),
            ),
        ],
    )
    result = await _app_tools.get_app_status("demo")
    assert result["last_failure"]["class"] == expected
    assert result["last_failure"]["message"] == message
    assert result["last_failure"]["evidence"] == "condition"
    assert [c["at"] for c in result["history"]] == sorted(c["at"] for c in result["history"])
    assert len(result["history"]) == 3
    assert result["history"][0]["class"] is None


async def test_history_is_sorted_oldest_first_and_trimmed(plane):
    plane.proto = make_pb(
        conditions=[
            (Status.DEPLOYMENT_STATUS_ACTIVE, Status.RUNNING, f"c{i}", NOW - timedelta(minutes=i))
            for i in range(10)
        ]
    )
    result = await _app_tools.get_app_status("demo", history=3)
    assert [c["message"] for c in result["history"]] == ["c2", "c1", "c0"]


async def test_a_failed_probe_is_read_from_the_system_log_when_no_condition_failed(plane, logs):
    logs.append(
        log(POD_NEW, "Readiness probe failed: timeout", timedelta(minutes=5), source="live", system=True)
    )
    logs.append(log(POD_NEW, "ordinary line", timedelta(minutes=1), source="live"))
    result = await _app_tools.get_app_status("demo")
    assert result["last_failure"]["class"] == "probe_failed"
    assert result["last_failure"]["evidence"] == "logs"
    assert result["last_failure"]["replica"] == POD_NEW


async def test_no_failure_is_null(plane, logs):
    result = await _app_tools.get_app_status("demo")
    assert result["last_failure"] is None


# --- summary: image and version (criterion 5), no env values (criterion 12) ---


async def test_the_summary_has_the_image_and_the_version(plane):
    result = await _app_tools.get_app("demo")
    assert result["image"] == "registry.example.com/demo@sha256:abc123"
    assert result["version"] == "v-9f8e7d"
    assert result["revision"] == 4
    assert result["replicas"] == {"current": 1, "min": 1, "max": 3}
    assert result["env_names"] == ["API_TOKEN", "MODE"]
    assert result["secret_names"] == ["queue-key"]
    assert result["status"] == "ACTIVE"


async def test_the_served_summary_keeps_env_and_secret_names_through_redaction(plane):
    from flyte_agent_factory import _factory_mcp

    assert _factory_mcp.app.mcp is not None
    served = str(await _factory_mcp.app.mcp.call_tool("get_app", {"name": "demo"}))
    assert "queue-key" in served  # `secret_names` names the secret; redaction must not take the name away
    assert "API_TOKEN" in served
    assert "[REDACTED]" not in served


async def test_the_served_summary_keeps_the_secret_name_the_pod_itself_names(
    plane: FakePlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    # conftest scrubs `AGENT_FACTORY_*`, so the pod's own environment is set here explicitly.
    from flyte_agent_factory import _factory_mcp

    monkeypatch.setattr(_mcp_redact, "_ENV_SECRETS", collect_secret_values_from_env(POD_ENV))
    assert _factory_mcp.app.mcp is not None
    served = str(await _factory_mcp.app.mcp.call_tool("get_app", {"name": "demo"}))
    assert "queue-key" in served
    assert UNION_KEY not in served


async def test_a_secret_mount_failure_names_the_secret_through_the_served_layer(
    plane: FakePlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flyte_agent_factory import _factory_mcp

    monkeypatch.setattr(_mcp_redact, "_ENV_SECRETS", collect_secret_values_from_env(POD_ENV))
    plane.proto = make_pb(
        conditions=[
            (Status.DEPLOYMENT_STATUS_ACTIVE, Status.SECRET_MOUNT_ERROR, 'secret "queue-key" not found', NOW)
        ]
    )
    assert _factory_mcp.app.mcp is not None
    served = str(await _factory_mcp.app.mcp.call_tool("get_app_status", {"name": "demo"}))
    assert "secret_mount" in served
    assert "queue-key" in served  # the condition message names the missing secret: that is the diagnosis


async def test_no_tool_result_carries_an_environment_value(plane, logs):
    inputs = HealthInputs()
    results = [
        await _app_tools.get_app("demo"),
        await _app_tools.get_app_status("demo"),
        await _app_tools.get_app_logs("demo"),
        await _app_tools.restart_app("demo"),
        await _app_tools.activate_app("demo"),
        await _app_tools.deactivate_app("demo"),
        await _app_tools.make_factory_mcp_health(inputs)(),
    ]
    for result in results:
        assert SECRET_VALUE not in json.dumps(result, default=str)
        assert "prod" not in json.dumps(result["summary"] if "summary" in result else {}, default=str)


# --- restart (criterion 6) ---


async def test_restart_deactivates_then_activates_and_never_deletes(plane):
    steps: list[str] = []
    result = await _app_tools.restart_app("demo", on_step=steps.append)
    kinds = [c.split(" ")[0].split(":")[0] for c in plane.calls]
    assert "delete" not in kinds
    assert kinds.index("deactivate") < kinds.index("activate")
    assert plane.calls.count("deactivate wait=False") == 1
    assert plane.calls.count("activate wait=False") == 1
    assert result["outcome"] == "restarted"
    assert result["acted"] == ["deactivate requested", "stopped", "activate requested", "active"]
    assert steps == result["acted"]
    assert result["revision_before"] == 4
    assert result["revision_after"] == 5
    assert result["replicas_before"] == 1
    assert result["requested_at"].endswith("Z")
    assert result["summary"]["status"] == "ACTIVE"


async def test_an_async_on_step_is_awaited(plane):
    seen: list[str] = []

    async def on_step(message: str) -> None:
        seen.append(message)

    await _app_tools.restart_app("demo", on_step=on_step)
    assert seen[0] == "deactivate requested"


async def test_restart_of_the_servers_own_app_is_refused_before_any_remote_call(plane, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "mcp-under-test")
    for tool in (_app_tools.restart_app, _app_tools.deactivate_app):
        with pytest.raises(ToolError, match="own app"):
            await tool("mcp-under-test")
    assert plane.calls == []
    assert (await _app_tools.restart_app("demo"))["outcome"] == "restarted"


async def test_the_production_name_is_the_default_own_app(plane):
    with pytest.raises(ToolError, match="own app"):
        await _app_tools.restart_app("factory-mcp")
    assert plane.calls == []


async def test_activating_the_own_app_is_allowed(plane, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    plane.proto = make_pb(status=Status.DEPLOYMENT_STATUS_STOPPED)
    assert (await _app_tools.activate_app("demo"))["outcome"] == "done"


async def test_a_restart_whose_stop_is_not_seen_still_asks_for_the_app_back(plane, monkeypatch):
    plane.lag = 10**6
    monkeypatch.setattr(_app_tools, "APP_TRANSITION_CAP_S", 0.05)
    monkeypatch.setattr(_app_tools, "POLL_INTERVAL_S", 0.01)
    result = await _app_tools.restart_app("demo")
    assert result["outcome"] == "restart_unconfirmed"
    assert result["acted"] == [
        "deactivate requested",
        "stop not seen in time",
        "activate requested",
        "active",
    ]
    assert "activate wait=False" in plane.calls
    assert "get_app_status" in result["next"]


async def test_a_deactivate_that_never_finishes_times_out_and_does_not_activate(plane, monkeypatch):
    plane.lag = 10**6
    monkeypatch.setattr(_app_tools, "APP_TRANSITION_CAP_S", 0.05)
    monkeypatch.setattr(_app_tools, "POLL_INTERVAL_S", 0.01)
    result = await _app_tools.deactivate_app("demo")
    assert result["outcome"] == "timed_out"
    assert result["acted"] == ["deactivate requested", "stop not seen in time"]
    assert "activate wait=False" not in plane.calls


async def test_a_failing_deactivate_is_reported_not_raised(plane):
    async def broken(wait: bool = False) -> None:
        raise RuntimeError("control plane down")

    plane.deactivate = broken
    result = await _app_tools.deactivate_app("demo")
    assert result["outcome"] == "failed"
    assert "RuntimeError: control plane down" in result["error"]
    assert result["acted"] == []


# --- scope (criterion 9) ---


@pytest.mark.parametrize(
    "call",
    [
        lambda: _app_tools.get_app("demo", project="other"),
        lambda: _app_tools.get_app_status("demo", domain="production"),
        lambda: _app_tools.get_app_logs("demo", project="other"),
        lambda: _app_tools.restart_app("demo", project="other"),
        lambda: _app_tools.activate_app("demo", domain="production"),
        lambda: _app_tools.deactivate_app("demo", project="other"),
    ],
)
async def test_another_project_or_domain_is_refused_before_any_remote_call(plane, logs, call):
    with pytest.raises(ToolError, match="not allowed"):
        await call()
    assert plane.calls == []


async def test_the_factorys_own_project_and_domain_are_accepted(plane):
    assert (await _app_tools.get_app("demo", project=PROJECT, domain=DOMAIN))["name"] == "demo"
    assert plane.calls == ["get:demo"]


# --- logs tool ---


async def test_the_log_tool_returns_attributed_lines_and_parses_the_window(plane, logs):
    logs.extend(
        [
            log(POD_OLD, "old", timedelta(hours=5)),
            log(POD_OLD, "mid", timedelta(minutes=30)),
            log(POD_NEW, "new", timedelta(minutes=2)),
        ]
    )
    result = await _app_tools.get_app_logs("demo", max_lines=200, since="1h", source="persisted")
    assert [(e["message"], e["replica"], e["revision"]) for e in result["lines"]] == [
        ("mid", POD_OLD, "demo-00003"),
        ("new", POD_NEW, "demo-00004"),
    ]
    assert result["project"] == PROJECT
    assert result["domain"] == DOMAIN
    by_revision = await _app_tools.get_app_logs("demo", revision="demo-00004", source="persisted")
    assert [e["message"] for e in by_revision["lines"]] == ["new"]


async def test_the_log_tool_rejects_a_bad_source_or_time(plane, logs):
    with pytest.raises(ToolError, match="source"):
        await _app_tools.get_app_logs("demo", source="tail")
    with pytest.raises(ToolError, match="not a time"):
        await _app_tools.get_app_logs("demo", since="soonish")


async def test_logs_without_a_cluster_endpoint_name_the_missing_variable(plane, monkeypatch):
    monkeypatch.delenv("_U_EP_OVERRIDE", raising=False)
    result = await _app_tools.get_app_logs("demo")
    assert result["lines"] == []
    assert "_U_EP_OVERRIDE" in result["gaps"][0]


def test_the_callers_headers_are_forwarded_and_nothing_else():
    headers = {"authorization": "Bearer t", "cookie": "c=1", "x-other": "no"}
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))
    assert _app_tools._build_caller_headers(ctx) == {"authorization": "Bearer t", "cookie": "c=1"}
    assert _app_tools._build_caller_headers(None) == {}


# --- health (criterion 19) ---


async def test_health_has_revision_uptime_restarts_and_errors(plane, logs, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    plane.proto = make_pb(
        conditions=[
            (
                Status.DEPLOYMENT_STATUS_FAILED,
                Status.OOM_KILLED,
                "Container was OOMKilled (exit code 137)",
                NOW - timedelta(hours=3),
            ),
            (Status.DEPLOYMENT_STATUS_ACTIVE, Status.RUNNING, "Service is ready", NOW - timedelta(hours=2)),
        ]
    )
    logs.extend(
        [
            log(POD_OLD, "INFO fine", timedelta(hours=3, minutes=1)),
            log(POD_NEW, "INFO fine", timedelta(minutes=30)),
            log(POD_NEW, "ERROR something broke", timedelta(minutes=20)),
            log(POD_NEW, "Traceback (most recent call last):", timedelta(minutes=19)),
            log(POD_NEW, "INFO fine", timedelta(minutes=10), source="live"),
        ]
    )
    health = _app_tools.make_factory_mcp_health(
        HealthInputs(
            calls=lambda: {"count": 140, "still_working": 2, "p95_ms": 8100, "tool_errors": 1},
            loop_lag=lambda: {"max_5m": 12, "p95_5m": 3},
            operations_in_flight=lambda: 0,
            budget_s=200,
        )
    )
    result = await health()
    assert result["revision"] == "demo-00004"
    assert result["replica"] == POD_NEW
    assert result["uptime_s"] >= 0
    assert result["started_at"].endswith("Z")
    assert result["restarts"]["count_24h"] == 1
    assert result["restarts"]["recent"][0]["replica"] == POD_OLD
    assert result["restarts"]["recent"][0]["class"] == "oom"
    assert "OOMKilled" in result["restarts"]["recent"][0]["reason"]
    assert result["errors_last_hour"]["count"] == 3  # two log lines and one tool error
    assert result["errors_last_hour"]["tool_errors"] == 1
    assert result["errors_last_hour"]["samples"] == [
        "ERROR something broke",
        "Traceback (most recent call last):",
    ]
    assert result["calls_last_hour"] == {"count": 140, "still_working": 2, "p95_ms": 8100}
    assert result["loop_lag_ms"] == {"max_5m": 12, "p95_5m": 3}
    assert result["budget_s"] == 200
    assert result["process"]["cpu_s"] >= 0
    assert result["image"].startswith("registry.example.com/demo@")


async def test_health_reports_the_dropped_requests_of_the_last_hour(plane, logs, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    logs.extend([log(POD_NEW, "INFO hi", timedelta(minutes=5))])
    calls = {"count": 9, "still_working": 0, "dropped": 2, "p95_ms": 900, "tool_errors": 0}
    result = await _app_tools.make_factory_mcp_health(HealthInputs(calls=lambda: calls))()
    assert result["calls_last_hour"]["dropped"] == 2


async def test_a_replaced_replica_without_a_condition_is_still_a_restart(plane, logs, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    logs.extend([log(POD_OLD, "INFO bye", timedelta(hours=2)), log(POD_NEW, "INFO hi", timedelta(hours=1))])
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert result["restarts"]["count_24h"] == 1
    assert result["restarts"]["recent"][0]["reason"] == "replaced (no condition)"


async def test_a_failure_condition_recorded_twice_is_one_restart(plane, logs, monkeypatch):
    # Seen live: Union kept two identical OOM_KILLED conditions for one kill (same time, same message), and
    # health counted it as two restarts.
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    oom = (
        Status.DEPLOYMENT_STATUS_PENDING,
        Status.OOM_KILLED,
        "Container exited with code 137 (out of memory)",
    )
    plane.proto = make_pb(
        conditions=[
            (*oom, NOW - timedelta(hours=3)),
            (*oom, NOW - timedelta(hours=3)),
            (*oom, NOW - timedelta(hours=1)),
        ]
    )
    logs.append(log(POD_NEW, "INFO hi", timedelta(minutes=30)))
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert result["restarts"]["count_24h"] == 2
    assert [r["class"] for r in result["restarts"]["recent"]] == ["oom", "oom"]


async def test_health_says_when_the_kept_conditions_do_not_reach_back_24_hours(plane, logs, monkeypatch):
    # Seen live: 40 conditions covered 16 hours, and the 24 h restart count fell as old OOMs aged out of them.
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    ready = (Status.DEPLOYMENT_STATUS_ACTIVE, Status.RUNNING, "Service is ready")
    plane.proto = make_pb(conditions=[(*ready, NOW - timedelta(minutes=10 * i)) for i in range(40)])
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert any("restarts before" in gap and "not counted" in gap for gap in result["gaps"])

    plane.proto = make_pb(conditions=[(*ready, NOW - timedelta(hours=30))] * 40)
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert not any("restarts before" in gap for gap in result["gaps"])


async def test_health_uses_the_start_clock_it_is_given(plane, logs, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    started = datetime.now(UTC) - timedelta(hours=2)
    result = await _app_tools.make_factory_mcp_health(HealthInputs(started_at=started))()
    assert result["started_at"] == started.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert 7190 <= result["uptime_s"] <= 7300


async def test_health_says_when_the_hour_read_was_cut(plane, logs, monkeypatch):
    # The hour holds 10 errors, then 1500 quiet lines: the newest 1000 lines are all INFO, so the errors are
    # not counted and the result has to say so.
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    logs.extend(
        log(POD_NEW, f"ERROR early {i}", timedelta(minutes=59, seconds=50 - i), "live") for i in range(10)
    )
    logs.extend(
        log(POD_NEW, f"INFO quiet {i}", timedelta(minutes=50) - timedelta(seconds=i), "live")
        for i in range(1500)
    )
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert result["errors_last_hour"]["count"] == 0
    cap = MAX_LINES_CAP
    assert any(
        f"errors_last_hour is counted over the newest {cap} lines only" in gap and "not counted" in gap
        for gap in result["gaps"]
    )


class HangingLiveLogs(FakeLogs):
    """A live stream as Union serves it: its lines, then silence that never ends."""

    async def stream(
        self, app: str, source: Literal["live", "persisted"], replica: str | None
    ) -> AsyncIterator[AppLogLine]:
        async for item in super().stream(app, source, replica):
            yield item
        if source == "live":
            await asyncio.sleep(3600)


def _hanging_reads(monkeypatch, lines, **options):
    monkeypatch.setattr(_app_tools, "_open_log_source", lambda ctx: HangingLiveLogs(lines))
    real = _app_tools._read_logs

    async def bounded(ctx, name, **kwargs):
        return await real(ctx, name, **kwargs, **options)

    monkeypatch.setattr(_app_tools, "_read_logs", bounded)


async def test_health_reports_no_gap_when_the_live_hour_read_merely_goes_idle(plane, monkeypatch):
    # The live stream never ends; going quiet after its lines is the normal stop and loses no errors.
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    lines = [
        log(POD_NEW, "ERROR one", timedelta(minutes=30), "live"),
        log(POD_NEW, "INFO two", timedelta(minutes=20), "live"),
    ]
    _hanging_reads(monkeypatch, lines, idle_s=0.05, first_line_s=0.05)
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert result["errors_last_hour"]["count"] == 1
    assert result["gaps"] == []


async def test_health_says_when_the_hour_read_hit_its_deadline(plane, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    _hanging_reads(monkeypatch, [], deadline_s=0.05, first_line_s=5, idle_s=5)
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert any("hit its deadline" in gap and "not counted" in gap for gap in result["gaps"])
    assert not any("lines only" in gap for gap in result["gaps"])


async def test_health_without_logs_reports_the_gap_instead_of_failing(plane, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.delenv("_U_EP_OVERRIDE", raising=False)
    result = await _app_tools.make_factory_mcp_health(HealthInputs())()
    assert any("_U_EP_OVERRIDE" in gap for gap in result["gaps"])
    assert result["errors_last_hour"]["count"] == 0


def test_the_module_imports_nothing_private_and_neither_neighbour_chunk():
    """Nothing private from flyte, no `_mcp_budget`, and from `_mcp_redact` only the redact-then-cut helper.

    A message cut to a cap has to be redacted before the cut (a cut secret matches no rule), so that one
    function is the only redaction this module does; every other result is redacted by `_serve`.
    """
    tree = ast.parse(Path(_app_tools.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            for name in names:
                parts = name.split(".")
                assert not (parts[0] == "flyte" and any(p.startswith("_") for p in parts[1:])), name
                assert "_mcp_budget" not in name
                if "_mcp_redact" in name:
                    assert isinstance(node, ast.ImportFrom), name
                    assert [a.name for a in node.names] == ["shorten_after_redaction"], name
        if isinstance(node, ast.ImportFrom) and node.module is None:
            assert not {a.name for a in node.names} & {"_mcp_budget", "_mcp_redact"}


async def test_an_app_is_looked_up_only_in_the_factory_project_and_domain(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_get(name: str, **kwargs: Any) -> str:
        seen.append((name, kwargs))
        return "app"

    monkeypatch.setattr(flyte.remote, "App", SimpleNamespace(get=SimpleNamespace(aio=fake_get)))
    assert await _app_tools._get_app("demo") == "app"
    assert seen == [("demo", {"project": PROJECT, "domain": DOMAIN})]


def test_process_usage_reads_the_container_limits_throttling_and_oom_kills(tmp_path: Path) -> None:
    (tmp_path / "memory.current").write_text("209715200\n")
    (tmp_path / "memory.peak").write_text("314572800\n")
    (tmp_path / "memory.max").write_text("536870912\n")
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 9000000\nnr_periods 100\nnr_throttled 40\nthrottled_usec 2500000\n"
    )
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 2\noom_kill 1\n")

    usage = _app_tools.read_process_usage(tmp_path)

    assert usage["memory_current_mb"] == 200.0
    assert usage["memory_peak_mb"] == 300.0
    assert usage["memory_max_mb"] == 512.0
    assert usage["cpu_throttled_s"] == 2.5
    assert usage["cpu_throttled_periods"] == 40
    assert usage["cpu_limit"] == 0.5
    assert usage["oom_kills"] == 1
    assert usage["cpu_s"] >= 0
    assert usage["rss_mb"] > 0


def test_process_usage_without_a_cgroup_or_limits_leaves_those_fields_out(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "cpu.max").write_text("max 100000\n")

    usage = _app_tools.read_process_usage(tmp_path)

    assert "memory_max_mb" not in usage
    assert usage["cpu_limit"] is None
    assert not {"memory_current_mb", "cpu_throttled_s", "oom_kills"} & set(usage)
    assert _app_tools.read_process_usage(tmp_path / "absent").keys() <= {"cpu_s", "rss_mb"}


# A secret that straddles a cap: cutting before redaction would return all but its last characters.
_AWS_ID_CUT = "AKIA" + "IOSFODNN7EXAMPLQ"
_AWS_KEY_CUT = "wJalrXUtnFEMI" + "/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"


async def test_a_health_sample_cut_at_the_cap_holds_no_part_of_a_secret(plane, logs, monkeypatch):
    monkeypatch.setenv(_app_tools.OWN_APP_ENV, "demo")
    monkeypatch.setenv("HOSTNAME", POD_NEW)
    msg = "ERROR " + "y" * 233 + " " + _AWS_ID_CUT + " " + _AWS_KEY_CUT + " tail"
    logs.append(log(POD_NEW, msg, timedelta(minutes=5), source="live"))
    health = _app_tools.make_factory_mcp_health(HealthInputs())
    sample = (await health())["errors_last_hour"]["samples"][0]
    assert _AWS_KEY_CUT[:20] not in sample
    assert _AWS_ID_CUT not in sample
    assert sample.startswith("ERROR " + "y" * 233)


async def test_a_condition_message_over_the_cap_is_redacted_before_it_is_cut(plane):
    cap = _app_tools._MESSAGE_CAP
    message = "x" * (cap - 30) + " " + _AWS_ID_CUT + " " + _AWS_KEY_CUT + " more"
    plane.proto = make_pb(
        status=Status.DEPLOYMENT_STATUS_FAILED,
        conditions=[(Status.DEPLOYMENT_STATUS_FAILED, Status.RUNNING, message, NOW - timedelta(minutes=5))],
    )
    result = await _app_tools.get_app_status("demo")
    for text in (result["last_failure"]["message"], result["history"][-1]["message"]):
        assert _AWS_KEY_CUT[:20] not in text
        assert _AWS_ID_CUT not in text
        assert text.startswith("x" * (cap - 30))
