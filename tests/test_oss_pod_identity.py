"""Every pod knows its backend kind, so an OSS pod without the profile text needs no Union key (#53).

The judges and reviewers run in the sub-agent environment, which gets the OSS endpoint but not the profile
text. Before `AGENT_FACTORY_BACKEND_KIND` such a pod could not tell OSS from a Union pod whose key was
missing, refused with `IdentityMissing: no Union identity for tier unknown`, and asked for `--union-keys`.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any

import flyte
import pytest

from flyte_agent_factory import _profile, _profile_oss, _union_identity
from tests.conftest import TEST_PROFILE
from tests.test_oss_backend import _POD_MODULES, EXAMPLE_OSS, ROOT, environments_under

INJECTED = "_UNION_EAGER_API_KEY"
PACKAGE = ROOT / "src" / "flyte_agent_factory"
#: Modules of the package that name an environment class but start no factory pod, and why.
NOT_POD_MODULES = {
    "_instructions": "the class name appears only in the session's instructions text",
    "child": "the class name appears only in its docstring; a project's own child uses the slot's identity",
    # The benchmark's hidden check runs a project's tests in a plain image and scrubs every key it might see
    # (`_bench_check.key_vars`): it never acts on Flyte, so it has no identity to establish on any backend.
    "_bench_check": "a hidden-check pod that never acts on Flyte",
    # The test lanes are raw ContainerTasks (`_slow_lane.make_lane_task`): they run a copy of the tree's own
    # offline suite or checks under bash, with no engine entrypoint, and never establish a factory identity.
    "_slow_lane": "a test-lane ContainerTask that runs the offline suite and never acts as the factory",
}


@pytest.fixture(scope="module")
def oss_environments() -> dict[str, dict[str, Any]]:
    return environments_under(EXAMPLE_OSS)


@pytest.fixture(scope="module")
def union_environments() -> dict[str, dict[str, Any]]:
    return environments_under(TEST_PROFILE)


@pytest.fixture
def bare_pod(monkeypatch, tmp_path):
    """This process as a pod given no profile at all: no selection, no text, no local default file."""
    for var in (
        _profile.PROFILE_ENV,
        _profile.TOML_ENV,
        _profile_oss.BACKEND_ENV,
        _union_identity.KEY_ENV,
        _union_identity.TIER_ENV,
        _union_identity.SECRET_ENV,
        "_U_EP_OVERRIDE",
        "_U_INSECURE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_profile, "PROFILES_DIR", tmp_path)
    assert _profile.current() is None
    _union_identity.current.cache_clear()
    yield monkeypatch
    _union_identity.current.cache_clear()


def _as_pod(monkeypatch: pytest.MonkeyPatch, env_vars: dict[str, str]) -> None:
    for name, value in env_vars.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(INJECTED, "injected")


# --- the failure, reproduced ----------------------------------------------------------------


def test_a_reviewer_pod_on_oss_acts_as_the_oss_identity_without_a_key(oss_environments, bare_pod):
    """The judge/reviewer pod (`agent.subagent_env`) of an OSS deploy, as Flyte starts it."""
    env_vars = oss_environments["agent.subagent_env"]["env"]
    assert _profile.TOML_ENV not in env_vars  # the sub-agent gets no profile text: the case of #53
    assert env_vars[_profile_oss.BACKEND_ENV] == "oss"
    _as_pod(bare_pod, env_vars)
    assert _union_identity.establish() is _union_identity.OSS_IDENTITY
    assert INJECTED not in os.environ


def test_a_reviewer_pod_on_union_without_its_key_still_refuses_with_the_existing_message(
    union_environments, bare_pod
):
    env_vars = union_environments["agent.subagent_env"]["env"]
    assert env_vars[_profile_oss.BACKEND_ENV] == "union"
    _as_pod(bare_pod, env_vars)  # the session tier's names, but its secret is not mounted
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    tier, secret = env_vars[_union_identity.TIER_ENV], env_vars[_union_identity.SECRET_ENV]
    assert str(raised.value) == _union_identity._MISSING.format(tier=tier, secret=secret)
    assert "--union-keys" in str(raised.value)
    assert INJECTED not in os.environ  # scrubbed, never taken as the identity (#183)


def test_the_union_message_is_the_one_it_always_was():
    assert _union_identity._MISSING == (
        "no Union identity for tier {tier}: secret {secret} is not mounted as AGENT_FACTORY_UNION_KEY. "
        "Create the key and store it with `python ops/bootstrap.py --union-keys` (or "
        "docs/setup.md#union-keys). The factory never falls back to the pod's injected key."
    )


def test_a_reviewer_pod_on_oss_starts_its_controller_without_a_key(oss_environments, bare_pod):
    env_vars = oss_environments["agent.subagent_env"]["env"]
    assert "FLYTE_AUTH_TYPE" not in env_vars and "FLYTE_AUTH_COMMAND" not in env_vars
    _as_pod(bare_pod, env_vars)
    seen: dict[str, Any] = {}

    def init_in_cluster(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"endpoint": os.environ["_U_EP_OVERRIDE"]}

    bare_pod.setattr(flyte, "init_in_cluster", init_in_cluster)
    assert _union_identity.controller_kwargs() == {"endpoint": os.environ["_U_EP_OVERRIDE"]}
    assert seen == {}  # the SDK reads the endpoint variables itself


# --- every pod kind --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile_fixture,kind", [("oss_environments", "oss"), ("union_environments", "union")]
)
def test_every_environment_of_every_pod_module_carries_the_backend_kind(request, profile_fixture, kind):
    found = request.getfixturevalue(profile_fixture)
    # The pod kinds of #53, by the environment each runs in.
    for name in (
        "agent.env",  # the slot: supervisor and session, and the delegates' parent
        "agent.subagent_env",  # judges, reviewers, delegates, every sub-agent role
        "agent.setup_env",
        "_factory.env",  # the factory loop and the planner
        "_factory.scan_env",
        "_deployer.env",  # the deployer, which starts the deploy children
        "_factory_mcp.app",
        "_webhook.env",
        "_webhook.issues_env",
        "browser.browser_env",
        "voice.env",
        "_bench_flow.env",
        "_loop_flow.env",
        "_reconcile.env",
    ):
        assert name in found, name
    for name, seen in found.items():
        assert seen["env"].get(_profile_oss.BACKEND_ENV) == kind, name


def _starts_a_pod(node: ast.AST) -> bool:
    """Whether `node` builds an environment or a task that runs as its own pod, by any constructor."""
    if not isinstance(node, ast.Call):
        return False
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    return name.endswith("Environment") or name in {"from_task", "ContainerTask"}


def test_the_guard_sees_every_way_of_building_a_pod():
    """`TaskEnvironment.from_task` and a bare `ContainerTask` start pods too (the test lanes use both)."""
    for source in (
        "flyte.TaskEnvironment(name='x')",
        "TaskEnvironment.from_task('x', task)",
        "ContainerTask(name='x', image='i')",
        "flyte.extras.ContainerTask(name='x')",
        "AppEnvironment(name='x')",
    ):
        assert any(_starts_a_pod(n) for n in ast.walk(ast.parse(source))), source
    assert not any(_starts_a_pod(n) for n in ast.walk(ast.parse("environment(); task.override()")))


def test_every_module_that_defines_an_environment_is_checked():
    """A new pod module must join `_POD_MODULES`, so the test above covers its environments."""
    defining = set()
    for path in PACKAGE.glob("*.py"):
        if any(_starts_a_pod(node) for node in ast.walk(ast.parse(path.read_text()))):
            defining.add(path.stem)
    assert defining - set(NOT_POD_MODULES) == set(_POD_MODULES)
    for name in NOT_POD_MODULES:
        assert (PACKAGE / f"{name}.py").is_file(), name


@pytest.mark.parametrize("path,kind", [(EXAMPLE_OSS, "oss"), (TEST_PROFILE, "union")])
def test_a_personal_browser_app_carries_the_backend_kind(path, kind):
    from flyte_agent_factory import browser

    profile = _profile.load(str(path))
    app = browser.personal_app(profile, _profile.BrowserIdentity("me", "s3://bucket/profile"), "")
    assert app.env_vars is not None and app.env_vars[_profile_oss.BACKEND_ENV] == kind


def test_a_deploy_child_gets_the_profile_text_and_so_the_kind():
    child = _profile.child_env(_profile.load(str(EXAMPLE_OSS)), {})
    assert _profile.parse(child[_profile.TOML_ENV], "child").backend.kind == "oss"


def test_the_kind_is_never_copied_from_the_deploying_shell(monkeypatch):
    from flyte_agent_factory import _names, agent

    monkeypatch.setenv(_profile_oss.BACKEND_ENV, "oss")
    assert _profile_oss.BACKEND_ENV in _names.POD_FIXED
    assert _profile_oss.BACKEND_ENV not in agent._shell_policy_env()


def test_backend_env_is_empty_without_a_profile(bare_pod):
    assert _profile_oss.backend_env() == {}


# --- what decides, and what wins ---------------------------------------------------------------


def test_a_tier_named_on_an_oss_pod_still_needs_its_key(bare_pod):
    """An OSS profile that lists [union] keys acts as them: the kind never stands in for a mounted key."""
    _as_pod(
        bare_pod,
        {
            _profile_oss.BACKEND_ENV: "oss",
            _union_identity.TIER_ENV: "session",
            _union_identity.SECRET_ENV: "factory-session-key",
        },
    )
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    assert "OSS backend" in str(raised.value) and "--union-keys" not in str(raised.value)


def test_the_profile_wins_over_the_variable(bare_pod):
    bare_pod.setenv(_profile.PROFILE_ENV, str(TEST_PROFILE))  # Union
    _as_pod(bare_pod, {_profile_oss.BACKEND_ENV: "oss"})
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    assert str(raised.value) == _union_identity._MISSING.format(tier="unknown", secret="unknown")


def test_a_union_kind_without_a_profile_keeps_the_existing_message(bare_pod):
    _as_pod(bare_pod, {_profile_oss.BACKEND_ENV: "union"})
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    assert str(raised.value) == _union_identity._MISSING.format(tier="unknown", secret="unknown")
    assert INJECTED not in os.environ


# --- the message when nothing says ------------------------------------------------------------


def test_an_undetermined_identity_says_so_and_does_not_ask_for_union_keys(bare_pod):
    _as_pod(bare_pod, {})
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    text = str(raised.value)
    assert text.startswith("cannot determine this pod's identity")
    assert _profile_oss.BACKEND_ENV in text and "redeploy" in text
    assert "--union-keys" not in text and "union-keys" not in text
    assert INJECTED not in os.environ


@pytest.mark.parametrize("kind", ["oss", ""])
def test_a_bad_key_on_oss_or_an_unknown_backend_is_not_sent_to_union_keys(bare_pod, kind):
    _as_pod(bare_pod, {_profile_oss.BACKEND_ENV: kind, _union_identity.KEY_ENV: "not-a-key"})
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    assert "--union-keys" not in str(raised.value)


def test_a_bad_key_on_union_keeps_its_advice(bare_pod):
    _as_pod(bare_pod, {_profile_oss.BACKEND_ENV: "union", _union_identity.KEY_ENV: "not-a-key"})
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    assert "--union-keys" in str(raised.value)


def test_the_recovery_path_is_the_same_redeploy_anchor_everywhere():
    """The pod's message, the owner's escalation and the compat doc all send to the same section."""
    from flyte_agent_factory import _review_advice

    assert "docs/setup.md#deploying" in _union_identity._UNDETERMINED
    missing = _union_identity._MISSING.format(tier="unknown", secret="unknown")
    undetermined = _union_identity.missing_message("", "unknown", "unknown")
    for reasons in (missing, undetermined):
        assert "docs/setup.md#deploying" in _review_advice.advise(reasons, "oss").recommendation
        assert "docs/setup.md#deploying" in _review_advice.escalation(reasons, "oss")[1]
    compat = (ROOT / "docs" / "oss-flyte-compat.md").read_text()
    bullet = compat[compat.index("- **Every pod knows its backend.**") :].split("\n\n")[0]
    assert "(setup.md#deploying)" in bullet
    assert re.search(r"(?m)^## Deploying$", (ROOT / "docs" / "setup.md").read_text())
