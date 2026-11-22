"""The profile's `[repo_aliases]` table: a repository's earlier URLs, after a GitHub rename.

Requests and projects from before the rename still name the old URL, so it must resolve to the same repo.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from . import _git


class AliasError(ValueError):
    """A `[repo_aliases]` table the profile cannot have; `_profile.parse` reports it as a `ProfileError`."""


def parse(table: Mapping[str, Any], repos: Mapping[str, str]) -> Mapping[str, tuple[str, ...]]:
    """Return each `[repo]` key's earlier URLs; raise `AliasError` for a table the profile cannot have."""
    current = {_git._repo_identity(url): key for key, url in repos.items()}
    seen: dict[str, str] = {}
    for key, urls in table.items():
        if key not in repos:
            raise AliasError(f"[repo_aliases] {key} is not a key of [repo] ({', '.join(repos)})")
        if not isinstance(urls, list) or not all(isinstance(u, str) and u.strip() for u in urls):
            raise AliasError(f"[repo_aliases] {key} must be a list of repository URLs")
        for url in urls:
            identity = _git._repo_identity(url)
            # An alias that is a current URL would silently send that repo's projects elsewhere.
            if identity in current:
                raise AliasError(
                    f"[repo_aliases] {key}: {url!r} is the current [repo] {current[identity]} URL"
                )
            if seen.setdefault(identity, key) != key:
                raise AliasError(f"[repo_aliases] {url!r} is listed under both {seen[identity]} and {key}")
    return MappingProxyType({key: tuple(u.strip() for u in urls) for key, urls in table.items()})
