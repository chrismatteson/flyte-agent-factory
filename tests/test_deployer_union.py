"""The deployer's Union identity: its tier secrets, the isolation canary before slots, what it reports."""

from __future__ import annotations

import base64
import dataclasses
import json
import subprocess
from types import SimpleNamespace
from typing import Any

import flyte
import pytest

from flyte_agent_factory import (
    _deploy,
    _deployer,
    _github,
    _github_mode,
    _isolation_canary,
    _pull_requests,
    _union_identity,
)
from flyte_agent_factory import _profile as profile_module
from flyte_agent_factory._isolation_canary import LaunchRefused, Verdict
from tests.test_deployer import SHA, make_profile
from tests.test_deployer import world as world

SECRET_VALUE = "client-secret-value"


def _key(client: str) -> str:
    return base64.b64encode(f"union.example.org:{client}:{SECRET_VALUE}:".encode()).decode()


def _with(text: str, *, session: str = "", queue: str = "", push: str = "") -> profile_module.Profile:
    base = make_profile("file:///s").text
    if session:
        base = base.replace(
            'deployer_key = "factory-deployer-key"\n',
            f'deployer_key = "factory-deployer-key"\nsession_key = "{session}"\n',
        )
    if queue:
        base = base.replace(
            'deployer_key = "factory-deployer-key"\n',
            f'deployer_key = "factory-deployer-key"\nqueue_key = "{queue}"\n',
        )
    if push:
        base = base.replace('app_id = "12345"\n', f'app_id = "12345"\npush = "{push}"\n')
    return profile_module.parse(base, "t")


def _profile(**kw: str) -> profile_module.Profile:
    return _with("", **kw)


# --- the pod: secrets and environment -----------------------------------------------------------


def test_the_deployer_pod_acts_as_its_tier_key_and_mounts_the_others_once():
    profile = _profile(session="factory-session-key")
    secrets, env = _deployer.tier_secrets_and_env(profile)
    mounted = {s.key: s.as_env_var for s in secrets}
    assert mounted["factory-deployer-key"] == "AGENT_FACTORY_UNION_KEY"
    # project and queue are the same secret: one mount, under the first tier's name
    assert mounted["factory-project-key"] == "AGENT_FACTORY_UNION_KEY_PROJECT"
    assert mounted["factory-session-key"] == "AGENT_FACTORY_UNION_KEY_SESSION"
    assert "AGENT_FACTORY_UNION_KEY_QUEUE" not in mounted.values()
    assert env[_union_identity.TIER_ENV] == "deployer"
    assert env[_deploy.DEPLOY_KEY_ENV] == _union_identity.KEY_ENV
    assert env["FLYTE_AUTH_TYPE"] == "ExternalCommand"


def test_a_tier_that_shares_the_deployers_secret_is_not_mounted_again():
    base = make_profile("file:///s").text.replace(
        'project_key = "factory-project-key"', 'project_key = "factory-deployer-key"'
    )
    secrets, _ = _deployer.tier_secrets_and_env(profile_module.parse(base, "t"))
    assert [s.as_env_var for s in secrets if s.key == "factory-deployer-key"] == ["AGENT_FACTORY_UNION_KEY"]


def test_the_deployer_environment_is_built_from_it():
    keys = {s.key for s in _deployer.env.secrets}  # type: ignore[union-attr]
    assert _deployer.PROFILE.union.key_for("deployer") in keys
    assert _deployer.env.env_vars[_deploy.DEPLOY_KEY_ENV] == _union_identity.KEY_ENV  # type: ignore[index]


def test_the_canary_is_a_task_of_the_deployer_that_the_launcher_gives_the_app_secret_to():
    assert _deployer.CANARY_TASK == f"{_deployer.env.name}.{_deployer.isolation_canary.name.split('.')[-1]}"
    # `override(secrets=...)` is what mounts the App key; the script must say it, with the key's own name
    assert "override(secrets=" in _deployer._CANARY_LAUNCH and "KEY_ENV_VAR" in _deployer._CANARY_LAUNCH


# --- identities ---------------------------------------------------------------------------------


def test_identities_name_tier_secret_and_client_id_never_the_key(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", _key("deployer-client"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", _key("project-client"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_SESSION", _key("session-client"))
    found = _deployer.identities(_profile(session="factory-session-key"))
    assert found == [
        {"tier": "project", "secret": "factory-project-key", "client_id": "project-client"},
        {"tier": "session", "secret": "factory-session-key", "client_id": "session-client"},
        {"tier": "queue", "secret": "factory-project-key", "client_id": "project-client"},
        {"tier": "deployer", "secret": "factory-deployer-key", "client_id": "deployer-client"},
    ]
    assert SECRET_VALUE not in json.dumps(found)


def test_an_unreadable_or_missing_key_reports_unknown_not_an_error(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", _key("deployer-client"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", "not-a-key")
    monkeypatch.delenv("AGENT_FACTORY_UNION_KEY_SESSION", raising=False)
    by_tier = {
        i["tier"]: i["client_id"] for i in _deployer.identities(_profile(session="factory-session-key"))
    }
    assert by_tier == {
        "project": "unknown",
        "session": "unknown",
        "queue": "unknown",
        "deployer": "deployer-client",
    }


async def test_the_task_calls_establish_first_and_reports_identities(world, monkeypatch):
    calls: list[str] = []
    reports: list[str] = []

    async def replace(html, do_flush=False):
        reports.append(html)

    async def run_pair(*args, **kw):
        calls.append("run_pair")
        return {"deployed": False}

    monkeypatch.setattr(_union_identity, "establish", lambda probe=True: calls.append("establish"))
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    monkeypatch.setattr(_deployer, "_app", lambda: world.app)
    monkeypatch.setattr(_deployer, "run_pair", run_pair)
    monkeypatch.setattr(
        _deployer, "identities", lambda profile: [{"tier": "t", "secret": "s", "client_id": "c"}]
    )
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="r")))
    monkeypatch.setattr(flyte.report, "replace", SimpleNamespace(aio=replace))
    output = await _deployer.deploy_pair.func(SHA, "")
    assert calls == ["establish", "run_pair"]
    assert output["identities"] == [{"tier": "t", "secret": "s", "client_id": "c"}]
    assert "t: s (c)" in reports[-1]


async def test_the_tick_establishes_its_identity_first(world, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(_union_identity, "establish", lambda probe=True: calls.append("establish"))
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)

    def app():
        calls.append("app")
        return world.app

    monkeypatch.setattr(_deployer, "_app", app)
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="r")))

    async def replace(html, do_flush=False):
        pass

    monkeypatch.setattr(flyte.report, "replace", SimpleNamespace(aio=replace))
    await _deployer.deploy_if_changed.func()
    assert calls[0] == "establish"


# --- the deploy children's environment -------------------------------------------------------------


async def test_children_get_neither_the_injected_key_nor_the_other_tiers_keys(world, monkeypatch):
    profile = dataclasses.replace(world.profile, union=_profile(session="factory-session-key").union)
    world.profile = profile
    monkeypatch.setattr(_deployer, "PROFILE", profile)
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", _key("d"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", "project-key-value")
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_SESSION", "session-key-value")
    prefixed = _pull_requests.prefixed_key_env_var("factory-session-key", {})
    monkeypatch.setenv(prefixed, "prefixed-copy")
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")
    monkeypatch.setenv("EAGER_API_KEY", "injected")
    await world.tick()
    env = world.slot_calls[0]["base_env"]
    assert env["AGENT_FACTORY_UNION_KEY"]  # the deployer's own, which DEPLOY_KEY_ENV names
    for name in (
        "AGENT_FACTORY_UNION_KEY_PROJECT",
        "AGENT_FACTORY_UNION_KEY_SESSION",
        prefixed,
        "_UNION_EAGER_API_KEY",
        "EAGER_API_KEY",
    ):
        assert name not in env, name
        assert name not in world.profile_checks[0]["base_env"], name


# --- the isolation canary ------------------------------------------------------------------------------


@pytest.fixture
def canary(world, monkeypatch):
    """A world whose profile wants App pushes, and a `launch` it controls."""
    world.profile = dataclasses.replace(
        world.profile, github=dataclasses.replace(world.profile.github, push="app")
    )
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    state = SimpleNamespace(
        answer={"app_key_mounted": False, "injected_key_present": False, "union_key_injected": False},
        error=None,
        calls=[],
    )

    def launch(secret):
        state.calls.append(secret)
        if state.error:
            raise state.error
        return state.answer

    monkeypatch.setattr(_deployer, "launch_canary", launch)
    return state


@pytest.mark.parametrize(
    ("answer", "error", "push", "reason"),
    [
        (
            {"app_key_mounted": False, "injected_key_present": False, "union_key_injected": False},
            None,
            "app",
            "no App key",
        ),
        (None, LaunchRefused("PERMISSION_DENIED"), "app", "cannot launch"),
        (
            {"app_key_mounted": True, "injected_key_present": False, "union_key_injected": False},
            None,
            "deploy_key",
            "mounted the App key",
        ),
        (
            {"app_key_mounted": False, "injected_key_present": True, "union_key_injected": False},
            None,
            "deploy_key",
            "injected App key",
        ),
        ({"app_key_mounted": "yes"}, None, "deploy_key", "not three booleans"),
        (None, subprocess.SubprocessError("exit 1"), "deploy_key", "could not be run"),
    ],
)
async def test_the_verdict_decides_the_push_mode_passed_to_the_slots(
    world, canary, answer, error, push, reason
):
    canary.answer, canary.error = answer, error
    await world.tick()
    assert canary.calls == [world.profile.github.private_key_secret]
    for env in (world.slot_calls[0]["base_env"], world.factory_calls[0]["env"]):
        assert env[_github_mode.PUSH_ENV_VAR] == push
    output = world.runs[-1].output
    assert output["github_push"] == push
    assert any(reason in r for r in output["isolation"])


async def test_without_an_app_the_canary_is_not_run(world, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(_deployer, "launch_canary", lambda secret: calls.append(secret))
    world.profile = dataclasses.replace(
        world.profile, github=dataclasses.replace(world.profile.github, app_id="", private_key_secret="")
    )
    assert not world.profile.github.configured
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    await world.tick()
    assert calls == []
    assert world.slot_calls[0]["base_env"][_github_mode.PUSH_ENV_VAR] == "deploy_key"
    assert world.runs[-1].output["github_push"] == "deploy_key"


async def test_with_an_app_the_canary_runs_whatever_push_is_and_its_reasons_reach_the_slots(
    world, canary, monkeypatch
):
    world.profile = dataclasses.replace(
        world.profile, github=dataclasses.replace(world.profile.github, push="deploy_key")
    )
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    canary.answer = {"app_key_mounted": False, "injected_key_present": False, "union_key_injected": True}
    await world.tick()
    assert canary.calls == [world.profile.github.private_key_secret]
    for env in (world.slot_calls[0]["base_env"], world.factory_calls[0]["env"]):
        assert env[_github_mode.PUSH_ENV_VAR] == "deploy_key"
        assert "Union injects" in env[_github_mode.ISOLATION_ENV_VAR]
    assert any("Union injects" in r for r in world.runs[-1].output["isolation"])


@pytest.mark.parametrize("error", [RuntimeError("boom"), subprocess.SubprocessError("exit 1")])
def test_push_mode_in_app_read_never_raises_and_gives_the_reason(world, canary, error):
    canary.error = error
    prof = dataclasses.replace(
        world.profile, github=dataclasses.replace(world.profile.github, push="deploy_key")
    )
    push, reasons = _deployer._push_mode(prof)
    assert push == "deploy_key" and reasons and "could not be run" in reasons[0]


def test_push_mode_app_read_runs_the_canary_and_app_needs_isolation(world, canary):
    github = world.profile.github
    read = dataclasses.replace(world.profile, github=dataclasses.replace(github, push="deploy_key"))
    assert _deployer._push_mode(read)[0] == "deploy_key" and len(canary.calls) == 1
    assert _deployer._push_mode(world.profile) == (
        "app",
        [_github_mode.ISOLATED, "the canary saw no App key"],
    )


def test_an_isolated_app_read_deploy_records_isolated_not_a_reachable_key(world, canary):
    github = world.profile.github
    read = dataclasses.replace(world.profile, github=dataclasses.replace(github, push="deploy_key"))
    push, reasons = _deployer._push_mode(read)
    assert (push, reasons) == ("deploy_key", [_github_mode.ISOLATED, "the canary saw no App key"])


async def test_a_missing_session_key_is_not_isolated_and_names_the_variable_not_a_value(world, monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_UNION_KEY_SESSION", raising=False)
    world.profile = dataclasses.replace(
        world.profile,
        github=dataclasses.replace(world.profile.github, push="app"),
        union=_profile(session="factory-session-key").union,
    )
    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    await world.tick()
    output = world.runs[-1].output
    assert output["github_push"] == "deploy_key"
    assert any("session" in r for r in output["isolation"])


def test_the_report_says_how_the_isolation_went(monkeypatch):
    monkeypatch.setattr(_deployer, "PROFILE", _profile(push="app"))
    text = _deployer._describe_outcome(
        {
            "deployed": True,
            "envs": ["a"],
            "github_push": "deploy_key",
            "isolation": ["a session-launched task mounted the App key"],
        }
    )
    assert "pushes by deploy key" in text and "mounted the App key" in text


# --- the canary launch (a subprocess acting as the session tier) --------------------------------------


def _fake_run(monkeypatch, *, stdout="", returncode=0, stderr=""):
    seen: dict[str, Any] = {}

    def run(args, **kw):
        seen.update(args=args, **kw)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_the_launch_runs_as_the_session_key_and_nothing_else(monkeypatch):
    monkeypatch.setattr(_deployer, "PROFILE", _profile(session="factory-session-key"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", "deployer-key-value")
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_SESSION", "session-key-value")
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", "project-key-value")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")
    seen = _fake_run(
        monkeypatch,
        stdout='noise\n{"app_key_mounted": false, "injected_key_present": false, '
        '"union_key_injected": false}\n',
    )
    assert _deployer.launch_canary("app_key_secret") == {
        "app_key_mounted": False,
        "injected_key_present": False,
        "union_key_injected": False,
    }
    env = seen["env"]
    assert env["AGENT_FACTORY_UNION_KEY"] == "session-key-value"
    assert "deployer-key-value" not in env.values() and "project-key-value" not in env.values()
    assert "GITHUB_APP_PRIVATE_KEY" not in env and "_UNION_EAGER_API_KEY" not in env
    assert "session-key-value" not in " ".join(seen["args"])  # never argv: the SDK logs command lines
    assert seen["timeout"]


def test_the_launch_uses_the_deployer_key_when_the_session_shares_it(monkeypatch):
    base = make_profile("file:///s").text.replace(
        'project_key = "factory-project-key"', 'project_key = "factory-deployer-key"'
    )
    monkeypatch.setattr(_deployer, "PROFILE", profile_module.parse(base, "t"))
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", "deployer-key-value")
    seen = _fake_run(
        monkeypatch,
        stdout='{"app_key_mounted": true, "injected_key_present": false, "union_key_injected": false}',
    )
    assert _deployer.launch_canary("s")["app_key_mounted"] is True
    assert seen["env"]["AGENT_FACTORY_UNION_KEY"] == "deployer-key-value"


def test_a_refused_launch_is_launch_refused(monkeypatch):
    monkeypatch.setattr(_deployer, "PROFILE", _profile())
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", "k")
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", "k")
    _fake_run(monkeypatch, stdout="REFUSED PERMISSION_DENIED\n")
    with pytest.raises(LaunchRefused):
        _deployer.launch_canary("s")


def test_a_failed_launch_says_the_exit_code_only(monkeypatch):
    monkeypatch.setattr(_deployer, "PROFILE", _profile())
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", "k")
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY_PROJECT", "k")
    _fake_run(monkeypatch, returncode=3, stderr="Traceback ... token=abc123")
    with pytest.raises(subprocess.SubprocessError) as raised:
        _deployer.launch_canary("s")
    assert "3" in str(raised.value) and "abc123" not in str(raised.value)
    # and `check` turns that into "not isolated", quoting it
    verdict = _isolation_canary.check(_profile(), _deployer.launch_canary)
    assert verdict == Verdict(False, [verdict.reasons[0]]) and "could not be run" in verdict.reasons[0]


def test_the_canary_task_answers_with_the_probe(monkeypatch):
    monkeypatch.setattr(
        _isolation_canary,
        "probe",
        lambda secret: {"app_key_mounted": False, "injected_key_present": False, "union_key_injected": False},
    )
    assert _deployer.isolation_canary.func("s") == {
        "app_key_mounted": False,
        "injected_key_present": False,
        "union_key_injected": False,
    }


# --- scoped GitHub reads --------------------------------------------------------------------------------


async def test_direction_and_engine_fetch_use_contents_read_and_commit_health_uses_ci_read(
    world, monkeypatch
):
    scopes: list[dict[str, str]] = []
    real = _github.GithubApp.scoped

    def scoped(self, permissions):
        scopes.append(dict(permissions))
        return real(self, permissions)

    monkeypatch.setattr(_github.GithubApp, "scoped", scoped)
    await world.tick()
    assert dict(_github.CI_READ) in scopes and dict(_github.CONTENTS_READ) in scopes
    # the fetch URLs carry a contents-only token
    assert world.fetched and all(url for url, _ in world.fetched)


async def test_the_slots_carry_the_identities_the_deploy_recorded(world):
    await world.tick()
    recorded = _deployer.identities(world.profile)
    for env in (world.slot_calls[0]["base_env"], world.factory_calls[0]["env"]):
        assert json.loads(env[profile_module.IDENTITIES_ENV]) == recorded
