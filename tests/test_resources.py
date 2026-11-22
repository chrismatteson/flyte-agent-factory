"""What each component asks Kubernetes for: the defaults fit one small node, and the budget is the deployment.

Offline. The environment objects are the real ones, built against the suite's profile.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import flyte
import pytest
from flyte._internal.runtime.resources_serde import get_proto_resources
from flyteidl2.core.tasks_pb2 import Resources  # type: ignore[import-untyped]  # no py.typed marker

from flyte_agent_factory import (
    _bench_check,
    _bench_flow,
    _deployer,
    _factory,
    _factory_base,
    _factory_mcp,
    _loop_flow,
    _profile,
    _profile_resources,
    _reconcile,
    _resources,
    _session_config,
    _slow_lane,
    _webhook,
    agent,
    browser,
)
from flyte_agent_factory._profile import ProfileError

_NAME = Resources.ResourceName.Name
PROFILES = Path(__file__).resolve().parents[1] / "profiles"
WORK = Path(__file__).resolve().parent / "profiles" / "work.toml"
#: Every optional component on: the webhook with intake, the board (reconcile), the loop, a personal browser.
EVERYTHING = (
    WORK.read_text().replace(
        '[repo]\nproject = "git@example.org:team/work-sandbox.git"',
        '[repo]\nproject = "git@example.org:team/work-sandbox.git"\n'
        'factory = "git@github.com:acme/factory.git"',
    )
    + """
[github]
app_id = "123456"
private_key_secret = "app_key"
webhook_secret = "webhook_secret"
maintainers = ["octocat"]

[state]
uri = "s3://bucket/state"

[board]
owner = "octocat"
owner_type = "user"
number = 1
pat_secret = "board_pat"

[bench]
repo = "git@github.com:example-org/factory-bench.git"
repo_key = "bench_key"

[loop]
enabled = true
repo = "acme/factory"

[browser.identities.github]
profile_uri = "memory://github"
"""
)


def parse(extra: str = "", base: str = EVERYTHING) -> _profile.Profile:
    return _profile.parse(base + extra, "test")


def test_the_default_budget_fits_one_small_node_with_every_optional_component_on():
    for profile in (_profile.load(str(PROFILES / "example.toml")), parse()):
        budget = _resources.budget(profile)
        assert budget.idle_total.fits, budget.idle_total
        assert budget.live_total.fits, budget.live_total
    names = {line.name for line in _resources.budget(parse()).idle}
    assert {
        "factory-webhook",
        "factory-issues",
        "factory-reconcile",
        "factory-loop",
        "factory-bench",
    } <= names


def test_the_slot_disk_request_fits_a_default_boot_disk():
    slot = _resources.for_component(None, "slot")
    assert 0 < slot.disk_gib <= _resources.BUDGET_DISK_GIB
    assert slot.disk_gib == 20


def test_one_live_project_counts_a_slot_and_one_lane_shard():
    # The lanes run as shards submitted at once; one must fit beside the slot, the rest wait for room.
    budget = _resources.budget(parse())
    assert [line.component for line in budget.live] == ["slot", "lanes"]
    assert budget.live[1].spec == budget.shard == _resources.lane_shard(_resources.LANE_CPUS_DEFAULT)
    assert budget.shard.cpu == 1.5 and budget.shard.memory_gib == 3


def test_the_default_node_holds_at_least_one_shard_and_the_budget_says_how_many():
    everything = _resources.budget(parse())
    assert everything.shards_at_once == 1  # 3.0 CPU budget: 1.0 idle + 0.5 slot + 1.5 per shard
    assert _resources.budget(_profile.load(str(PROFILES / "example.toml"))).shards_at_once >= 1
    fewer_cpus = _resources.budget(parse('\n[resources.lanes]\ncpu = ["500m", "2"]\n'))
    assert fewer_cpus.shard.cpu == 0.5 and fewer_cpus.live_total.cpu == everything.live_total.cpu - 1.0
    assert (
        fewer_cpus.shards_at_once == 2
    )  # memory binds: 12 GiB - 2.75 idle - 1.5 slot leaves 7.75, 3 per shard
    smaller = _resources.budget(parse('\n[resources.lanes]\ncpu = ["500m", "2"]\nmemory = ["1Gi", "5Gi"]\n'))
    assert smaller.shards_at_once == 3  # CPU binds: 3.0 - 1.0 idle - 0.5 slot leaves 1.5, 0.5 per shard


def test_the_budget_lists_each_deployed_environment_once():
    profile = parse()
    names = [line.name for line in _resources.budget(profile).idle]
    assert len(names) == len(set(names))
    assert sorted(names) == sorted(
        [
            "factory-mcp",
            "factory",
            "factory-deployer",
            "factory-webhook",
            "factory-issues",
            "factory-reconcile",
            "factory-loop",
            "factory-bench",
            "gardening-scan",
            *(identity.app for identity in profile.browser.identities),
        ]
    )


def test_the_benchmark_checks_are_optional_and_not_in_the_totals():
    budget = _resources.budget(parse())
    assert [line.name for line in budget.optional] == ["factory-bench-check"]
    assert "factory-bench" in {line.name for line in budget.idle}


def test_each_environment_asks_for_what_the_budget_counts():
    profile = _factory_base.PROFILE
    session = _session_config._PROFILE
    expected: list[tuple[Any, tuple[Any, str]]] = [
        (agent.env, (session, "slot")),
        (agent.subagent_env, (session, "subagent")),
        (agent.setup_env, (session, "setup")),
        (_factory.env, (profile, "ticks")),
        (_factory.scan_env, (profile, "scan")),
        (_reconcile.env, (_reconcile.PROFILE, "ticks")),
        (_loop_flow.env, (_loop_flow.PROFILE, "ticks")),
        (_deployer.env, (_deployer.PROFILE, "deployer")),
        (_webhook.issues_env, (_webhook.PROFILE, "ticks")),
        (_webhook.env, (_webhook.PROFILE, "webhook")),
        (_factory_mcp.app, (profile, "mcp")),
        (_bench_flow.env, (_bench_flow.PROFILE, "ticks")),
        (_bench_check.env, (profile, "bench")),
        (_slow_lane.task, (profile, "lanes")),
        (_slow_lane.task_on_demand, (profile, "lanes")),
    ]
    for env, (source, component) in expected:
        want = _resources.for_component(source, component).to_flyte()
        assert env.resources == want, env.name


def test_the_browser_apps_keep_shared_memory_and_a_memory_limit():
    profile = parse()
    personal = browser.personal_app(profile, profile.browser.identities[0], exit_node="")
    for app in (browser.browser_env, personal):
        assert app.resources == flyte.Resources(memory=("256Mi", "4Gi"), disk="10Gi", shm="2Gi"), app.name


def test_the_mcp_and_webhook_apps_request_nothing():
    # Stricter than it was: the webhook still asks for nothing, but `mcp` has a real request and a memory
    # limit (measured RSS 114-126 MiB), so it is no longer the first pod a node evicts.
    assert get_proto_resources(_webhook.env.resources) is None
    assert _resources.DEFAULTS["mcp"] == _resources.Spec("100m", "1", "256Mi", "512Mi")
    proto = get_proto_resources(_factory_mcp.app.resources)
    assert proto is not None
    assert {_NAME(r.name): r.value for r in proto.requests} == {"CPU": "100m", "MEMORY": "256Mi"}
    assert {_NAME(r.name): r.value for r in proto.limits} == {"CPU": "1", "MEMORY": "512Mi"}


def test_a_bounded_component_serializes_a_request_and_a_limit():
    proto = get_proto_resources(_resources.for_component(None, "slot").to_flyte())
    assert proto is not None
    assert {_NAME(r.name): r.value for r in proto.requests} == {
        "CPU": "500m",
        "MEMORY": "1536Mi",
        "EPHEMERAL_STORAGE": "20Gi",
    }
    assert {_NAME(r.name): r.value for r in proto.limits} == {"CPU": "4", "MEMORY": "8Gi"}


def test_a_profile_table_replaces_only_the_keys_it_sets():
    profile = parse(
        """
[resources.slot]
cpu = ["2", "8"]
memory = "4Gi"
"""
    )
    slot = _resources.for_component(profile, "slot")
    assert (slot.cpu_request, slot.cpu_limit) == ("2", "8")
    assert (slot.memory_request, slot.memory_limit) == ("4Gi", None)  # a scalar replaces the limit too
    assert slot.disk == "20Gi"
    assert _resources.for_component(profile, "subagent") == _resources.DEFAULTS["subagent"]
    assert _resources.budget(profile).live_total.cpu == _resources.budget(parse()).live_total.cpu + 1.5


def test_a_profile_can_give_a_component_that_has_none_a_request():
    webhook = _resources.for_component(
        parse('\n[resources.webhook]\nmemory = ["128Mi", "512Mi"]\n'), "webhook"
    )
    assert webhook.to_flyte().memory == ("128Mi", "512Mi")
    assert webhook.to_flyte().cpu is None


def test_the_deployment_text_carries_the_table_to_the_pods():
    profile = parse('\n[resources.slot]\ncpu = ["2", "8"]\n')
    again = _profile.parse(profile.deployment_text(), "pod")
    assert _resources.for_component(again, "slot") == _resources.for_component(profile, "slot")


def test_a_deployed_slot_environment_reflects_the_profile():
    """The profile reaches the environment the way a pod reads it: as text in the environment."""
    text = parse('\n[resources.slot]\ncpu = ["2", "8"]\nmemory = ["3Gi", "10Gi"]\ndisk = "30Gi"\n').text
    env = {k: v for k, v in os.environ.items() if k != "AGENT_FACTORY_PROFILE"}
    env["AGENT_FACTORY_PROFILE_TOML"] = text
    code = "from flyte_agent_factory import agent; r = agent.env.resources; print(r.cpu, r.memory, r.disk)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "('2', '8') ('3Gi', '10Gi') 30Gi"


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("[resources.sloth]\ncpu = 1\n", "unknown [resources.sloth]"),
        ("[resources.slot]\ncores = 1\n", "unknown key(s): cores"),
        ('[resources.slot]\ncpu = "4 cores"\n', "not a CPU quantity"),
        ("[resources.slot]\ncpu = 0\n", "must be positive"),
        ("[resources.slot]\ncpu = -1\n", "not a CPU quantity"),
        ('[resources.slot]\nmemory = "lots"\n', "not a memory or disk quantity"),
        ('[resources.slot]\nmemory = ["8Gi", "1Gi"]\n', "request 8Gi is above the limit 1Gi"),
        ('[resources.slot]\ncpu = ["2", "1"]\n', "request 2 is above the limit 1"),
        ('[resources.slot]\ncpu = ["1", "2", "3"]\n', "[request, limit]"),
        ('[resources.slot]\ndisk = ["1Gi", "2Gi"]\n', "one quantity"),
        ("[resources.slot]\ncpu = true\n", "must be a quantity"),
        ("resources = 3\n", "resources"),
    ],
)
def test_a_bad_resources_table_is_a_profile_error(table, message):
    base = EVERYTHING
    if table.startswith("resources"):
        # A top-level key must come before every table.
        base, table = table + EVERYTHING, ""
    with pytest.raises(ProfileError, match="test: .*" + re.escape(message)):
        parse(table, base)


def test_the_example_profile_documents_the_table_and_still_parses():
    text = (PROFILES / "example.toml").read_text()
    assert "[resources.slot]" in text
    uncommented = text.replace("# [resources.slot]", "[resources.slot]").replace(
        '# cpu = ["2", "8"]', 'cpu = ["2", "8"]'
    )
    assert _resources.for_component(_profile.parse(uncommented, "example"), "slot").cpu_limit == "8"


def test_the_budget_prints_as_markdown():
    result = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._resources", "--profile", str(PROFILES / "example.toml")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "| one project live |" in result.stdout
    assert "Lane shards that fit at once beside one live slot: " in result.stdout


def test_the_docs_show_the_default_profiles_budget_as_it_prints():
    doc = (PROFILES.parent / "docs" / "small-nodes.md").read_text()
    assert _resources.markdown(_profile.load(str(PROFILES / "example.toml"))) in doc


def test_the_lane_launcher_reads_the_pod_profile_text_without_importing_the_profile():
    """`_slow_lane` imports test-harness modules only, so it reads the profile's text through `_resources`."""
    assert _resources.PROFILE_TOML_ENV == _profile.TOML_ENV
    text = parse('\n[resources.lanes]\ncpu = ["2", "16"]\n').text
    assert _resources.for_profile_text(text, "lanes").to_flyte().cpu == ("2", "16")
    assert _resources.for_profile_text("", "lanes") == _resources.DEFAULTS["lanes"]
    assert _resources.for_profile_text(EVERYTHING, "slot") == _resources.DEFAULTS["slot"]


def test_every_component_a_profile_may_name_has_a_default_and_no_more():
    assert tuple(_resources.DEFAULTS) == _profile_resources.COMPONENTS


#: Environments the budget leaves out on purpose, with why. Everything else must be counted.
NOT_COUNTED = {
    "claude-browser": "the shared browser is deployed by hand; no profile enables it",
    "factory-setup": "an owner's one-off bootstrap, not part of a project's run; the headroom covers one",
    "voice-factory": "a GPU retraining flow outside the factory's footprint",
}

_LIST_ENVIRONMENTS = """
import importlib, pkgutil, flyte, flyte.app, flyte_agent_factory as package
names = set()
for module in pkgutil.iter_modules(package.__path__):
    imported = importlib.import_module(f"flyte_agent_factory.{module.name}")
    for value in vars(imported).values():
        if isinstance(value, (flyte.TaskEnvironment, flyte.app.AppEnvironment)):
            names.add(value.name)
print("\\n".join(sorted(names)))
"""


def test_every_environment_in_the_package_is_in_the_budget_or_left_out_with_a_reason():
    # A fresh process imports every module, so an environment added anywhere later is seen here.
    found = subprocess.run(
        [sys.executable, "-c", _LIST_ENVIRONMENTS],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(_resources.__file__).parents[1])},
    ).stdout.split()
    budget = _resources.budget(parse())
    # The budget names the slot, sub-agent and lane by role; their deployed names come from the slot config.
    counted = {line.name for line in budget.idle + budget.optional} | {
        agent.env.name,
        agent.subagent_env.name,
        _slow_lane.env.name,
        _slow_lane.env_on_demand.name,
    }
    assert {line.component for line in budget.live} <= {"slot", "lanes", "subagent"}
    assert set(found) - counted - set(NOT_COUNTED) == set()
    assert set(NOT_COUNTED) <= set(found)
