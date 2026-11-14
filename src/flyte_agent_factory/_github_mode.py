"""How this deploy talks to GitHub, in one word and one sentence for `factory.status` and the report."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ._profile import Profile

#: The deployer sets this to `app` in a slot's env only when `[github] push = "app"` and the isolation
#: canary passed (`_isolation_canary`); anything else, or nothing, means deploy keys.
PUSH_ENV_VAR = "AGENT_FACTORY_GITHUB_PUSH"
#: The canary's reasons, joined, recorded beside `PUSH_ENV_VAR` so `factory.status` can say why.
ISOLATION_ENV_VAR = "AGENT_FACTORY_GITHUB_ISOLATION"
#: The first reason the deployer records when the canary passed: sessions cannot reach the App key.
ISOLATED = "isolated"
Mode = Literal["app", "app-read", "deploy-keys"]


def mode(profile: Profile, env: Mapping[str, str]) -> Mode:
    """`app`: App for everything; `app-read`: pushes by deploy key; `deploy-keys`: no App."""
    if not profile.github.configured:
        return "deploy-keys"
    wants_app = getattr(profile.github, "push", "deploy_key") == "app"
    return "app" if wants_app and env.get(PUSH_ENV_VAR) == "app" else "app-read"


def describe(profile: Profile, env: Mapping[str, str], reason: str = "") -> str:
    """Return the sentence for `mode(profile, env)`; `reason` is why isolation is not proven, if known.

    The reason defaults to what the deployer recorded in `ISOLATION_ENV_VAR`. In `app-read` a session can
    reach the App key until the canary says otherwise, so `factory/verifier` is advisory either way.
    """
    current = mode(profile, env)
    if current == "app":
        return "GitHub App: CI visible; pushes by repo-scoped token"
    if current == "deploy-keys":
        return "deploy keys only: no CI visibility, no PRs, no factory/verifier"
    reason = reason or env.get(ISOLATION_ENV_VAR, "")
    if reason.split("; ")[0] == ISOLATED:
        why = reason.partition("; ")[2] or ISOLATED
        return f'GitHub App: CI visible; pushes by deploy key (isolated: {why}; set [github] push = "app")'
    if not reason:
        return (
            "GitHub App: CI visible; pushes by deploy key (isolation not proven: the deployer has not "
            "measured it); factory/verifier advisory"
        )
    return (
        "GitHub App: CI visible; pushes by deploy key; App key reachable from sessions: "
        f"{reason}; factory/verifier advisory"
    )
