"""What an OSS backend (`[backend] kind = "oss"`) adds to the profile.

OSS Flyte has no API-key service and no remote image builder, and pods are not told where Flyte is, so the
profile says: `[backend] internal_endpoint`, `insecure`, `image_builder`, and `[mcp] auth`. The accessors take
the parsed profile (or its backend) and apply the default for its kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._profile import Backend, Profile

#: What `flyte.init`'s `image_builder` accepts (`ImageBuildEngine.ImageBuilderType`).
IMAGE_BUILDERS = ("local", "local-podman", "remote")
#: "union": the MCP app checks the caller's Union token. "proxy": an authenticating proxy in front of it does.
MCP_AUTH = ("union", "proxy")


def validate_keys(backend: Mapping[str, str], insecure: object, mcp_auth: object) -> None:
    """Raise `ValueError` for an OSS key value the code does not know (the parser adds the file name).

    `mcp_auth` is the `[mcp] auth` value as written, None when the key is absent.
    """
    if backend.get("image_builder", "local") not in IMAGE_BUILDERS:
        raise ValueError(
            f"[backend] image_builder must be one of {IMAGE_BUILDERS}, not {backend['image_builder']!r}"
        )
    if not isinstance(insecure, bool):
        raise ValueError("[backend] insecure must be true or false")
    if mcp_auth is not None and mcp_auth not in MCP_AUTH:
        raise ValueError(f"[mcp] auth must be one of {MCP_AUTH}, not {mcp_auth!r}")


def endpoint_for_pods(backend: Backend) -> str:
    """Return the URL pods reach Flyte at; on oss a profile without it raises `ProfileError`.

    Not checked at parse: profiles written before the key (and their fixtures) must keep loading. Without it a
    pod falls back to the SDK's laptop default (host.docker.internal:8090) and fails at its first Flyte call,
    long after the deploy succeeded, so the deploy and the pod's start ask here.
    """
    from ._profile import ProfileError  # `_profile` imports this module

    if backend.kind == "oss" and not backend.internal_endpoint:
        raise ProfileError(
            '[backend] internal_endpoint is required when kind = "oss": the in-cluster URL pods reach Flyte '
            "at; see profiles/oss-example.toml"
        )
    return backend.internal_endpoint


def builder(backend: Backend) -> str:
    """Return the deploy's image builder: `remote` on Union, `local` on OSS (which has no remote one)."""
    return backend.image_builder or ("remote" if backend.kind == "union" else "local")


def mcp_auth(profile: Profile) -> str:
    """Return how the MCP authenticates callers: "union" on a Union backend, "proxy" on OSS, unless set."""
    return (profile.mcp.auth if profile.mcp else "") or (
        "union" if profile.backend.kind == "union" else "proxy"
    )


def keyless(profile: Profile) -> bool:
    """Whether the factory acts as no Union key: an OSS backend whose profile lists no `[union]` keys.

    That is the OSS deployment (`profiles/oss-example.toml`): OSS Flyte has no API-key service, so pods mount
    no key and `establish` returns `OSS_IDENTITY`. An OSS-kind profile that does list keys (a backend that
    still issues them) keeps acting as them, as it did before; it never falls back to an injected key.
    """
    return profile.backend.kind == "oss" and not profile.union.secrets


def endpoint_env(profile: Profile | None = None) -> dict[str, str]:
    """Return the variables the SDK reads in `init_in_cluster` (Union injects them itself); oss only.

    For an environment that reaches Flyte, with the profile text (`_profile.pod_env`) or without it (a
    sub-agent, a browser app). Left out without an endpoint: modules read this at import, and tests import
    under profiles that predate the key. The deploy (`init_for_script`) and the pod (`controller_kwargs`)
    refuse instead.
    """
    from . import _profile

    profile = profile or _profile.current()
    if profile is None or profile.backend.kind != "oss" or not profile.backend.internal_endpoint:
        return {}
    env = {"_U_EP_OVERRIDE": profile.backend.internal_endpoint}
    if profile.backend.insecure:
        env["_U_INSECURE"] = "true"
    return env
