"""The time formats of the MCP tools' results: UTC with a `Z` suffix.

To the second for tool fields (`format_utc`), to the millisecond for log lines (`format_utc_ms`).

A module of its own so `_app_logs` and `_app_tools`, which the factory's task image loads without the `mcp`
package, share it with `_mcp_budget`, which needs that package.
"""

from __future__ import annotations

from datetime import UTC, datetime


def format_utc(moment: datetime | None) -> str | None:
    """Return `moment` as `YYYY-MM-DDTHH:MM:SSZ` in UTC, or None when there is no moment."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def format_utc_ms(when: datetime) -> str:
    """Return `when` as `YYYY-MM-DDTHH:MM:SS.mmmZ` in UTC: the log-line format. Truncates, never rounds."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{when.microsecond // 1000:03d}Z"
