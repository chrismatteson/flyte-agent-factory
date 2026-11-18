"""Who a factory process acts as on Union.

A pod gets one key, mounted as `AGENT_FACTORY_UNION_KEY`, chosen by its tier (`_profile_union`). The SDK
reads its credentials from `FLYTE_AUTH_TYPE=ExternalCommand` and `FLYTE_AUTH_COMMAND` before it reads the
key Union injects into every task pod, so setting those two on a pod environment replaces the injected key
for the runtime's controller, `flyte.remote`, nested task calls and `attach()` alike (`init_in_cluster`).
The command (`_union_token`) exchanges the mounted key for a token.

Considered and rejected: overwriting the injected variable early (too late for the controller the runtime
builds, and it keeps the name in our code), and `init_from_api_key` in every entry point (cannot reach a
controller or an app the runtime builds). When a tier's key is missing the process fails; it never falls
back to the injected key, which is another tier's identity.

On an OSS backend (`[backend] kind = "oss"`) whose profile lists no `[union]` keys there is no identity.
OSS Flyte has no API-key service and its API is unauthenticated: the network is the trust boundary (tailnet,
network policy, an OAuth proxy in front of the MCP). Pods mount no key and set no auth variables, and
`establish` returns `OSS_IDENTITY`. Anything that can reach the internal endpoint can act as the factory.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _profile_oss, _profile_union

if TYPE_CHECKING:
    import flyte

    from ._profile import Profile

KEY_ENV = "AGENT_FACTORY_UNION_KEY"  # the key this process acts as, under this name in every tier
#: Mounted in slot pods only when the session tier's key differs from the project's: the claude process
#: they spawn acts as it, the supervisor does not.
SESSION_KEY_ENV = "AGENT_FACTORY_SESSION_UNION_KEY"
TIER_ENV = "AGENT_FACTORY_UNION_TIER"  # names, not values, so the token command can say who failed
SECRET_ENV = "AGENT_FACTORY_UNION_SECRET"
#: Whose this process is: never copied from a deploying shell into an environment it defines
#: (`agent._shell_policy_env`), where the key would sit in plain text and every pod would act as it (#183).
OWN_IDENTITY_ENV = frozenset({KEY_ENV, SESSION_KEY_ENV, TIER_ENV, SECRET_ENV})
#: A JSON array: the SDK accepts that or a shell-quoted line. `python` from PATH, as in the image.
TOKEN_COMMAND = '["python", "-m", "flyte_agent_factory._union_token"]'
#: Union injects these into task pods. The only place the factory names them: to drop them.
INJECTED_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY")


_MISSING = (
    "no Union identity for tier {tier}: secret {secret} is not mounted as AGENT_FACTORY_UNION_KEY. Create "
    "the key and store it with `python ops/bootstrap.py --union-keys` (or docs/setup.md#union-keys). The "
    "factory never falls back to the pod's injected key."
)


@dataclass(frozen=True)
class Identity:
    tier: str
    secret: str
    client_id: str  # never the key


#: What `establish` returns on an OSS backend: no tier, secret or client; the network is the identity.
OSS_IDENTITY = Identity(tier="oss", secret="", client_id="oss")


def describe_identity(identity: Identity) -> str:
    """Return a line saying who the process acts as, for a timeline; no key."""
    if identity is OSS_IDENTITY:
        return "OSS backend: no Union identity; the network is the credential"
    return f"Union: tier {identity.tier}, secret {identity.secret}, client {identity.client_id}"


class IdentityMissing(RuntimeError):
    """The pod has no key for its tier."""


class IdentityDenied(PermissionError):
    """Union refused an action to the pod's identity."""


def pod_spec(profile: Profile, tier: str) -> tuple[list[flyte.Secret], dict[str, str]]:
    """Return the secrets and environment a pod of `tier` needs to act as that tier's key."""
    import flyte

    if _profile_oss.keyless(profile):
        return [], {}
    union = profile.union
    secret = union.key_for(tier)  # raises on an unknown tier
    if not secret:
        raise IdentityMissing(f"profile {profile.name!r} has no [union] key for tier {tier}")
    secrets = [flyte.Secret(key=secret, as_env_var=KEY_ENV)]
    session = union.key_for("session")
    if tier == "project" and session != secret:
        secrets.append(flyte.Secret(key=session, as_env_var=SESSION_KEY_ENV))
    env = {
        "FLYTE_AUTH_TYPE": "ExternalCommand",
        "FLYTE_AUTH_COMMAND": TOKEN_COMMAND,
        TIER_ENV: tier,
        SECRET_ENV: secret,
    }
    return secrets, env


def pod_for(profile: Profile | None, tier: str) -> tuple[list[flyte.Secret], dict[str, str]]:
    """`pod_spec`, or nothing where there is no profile or no `[union]` key (a backend without Union keys)."""
    if profile is None or _profile_oss.keyless(profile) or not profile.union.key_for(tier):
        return [], {}
    return pod_spec(profile, tier)


def _decode(key: str) -> str:
    """Return the client id in an API key. Raises ValueError, never quoting any part of the key."""
    from flyte.remote._client.auth._auth_utils import decode_api_key

    try:
        return decode_api_key(key)[1]
    except ValueError:  # binascii and unicode errors are ValueErrors; the SDK's message can quote the key
        raise ValueError("not a Union API key") from None


def client_id_of(key: str) -> str:
    """Return the client id in a pasted API key; `ValueError`, never quoting the key, when it is not one."""
    return _decode(key)


def _load_profile_or_none() -> Profile | None:
    """Return the selected profile; None when none is selected or it does not parse."""
    from . import _profile

    # A profile that does not parse is not an OSS one: the union path then fails closed as it always did.
    try:
        return _profile.current()
    except _profile.ProfileError:
        return None


def _probe() -> None:
    """One cheap call that needs read access in our own project and domain."""
    import flyte.remote

    list(flyte.remote.Run.listall(limit=1))


def establish(probe: bool = True) -> Identity:
    """Take the pod's tier key as its identity or raise `IdentityMissing`; `probe` checks Union accepts it.

    On an OSS backend there is no key: the result is `OSS_IDENTITY` and nothing is probed.
    """
    for var in INJECTED_VARS:  # children inherit the scrubbed environment
        os.environ.pop(var, None)
    profile = _load_profile_or_none()
    if profile is not None and _profile_oss.keyless(profile):
        return OSS_IDENTITY
    tier = os.environ.get(TIER_ENV, "unknown")
    secret = os.environ.get(SECRET_ENV, "unknown")
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise IdentityMissing(_MISSING.format(tier=tier, secret=secret))
    try:
        client_id = _decode(key)
    except ValueError as exc:
        raise IdentityMissing(
            f"the Union key for tier {tier} (secret {secret}) in AGENT_FACTORY_UNION_KEY is {exc}: store a "
            "fresh one with `python ops/bootstrap.py --union-keys` (docs/setup.md#union-keys)"
        ) from None
    identity = Identity(tier=tier, secret=secret, client_id=client_id)
    if probe:
        with denied("listing runs", identity):
            _probe()
    return identity


@functools.cache
def current() -> Identity:
    """Return this process's identity, established once without a probe (a failure is not cached)."""
    return establish(probe=False)


def session_key(env: Mapping[str, str]) -> str:
    """Return a claude child's `KEY_ENV`: the session tier's key if this pod holds it apart, else its own."""
    return env.get(SESSION_KEY_ENV) or env.get(KEY_ENV) or ""


def _describe_location() -> str:
    import flyte.errors
    from flyte._initialize import get_init_config

    try:
        cfg = get_init_config()
        return f"{cfg.project}/{cfg.domain}"
    except flyte.errors.InitializationError:  # only for a message; not initialised is a normal state
        return "the current project/domain"


@contextmanager
def denied(action: str, identity: Identity | None = None) -> Iterator[None]:
    """Turn Union's refusal of `action` into `IdentityDenied` that names who was refused."""
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from flyte.remote._client.auth.errors import AuthenticationError

    try:
        yield
    except (ConnectError, AuthenticationError) as exc:
        if isinstance(exc, ConnectError):
            if exc.code not in (Code.PERMISSION_DENIED, Code.UNAUTHENTICATED):
                raise
            reason = exc.code.name
        else:
            reason = "token command failed"  # the command's own stderr says why
        who = identity or _identity_for_message()
        raise IdentityDenied(
            f"Union refused {action} for {who.tier} identity {who.client_id} (secret {who.secret}) in "
            f"{_describe_location()}: {reason}. Bind the role it needs to that project/domain "
            "(docs/setup.md#union-keys); the factory does not fall back to the pod's injected key."
        ) from exc


def _identity_for_message() -> Identity:
    try:
        return current()
    except IdentityMissing:
        return Identity(os.environ.get(TIER_ENV, "unknown"), os.environ.get(SECRET_ENV, "unknown"), "unknown")


#: The supported one-off deploy for an install whose deployer is too old, and where docs/setup.md explains it.
ONE_OFF_DEPLOY = "ops/deploy_as_key.py"
ONE_OFF_DEPLOY_DOC = "docs/setup.md#migrating-an-existing-install"


def _sdk_finds_a_config() -> bool:
    """Whether `flyte.init_from_config(None)` would find a config file; True if that cannot be told."""
    try:
        from flyte.config._reader import resolve_config_path
    except ImportError:  # a private SDK name: without it, leave the decision to the SDK, as before
        return True
    return resolve_config_path() is not None


def check_script_identity(profile: Profile, key_env: str = "", decode: bool = True) -> None:
    """Raise `IdentityMissing`, once and in words, if `init_for_script` could not act as anyone.

    Without this a deploy child in a pod that has neither the deployer's key nor a CLI config falls into
    `flyte.init_from_config`, and every child prints an SDK traceback. That pod is a deployer older than
    the profile's `[union]` table, which does not set `key_env` for the children of this engine.
    A laptop with no config file is left alone: the SDK searches its default places, and says so itself.
    A key variable whose value is not an API key is refused too (`decode`): the deploy's check could not
    list the secrets with it, and would otherwise go on. `init_for_script` does not decode again: the
    deploy child's preflight has, and its other callers' keys go to the SDK, which refuses a bad one.
    """
    if _profile_oss.keyless(profile):
        return
    if key_env:
        if not os.environ.get(key_env):
            raise IdentityMissing(
                f"AGENT_FACTORY_DEPLOY_KEY_ENV names {key_env}, but {key_env} is not set: it must hold the "
                "Union API key this deploy acts as. To deploy as your own CLI login, unset "
                "AGENT_FACTORY_DEPLOY_KEY_ENV"
            )
        try:
            if decode:
                _decode(os.environ[key_env])
        except ValueError:
            raise IdentityMissing(
                f"AGENT_FACTORY_DEPLOY_KEY_ENV names {key_env}, but its value is not a Union API key "
                "(it does not decode to endpoint, client id, secret and org): it must hold the "
                "deployer_key's API key as Union issued it"
            ) from None
        return
    if not os.environ.get("FLYTE_INTERNAL_EXECUTION_ID"):
        return
    if profile.backend.config:
        found = Path(profile.backend.config).expanduser().is_file()
    else:  # `init_from_config(None)` searches the SDK's own places (FLYTECTL_CONFIG, ~/.flyte, ...)
        found = _sdk_finds_a_config()
    if not found:
        raise IdentityMissing(
            "this deploy runs in a pod with no deployer key and no CLI config, so it has no Union identity. "
            "The deployer that started it is too old to deploy this engine: it predates the profile's "
            "[union] table and does not hand its key to the deploy. Deploy once with "
            f"`python {ONE_OFF_DEPLOY}` ({ONE_OFF_DEPLOY_DOC}), or by hand with a CLI login; the deployer "
            "deployed by that release then deploys the rest."
        )


def init_for_script(profile: Profile, key_env: str = "", **init_kwargs: Any) -> None:
    """Initialise the SDK for a laptop script or a deploy child.

    With `key_env` it acts as the key in that variable (the deployer passes its tier key's). Without, it
    uses the user's own CLI login (`flyte.init_from_config`, the profile's config file if it names one), so a
    hand deploy needs no API key at all, and no laptop ever needs the pod's injected one. `init_kwargs` go to
    the SDK's init (`project` and `domain` override the profile's; a deploy child adds `root_dir` and
    `image_builder`).

    On an OSS backend `key_env` is ignored (OSS has no keys) and, in a pod or when the profile names no config
    file, the SDK connects to the internal endpoint directly (a laptop's config path does not exist in a pod).
    """
    import flyte

    check_script_identity(profile, key_env, decode=False)
    oss = _profile_oss.keyless(profile)
    if oss:  # fail the deploy here, not in a pod that cannot reach Flyte
        _profile_oss.endpoint_for_pods(profile.backend)
    init_kwargs = {"project": profile.backend.project, "domain": profile.backend.domain, **init_kwargs}
    if not key_env or oss:  # OSS has no API keys: a `key_env` is ignored, the network is the credential
        if oss and (not profile.backend.config or os.environ.get("FLYTE_INTERNAL_EXECUTION_ID")):
            # In a pod (the deployer) there is no CLI config; the internal endpoint is the way in.
            flyte.init(
                endpoint=_profile_oss.endpoint_for_pods(profile.backend),
                insecure=profile.backend.insecure,
                **init_kwargs,
            )
            return
        config = str(Path(profile.backend.config).expanduser()) if profile.backend.config else None
        flyte.init_from_config(config, **init_kwargs)
        return
    flyte.init_from_api_key(api_key=os.environ[key_env], **init_kwargs)  # `check_script_identity` saw it set


def controller_kwargs() -> dict[str, Any]:
    """Return `init_in_cluster`'s result: what `create_remote_controller` needs to act as this pod.

    Raises `IdentityMissing` when the pod has no tier key (never on an OSS backend, which has none).
    """
    import flyte

    # `init_in_cluster` falls back to the key Union injects into every pod when nothing else is
    # configured, so a pod without its tier key would quietly act as another identity (#183).
    # `establish` refuses that pod and scrubs the injected key, leaving no fallback to take.
    establish(probe=False)
    profile = _load_profile_or_none()
    if profile is not None and _profile_oss.keyless(profile):
        # Explicit, though `pod_env` sets the same two variables: the controller must not depend on them.
        return flyte.init_in_cluster(
            endpoint=_profile_oss.endpoint_for_pods(profile.backend), insecure=profile.backend.insecure
        )
    return flyte.init_in_cluster()


def describe(profile: Profile) -> list[dict[str, str]]:
    """Each tier and the secret it acts as, for status and reports. No keys, no client ids. None on OSS."""
    if _profile_oss.keyless(profile):
        return []
    return [{"tier": tier, "secret": profile.union.key_for(tier)} for tier in _profile_union.TIERS]


# The SDK runs `_union_token` by name (`TOKEN_COMMAND`) in a process of its own, so nothing else imports it.
# Flyte's code bundle carries only the modules that are loaded when an environment is deployed. Without this
# import no pod has the module: the token command fails with "No module named ..._union_token" and every
# tier is refused at `establish`. At the end of the file because `_union_token` imports this module.
from . import _union_token as _union_token  # noqa: E402 -- last: _union_token imports this module
