"""Derive the devbox profile from the engine's `profiles/oss-example.toml`, as a new user would.

Copy the example, change only the keys that must change, and record each change with its reason.

Copied unchanged from projects/oss-setup-selftest/selftest/profile.py (another project's tree, so copied
rather than imported).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Change:
    """One key changed from the example profile, and why."""

    table: str  # "" for top level
    key: str
    old: object
    new: object
    why: str


def _format_toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported value {v!r}")


def derive_profile(example: str, edits: Sequence[tuple[str, str, object, str]]) -> tuple[str, list[Change]]:
    """Apply `(table, key, new_value, why)` edits to the example's text, keeping comments and layout.

    Only a key that already exists in the example (in its first occurrence of the table) is
    edited: a key the example lacks is a gap in the example, and is refused here so the caller
    records it rather than silently inventing profile keys.
    """
    data = tomllib.loads(example)
    lines = example.splitlines(keepends=True)
    changes: list[Change] = []
    for table, key, new, why in edits:
        old = (
            (data.get(table, {}) if table else data).get(key)
            if not isinstance(data.get(table), list)
            else None
        )
        idx = _find_line(lines, table, key)
        if idx is None:
            raise KeyError(f"[{table or 'top level'}] {key} is not in the example profile")
        m = re.match(
            r"^(\s*" + re.escape(key) + r"\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|[^#\s]+)(.*)$",
            lines[idx],
            re.S,
        )
        assert m, lines[idx]
        lines[idx] = m.group(1) + _format_toml_value(new) + m.group(3)
        changes.append(Change(table, key, old, new, why))
    text = "".join(lines)
    tomllib.loads(text)  # still valid TOML
    return text, changes


def _find_line(lines: list[str], table: str, key: str) -> int | None:
    current = ""
    for i, line in enumerate(lines):
        s = line.strip()
        h = re.match(r"^\[\[?([^\]]+)\]\]?", s)
        if h:
            if current == table and table:  # left the wanted table without finding the key
                return None
            current = h.group(1).strip()
            continue
        if current == table and re.match(r"^" + re.escape(key) + r"\s*=", s):
            return i
    return None
