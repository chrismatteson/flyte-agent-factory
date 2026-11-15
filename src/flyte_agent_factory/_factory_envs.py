"""The secrets the factory's task environments mount: the queue env's and a gardening scan's."""

from __future__ import annotations

from typing import cast

import flyte

from . import _deploy, _factory_base, _factory_meters, _git, _profile


def env_secrets(profile: _profile.Profile) -> list[str | flyte.Secret]:
    mounted: list[str | flyte.Secret] = (
        [
            flyte.Secret(key=profile.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV),
            flyte.Secret(key=cast(_profile.Board, profile.board).pat_secret, as_env_var="GITHUB_BOARD_PAT"),
        ]
        if profile.planner.admission == "planner"
        else []
    )
    if profile.github.configured and profile.planner.admission != "planner":
        # Read-only use: `gardening_decision` counts the open `gardening` issues.
        mounted.append(flyte.Secret(key=profile.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV))
    if profile.secrets.claude_token:
        # Only for `mounted_token_fingerprint`: admission must tell the current token's readings from a
        # swapped-out one's, and only the value can.
        mounted.append(flyte.Secret(key=profile.secrets.claude_token, as_env_var=_factory_meters.TOKEN_ENV))
    # Only for `factory.browser_link`, which asks an identity's app for a view ticket.
    mounted.extend(
        flyte.Secret(key=identity.token_secret, as_env_var=identity.token_env)
        for identity in profile.browser.identities
    )
    # The queue, leases, reserves and wait reasons are files on the state branch of `[repo] project`.
    mounted.extend(_factory_base.state_secrets(profile))
    return mounted


def scan_secrets(profile: _profile.Profile) -> list[str | flyte.Secret]:
    """Return the deploy keys a scan pod clones with: the default one and each `[repo_keys]` entry."""
    keys = [
        (profile.secrets.deploy_key, _git.GIT_DEPLOY_KEY_ENV_VAR),
        *((secret, _git.repo_key_env_var(key)) for key, secret in profile.repo_keys.items()),
    ]
    return [flyte.Secret(key=secret, as_env_var=var) for secret, var in keys if secret]
