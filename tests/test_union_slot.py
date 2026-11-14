"""The slot side of the Union identity: what the supervisor and its claude child act as."""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from typing import cast

import flyte
import pytest

from flyte_agent_factory import _profile, _secret_env, _session_config, _union_identity, agent
from tests.conftest import TEST_PROFILE

SRC = Path(__file__).resolve().parents[1] / "src" / "flyte_agent_factory"
KEY = base64.b64encode(b"union.example.org:project-client:s3cr3t:").decode()
SESSION_KEY = base64.b64encode(b"union.example.org:session-client:other:").decode()
INJECTED = base64.b64encode(b"union.example.org:injected-client:inj:").decode()


@pytest.fixture(autouse=True)
def _fresh():
    _union_identity.current.cache_clear()
    yield
    _union_identity.current.cache_clear()


def _profile_with_session():
    text = TEST_PROFILE.read_text().replace(
        'deployer_key = "factory-deployer-key"\n',
        'deployer_key = "factory-deployer-key"\nsession_key = "factory-session-key"\n',
        1,
    )
    return _profile.parse(text)


def _supervisor_env(**extra):
    return {
        _union_identity.KEY_ENV: KEY,
        _union_identity.SESSION_KEY_ENV: SESSION_KEY,
        _union_identity.TIER_ENV: "project",
        _union_identity.SECRET_ENV: "factory-project-key",
        "FLYTE_AUTH_TYPE": "ExternalCommand",
        "FLYTE_AUTH_COMMAND": _union_identity.TOKEN_COMMAND,
        "_UNION_EAGER_API_KEY": INJECTED,
        "EAGER_API_KEY": INJECTED,
        "PATH": "/usr/bin",
        **extra,
    }


def test_the_child_acts_as_the_session_key_and_loses_the_rest():
    profile = _profile_with_session()
    env = _secret_env.session_env(_supervisor_env(), profile)
    assert env[_union_identity.KEY_ENV] == SESSION_KEY
    assert env[_union_identity.TIER_ENV] == "session"
    assert env[_union_identity.SECRET_ENV] == "factory-session-key"
    assert env["FLYTE_AUTH_TYPE"] == "ExternalCommand"
    assert env["FLYTE_AUTH_COMMAND"] == _union_identity.TOKEN_COMMAND
    assert _union_identity.SESSION_KEY_ENV not in env
    assert all(var not in env for var in _union_identity.INJECTED_VARS)
    assert KEY not in env.values()


def test_a_pod_with_one_key_hands_that_key_to_the_child():
    env = _secret_env.session_env(_supervisor_env(), _profile.parse(TEST_PROFILE.read_text()))
    env_one = _secret_env.session_env(
        {k: v for k, v in _supervisor_env().items() if k != _union_identity.SESSION_KEY_ENV},
        _profile.parse(TEST_PROFILE.read_text()),
    )
    assert env[_union_identity.KEY_ENV] == SESSION_KEY
    assert env_one[_union_identity.KEY_ENV] == KEY
    assert _union_identity.SESSION_KEY_ENV not in env_one


def test_the_child_environment_is_built_without_any_injected_key(monkeypatch):
    for var in _union_identity.INJECTED_VARS:
        monkeypatch.delenv(var, raising=False)
    env = _secret_env.session_env(
        {k: v for k, v in _supervisor_env().items() if k not in _union_identity.INJECTED_VARS}, None
    )
    assert env[_union_identity.KEY_ENV] == SESSION_KEY


def test_a_pod_without_a_union_identity_gets_none_invented():
    assert _secret_env.session_env({"PATH": "/usr/bin"}, None) == {"PATH": "/usr/bin"}


def test_own_client_id_is_the_tier_keys(monkeypatch):
    from flyte_agent_factory import _auth

    monkeypatch.delenv("_UNION_EAGER_API_KEY", raising=False)
    monkeypatch.setenv(_union_identity.KEY_ENV, KEY)
    assert _auth.own_client_id() == "project-client"


def test_the_console_token_is_the_session_tier_key():
    source = (SRC / "agent.py").read_text()
    assert "console_token.write_text(session_key)" in source
    assert _union_identity.session_key(_supervisor_env()) == SESSION_KEY


def test_local_browser_reads_only_the_tier_key(monkeypatch):
    from flyte_agent_factory import _local_browser

    assert _local_browser.API_KEY_VARS == (_union_identity.KEY_ENV,)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", INJECTED)
    monkeypatch.delenv(_union_identity.KEY_ENV, raising=False)
    assert _local_browser.api_key() == ""
    monkeypatch.setenv(_union_identity.KEY_ENV, KEY)
    assert _local_browser.api_key() == KEY


def test_the_task_environments_mount_their_tiers_key():
    mounted = {s.key for s in cast(list[flyte.Secret], agent.env.secrets)}
    session_mounted = {s.key for s in cast(list[flyte.Secret], agent.subagent_env.secrets)}
    profile = _session_config._PROFILE
    assert profile is not None and profile.union.key_for("project"), "the test profile names its tier keys"
    assert profile.union.key_for("project") in mounted
    assert profile.union.key_for("session") in session_mounted
    for env, tier in ((agent.env, "project"), (agent.setup_env, "project"), (agent.subagent_env, "session")):
        assert (env.env_vars or {}).get("AGENT_FACTORY_UNION_TIER") == tier


@pytest.mark.parametrize("name", ["project", "subagent", "codex_bootstrap_login", "gcp_bootstrap"])
def test_every_entry_task_establishes_before_anything_else_remote(name):
    tree = ast.parse((SRC / "agent.py").read_text())
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    lines: dict[str, int] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            text = ast.unparse(node.func)
            if text == "_union_identity.establish":
                lines.setdefault("establish", node.lineno)
            elif "flyte.remote" in text or "open_condition" in text:
                lines.setdefault("remote", node.lineno)
    assert "establish" in lines
    assert lines["establish"] < lines.get("remote", 10**9)


def test_nothing_in_my_modules_names_the_injected_key():
    for module in (
        "agent",
        "_secret_env",
        "_auth",
        "child",
        "_local_browser",
        "_session_setup",
        "_checkpoint",
    ):
        tree = ast.parse((SRC / f"{module}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "EAGER_API_KEY" not in node.value, module
            if isinstance(node, ast.Call):
                assert ast.unparse(node) != 'create_controller("rust")', module
