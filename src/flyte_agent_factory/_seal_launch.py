"""Resolve a launch's seal: the spec the operator gave, the benchmark workspace repo it runs in.

Operator-only: `ops/operate.py launch --seal` and direct `launch_project` callers reach it; the intake
`launch` task has no seal parameter.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ._profile import Profile
from ._seal_spec import SealSpec


def spec_from_file(path: str | Path) -> str:
    """Read `path` (a TOML seal spec) and return its canonical JSON; `ValueError` names a bad field."""
    return SealSpec.from_toml(Path(path)).to_json()


def resolve(seal: str, profile: Profile) -> tuple[str, str]:
    """Return `(canonical spec JSON, repo URL)` for a sealed launch.

    The repo is `[seal] workspace_repo`, and the spec's `push_remote` is made to point at it: a sealed
    project never falls back to the regular workspace repo, whose history holds earlier attempts.
    """
    spec = SealSpec.from_json(seal)
    slug = profile.seal.workspace_repo
    if not slug:
        raise ValueError(
            "a sealed launch needs [seal] workspace_repo in the deployment profile: the empty benchmark "
            "workspace repo (owner/name). It never falls back to the regular workspace repo, whose "
            "history holds earlier attempts"
        )
    url = f"https://github.com/{slug}.git"
    if spec.push_remote and spec.push_remote != url:
        raise ValueError(f"seal spec push_remote {spec.push_remote!r} is not [seal] workspace_repo ({url})")
    return dataclasses.replace(spec, push_remote=url).to_json(), url
