"""A session's git over HTTPS with a repo-scoped GitHub App token, instead of a deploy key.

git's built-in `credential-store` helper reads the token from a 0600 file, so there is no custom helper
and no round trip to the supervisor. The session can read the file; that is accepted because the token
covers one repository and lasts an hour. The App's key never leaves the supervisor (`take_app_key`).
The supervisor re-mints before expiry (`refresh_loop`) and the file is replaced atomically, so a git
process never reads a half-written token.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from . import _github
from ._auth import write_private
from ._workspace import run_git

#: Tokens last 60 minutes; re-minting at 45 leaves `TOKEN_MARGIN` and a failed attempt's retries.
REFRESH_SECONDS = 45 * 60
#: Retry sooner than a full period after a failed mint: the old token is still good for a while.
RETRY_SECONDS = 5 * 60


def remote_url(repo: str) -> str:
    """Return the exact `origin` URL for `owner/repo`; `_push_guard.install` must be given this spelling."""
    return f"https://github.com/{repo}.git"


def install(checkout: Path, repo: str, store: Path) -> None:
    """Point `checkout` at `repo` over HTTPS, authenticating from the credential file `store`."""
    url = remote_url(repo)
    if "origin" in run_git(checkout, "remote").split():
        run_git(checkout, "remote", "set-url", "origin", url)
    else:
        run_git(checkout, "remote", "add", "origin", url)
    run_git(checkout, "config", "credential.helper", f"store --file={store}")
    # One token per repo; by path would make git ask for a credential per URL path instead.
    run_git(checkout, "config", "credential.useHttpPath", "false")
    # Nobody is at a terminal: a refused token must fail the push, not wait for a password.
    os.environ["GIT_TERMINAL_PROMPT"] = "0"


def config_env(store: Path) -> dict[str, str]:
    """Environment making any git process use `store` (a clone has no checkout to configure yet)."""
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": f"store --file={store}",
        "GIT_CONFIG_KEY_1": "credential.useHttpPath",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_TERMINAL_PROMPT": "0",
    }


def write_token(store: Path, token: str) -> None:
    """Replace `store` with `token`, 0600 throughout: the new file is complete before it takes the name."""
    partial = store.with_name(store.name + ".new")
    write_private(partial, f"https://x-access-token:{token}@github.com\n")
    partial.replace(store)


def mint(app: _github.GithubApp, repo: str, store: Path, scope: Mapping[str, str]) -> None:
    """Mint a fresh token for `repo` and write it to `store`. Raises `GithubError`."""
    write_token(store, app.installation_token(repo, scope, fresh=True))


async def refresh_loop(
    app: _github.GithubApp,
    repo: str,
    store: Path,
    scope: Mapping[str, str],
    row: Callable[[str, str], object],
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Re-mint the token every `REFRESH_SECONDS`, forever (cancel it to stop).

    A failed mint keeps the old token, which stays valid until it expires, says so in the timeline and is
    retried after `RETRY_SECONDS`.
    """
    delay = REFRESH_SECONDS
    while True:
        await sleep(delay)
        try:
            await asyncio.to_thread(mint, app, repo, store, scope)
            delay = REFRESH_SECONDS
        except (_github.GithubError, OSError) as exc:
            # Redacted: an error text must never carry a token, and this is about to be published.
            row("⚠️", f"could not refresh the GitHub push token; keeping the old one: {app.redact(str(exc))}")
            logging.getLogger(__name__).warning("push token refresh failed: %s", type(exc).__name__)
            delay = RETRY_SECONDS
