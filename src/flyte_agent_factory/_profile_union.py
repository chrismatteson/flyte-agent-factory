"""The profile's `[union]` table: which Union secret each tier of the factory acts as.

A tier is a set of pods that share one Union identity (`_union_identity` mounts the key and uses it). The
table names the Union secrets that hold an API key; it never holds a key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: project: slot supervisors and setup tasks. session: every claude process, sub-agents, delegates and
#: reviewers (they share `subagent_env`). queue: the factory-level apps and loops. deployer: the deployer
#: and its deploy children.
TIERS = ("project", "session", "queue", "deployer")
_REQUIRED = ("project_key", "deployer_key")
_OPTIONAL = ("session_key", "queue_key")
_PROJECT_KEY_MISSING = (
    "[union] project_key is required for a Union backend: the Union secret holding the API key the "
    "factory's project tier acts as (profiles/README.md#union)"
)


class UnionError(ValueError):
    """The `[union]` table is missing or invalid. `_profile.parse` turns it into a `ProfileError`."""


@dataclass(frozen=True)
class Union:
    project_key: str = ""
    deployer_key: str = ""
    session_key: str = ""  # empty: project_key
    queue_key: str = ""  # empty: project_key

    def key_for(self, tier: str) -> str:
        """Return the secret `tier` acts as. Empty only for a profile with no `[union]` (an OSS backend)."""
        if tier == "project":
            return self.project_key
        if tier == "deployer":
            return self.deployer_key
        if tier == "session":
            return self.session_key or self.project_key
        if tier == "queue":
            return self.queue_key or self.project_key
        raise ValueError(f"unknown Union tier {tier!r}: one of {', '.join(TIERS)}")

    @property
    def secrets(self) -> frozenset[str]:
        return frozenset(self.key_for(tier) for tier in TIERS) - {""}


def parse(table: Any, *, required: bool) -> Union:
    """Return the `[union]` table. `required` is set for a Union backend, which has no other identity."""
    if table is None:
        if required:
            raise UnionError(_PROJECT_KEY_MISSING)
        return Union()
    if not isinstance(table, Mapping):
        raise UnionError("[union] must be a table")
    unknown = sorted(set(table) - {*_REQUIRED, *_OPTIONAL})
    if unknown:
        raise UnionError(f"unknown key(s) in [union]: {', '.join(unknown)}")
    for key in (*_REQUIRED, *_OPTIONAL):
        value = table.get(key, "")
        # A secret name goes into an environment and a log line: keep it to one word.
        if not isinstance(value, str) or value != value.strip() or any(c.isspace() for c in value):
            raise UnionError(f"[union] {key} must be the name of a Union secret, without spaces")
    if required and not table.get("project_key"):
        raise UnionError(_PROJECT_KEY_MISSING)
    if required and not table.get("deployer_key"):
        raise UnionError(
            "[union] deployer_key is required for a Union backend: the Union secret holding the API key "
            "the deployer and its deploy children act as; it may equal project_key (profiles/README.md#union)"
        )
    return Union(**{key: table.get(key, "") for key in (*_REQUIRED, *_OPTIONAL)})
