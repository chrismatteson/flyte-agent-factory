"""The profile's `[telemetry]` table: whether deployed environments let the Flyte SDK report home.

The SDK sends crash reports (with frame local variables, which in a factory pod can hold tokens) and
per-operation usage counters to a hosted Sentry unless `DO_NOT_TRACK` is set (see REPORT.md, crash
reporting, and docs/security.md). The factory turns that off everywhere it deploys, and
`[telemetry] flyte_sdk = true` turns it back on. Nothing from the factory is imported here, as in
`_profile_resources`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._profile import Profile

KEYS = ("flyte_sdk",)


class TelemetryError(ValueError):
    """A `[telemetry]` table the profile cannot have; `_profile.parse` reports it as a `ProfileError`."""


def parse(table: Mapping[str, Any]) -> bool:
    """Return `flyte_sdk`: a TOML boolean, false when the key (or the table) is left out."""
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise TelemetryError(f"[telemetry] unknown key(s): {', '.join(unknown)}; one of {', '.join(KEYS)}")
    value = table.get("flyte_sdk", False)
    if not isinstance(value, bool):
        raise TelemetryError(f"[telemetry] flyte_sdk must be true or false, not {value!r}")
    return value


def telemetry_env(profile: Profile | None) -> dict[str, str]:
    """Return the environment that keeps the Flyte SDK from reporting, or nothing when the profile opts in.

    Never read from this process's environment, as `_profile.pod_env` never reads the deployer's: a deploy
    from a shell that happens to carry a value must not decide what the pods do. `DO_NOT_TRACK` is the SDK's
    main switch (it also stops the usage counters); the legacy `FLYTE_DISABLE_SENTRY` is not used.
    """
    if profile is not None and profile.telemetry_flyte_sdk:
        return {}
    return {"DO_NOT_TRACK": "1"}
