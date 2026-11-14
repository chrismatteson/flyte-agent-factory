"""Only the deployer, after its isolation canary, puts the GitHub push mode into deployed environments (#179).

A hand deploy from a shell that happens to carry `AGENT_FACTORY_GITHUB_PUSH=app` must not bake App pushes into
the slots: `_profile.pod_env` never reads it from the deploying process's environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from flyte_agent_factory import _deploy, _deploy_child, _profile

SRC = Path(__file__).resolve().parents[1] / "src"
PUSH = "AGENT_FACTORY_GITHUB_PUSH"
ISOLATION = "AGENT_FACTORY_GITHUB_ISOLATION"


@pytest.fixture(autouse=True)
def no_deployer_env(monkeypatch):
    monkeypatch.setattr(_profile, "_deployer_env", {}, raising=False)


@pytest.mark.parametrize("name", _profile.DEPLOYER_ONLY_ENV)
def test_pod_env_never_takes_a_deployer_setting_from_the_ambient_environment(monkeypatch, name):
    monkeypatch.setenv(name, "app")
    assert name not in _profile.pod_env()


def test_pod_env_carries_what_the_deployer_set():
    _profile.set_deployer_env({PUSH: "app", ISOLATION: "isolated", _profile.IDENTITIES_ENV: ""})
    env = _profile.pod_env()
    assert env[PUSH] == "app" and env[ISOLATION] == "isolated"
    assert _profile.IDENTITIES_ENV not in env  # empty is not set


def test_only_deployer_settings_can_be_set():
    with pytest.raises(ValueError, match="AGENT_FACTORY_PROFILE"):
        _profile.set_deployer_env({"AGENT_FACTORY_PROFILE": "x"})


def _child_pod_env(monkeypatch, tmp_path, ambient: dict[str, str], deployer_env: dict[str, str] | None):
    """What `pod_env` returns inside a real deploy child given `ambient` and `deployer_env`."""
    out = tmp_path / "pod_env.json"
    script = (
        _deploy._CHILD_START
        + _deploy._TAKE_DEPLOYER_ENV
        + f"open({str(out)!r}, 'w').write(json.dumps(_profile.pod_env()))\n"
    )
    env = {**os.environ, **ambient}
    tail: list[str] = []
    code = _deploy._run_child(
        _profile.require(), script, SRC, env, dry_run=True, stderr_tail=tail, deployer_env=deployer_env
    )
    assert code == 0, tail
    found: dict[str, str] = json.loads(out.read_text())
    return found


def test_a_hand_deploy_child_ignores_a_push_mode_in_the_shell(monkeypatch, tmp_path):
    ambient = {PUSH: "app", ISOLATION: "isolated", _deploy.DEPLOYER_ENV: json.dumps({PUSH: "app"})}
    env = _child_pod_env(monkeypatch, tmp_path, ambient, None)
    assert PUSH not in env and ISOLATION not in env


def test_the_deployers_child_carries_the_push_mode_it_was_given(monkeypatch, tmp_path):
    env = _child_pod_env(monkeypatch, tmp_path, {}, {PUSH: "app", ISOLATION: "isolated; ok"})
    assert env[PUSH] == "app" and env[ISOLATION] == "isolated; ok"


def test_the_deploy_child_passes_the_deployers_settings_to_every_stage(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    def stage_of(name):
        def stage(*args, deployer_env=None, **kwargs):
            seen[name] = deployer_env
            return 0

        return stage

    for stage in ("deploy_slots", "deploy_factory", "deploy_apps"):
        monkeypatch.setattr(_deploy, stage, stage_of(stage))
    base = {PUSH: "app", ISOLATION: "isolated", "UNRELATED": "x"}
    _deploy_child.deploy(_profile.require(), tmp_path, project="p", domain="d", ref="x", base_env=base)
    want = {PUSH: "app", ISOLATION: "isolated"}
    assert seen == {"deploy_slots": want, "deploy_factory": want, "deploy_apps": want}
