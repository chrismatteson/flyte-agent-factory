"""Every pod that touches the state mounts the state volume and runs as the configured service account.

The environments are imported in a child process under the profile (as the deploy check does), because each
reads `PROFILE` at import. What is read back is what Flyte serializes: `_get_k8s_pod` on the task template.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _deploy, _profile

SRC = Path(__file__).resolve().parents[1] / "src"
WORK = (Path(__file__).parent / "profiles/work.toml").read_text()
VOLUME = (
    '\n[state]\nvolume = "/state"\nvolume_claim = "factory-state"\n'
    'factory_service_account = "flyte-factory"\nbrowser_service_account = "flyte-browser"\n'
    "\n[browser.identities.github]\n"
)
#: Defined by the modules below, and not touching state.
NO_STATE = {"sub-one", "sub-two", "factory-setup", "factory-deployer", "claude-browser"}

_PRINT = """
import json, sys
import flyte
from flyte.app import AppEnvironment
from flyte._internal.runtime.task_serde import _get_k8s_pod
from flyteidl2.core import tasks_pb2
from google.protobuf.json_format import MessageToDict
from flyte_agent_factory import (
    _deployer, _factory, _factory_mcp, _loop_flow, _reconcile, _webhook, agent, browser,
)

found = {}
for module in (_deployer, _factory, _factory_mcp, _loop_flow, _reconcile, _webhook, agent, browser):
    for value in vars(module).values():
        if isinstance(value, (flyte.TaskEnvironment, AppEnvironment)):
            found[value.name] = value
for app in browser.identity_apps.values():
    found[app.name] = app

def mounts(env):
    if not isinstance(env.pod_template, flyte.PodTemplate):
        return None
    # What Flyte serializes: the template's volumes and the primary container's mounts are kept.
    k8s = _get_k8s_pod(tasks_pb2.Container(image="img"), env.pod_template)
    pod = MessageToDict(k8s.pod_spec)
    return {
        "claims": [v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes", [])],
        "paths": [m["mountPath"] for c in pod["containers"] for m in c.get("volumeMounts", [])],
    }

print(json.dumps({n: [e.service_account, mounts(e)] for n, e in sorted(found.items())}))
"""


def _print_environments(profile_text: str, slot: str) -> dict[str, list[Any]]:
    profile = _profile.parse(profile_text)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_FACTORY_", "BROWSER_"))}
    env = _profile.child_env(profile, _deploy.recipe_env(profile, slot, {**env, "PYTHONPATH": str(SRC)}))
    out = subprocess.run([sys.executable, "-c", _PRINT], env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    seen: dict[str, list[Any]] = json.loads(out.stdout.strip().splitlines()[-1])
    return seen


def test_every_state_touching_environment_mounts_the_volume_and_runs_as_the_factory_account():
    seen = _print_environments(WORK + VOLUME, "agent-one")
    touching = {
        name: (account, template)
        for name, (account, template) in seen.items()
        if name not in NO_STATE and name not in ("claude-browser-github", "factory-webhook")
    }
    assert set(touching) == {
        "agent-one",
        "gardening-scan",
        "factory",
        "factory-mcp",
        "factory-issues",
        "factory-reconcile",
        "factory-loop",
    }
    for name, (account, template) in touching.items():
        assert account == "flyte-factory", name
        assert template == {"claims": ["factory-state"], "paths": ["/state"]}, name
    # The webhook touches state only when the planner runs in it (`[planner] admission`): not here.
    for name in (NO_STATE | {"factory-webhook"}) & seen.keys():
        assert seen[name] == [None, None], name
    assert seen["claude-browser-github"][0] == "flyte-browser"


@pytest.mark.parametrize("state", ["", '\n[state]\nbucket = "s3://bkt/factory"\n'], ids=["git", "bucket"])
def test_git_and_bucket_profiles_add_no_pod_template(state):
    seen = _print_environments(WORK + state, "agent-one")
    assert seen["agent-one"] == [None, None]
    assert seen["factory"] == [None, None]
    assert seen["factory-mcp"] == [None, None]


def test_an_app_environment_names_its_primary_container_app():
    from flyte_agent_factory import _state_pod

    profile = _profile.parse(WORK + VOLUME)
    app = _state_pod.environment_kwargs(profile, "factory", app=True)["pod_template"]
    task = _state_pod.environment_kwargs(profile, "factory")["pod_template"]
    # Flyte's app serializer requires the primary container to be `app`; a task's is `primary`.
    assert (app.primary_container_name, task.primary_container_name) == ("app", "primary")
