"""A hand deploy acts as the person's own `flyte` login, never an API key in the env."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from flyte_agent_factory import _deploy, _union_identity

OPS = Path(__file__).resolve().parents[1] / "ops"
#: What a person's shell may carry that would turn a hand deploy away from their own login.
KEY_NAMES = (_deploy.DEPLOY_KEY_ENV, *_union_identity.INJECTED_VARS)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, OPS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def keyed_shell(monkeypatch):
    """A shell with the deployer-key selector and the pod's injected key names set."""
    for name in KEY_NAMES:
        monkeypatch.setenv(name, "AGENT_FACTORY_UNION_KEY" if name == _deploy.DEPLOY_KEY_ENV else "a-key")
    monkeypatch.setenv("FACTORY_MCP_PUBLIC_URL", "https://factory.example")


def _child_envs(monkeypatch) -> list[dict[str, str]]:
    """Record the environment of every deploy child (`python -c <script>`) the script starts."""
    envs: list[dict[str, str]] = []

    def run(argv, **kw):
        if "-c" in argv:
            envs.append(kw["env"])
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)  # the module object _deploy and the scripts call
    return envs


def test_hand_env_drops_every_key_name_and_keeps_the_rest():
    base = {name: "x" for name in KEY_NAMES} | {"FLYTE_CONFIG": "/home/me/.flyte/config.yaml", "PATH": "/bin"}
    assert _deploy.strip_hand_deploy_keys(base) == {
        "FLYTE_CONFIG": "/home/me/.flyte/config.yaml",
        "PATH": "/bin",
    }


def test_deploy_factory_child_env_carries_no_key(keyed_shell, monkeypatch):
    envs = _child_envs(monkeypatch)
    assert _load("deploy_factory").main(["--dry-run"]) == 0
    assert envs
    for env in envs:
        assert not set(KEY_NAMES) & set(env)


def test_deploy_work_child_env_carries_no_key(keyed_shell, monkeypatch):
    envs = _child_envs(monkeypatch)
    deploy_work = _load("deploy_work")
    assert deploy_work.main(["--dry-run"]) == 0
    assert len(envs) == len(deploy_work.SLOTS)
    for env in envs:
        assert not set(KEY_NAMES) & set(env)
        assert env["AGENT_FACTORY_ENV_NAME"] in deploy_work.SLOTS  # still the slot's recipe


def test_the_deployer_keeps_its_key_selector(monkeypatch):
    """The deployer sets the selector on purpose (`_deployer`); only hand deploys strip it."""
    monkeypatch.setenv(_deploy.DEPLOY_KEY_ENV, "AGENT_FACTORY_UNION_KEY")
    assert _deploy.factory_env(os.environ, "p", "d")[_deploy.DEPLOY_KEY_ENV] == "AGENT_FACTORY_UNION_KEY"


def test_wait_deploy_does_not_log_in_at_import(monkeypatch):
    import flyte

    def refuse(*args, **kwargs):
        pytest.fail("importing wait_deploy must not initialise Flyte")

    monkeypatch.setattr(_union_identity, "init_for_script", refuse)
    for name in ("init", "init_from_config", "init_from_api_key"):
        monkeypatch.setattr(flyte, name, refuse)
    assert callable(_load("wait_deploy").main)
