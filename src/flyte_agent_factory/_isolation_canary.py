"""Can a session reach the GitHub App's private key? Ask with the session tier's own identity.

Pushing by App token gives the App `contents: write`, so it is only safe when a session cannot get at the
key: neither by launching a child that declares the key's secret, nor through a key Union injects into every
pod it launches. This module decides from one measurement: the deployer launches a no-op task that
declares the secret, using the key a session holds (a subprocess, so the real key never enters the
deployer), and the task reports whether the key reached it (`probe`). The launch itself is `launch`,
injected by the deployer; this module knows nothing of Union.

Isolated means: the launch was refused for lack of permission, or the pod ran and saw neither the App key nor
the key Union injects into pods (`_union_identity.INJECTED_VARS`), with which a pod can launch anything.
Everything else is not isolated, and the reasons are quoted in the deploy report. An answer we cannot
read is not isolated either: fail closed.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from . import _pull_requests, _union_identity
from ._errors import REMOTE_ERRORS
from ._profile import Profile


class LaunchRefused(Exception):
    """`launch` was refused for lack of permission: what the session tier should get."""


@dataclass(frozen=True)
class Verdict:
    isolated: bool
    reasons: list[str] = field(default_factory=list)


def probe(secret_key: str, env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Return what the canary task reports: three booleans, never a value.

    `app_key_mounted`: the secret the task declared arrived under its own name. `injected_key_present`:
    the key is there under Union's prefixed name, which a pod carries without declaring anything.
    `union_key_injected`: Union's own key is in the pod, which can launch a child that mounts the App key.
    """
    env = os.environ if env is None else env
    prefixed = _pull_requests.prefixed_key_env_var(secret_key, env)
    return {
        "app_key_mounted": bool(env.get(_pull_requests.KEY_ENV_VAR)),
        "injected_key_present": bool(prefixed and env.get(prefixed)),
        "union_key_injected": any(env.get(name) for name in _union_identity.INJECTED_VARS),
    }


def check(profile: Profile, launch: Callable[[str], Mapping[str, object]]) -> Verdict:
    """Launch the canary through `launch(private_key_secret)` and judge what comes back."""
    github = profile.github
    if not github.configured:
        return Verdict(False, ["no GitHub App is configured"])
    try:
        answer = launch(github.private_key_secret)
    except LaunchRefused:
        return Verdict(True, ["the session tier cannot launch a task that mounts the App key"])
    except (*REMOTE_ERRORS, subprocess.SubprocessError) as exc:  # the launch failing proves nothing
        return Verdict(False, [f"the canary could not be run ({type(exc).__name__}: {exc})"])
    flags = {
        name: answer.get(name) for name in ("app_key_mounted", "injected_key_present", "union_key_injected")
    }
    if not all(isinstance(value, bool) for value in flags.values()):
        return Verdict(False, ["the canary's answer was not three booleans"])
    reasons = [
        text
        for name, text in (
            ("app_key_mounted", "a session-launched task mounted the App key"),
            ("injected_key_present", "a session-launched pod carries an injected App key"),
            (
                "union_key_injected",
                "a session-launched pod carries the key Union injects into every pod, which can launch a task"
                " that mounts the App key",
            ),
        )
        if flags[name]
    ]
    return Verdict(not reasons, reasons or ["the canary saw no App key"])
