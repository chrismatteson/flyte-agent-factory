"""Deploy this working tree's factory MCP server as a short-lived test app, beside production, and remove it.

    PYTHONPATH=src uv run python ops/live_mcp_test_app.py --dry-run       # print the plan, deploy nothing
    PYTHONPATH=src uv run python ops/live_mcp_test_app.py                 # deploy, hold, delete on Ctrl-C
    PYTHONPATH=src uv run python ops/live_mcp_test_app.py --keep          # deploy, print, leave it running
    PYTHONPATH=src uv run python ops/live_mcp_test_app.py --delete        # deactivate, then delete it

The script owns the app's lifecycle. By default it deploys the app, prints its endpoint, and then holds
(blocks until Ctrl-C, SIGTERM or end of stdin) so `ops/mcp_soak.py` and `ops/mcp_burst.py` can run against
it from another shell; leaving the hold deletes the app, as does a failed deploy, so nothing is left running
by accident. `--keep` deploys, prints and exits without deleting (a failed deploy is still deleted), and
prints the `--delete` command; use it when the app must outlive this shell. `--delete` removes an app left by
`--keep` or by a killed run; an app that is already gone is not an error. Nothing printed is a token: the
connector URL is `<endpoint>/flyte-mcp/mcp`, and the soak and burst scripts read the bearer token themselves.

The app is the production one (`_factory_mcp.app`, same profile, image, resources, secrets and tools) under
the name `factory-mcp-test-<shortsha>`. It is renamed in place rather than copied: `dataclasses.replace`
re-runs `__post_init__`, which builds a fresh MCP server without the factory's tool wiring, and the deployed
pod finds its server by looking up `flyte_agent_factory._factory_mcp:app`, so the object deployed must be
that one.
`factory-mcp` itself is never deployed, restarted or deleted here; every action refuses a name without the
test prefix.

Two settings differ from production, both as `env_vars`, because the pod re-imports `_factory_mcp` and builds
its server from its own environment:

- `AGENT_FACTORY_MCP_APP` names the test app, so the app tools and the health tool report on it.
- `FACTORY_MCP_PUBLIC_URL` is the test app's own URL. Production's profile names production's host, and the
  server rejects any request whose `Host` is not in its allowlist (DNS-rebinding protection). The test app's
  URL is not known until Union has created it, so the first deploy sends an empty value (protection off) and,
  once the endpoint is known, a second deploy sends that URL: the server measured runs with the same host
  check as production.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError

# Run under `uv run`, which does not install this project (`package = false`), as in ops/deploy_status.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _app_tools, _profile_oss, _union_identity  # noqa: E402 -- after select_from_argv
from flyte_agent_factory._app_tools import DEFAULT_OWN_APP  # noqa: E402 -- after select_from_argv

REPO = Path(__file__).resolve().parent.parent
PREFIX = "factory-mcp-test-"
PUBLIC_URL_ENV = "FACTORY_MCP_PUBLIC_URL"
#: A local git query answers in milliseconds; a hung one (a lock, a network filesystem) must not hang the run.
_GIT_TIMEOUT_S = 60.0


def read_short_sha() -> str:
    """Return this tree's short commit; the tree may carry uncommitted changes, which deploy with it."""
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout.strip()


def make_app_name(sha: str) -> str:
    """Return the test app's name for commit `sha`."""
    return f"{PREFIX}{sha}"


def refuse_unless_test(name: str) -> None:
    """Refuse any app this script must not act on: production, or anything without the test prefix."""
    if name == DEFAULT_OWN_APP or not name.startswith(PREFIX):
        raise SystemExit(f"refusing to act on {name!r}: only apps named {PREFIX}<sha> are this script's")


def is_dirty() -> bool:
    """Return whether the working tree differs from HEAD (the deploy ships the tree, not the commit)."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout
    return bool(out.strip())


def prepare_app(name: str, public_url: str) -> Any:
    """Return `_factory_mcp.app` renamed to `name`, its env pointing the app tools and host check at it."""
    # Only `env_vars` reach the pod, which builds its own server from them; the host allowlist this process
    # built at import is never deployed.
    from flyte_agent_factory import _factory_mcp

    app = _factory_mcp.app
    app.name = name
    app.env_vars = {**(app.env_vars or {}), _app_tools.OWN_APP_ENV: name, PUBLIC_URL_ENV: public_url}
    return app


def deploy(name: str, public_url: str) -> str:
    """Deploy the test app, wait until it is active, and return its endpoint."""
    import flyte
    from flyte.app import DeployedAppEnvironment

    from flyte_agent_factory._image_user import check_unprivileged

    app = prepare_app(name, public_url)
    check_unprivileged(app)
    endpoint = ""
    for deployment in flyte.deploy(app):
        for deployed in deployment.envs.values():
            if not isinstance(deployed, DeployedAppEnvironment) or deployed.get_name() != name:
                raise SystemExit(f"the deploy shipped {deployed.get_name()!r}, not {name!r}")
            endpoint = _poll_app(name, lambda a: a.is_active()).endpoint
    if not endpoint:
        raise SystemExit(f"{name} activated without an endpoint; see the console's Apps page")
    return str(endpoint).rstrip("/")


def delete(name: str) -> None:
    """Deactivate the test app, wait until it has stopped, then delete it."""
    import flyte.remote as remote

    # This runs in a `finally` after a failed deploy, when the app may never have been created: a missing app
    # is the wanted end state, and an error here would hide the deploy's own.
    try:
        remote.App.get(name).deactivate(wait=False)
        _poll_app(name, lambda a: a.is_deactivated())
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
        return
    remote.App.delete(name)  # tolerates an app that is already gone


def _poll_app(name: str, done: Callable[[Any], bool], timeout_s: float = 900.0) -> Any:
    """Return the app once `done(app)`, reading it every 10 s.

    Not `App.watch`: its stream needs a permission to view the organisation's executions that a
    project-scoped identity does not have ("permission denied ... ACTION_VIEW_FLYTE_EXECUTIONS").
    """
    import flyte.remote as remote

    deadline = time.monotonic() + timeout_s
    while True:
        app = remote.App.get(name)
        if done(app):
            return app
        if time.monotonic() > deadline:
            raise SystemExit(f"{name} did not reach the wanted state in {timeout_s:.0f} s; see the console")
        time.sleep(10)


def deploy_with_host_check(name: str) -> str:
    """Deploy twice, the second time with the endpoint the first one produced, and return the endpoint."""
    return deploy(name, deploy(name, ""))


@contextlib.contextmanager
def deploy_test_app(name: str) -> Iterator[str]:
    """Deploy the test app, yield its endpoint, and delete it on exit, also after a failure."""
    refuse_unless_test(name)
    try:
        yield deploy_with_host_check(name)
    finally:
        delete(name)


def print_endpoint(name: str, endpoint: str) -> None:
    """Print where the app is and how to form the connector URL; the bearer token is never printed."""
    print(f"{name} endpoint: {endpoint}")
    print(f"Connector URL: {endpoint}/flyte-mcp/mcp")


def hold() -> None:
    """Block until Ctrl-C, SIGTERM or end of stdin."""

    def stop(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        print("stopping", flush=True)


def describe_plan(name: str, deleting: bool) -> list[str]:
    """Return what a real run would do, one step per line."""
    if deleting:
        return [f"deactivate {name} and wait until it has stopped", f"delete {name}"]
    tree = "with uncommitted changes" if is_dirty() else "clean"
    return [
        f"ship this working tree ({tree}) as app {name}: _factory_mcp.app renamed, nothing else changed",
        f"env_vars add {_app_tools.OWN_APP_ENV}={name} and {PUBLIC_URL_ENV}= (empty: host check off)",
        "deploy, wait until activated, read the endpoint",
        f"redeploy with {PUBLIC_URL_ENV}=<endpoint> (production's host check), wait until activated",
        "print the endpoint and the connector URL <endpoint>/flyte-mcp/mcp",
        "hold until Ctrl-C, SIGTERM or end of stdin, then delete the app (--keep: exit without deleting)",
        f"{DEFAULT_OWN_APP} is not touched",
    ]


def main() -> int:
    """Deploy, or delete, the test app."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile (read before argparse runs)")
    parser.add_argument("--name", help=f"test app name (default: {PREFIX}<short sha of this tree's HEAD>)")
    parser.add_argument("--delete", action="store_true", help="deactivate and delete the test app")
    parser.add_argument("--keep", action="store_true", help="deploy and exit; leave the app running")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; deploy nothing")
    args = parser.parse_args()

    name = args.name or make_app_name(read_short_sha())
    refuse_unless_test(name)
    if args.dry_run:
        print("\n".join(describe_plan(name, args.delete)))
        return 0

    profile = _profile.require()
    _union_identity.init_for_script(
        profile,
        key_env=_union_identity.KEY_ENV,
        root_dir=REPO,
        image_builder=_profile_oss.builder(profile.backend),
    )
    if args.delete:
        delete(name)
        print(f"deleted {name}")
        return 0

    if args.keep:
        # Not `deploy_test_app`: a generator-based context manager deletes when it is closed, which
        # `ExitStack.pop_all` cannot prevent. The callback goes first so a failed deploy still deletes.
        with contextlib.ExitStack() as stack:
            stack.callback(delete, name)
            endpoint = deploy_with_host_check(name)
            stack.pop_all()
        print_endpoint(name, endpoint)
        print(f"Remove it with: ops/live_mcp_test_app.py --delete --name {name}")
        return 0

    with deploy_test_app(name) as endpoint:
        print_endpoint(name, endpoint)
        print("Holding; Ctrl-C (or closing stdin) deletes the app.", flush=True)
        hold()
    print(f"deleted {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
