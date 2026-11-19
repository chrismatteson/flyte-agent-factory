"""The profile's `[gardening]`, `[judgement]` and `[seal]` tables."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class PolicyError(ValueError):
    """A policy table is invalid. `_profile.parse` turns it into a `ProfileError`."""


@dataclass(frozen=True)
class Gardening:
    """How hard the weekly gardening project (`_gardening`) is guaranteed a slot."""

    min_runs_per_week: int = 1
    max_days_between: int = 7
    escalate_on_regression: bool = True
    max_open_issues: int = 10


@dataclass(frozen=True)
class Seal:
    """Where sealed benchmark projects live: `[seal] workspace_repo = "owner/name"`, the empty benchmark repo.

    Never the regular workspace repo: its history holds earlier attempts a sealed project must not see.
    """

    workspace_repo: str = ""


_OWNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


def parse_seal(table: Mapping[str, Any]) -> Seal:
    """Parse `[seal]`: only `workspace_repo`, as `owner/name`."""
    unknown = sorted(set(table) - set(Seal.__dataclass_fields__))
    if unknown:
        raise PolicyError(f"unknown key(s) in [seal]: {', '.join(unknown)}")
    repo = table.get("workspace_repo", "")
    if not isinstance(repo, str) or (repo and not _OWNER_NAME.fullmatch(repo)):
        raise PolicyError(f"[seal] workspace_repo must be owner/name, got {repo!r}")
    return Seal(workspace_repo=repo)


@dataclass(frozen=True)
class JudgementPolicy:
    """Extras to the judge's built-in rules (`_judgement`); a profile adds to them and cannot remove any."""

    owner_reserved: tuple[str, ...] = ()
    standing_direction: tuple[str, ...] = ()


def parse_gardening(table: Mapping[str, Any]) -> Gardening:
    """Parse `[gardening]`, refusing unknown keys and out-of-range values."""
    unknown = sorted(set(table) - set(Gardening.__dataclass_fields__))
    if unknown:
        raise PolicyError(f"unknown key(s) in [gardening]: {', '.join(unknown)}")
    values = dict(table)
    for key, minimum in (("min_runs_per_week", 0), ("max_days_between", 1), ("max_open_issues", 0)):
        value = values.get(key, getattr(Gardening, key))
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise PolicyError(f"[gardening] {key} must be an integer of at least {minimum}")
    if not isinstance(values.get("escalate_on_regression", True), bool):
        raise PolicyError("[gardening] escalate_on_regression must be a boolean")
    return Gardening(**values)


def parse_judgement(table: Mapping[str, Any]) -> JudgementPolicy:
    """Parse `[judgement]`: each key a list of non-empty strings, stripped."""
    unknown = sorted(set(table) - set(JudgementPolicy.__dataclass_fields__))
    if unknown:
        raise PolicyError(f"unknown key(s) in [judgement]: {', '.join(unknown)}")
    lists: dict[str, tuple[str, ...]] = {}
    for key, value in table.items():
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise PolicyError(f"[judgement] {key} must be a list of non-empty strings")
        lists[key] = tuple(v.strip() for v in value)
    return JudgementPolicy(**lists)
