import ast
import importlib.metadata
import inspect
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import pytest
from flyte._initialize import _get_init_config as real_get_init_config
from flyte.ai.mcp import ALL_MCP_TOOLS, FlyteMCPAppEnvironment
from flyte.models import ActionPhase
from mcp.server.fastmcp.exceptions import ToolError

from flyte_agent_factory import _factory, _factory_base, _git
from flyte_agent_factory import _factory_mcp as factory

ROOT = Path(factory.__file__).resolve().parents[2]


def test_is_the_stock_flyte_mcp_app_behind_union_auth():
    # Union's ingress authenticates each request and Flyte runs each call as the caller, so a
    # sign-in token and a Union API key both work as the bearer; nothing here checks tokens.
    # A subclass only to serve the grant page; every tool and setting is the stock environment's.
    assert type(factory.app) is factory.FactoryMCPAppEnvironment
    assert isinstance(factory.app, FlyteMCPAppEnvironment)
    assert factory.app.requires_auth is True
    # No token of its own: its one secret is the queue tier's Union key, so it acts as that key.
    assert [(s.key, s.as_env_var) for s in cast(list[flyte.Secret], factory.app.secrets)] == [
        (_factory_base.PROFILE.union.key_for("queue"), "AGENT_FACTORY_UNION_KEY")
    ]


def test_chat_can_read_a_tasks_result():
    assert "get_run_io" in factory.TOOLS and "get_run_io" in factory.app.enabled_tools


def test_the_sweep_runs_every_five_minutes_on_spot():
    [trigger] = _factory.sweep.triggers
    assert trigger.name == "queue_sweep"
    assert isinstance(trigger.automation, flyte.FixedRate)
    assert trigger.automation.interval_minutes == 5
    assert _factory.sweep.interruptible is True


def test_scope_defaults_to_the_factory_project():
    assert factory.app.env_vars is not None
    assert factory.app.env_vars["FLYTE_MCP_PROJECT"] == _factory.PROJECT
    assert factory.app.env_vars["FLYTE_MCP_DOMAIN"] == _factory.DOMAIN


def test_instructions_and_skill_describe_launching_through_the_queue():
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "factory.launch" in text and "factory.queue_list" in text and "factory.queue_cancel" in text
        assert "get_run_io" in text
        assert "factory.status" in text
        assert "signal_condition" in text
        assert "launch_project" not in text
    for url in factory.REPOS.values():
        assert url in factory.INSTRUCTIONS


def test_instructions_have_operator_chats_decide_engineering_asks():
    text = " ".join(factory.INSTRUCTIONS.split())
    assert "Decide an engineering-judgement ask- yourself" in text
    assert "what is the right thing to do, never a waiver" in text
    assert 'starts "Operator decision (engineering judgement): "' in text
    assert "tell the user what you decided" in text
    assert "Relay only owner-reserved asks" in text
    assert "one at a time" in text and "signal_condition their answer verbatim" in text
    assert "decisions, with reasoning" in text
    assert "overrule decision-003" in text


def test_operator_relay_list_carries_the_whole_floor_and_profile_extras(monkeypatch):
    """The relay sentence had silently dropped two floor categories; it renders the one list now."""
    from flyte_agent_factory import _profile, _reserved

    text = " ".join(factory.INSTRUCTIONS.split())
    for entry in _reserved.OWNER_RESERVED_FLOOR:
        assert " ".join(entry.split()) in text
    extra = "changes to the public API of the billing service"
    assert extra not in text
    source = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()
    profile = _profile.parse(source + f'\n[judgement]\nowner_reserved = ["{extra}"]\n')
    monkeypatch.setattr(factory, "PROFILE", profile)
    assert extra in factory._relay_list()


def test_chat_is_not_told_to_pick_a_slot():
    # Replaces test_instructions_count_every_live_phase_as_a_busy_slot, which pinned the instruction to
    # check each slot with list_runs before launching; a launch now queues when the slots are full.
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "run_task on a free slot" not in text
        for slot in _factory_base.SLOTS:
            assert slot not in text
        assert "never fail" in text
        assert "same name" in text or "same `name`" in text


def test_instructions_pass_the_launch_model_explicitly():
    # The slot task's own default is the author model, not the launch model.
    from flyte_agent_factory._policy import LAUNCH_MODEL

    assert f"always pass it: {LAUNCH_MODEL}" in factory.INSTRUCTIONS
    assert "always pass it" in (ROOT / "skills/agent-factory/SKILL.md").read_text()


def test_instructions_say_where_issue_comments_are_posted():
    # Rewritten in state step 1 (the design's gating change): status comments for a linked issue are
    # posted wherever the App is configured, no longer only where factory-issues drained them.
    assert "status comments are posted on it\nwherever the factory's GitHub App is configured" in (
        factory.INSTRUCTIONS
    )


def test_instructions_and_skill_describe_benchmark_inputs_and_spend_limits():
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "factory-bench.benchmark" in text
        for name in (
            "candidate_sha",
            "baseline_sha",
            "tasks",
            "repeats",
            "candidate_policy",
            "baseline_policy",
            "reuse_baseline_days",
            "max_usd",
        ):
            assert name in text
        assert "estimate" in text and "spends nothing" in text
        assert "ceiling_usd" in text and "[bench] max_usd" in text


@pytest.fixture
def chat_benchmark(monkeypatch):
    from flyte_agent_factory import _bench_flow
    from tests.test_bench import profile

    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="chat-bench")))

    def unexpected_spend(*args, **kwargs):
        pytest.fail("validation and estimates must not deploy, launch cases or publish results")

    monkeypatch.setattr(_bench_flow, "deploy_engine", unexpected_spend)
    monkeypatch.setattr(_bench_flow, "run_case", unexpected_spend)
    monkeypatch.setattr(_bench_flow, "_store", unexpected_spend)
    return _bench_flow.benchmark.func


@pytest.mark.parametrize(
    "inputs, error",
    [
        ({"candidate_sha": "z" * 40}, "not a 40-character commit SHA"),
        ({"baseline_sha": "z" * 40}, "not a 40-character commit SHA"),
        ({"tasks": ["unknown-task"]}, "no task 'unknown-task'"),
        ({"repeats": 4}, "repeats must be 1 to 3"),
        ({"repeats": 0}, "repeats must be 1 to 3"),
        ({"max_usd": 39}, r"can spend up to \$40.*over max_usd 39"),
        ({"baseline_sha": "f" * 40, "max_usd": 79}, r"can spend up to \$80.*over max_usd 79"),
        ({"max_usd": 101}, "over the profile's"),
        ({"max_usd": -1}, "max_usd must be 0"),
        ({"max_usd": float("inf")}, "must be finite"),
        ({"max_usd": float("nan")}, "must be finite"),
        ({"candidate_policy": "[]"}, "policy must be a JSON object"),
        ({"baseline_sha": "f" * 40, "baseline_policy": "[]"}, "policy must be a JSON object"),
    ],
)
async def test_chat_benchmark_refuses_invalid_inputs_before_spending(chat_benchmark, inputs, error):
    args = {"candidate_sha": "a" * 40, "tasks": ["bugfix-shortlinks"], "repeats": 1, "max_usd": 40}
    with pytest.raises(ValueError, match=error):
        await chat_benchmark(**(args | inputs))


@pytest.mark.parametrize("baseline_sha, cases, ceiling", [("", 1, 40), ("f" * 40, 2, 80)])
async def test_chat_benchmark_estimate_spends_nothing(chat_benchmark, baseline_sha, cases, ceiling):
    summary, jsonl = await chat_benchmark(
        candidate_sha="a" * 40,
        baseline_sha=baseline_sha,
        tasks=["bugfix-shortlinks"],
        repeats=1,
        max_usd=0,
    )
    result = json.loads(summary)
    assert result["estimate_only"] is True
    assert result["engine_sha"] == "a" * 40
    assert result["estimate"]["cases"] == cases
    assert result["estimate"]["ceiling_usd"] == ceiling
    assert result["estimate"]["max_usd"] == 0
    assert jsonl == ""


def _phase_text(phase: Any) -> str:
    return str(getattr(phase, "value", phase)).lower()


class LaggingRun:
    """A run whose read model reports it done only after `lag` reads, as Union's does after SUCCEEDED."""

    def __init__(
        self, lag: int, phase: Any = ActionPhase.SUCCEEDED, name: str = "r1", stale_phase: Any = None
    ) -> None:
        """Start a run that reads as done only after `lag` reads; `stale_phase` shows until then."""
        self.reads, self.lag, self.name, self.url = 0, lag, name, "http://run"
        self.final_phase, self.stale_phase = phase, stale_phase

    @property
    def phase(self):
        """The phase the read model shows: stale until the lag has passed, then the final one."""
        # The read model can still show an earlier phase for a while after the run has SUCCEEDED.
        if self.stale_phase is not None and self.reads <= self.lag:
            return self.stale_phase
        return self.final_phase

    def done(self) -> bool:
        """Whether the run reads as done: finished and past the lag."""
        return _phase_text(self.final_phase) != "running" and self.reads > self.lag

    async def _inputs(self):
        return {"name": "x"}

    @property
    def inputs(self):
        """The run's inputs, as the remote `Run.inputs` exposes them."""
        return SimpleNamespace(aio=self._inputs)

    async def _outputs(self):
        return SimpleNamespace(named_outputs={"o0": '{"status": "started"}'})

    @property
    def outputs(self):
        """The run's outputs, as the remote `Run.outputs` exposes them."""
        return SimpleNamespace(aio=self._outputs)


@pytest.fixture
def lagging(monkeypatch):
    """Make `Run.get` return a `LaggingRun`, and wait_for_run's watch return at once."""
    import flyte.remote

    def install(run):
        async def get(name):
            run.reads += 1
            return SimpleNamespace(
                name=run.name,
                url=run.url,
                phase=run.phase,
                done=run.done,
                inputs=run.inputs,
                outputs=run.outputs,
                wait=SimpleNamespace(aio=lambda quiet=True: _done()),
            )

        async def _done() -> None:
            return None

        monkeypatch.setattr(flyte.remote.Run, "get", SimpleNamespace(aio=get))
        return run

    import flyte._initialize

    # `_scoped` only needs an init config whose scope already matches; there is no cluster to reach.
    scope = SimpleNamespace(project=_factory.PROJECT, domain=_factory.DOMAIN)
    monkeypatch.setattr(flyte._initialize, "_get_init_config", lambda: scope)
    monkeypatch.setenv("FLYTE_MCP_PROJECT", _factory.PROJECT)
    monkeypatch.setenv("FLYTE_MCP_DOMAIN", _factory.DOMAIN)
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 2.0)
    monkeypatch.setattr(factory, "_POLL_FIRST_S", 0.01)
    monkeypatch.setattr(factory, "_POLL_MAX_S", 0.05)
    return install


async def call(tool: str, **arguments: Any) -> Any:
    """Call `tool` on the factory's MCP app and return its JSON result."""
    assert factory.app.mcp is not None
    [content] = await factory.app.mcp.call_tool(tool, arguments)
    return json.loads(cast(Any, content).text)  # call_tool is typed as any content block; ours are text


async def test_get_run_io_waits_for_outputs_the_read_model_has_not_published(lagging):
    """`get_run_io` waits for outputs the read model has not yet published."""
    run = lagging(LaggingRun(lag=6))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_get_run_io_says_so_when_the_outputs_never_become_readable(lagging, monkeypatch):
    """`get_run_io` says so when the outputs never become readable."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None
    assert "not yet readable" in result["outputs_error"] and "Retry get_run_io" in result["outputs_error"]


@pytest.mark.parametrize("phase", [ActionPhase.FAILED, ActionPhase.ABORTED, ActionPhase.TIMED_OUT])
async def test_get_run_io_does_not_wait_for_a_run_that_has_no_outputs_coming(lagging, phase):
    """`get_run_io` does not wait for a run that has no outputs coming."""
    run = lagging(LaggingRun(lag=10**9, phase=phase))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None and "outputs_error" not in result
    assert run.reads <= 4


async def test_get_run_io_waits_while_the_read_model_still_shows_a_running_phase(lagging):
    """`get_run_io` keeps waiting while the lagging read model still shows a running phase."""
    # The stock get_run reads the same lagging model, so during the lag it says "running".
    run = lagging(LaggingRun(lag=6, stale_phase=ActionPhase.RUNNING))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_get_run_io_never_returns_a_bare_null_during_the_lag(lagging, monkeypatch):
    """`get_run_io` never returns a bare null during the lag."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9, stale_phase=ActionPhase.RUNNING))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None
    assert "not yet readable" in result["outputs_error"] and "Retry get_run_io" in result["outputs_error"]


@pytest.mark.parametrize("phase", [ActionPhase.SUCCEEDED, "SUCCEEDED", "succeeded", ActionPhase.RECOVERED])
async def test_get_run_io_waits_whatever_the_phase_is_spelled_as(lagging, phase):
    """`get_run_io` waits whatever way the phase is spelled."""
    run = lagging(LaggingRun(lag=6, phase=phase))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_wait_for_run_returns_once_the_outputs_are_readable(lagging):
    """`wait_for_run` returns once the outputs are readable."""
    lagging(LaggingRun(lag=6))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "succeeded" and result["outputs_ready"] is True


async def test_wait_for_run_reports_outputs_not_ready_instead_of_hanging(lagging, monkeypatch):
    """`wait_for_run` reports outputs not ready instead of hanging."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "succeeded" and result["outputs_ready"] is False


async def test_wait_for_run_leaves_a_failed_run_alone(lagging):
    """`wait_for_run` leaves a failed run alone."""
    run = lagging(LaggingRun(lag=10**9, phase=ActionPhase.FAILED))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "failed" and "outputs_ready" not in result
    assert run.reads <= 3


async def test_the_patient_tools_replace_the_stock_ones_without_changing_the_tool_set():
    """The patient tools replace the stock ones without changing the tool set."""
    assert factory.app.mcp is not None
    tools = {tool.name: tool for tool in await factory.app.mcp.list_tools()}
    assert set(tools) == set(factory.TOOLS)
    for name in ("get_run_io", "wait_for_run"):
        description = tools[name].description or ""
        assert "bounded" in description or "outputs_ready" in description
    annotations = tools["get_run_io"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True


def test_instructions_tell_chat_what_to_do_when_outputs_are_not_yet_readable():
    """The instructions tell chat what to do when outputs are not yet readable."""
    assert "not yet readable" in factory.INSTRUCTIONS and "call get_run_io again" in factory.INSTRUCTIONS


# ---- every stock tool, limited to the factory's project and domain ----

OTHER = {"project": "other-project", "domain": "other-domain"}


def _registered_tools() -> dict[str, Any]:
    assert factory.app.mcp is not None
    return {tool.name: tool for tool in factory.app.mcp._tool_manager.list_tools()}


def _scope_parameters(fn: Any) -> list[str]:
    """The parameters of `fn` whose name mentions a project or a domain (`project`, `run_domain`, ...)."""
    return [p for p in inspect.signature(fn).parameters if "project" in p or "domain" in p]


def _scoped_tool_names() -> list[str]:
    """Every registered tool that takes a project or a domain, found from the tools themselves."""
    return sorted(name for name, tool in _registered_tools().items() if _scope_parameters(tool.fn))


def _required_arguments(tool_name: str) -> dict[str, Any]:
    """Valid values for the arguments a tool cannot do without, apart from project and domain."""
    parameters = inspect.signature(_registered_tools()[tool_name].fn).parameters
    required = {
        name: p
        for name, p in parameters.items()
        if p.default is inspect.Parameter.empty and name not in ("ctx", "project", "domain")
    }
    # `run_task` and `get_task` cannot do without a project and a domain; the factory's are always valid.
    in_scope = {
        name: {"project": _factory.PROJECT, "domain": _factory.DOMAIN}[name]
        for name in ("project", "domain")
        if name in parameters and parameters[name].default is inspect.Parameter.empty
    }
    return {name: {} if name == "inputs" else "x" for name in required} | in_scope


async def call_list(tool: str, **arguments: Any) -> list[dict[str, Any]]:
    """Call a tool that returns a list; FastMCP gives those as (content blocks, {"result": the list})."""
    assert factory.app.mcp is not None
    _content, structured = cast(Any, await factory.app.mcp.call_tool(tool, arguments))
    return cast(list[dict[str, Any]], structured["result"])


def test_exposes_launching_and_following_projects_only():
    """The tools for launching and following projects are there, limited to the factory's project.

    This test also pinned the absence of the secret and trigger tools; the brief asks for every stock tool,
    so that is now `test_enables_every_stock_tool`, and "only" is the project/domain
    scope check.
    """
    assert factory.app.enabled_tools == set(factory.TOOLS)
    assert "run_task" in factory.app.enabled_tools
    assert factory.app.env_vars is not None and factory.app.env_vars["FLYTE_MCP_PROJECT"] == _factory.PROJECT
    following = {"run_task", "get_run", "get_run_io", "wait_for_run", "list_conditions"}
    assert set(_scoped_tool_names()) >= following


def test_enables_every_stock_tool():
    assert factory.app.enabled_tools == set(ALL_MCP_TOOLS)
    assert set(factory.TOOLS) == set(ALL_MCP_TOOLS)
    assert set(_registered_tools()) == set(ALL_MCP_TOOLS)
    # `ALL_MCP_TOOLS` is every tool the SDK implements: a plain stock environment registers exactly those.
    # (This compared against the private `TOOL_REGISTRY` before; the stock app raises if the two drift.)
    stock = FlyteMCPAppEnvironment(name="stock")
    assert stock.mcp is not None
    assert {tool.name for tool in stock.mcp._tool_manager.list_tools()} == set(ALL_MCP_TOOLS)


def test_the_allowlist_is_exactly_the_factory_chat_tasks_and_benchmark():
    """No task allowlist any more; the chat tasks are the ones the instructions steer to, and no slot task."""
    # The brief drops the per-task allowlist: any task registered in the factory project and domain can be
    # run, and the scope check below keeps chat inside it. What this test pinned as the allowlist is now
    # `CHAT_TASKS`, the set the instructions steer chat to, which still names no slot task.
    assert factory.app.task_allowlist is None
    assert factory.CHAT_TASKS == (
        "launch",
        "queue_list",
        "queue_cancel",
        "status",
        "set_reserve",
        "get_reserve",
        "clear_limit",
        "rotate_token",
        "browser_link",
        "credential_link",
        "credentials",
    )
    assert factory.CODEX_LOGIN_TASK == "factory-setup.codex_bootstrap_login"
    assert not any(slot in task for task in factory.CHAT_TASKS for slot in _factory_base.SLOTS)


@pytest.fixture
def remote(lagging, monkeypatch):
    """Fake the `flyte.remote` calls the tools make, and record each one in `calls`."""
    import flyte.remote

    calls: list[tuple[str, Any]] = []

    def task_get(**kwargs):
        calls.append(("Task.get", kwargs))
        return SimpleNamespace(name=kwargs["name"])

    async def run(task, **inputs):
        calls.append(("run", (task.name, inputs)))
        return SimpleNamespace(url="http://run", name="r1")

    async def project_listall(*args, **kwargs):
        calls.append(("Project.listall", None))
        for project in (_factory.PROJECT, "other-project"):
            yield SimpleNamespace(pb2=SimpleNamespace(id=project, name=project, description="", state=0))

    async def project_get(name):
        calls.append(("Project.get", name))
        raise AssertionError("get_project must refuse before asking Union")

    async def secret_listall(*args, **kwargs):
        # A secret object that carries its value, as a careless SDK change might make it.
        yield SimpleNamespace(name="claude_token", type="regular", value="hunter2-secret-value")

    async def secret_create(name, value, type="regular"):
        calls.append(("Secret.create", name))

    monkeypatch.setattr(flyte.remote.Task, "get", task_get)
    monkeypatch.setattr(flyte.run, "aio", run)
    monkeypatch.setattr(flyte.remote.Project, "listall", SimpleNamespace(aio=project_listall))
    monkeypatch.setattr(flyte.remote.Project, "get", SimpleNamespace(aio=project_get))
    monkeypatch.setattr(flyte.remote.Secret, "listall", SimpleNamespace(aio=secret_listall))
    monkeypatch.setattr(flyte.remote.Secret, "create", SimpleNamespace(aio=secret_create))
    lagging(LaggingRun(lag=0))
    return calls


@pytest.mark.parametrize(
    "project, name, allowed",
    [
        (_factory.PROJECT, "factory.launch", True),
        (_factory.PROJECT, "factory.status", True),
        (_factory.PROJECT, "factory.queue_list", True),
        (_factory.PROJECT, "factory.queue_cancel", True),
        (_factory.PROJECT, "factory-bench.benchmark", True),
        # The cases below were refused by the old allowlist. The brief has chat run any task in the factory
        # project and domain (slot tasks are unsupported, not blocked), so they now start.
        (_factory.PROJECT, "factory-deployer.deploy_if_changed", True),
        (_factory.PROJECT, "factory-setup.gcp_bootstrap", True),
        (_factory.PROJECT, "factory-bench.deploy_engine", True),
        (_factory.PROJECT, "factory-bench.run_case", True),
        (_factory.PROJECT, "factory-bench.hidden_check", True),
        (_factory.PROJECT, _factory_base.SLOTS[0], True),
        (_factory.PROJECT, _factory_base.SLOTS[-1], True),
        (_factory.PROJECT, "factory.sweep", True),
        (_factory.PROJECT, "claude-agent.project", True),
        (_factory.PROJECT, "agent-factory.project", True),
        (_factory.PROJECT, "factory.read_login_meter", True),
        # Still refused, now by the server's project/domain check rather than the allowlist.
        ("other-project", "factory.launch", False),
        ("other-project", "factory-bench.benchmark", False),
    ],
)
async def test_run_task_reaches_the_queue_tasks_in_the_factory_project_only(remote, project, name, allowed):
    """Any task in the factory project starts through run_task; nothing in another project does."""
    arguments = {"project": project, "domain": _factory.DOMAIN, "name": name, "inputs": {"a": 1}}
    if not allowed:
        with pytest.raises(ToolError, match=project):
            await call("run_task", **arguments)
        assert not remote
        return
    assert await call("run_task", **arguments) == {"url": "http://run", "name": "r1"}
    assert ("run", (name, {"a": 1})) in remote


def _scoped_tool_arguments() -> list[tuple[str, str]]:
    """(tool, argument) for every registered tool and each argument of it that names a project or domain."""
    return [
        (name, argument)
        for name in _scoped_tool_names()
        for argument in _scope_parameters(_registered_tools()[name].fn)
    ]


@pytest.mark.parametrize("tool, argument", _scoped_tool_arguments())
async def test_a_tool_refuses_another_project_or_domain_before_any_remote_call(
    remote, monkeypatch, tool, argument
):
    reached: list[tuple[Any, ...]] = []

    def scoped(*args: Any, **kwargs: Any) -> None:
        reached.append(args)
        pytest.fail(f"{tool} reached the remote scope")

    monkeypatch.setattr(factory.app, "_scoped", scoped)
    other = OTHER["project" if "project" in argument else "domain"]
    arguments = _required_arguments(tool) | {argument: other}
    with pytest.raises(ToolError, match=other):
        assert factory.app.mcp is not None
        await factory.app.mcp.call_tool(tool, arguments)
    assert not reached and not remote


def test_every_tool_that_takes_a_project_or_domain_is_covered_by_the_scope_tests():
    assert {"run_task", "get_task", "get_run", "wait_for_run", "get_run_io", "list_secrets"} <= set(
        _scoped_tool_names()
    )
    # `run_task` can create its run in another project: the bypass the stock `project` check alone would miss.
    assert {"run_project", "run_domain"} <= set(_scope_parameters(_registered_tools()["run_task"].fn))
    assert not {"whoami", "search_full_docs", "list_projects", "get_project"} & set(_scoped_tool_names())


async def test_the_factorys_own_project_and_domain_are_accepted(remote):
    result = await call_list("list_secrets", project=_factory.PROJECT, domain=_factory.DOMAIN)
    assert result == [{"name": "claude_token", "type": "regular"}]


async def test_scoping_leaves_each_tools_input_schema_unchanged():
    stock = FlyteMCPAppEnvironment(name="stock", tools=list(factory.TOOLS))
    assert stock.mcp is not None
    stock_tools = {t.name: t for t in stock.mcp._tool_manager.list_tools()}
    ours = _registered_tools()
    assert set(ours) == set(stock_tools)
    for name, tool in ours.items():
        assert tool.parameters == stock_tools[name].parameters, name
        assert tool.annotations == stock_tools[name].annotations, name


async def test_list_projects_shows_only_the_factory_project(remote):
    result = await call_list("list_projects")
    assert [p["id"] for p in result] == [_factory.PROJECT]


@pytest.mark.parametrize("limit", [-1, 0, 1])
async def test_list_projects_shows_the_factory_project_for_any_limit_as_stock_does(remote, limit):
    result = await call_list("list_projects", limit=limit)
    assert [p["id"] for p in result] == [_factory.PROJECT]


async def test_get_project_refuses_another_project(remote):
    with pytest.raises(ToolError, match="other-project"):
        assert factory.app.mcp is not None
        await factory.app.mcp.call_tool("get_project", {"name": "other-project"})
    assert ("Project.get", "other-project") not in remote


async def test_list_secrets_returns_names_and_types_only(remote):
    result = await call_list("list_secrets")
    assert result == [{"name": "claude_token", "type": "regular"}]
    assert "hunter2-secret-value" not in json.dumps(result)


def _asgi_call(client: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call `tool` over the app's streamable-http endpoint, as a client does, and return its JSON result."""
    response = client.post(
        "/flyte-mcp/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"Accept": "application/json, text/event-stream", "Cookie": "union=signed-in"},
    )
    assert response.status_code == 200, response.text
    [content] = response.json()["result"]["content"]
    return cast(dict[str, Any], json.loads(content["text"]))


@pytest.fixture
def asgi_client(remote, monkeypatch):
    """A client of the factory's real ASGI app, with the Flyte calls behind it faked."""
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from starlette.testclient import TestClient

    async def init_passthrough(**_: Any) -> None:
        return None

    async def secret_get(name):
        raise ConnectError(Code.NOT_FOUND, f"{name} not found")

    monkeypatch.setattr(flyte, "init_passthrough", SimpleNamespace(aio=init_passthrough))
    monkeypatch.setattr(flyte.remote.Secret, "get", SimpleNamespace(aio=secret_get))
    flyte.init(project=_factory.PROJECT, domain=_factory.DOMAIN)
    assert factory.app._starlette_app is not None
    with TestClient(factory.app._starlette_app, base_url="https://factory-mcp.example") as client:
        yield client


def test_create_secret_does_not_echo_the_value(asgi_client, remote):
    """The old test's property on the new tool: no value in the result; the value is typed elsewhere.

    The stock `create_secret` no longer takes a `value` (2.13.5, `_secret_tools.py` `create_secret`): it
    returns a one-time `/secret-input/` link on this server, and the owner types the value into that page.
    One test for the whole flow, because an app's MCP session manager starts once per process.
    """
    from urllib.parse import urlsplit

    result = _asgi_call(asgi_client, "create_secret", {"name": "claude_token"})
    assert result["status"] == "awaiting_value" and "value" not in result
    assert result["url"].startswith("https://factory-mcp.example/secret-input/")
    assert (
        "Secret.create",
        "claude_token",
    ) not in remote  # nothing is written until the owner opens the link

    # The link is served by this app: the owner's form, then the write, with the value going to Flyte only.
    path, signed_in = urlsplit(result["url"]).path, {"Cookie": "union=signed-in"}
    form = asgi_client.get(path, headers=signed_in)
    assert form.status_code == 200 and "type=password" in form.text
    saved = asgi_client.post(path, data={"value": "hunter2-secret-value"}, headers=signed_in)
    assert saved.status_code == 200 and "hunter2-secret-value" not in saved.text
    assert ("Secret.create", "claude_token") in remote
    assert asgi_client.get(path, headers=signed_in).status_code == 410  # one use


def test_the_secret_input_route_is_served_by_the_factory_app():
    assert factory.app._starlette_app is not None
    paths = {getattr(route, "path", "") for route in factory.app._starlette_app.routes}
    assert "/secret-input/{token}" in paths
    assert "create_secret" in factory.app.enabled_tools


def test_the_secret_value_is_not_an_argument_of_any_served_tool():
    assert factory.app.allow_inline_secret_values is False
    assert all(
        "value" not in inspect.signature(t.fn).parameters or n == "signal_condition"
        for n, t in _registered_tools().items()
    )


async def test_the_patient_tools_are_still_patient_once_scoped(lagging):
    # The scope wrappers sit over the patient ones; the existing lag tests above go through both. This checks
    # the wrapped function itself is the patient one and that it still passes the scope through.
    run = lagging(LaggingRun(lag=6))
    result = await call("get_run_io", name="r1", project=_factory.PROJECT, domain=_factory.DOMAIN)
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6
    assert "bounded" in (_registered_tools()["get_run_io"].description or "")


def test_instructions_and_skill_describe_get_logs_as_a_tail_with_paging():
    """Stock `get_logs` returns the newest `max_lines` lines and pages back with `before_line`."""
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        flat = " ".join(text.split())
        assert "before_line" in flat
        assert "newest" in flat
        assert "from the beginning" not in flat and "starts at the beginning" not in flat


def test_instructions_describe_the_wider_surface_and_its_limits():
    text = factory.INSTRUCTIONS
    assert "factory-deployer.deploy_if_changed" in text
    assert "names only" in text and "list_secrets" in text
    assert "list_apps" in text and "list_triggers" in text
    assert "unsupported" in text
    assert "limited to this project and domain" in text


def test_search_tools_read_a_corpus_the_image_carries_outside_root():
    for path in (factory.app.sdk_examples_path, factory.app.docs_examples_path, factory.app.full_docs_path):
        assert path is not None and path.startswith("/opt/flyte-mcp-docs/")
    commands = " ".join(
        str(layer)
        for layer in factory.app.image._layers  # type: ignore[union-attr]
    )
    assert factory.FLYTE_SDK_TAG in commands and "llms.txt" in commands and "/opt/flyte-mcp-docs" in commands


def _grant_page_client(requires_auth: bool) -> Any:
    from starlette.testclient import TestClient

    environment = factory.FactoryMCPAppEnvironment(
        name="grant-test", requires_auth=requires_auth, tools=["whoami"]
    )
    assert environment._starlette_app is not None
    return TestClient(environment._starlette_app)


def test_the_grant_page_is_served_by_the_factory_app_at_its_public_path():
    from flyte_agent_factory import _grant_page
    from flyte_agent_factory._grant_seal import PAGE_PATH

    assert factory.app._starlette_app is not None
    from starlette.testclient import TestClient

    client = TestClient(factory.app._starlette_app)
    # Union mode: the stock auth middleware wants the browser's Union cookie (or a bearer), then serves it.
    expected = 401 if factory.app.requires_auth else 200
    assert client.get(PAGE_PATH).status_code == expected
    response = client.get(PAGE_PATH, headers={"Cookie": "union=signed-in"})
    assert response.status_code == 200
    assert response.text == _grant_page.PAGE_HTML
    assert response.headers["Cache-Control"] == "no-store"
    assert "connect-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("requires_auth, unauthenticated", [(True, 401), (False, 200)])
def test_the_grant_page_is_behind_the_stock_auth_of_each_mode(requires_auth, unauthenticated):
    """Union mode asks for a sign-in, as when the page was a FastMCP route; proxy mode asks for nothing."""
    from flyte_agent_factory import _grant_page
    from flyte_agent_factory._grant_seal import PAGE_PATH

    client = _grant_page_client(requires_auth)
    assert client.get(PAGE_PATH).status_code == unauthenticated
    signed_in = client.get(PAGE_PATH, headers={"Authorization": "Bearer t"})
    assert signed_in.status_code == 200 and signed_in.text == _grant_page.PAGE_HTML
    assert client.head(PAGE_PATH, headers={"Authorization": "Bearer t"}).status_code == 200
    assert client.post(PAGE_PATH, headers={"Authorization": "Bearer t"}).text != _grant_page.PAGE_HTML
    # Everything else is the stock app's: the health check is open, the MCP mount is still there.
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/flyte-mcp/not-a-route", headers={"Authorization": "Bearer t"}).status_code == 404


def test_the_grant_page_adds_no_mcp_tool():
    assert factory.app.enabled_tools == set(factory.TOOLS)
    assert factory.app.mcp is not None
    assert set(_registered_tools()) == set(factory.TOOLS)


def test_instructions_declare_the_operator_state_file_and_write_targets():
    # The takeover chat takes these from the server, so they must be the profile's own values.
    text = " ".join(factory.INSTRUCTIONS.split())
    workspace = _git.get_repo_slug(_factory.REPO)
    assert f"operator/STATE.md in the workspace repo {workspace}" in text
    writes = ", ".join(_git.get_repo_slug(url) for url in factory.REPOS.values())
    assert f"writes STATE.md, issues and pull-request branches to: {writes}" in text
    assert workspace == "team/work-sandbox"


def test_instructions_say_a_state_md_elsewhere_is_reported_in_the_first_reply_and_not_used():
    # #185: a skill or note from before the move may still name the old STATE.md home.
    text = " ".join(factory.INSTRUCTIONS.split())
    assert "a STATE.md elsewhere is not the operator's" in text
    assert "say so in the first reply and do not use it" in text


def test_instructions_tell_a_takeover_to_report_a_stale_or_unversioned_skill():
    # A skill saved before versioned skills has no takeover step of its own; the server instructions reach it.
    text = " ".join(factory.INSTRUCTIONS.split())
    assert "Skill version: <digest>" in text and "If it has no such line" in text
    assert factory.SKILL_VERSION_URL.endswith("/skills/dist/VERSION") and factory.SKILL_VERSION_URL in text
    assert "say in the first reply that the installed skill is stale" in text


def test_write_targets_always_hold_the_workspace_even_when_mcp_repos_leave_it_out():
    # `[mcp] repos = ["factory"]`: STATE.md's repo is still one the operator writes to, and listed once.
    assert factory.list_write_targets("git@github.com:team/work.git", ["git@github.com:team/engine.git"]) == [
        "team/work",
        "team/engine",
    ]
    assert factory.list_write_targets(
        "git@github.com:team/work.git", ["https://github.com/team/work", "git@github.com:team/engine.git"]
    ) == ["team/work", "team/engine"]


# --- signal_condition: any answer to an ask reaches the project ---------------------------------------------


class _Cond:
    """A condition as `Condition.get`/`listall` return it, recording what it is signalled."""

    def __init__(self, name, phase="ACTION_PHASE_PAUSED", expected=str, refuse=False):
        self.name, self.phase, self.expected, self.refuse = name, phase, expected, refuse
        self.signalled: list[Any] = []
        self.pb2 = SimpleNamespace(expected=expected)
        self.signal = SimpleNamespace(aio=self._signal)
        # What `list_conditions` reads of a `flyte.remote.Condition` (details included).
        self.expected_type, self.action_name, self.run_name = expected, name, "run1"
        self.prompt = self.prompt_type = self.description = self.timeout = None
        self.fetch_details = SimpleNamespace(aio=self._details)

    async def _details(self):
        return self

    async def _signal(self, payload):
        # The server signals only a paused condition: its refusal is "condition is X, expected PAUSED".
        if self.refuse or "PAUSED" not in self.phase:
            from connectrpc.code import Code
            from connectrpc.errors import ConnectError

            raise ConnectError(
                Code.FAILED_PRECONDITION, f"{self.name}: condition is TIMED_OUT, expected PAUSED"
            )
        self.signalled.append(payload)


@pytest.fixture
def conditions(remote, monkeypatch):
    """A run holding `Cond`s by name; `Condition.get` and `listall` read them."""
    import flyte.remote
    import flyte.remote._condition as stock

    held: dict[str, _Cond] = {}

    async def get(name, run_name):
        return held.get(name)

    async def listall(run_name, limit=100, action_name=None):
        for condition in held.values():
            yield condition

    monkeypatch.setattr(flyte.remote.Condition, "get", SimpleNamespace(aio=get))
    monkeypatch.setattr(flyte.remote.Condition, "listall", SimpleNamespace(aio=listall))
    monkeypatch.setattr(stock, "resolve_condition_expected_type", lambda pb2: pb2.expected)
    return held


async def _signal(name="ask-a0-001", value="yes", **extra):
    return await call("signal_condition", name=name, run_name="run1", value=value, **extra)


async def test_a_yes_to_a_bool_ask_is_signalled_as_true(conditions):
    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001", expected=bool)
    result = await _signal(value="Yes, go ahead")
    assert ask.signalled == [True] and result["signaled"] is True and "rerouted" not in result


async def test_a_text_ask_gets_any_value_as_text(conditions):
    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001")
    await _signal(value="hold off until Monday")
    assert ask.signalled == ["hold off until Monday"]


async def test_an_answer_to_an_ask_that_ended_goes_to_the_direct_inbox(conditions):
    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001", phase="ACTION_PHASE_ABORTED")
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    result = await _signal(value="yes, go ahead")
    assert ask.signalled == [] and inbox.signalled == ["/late-answer ask-a0-001 yes, go ahead"]
    assert result["rerouted"] is True and result["signaled"] is True
    assert result["delivered_to"] == "direct-a0-004" and result["run_name"] == "run1"
    assert "ABORTED" in result["reason"] and result["name"] == "ask-a0-001"


async def test_an_answer_the_ask_refuses_because_it_just_ended_goes_to_the_direct_inbox(conditions):
    conditions["ask-a0-001"] = _Cond("ask-a0-001", refuse=True)
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    result = await _signal(value="no")
    assert inbox.signalled == ["/late-answer ask-a0-001 no"] and result["rerouted"] is True


async def test_an_answer_that_cannot_be_coerced_goes_to_the_direct_inbox(conditions):
    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001", expected=bool)
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    result = await _signal(value="hold off until Monday")
    assert ask.signalled == []
    assert inbox.signalled == ["/late-answer ask-a0-001 hold off until Monday"] and result["rerouted"] is True


async def test_a_direct_inbox_that_is_not_open_is_not_used(conditions):
    conditions["ask-a0-001"] = _Cond("ask-a0-001", phase="ACTION_PHASE_ABORTED")
    conditions["direct-a0-003"] = old = _Cond("direct-a0-003", phase="ACTION_PHASE_SUCCEEDED")
    with pytest.raises(ToolError, match="NOT delivered.*no open direct- inbox"):
        await _signal()
    assert old.signalled == []


async def test_an_empty_answer_to_an_ask_that_ended_is_not_reported_as_delivered(conditions):
    conditions["ask-a0-001"] = _Cond("ask-a0-001", phase="ACTION_PHASE_TIMED_OUT")
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    with pytest.raises(ToolError, match="NOT delivered.*empty"):
        await _signal(value="  ")
    assert inbox.signalled == []


async def test_a_value_the_condition_cannot_take_is_still_an_error_outside_asks(conditions):
    conditions["direct-a0-004"] = _Cond("direct-a0-004", expected=bool)
    with pytest.raises(ToolError, match="could not parse 'soon' as bool"):
        await _signal(name="direct-a0-004", value="soon")


async def test_signal_condition_still_refuses_another_project(conditions):
    with pytest.raises(ToolError, match="other-project"):
        await _signal(project="other-project")


def test_instructions_say_plain_answers_are_fine_and_late_ones_are_rerouted():
    assert "Plain yes or no, or a sentence" in factory.INSTRUCTIONS
    assert "rerouted" in factory.INSTRUCTIONS and "NOT delivered" in factory.INSTRUCTIONS


# --- what each kept wrapper is for, shown on the stock flyte tool it wraps ----------------------------------
#
# The stock tools are fetched from a stock `FlyteMCPAppEnvironment` built here, so each test fails if the SDK
# closes the gap, and says then that the wrapper can go.


def _stock_tool(name: str) -> Any:
    """Return the stock function of tool `name`, as flyte registers it on a plain environment."""
    stock = FlyteMCPAppEnvironment(name="stock", tools=[name])
    assert stock.mcp is not None
    tool = stock.mcp._tool_manager.get_tool(name)
    assert tool is not None
    return tool.fn


async def test_stock_get_run_io_gives_null_outputs_on_a_succeeded_run_the_read_model_still_shows_going(
    lagging,
):
    """The gap `get_run_io` fills: the stock `_tools.py` `get_run_io` reads `Run.get` once, says nothing."""
    run = lagging(LaggingRun(lag=6))
    io = await _stock_tool("get_run_io")(name="r1")
    assert io["outputs"] is None and "outputs_error" not in io
    assert run.reads == 1
    assert (await call("get_run_io", name="r1"))["outputs"] == {"o0": '{"status": "started"}'}


async def test_stock_wait_for_run_returns_before_the_outputs_are_readable(lagging):
    """The gap `wait_for_run` fills: the stock one returns on the watch stream; the outputs lag."""
    lagging(LaggingRun(lag=6))
    result = await _stock_tool("wait_for_run")(name="r1", timeout_s=5)
    assert result["phase"] == "succeeded" and "outputs_ready" not in result
    assert (await _stock_tool("get_run_io")(name="r1"))["outputs"] is None
    assert (await call("wait_for_run", name="r1", timeout_s=5))["outputs_ready"] is True


async def test_stock_signal_condition_raises_on_a_sentence_for_a_bool_ask(conditions):
    """The gap `signal_condition` fills: the stock one coerces and raises, and the answer is lost."""
    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001", expected=bool)
    with pytest.raises(ValueError, match="could not parse"):
        await _stock_tool("signal_condition")(name="ask-a0-001", run_name="run1", value="yes please")
    assert ask.signalled == []
    await _signal(value="yes please")
    assert ask.signalled == [True]


async def test_stock_signal_condition_raises_on_an_ask_that_has_ended(conditions):
    from connectrpc.errors import ConnectError

    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001", phase="ACTION_PHASE_TIMED_OUT")
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    with pytest.raises(ConnectError, match="expected PAUSED"):
        await _stock_tool("signal_condition")(name="ask-a0-001", run_name="run1", value="no")
    assert inbox.signalled == []
    assert (await _signal(value="no"))["rerouted"] is True
    assert ask.signalled == [] and inbox.signalled == ["/late-answer ask-a0-001 no"]


async def test_a_yes_to_an_ask_that_ended_is_rerouted_too(conditions):
    """A sentence for a bool ask that has also ended: the bool retry is refused as well, so it is rerouted."""
    conditions["ask-a0-001"] = _Cond("ask-a0-001", expected=bool, refuse=True)
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")
    result = await _signal(value="yes please")
    assert result["rerouted"] is True and inbox.signalled == ["/late-answer ask-a0-001 yes please"]


async def test_a_signal_the_server_refuses_for_another_reason_is_not_rerouted(conditions):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    conditions["ask-a0-001"] = ask = _Cond("ask-a0-001")
    conditions["direct-a0-004"] = inbox = _Cond("direct-a0-004")

    async def denied(payload):
        raise ConnectError(Code.PERMISSION_DENIED, "not yours")

    ask.signal = SimpleNamespace(aio=denied)
    with pytest.raises(ToolError, match="not yours"):
        await _signal(value="go")
    assert inbox.signalled == []


async def test_the_reroute_is_made_in_the_factorys_project_through_the_stock_tools(conditions, monkeypatch):
    """`Condition.get/listall` take no project or domain (remote/_condition.py): the scope is the tools'."""
    seen: list[tuple[str | None, str | None]] = []
    listall = flyte.remote.Condition.listall

    async def recording_listall(**kwargs):
        config = real_get_init_config()
        assert config is not None
        seen.append((config.project, config.domain))
        async for condition in listall.aio(**kwargs):
            yield condition

    conditions["ask-a0-001"] = _Cond("ask-a0-001", phase="ACTION_PHASE_ABORTED")
    conditions["direct-a0-004"] = _Cond("direct-a0-004")
    monkeypatch.setattr(flyte._initialize, "_get_init_config", real_get_init_config)
    monkeypatch.setattr(flyte.remote.Condition, "listall", SimpleNamespace(aio=recording_listall))
    flyte.init(project="elsewhere", domain="elsewhere-domain")
    await _signal()
    assert seen and set(seen) == {(_factory.PROJECT, _factory.DOMAIN)}


async def test_stock_list_projects_lists_every_project(remote):
    """The gap `list_projects` fills: `_tools.py` `list_projects` has no scope."""
    ids = [p["id"] for p in await _stock_tool("list_projects")()]
    assert ids == [_factory.PROJECT, "other-project"]
    assert [p["id"] for p in await call_list("list_projects")] == [_factory.PROJECT]


async def test_stock_get_project_gives_any_project(remote, monkeypatch):
    async def project_get(name):
        pb2 = SimpleNamespace(
            id=name, name=name, description="", state=0, domains=[], HasField=lambda _: False
        )
        return SimpleNamespace(pb2=pb2)

    monkeypatch.setattr(flyte.remote.Project, "get", SimpleNamespace(aio=project_get))
    assert (await _stock_tool("get_project")(name="other-project"))["id"] == "other-project"


@pytest.fixture
def real_scope(monkeypatch):
    """Use the SDK's own init config, as the tools do, so the scope a call really ran in can be read back."""
    monkeypatch.setattr(flyte._initialize, "_get_init_config", real_get_init_config)
    flyte.init(project="elsewhere", domain="elsewhere-domain")


@pytest.mark.parametrize("argument", ["project", "domain", "run_project", "run_domain"])
async def test_stock_run_task_runs_in_any_project_or_domain(remote, real_scope, monkeypatch, argument):
    """The gap the scope wrappers fill: stock `run_task`, and `run_project`/`run_domain` since 2.13.x."""
    context: dict[str, Any] = {}

    async def run(**inputs):
        return SimpleNamespace(url="http://run", name="r1")

    def with_runcontext(**kwargs):
        context.update(kwargs)
        return SimpleNamespace(run=SimpleNamespace(aio=lambda task, **inputs: run()))

    monkeypatch.setattr(flyte, "with_runcontext", with_runcontext)
    arguments = {
        "project": _factory.PROJECT,
        "domain": _factory.DOMAIN,
        "name": "factory.launch",
        "inputs": {},
    }
    await _stock_tool("run_task")(**arguments | {argument: f"other-{argument}"})
    [(_, task_get)] = [c for c in remote if c[0] == "Task.get"]
    reached = {**task_get, **{f"run_{k}": v for k, v in context.items()}}
    assert reached[argument] == f"other-{argument}"


async def test_stock_list_secrets_reads_any_project(remote, real_scope, monkeypatch):
    scopes: list[tuple[str | None, str | None]] = []

    async def secret_listall(*args, **kwargs):
        config = real_get_init_config()
        assert config is not None
        scopes.append((config.project, config.domain))
        yield SimpleNamespace(name="claude_token", type="regular")

    monkeypatch.setattr(flyte.remote.Secret, "listall", SimpleNamespace(aio=secret_listall))
    await _stock_tool("list_secrets")(project="other-project", domain="other-domain")
    assert scopes == [("other-project", "other-domain")]


async def test_a_call_without_a_project_or_domain_runs_in_the_factorys(remote, real_scope, monkeypatch):
    """With no project or domain the stock resolves `FLYTE_MCP_PROJECT`/`_DOMAIN`, which the app sets."""
    assert factory.app.env_vars is not None
    for key in ("FLYTE_MCP_PROJECT", "FLYTE_MCP_DOMAIN"):
        monkeypatch.setenv(key, factory.app.env_vars[key])
    scopes: list[tuple[str | None, str | None]] = []

    async def get(name):
        config = real_get_init_config()
        assert config is not None
        scopes.append((config.project, config.domain))
        return SimpleNamespace(name=name, url="http://run", phase=ActionPhase.RUNNING, done=lambda: False)

    monkeypatch.setattr(flyte.remote.Run, "get", SimpleNamespace(aio=get))
    assert factory.app.resolve_scope(None, None) == (_factory.PROJECT, _factory.DOMAIN)
    await call("get_run", name="r1")
    assert scopes == [(_factory.PROJECT, _factory.DOMAIN)]


@pytest.mark.parametrize("project_given", ["", None])
async def test_an_empty_project_means_the_factorys(remote, project_given):
    result = await call_list("list_secrets", project=project_given) if project_given is not None else None
    assert result in (None, [{"name": "claude_token", "type": "regular"}])


async def test_the_wrappers_keep_the_stock_input_schema_and_only_the_patient_ones_change_the_text():
    stock = FlyteMCPAppEnvironment(name="stock", tools=list(factory.TOOLS))
    assert stock.mcp is not None
    stock_tools = {t.name: t for t in stock.mcp._tool_manager.list_tools()}
    ours = _registered_tools()
    for name, tool in ours.items():
        assert tool.parameters == stock_tools[name].parameters, name
        assert tool.annotations == stock_tools[name].annotations, name
    changed = {n for n, t in ours.items() if t.description != stock_tools[n].description}
    assert changed == {"get_run_io", "wait_for_run"}
    # The new arguments of get_run_io are there, so a chat can use them.
    assert {"action_name", "inputs_only", "outputs_only"} <= set(ours["get_run_io"].parameters["properties"])


async def test_get_run_io_with_an_action_or_inputs_only_does_not_wait(lagging):
    run = lagging(LaggingRun(lag=10**9))
    inputs = await call("get_run_io", name="r1", inputs_only=True)
    assert inputs == {"name": "r1", "inputs": {"name": "x"}}
    assert run.reads == 1


async def test_get_run_io_polls_for_the_outputs_only(lagging, monkeypatch):
    """The polls ask for `outputs_only`, so the inputs are fetched once and not on every poll."""
    inputs_fetched = 0
    run = lagging(LaggingRun(lag=6))
    stock_inputs = run._inputs

    async def counted_inputs():
        nonlocal inputs_fetched
        inputs_fetched += 1
        return await stock_inputs()

    monkeypatch.setattr(run, "_inputs", counted_inputs)
    result = await call("get_run_io", name="r1")
    assert result["inputs"] == {"name": "x"} and result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6 and inputs_fetched == 1


# --- what the stock settings do, where a wrapper or a rebuild used to ---------------------------------------


def test_the_stock_instructions_setting_is_what_the_server_tells_a_client():
    stock = FlyteMCPAppEnvironment(name="stock", tools=["whoami"], instructions="read this first")
    assert stock.mcp is not None and stock.mcp.instructions == "read this first"
    assert factory.app.mcp is not None and factory.app.mcp.instructions == factory.INSTRUCTIONS


async def test_the_stock_docs_paths_are_what_the_search_tools_read(tmp_path):
    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk" / "a.py").write_text("sdk_needle = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.py").write_text("docs_needle = 1\n")
    (tmp_path / "llms.txt").write_text("full_needle\n")
    stock = FlyteMCPAppEnvironment(
        name="stock",
        tools=["search_flyte_sdk_examples", "search_flyte_docs_examples", "search_full_docs"],
        sdk_examples_path=str(tmp_path / "sdk"),
        docs_examples_path=str(tmp_path / "docs"),
        full_docs_path=str(tmp_path / "llms.txt"),
    )
    assert stock.mcp is not None
    for tool, needle in (
        ("search_flyte_sdk_examples", "sdk_needle"),
        ("search_flyte_docs_examples", "docs_needle"),
        ("search_full_docs", "full_needle"),
    ):
        assert needle in str(await stock.mcp.call_tool(tool, {"pattern": needle}))
    assert (factory.app.sdk_examples_path, factory.app.docs_examples_path, factory.app.full_docs_path) == (
        f"{factory.DOCS_DIR}/flyte-sdk/examples",
        f"{factory.DOCS_DIR}/unionai-examples/v2",
        f"{factory.DOCS_DIR}/llms.txt",
    )


def test_the_stock_allowed_hosts_and_origins_settings_refuse_another_host_and_origin():
    from starlette.testclient import TestClient

    environment = FlyteMCPAppEnvironment(
        name="stock",
        tools=["whoami"],
        requires_auth=False,
        allowed_hosts=["factory-mcp.example"],
        allowed_origins=["https://factory-mcp.example"],
    )
    assert environment._starlette_app is not None
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    with TestClient(environment._starlette_app) as client:
        assert (
            client.post(
                "/flyte-mcp/mcp", json=request, headers={**accept, "Host": "evil.example"}
            ).status_code
            == 421
        )
        elsewhere = {**accept, "Host": "factory-mcp.example", "Origin": "https://evil.example"}
        assert client.post("/flyte-mcp/mcp", json=request, headers=elsewhere).status_code == 403
        home = {**accept, "Host": "factory-mcp.example", "Origin": "https://factory-mcp.example"}
        assert client.post("/flyte-mcp/mcp", json=request, headers=home).status_code == 200
    if factory._public_url:
        assert factory.app.resolved_allowed_hosts() == [factory._public_url.split("//", 1)[-1].split("/")[0]]
        assert factory.app.resolved_allowed_origins() == [factory._public_url]


def test_the_stock_scaling_setting_is_one_replica_for_the_secret_input_links():
    assert flyte.app.Scaling(replicas=1).get_replicas() == (1, 1)
    assert factory.app.scaling.get_replicas() == (1, 1)


# --- the flyte pins and the SDK's private modules -----------------------------------------------------------


def test_the_image_pins_the_same_flyte_as_the_project_and_its_tag_is_the_installed_one():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert factory.FLYTE_PIN in pyproject["project"]["dependencies"]
    assert factory.FLYTE_SDK_TAG == f"v{importlib.metadata.version('flyte')}"
    layers = " ".join(str(layer) for layer in factory.app.image._layers)  # type: ignore[union-attr]
    assert factory.FLYTE_PIN in layers


def _imports_of(path: Path) -> list[str]:
    """Return every module a source file imports, and every string in it that names a module."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [f"{node.module}.{alias.name}" for alias in node.names]
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("flyte.")
        ):
            found.append(node.value)
    return found


def test_no_module_imports_a_private_module_of_flyte_ai_mcp():
    sources = sorted((ROOT / "src").rglob("*.py"))
    assert factory.__file__ in {str(p) for p in sources}
    offenders = [
        f"{path.relative_to(ROOT)}: {module}"
        for path in sources
        for module in _imports_of(path)
        if module.startswith("flyte.ai.mcp._")
    ]
    assert offenders == []


def test_the_factory_mcp_server_uses_no_private_name_of_the_flyte_environment():
    tree = ast.parse(Path(factory.__file__).read_text())
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {
        "_scoped",
        "_starlette_app",
        "_create_starlette_app",
        "_enabled_tools",
        "_secret_inputs",
    }
    assert not [m for m in _imports_of(Path(factory.__file__)) if m.startswith("flyte.remote._")]
    # FastMCP's registry is reached in one place, `_registered_tool_functions`.
    assert [
        n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "_tool_manager"
    ].__len__() == 1
