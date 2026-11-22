"""`[telemetry] flyte_sdk`: the Flyte SDK reports nothing from a factory pod unless the profile opts in."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _profile, _profile_telemetry

WORK = (Path(__file__).parent / "profiles/work.toml").read_text()
TWO_IDENTITIES = (
    '\n[browser]\nexit_node = "home-router"\ntailscale_tag = "tag:factory-browser"\n'
    '[browser.identities.github]\nprofile_uri = "memory://github"\n'
    '[browser.identities.linkedin]\nprofile_uri = "memory://linkedin"\nexit_node = ""\n'
)
OFF = {"DO_NOT_TRACK": "1"}


def _profile_with(extra: str = "") -> _profile.Profile:
    return _profile.parse(WORK + extra)


def test_telemetry_is_off_unless_the_profile_opts_in():
    assert _profile_with().telemetry_flyte_sdk is False
    assert _profile_with("\n[telemetry]\nflyte_sdk = false\n").telemetry_flyte_sdk is False
    assert _profile.telemetry_env(_profile_with()) == OFF
    assert _profile.telemetry_env(None) == OFF


def test_opting_in_leaves_the_sdk_switch_out():
    profile = _profile_with("\n[telemetry]\nflyte_sdk = true\n")
    assert profile.telemetry_flyte_sdk is True
    assert _profile.telemetry_env(profile) == {}


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('flyte_sdk = "true"', "flyte_sdk must be true or false"),
        ("flyte_sdk = 1", "flyte_sdk must be true or false"),
        ("sdk_crash_reports = true", "unknown key(s): sdk_crash_reports"),
        ("flyte_sdk = true\nextra = 1", "unknown key(s): extra"),
    ],
)
def test_the_telemetry_table_is_strict(table, message):
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile_with(f"\n[telemetry]\n{table}\n")


def test_the_parser_reports_its_own_error():
    with pytest.raises(_profile_telemetry.TelemetryError):
        _profile_telemetry.parse({"flyte_sdk": 1})
    assert _profile_telemetry.parse({}) is False


@pytest.mark.parametrize("value", ["0", "1", ""])
def test_the_helper_never_reads_the_process_environment(monkeypatch, value):
    monkeypatch.setenv("DO_NOT_TRACK", value)
    monkeypatch.setenv("FLYTE_DISABLE_SENTRY", "0")
    assert _profile.telemetry_env(_profile_with()) == OFF
    assert _profile.telemetry_env(_profile_with("\n[telemetry]\nflyte_sdk = true\n")) == {}
    monkeypatch.delenv("DO_NOT_TRACK")
    assert _profile.telemetry_env(_profile_with("\n[telemetry]\nflyte_sdk = true\n")) == {}


def test_pod_env_and_child_env_carry_it():
    assert _profile.pod_env()["DO_NOT_TRACK"] == "1"
    child = _profile.child_env(_profile_with(), {})
    assert child["DO_NOT_TRACK"] == "1"
    opted_in = _profile.child_env(_profile_with("\n[telemetry]\nflyte_sdk = true\n"), {})
    assert "DO_NOT_TRACK" not in opted_in


def test_child_env_follows_the_profile_not_the_inherited_environment():
    inherited = {"DO_NOT_TRACK": "0", "KEEP": "x"}
    assert _profile.child_env(_profile_with(), inherited)["DO_NOT_TRACK"] == "1"
    opted_in = _profile.child_env(_profile_with("\n[telemetry]\nflyte_sdk = true\n"), inherited)
    assert "DO_NOT_TRACK" not in opted_in
    assert opted_in["KEEP"] == "x"


def _vars(env: Any) -> dict[str, str]:
    return dict(env.env_vars or {})


def _closure(roots: list[Any]) -> dict[str, Any]:
    """Every environment a deploy of `roots` sends: the roots and, recursively, their `depends_on`."""
    found: dict[str, Any] = {}
    pending = list(roots)
    while pending:
        env = pending.pop()
        if env.name not in found:
            found[env.name] = env
            pending.extend(env.depends_on)
    return found


def _deployed_environments() -> dict[str, Any]:
    """The deploy's real environments: the roots `_deploy_child` imports, plus the apps, with dependencies."""
    from flyte_agent_factory import (
        _bench_flow,
        _deployer,
        _factory,
        _factory_mcp,
        _loop_flow,
        _reconcile,
        _webhook,
        agent,
        browser,
    )

    profile = _profile_with(TWO_IDENTITIES)
    personal = [
        browser.personal_app(profile, i, browser.identity_exit_node(i)) for i in profile.browser.identities
    ]
    roots = [
        _factory.env,
        _factory_mcp.app,
        _deployer.env,
        _reconcile.env,
        _webhook.env,
        _webhook.issues_env,
        _bench_flow.env,
        _loop_flow.env,
        agent.env,
        agent.subagent_env,
        agent.setup_env,
        browser.browser_env,
        *personal,
    ]
    return _closure(roots)


def test_every_deployed_environment_has_the_sdk_switch():
    envs = _deployed_environments()
    # The list is the real one: the MCP app, the sub-agents and the browser apps are all in it.
    assert {
        "factory-mcp",
        "agent-factory-subagent",
        "claude-browser",
        "claude-browser-github",
        "claude-browser-linkedin",
        "factory-webhook",
        "factory-deployer",
        "gardening-scan",
    } <= set(envs)
    missing = {
        name: _vars(env).get("DO_NOT_TRACK")
        for name, env in envs.items()
        if _vars(env).get("DO_NOT_TRACK") != "1"
    }
    assert not missing


def test_the_subagent_still_gets_no_profile_text():
    from flyte_agent_factory import agent

    assert _vars(agent.subagent_env)["DO_NOT_TRACK"] == "1"
    assert _profile.TOML_ENV not in _vars(agent.subagent_env)


def test_an_opted_in_personal_browser_has_no_switch():
    from flyte_agent_factory import browser

    profile = _profile_with(TWO_IDENTITIES + "\n[telemetry]\nflyte_sdk = true\n")
    identity = profile.browser.identities[0]
    app = browser.personal_app(profile, identity, browser.identity_exit_node(identity))
    assert "DO_NOT_TRACK" not in _vars(app)


def test_the_process_environment_is_untouched():
    before = dict(os.environ)
    _profile.telemetry_env(_profile_with())
    assert dict(os.environ) == before
