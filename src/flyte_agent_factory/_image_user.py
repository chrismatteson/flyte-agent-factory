"""Refuse to deploy an image that the SDK's local builder would leave running as root.

Claude Code will not run `bypassPermissions` as root, and the local builder's Dockerfile starts with
`USER root`; only `_images.run_as_flyte` makes it end as `flyte`. The Dockerfile is rendered by the SDK's own
builder in dry-run mode, so this check cannot drift from the template.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
from typing import TYPE_CHECKING

import flyte
from flyte._internal.imagebuild.docker_builder import DockerImageBuilder

try:
    from ._deploy import Refused
except ImportError:  # loaded by path, like `_deploy` itself
    from flyte_agent_factory._deploy import Refused

if TYPE_CHECKING:
    from flyte import Environment


class _DryRunBuilder(DockerImageBuilder):
    @classmethod
    async def _build_command_prefix(cls) -> list[str]:
        # The real prefix creates a buildx builder, which needs a docker binary.
        return ["docker", "build"]


def local_dockerfile(image: flyte.Image) -> str:
    """Return the Dockerfile the SDK's local builder generates for `image`; builds and pushes nothing."""
    out = io.StringIO()
    # The builder prints the Dockerfile with click.echo, which looks up sys.stdout on each call.
    with contextlib.redirect_stdout(out):
        asyncio.run(_DryRunBuilder()._build_image(image, push=False, dry_run=True))
    match = re.search(r"^Dockerfile: (.*?)\nCommand: ", out.getvalue(), re.DOTALL | re.MULTILINE)
    if match is None:
        raise RuntimeError(f"the SDK's dry-run build printed no Dockerfile: {out.getvalue()[-500:]!r}")
    return match.group(1)


def final_user(dockerfile: str) -> str | None:
    """Return the user of the last `USER` instruction (without its group), or None if there is none."""
    users = re.findall(r"^\s*USER\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)
    return users[-1].split(":")[0] if users else None


def check_unprivileged(*envs: Environment) -> None:
    """Raise `Refused` if an environment's built image would run as root (`depends_on` is followed)."""
    seen: set[int] = set()
    pending = list(envs)
    while pending:
        env = pending.pop()
        if id(env) in seen:
            continue
        seen.add(id(env))
        pending.extend(env.depends_on)
        image = env.image
        # A string is a prebuilt URI and `auto` has no layers: nothing the local builder would build.
        if not isinstance(image, flyte.Image) or not image._layers:
            continue
        user = final_user(local_dockerfile(image))
        if user in (None, "root", "0"):
            raise Refused(
                f"refusing to deploy {env.name!r}: its image {image.name!r} would run as "
                f"{user or 'root (no USER instruction)'} under the local image builder, and Claude Code "
                "refuses bypassPermissions as root; make `_images.run_as_flyte(image)` its last layer"
            )
