"""The profile's `[resources]` tables: per component, the CPU, memory and disk that replace its default.

`_resources` holds the defaults and lays these over them. A value is one quantity (a request) or
`[request, limit]`; `disk` is a request only. Nothing from the factory is imported here, as in `_resources`:
the slow-lane launcher loads both (`_gates._TEST_HARNESS`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import flyte


class ResourcesError(ValueError):
    """A `[resources]` table the profile cannot have; `_profile.parse` reports it as a `ProfileError`."""


_CPU = re.compile(r"(\d+(\.\d+)?|\.\d+)m?")
_MEMORY = re.compile(r"(\d+(\.\d+)?|\.\d+)(Ki|Mi|Gi|Ti|k|M|G|T)?")
_MEMORY_UNIT = {
    "": 1,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
}
KEYS = ("cpu", "memory", "disk")
#: The components a `[resources.<name>]` table may name; `_resources.DEFAULTS` gives each its default.
COMPONENTS = (
    "slot",
    "subagent",
    "setup",
    "mcp",
    "webhook",
    "browser",
    "ticks",
    "deployer",
    "scan",
    "lanes",
    "bench",
)


@dataclass(frozen=True)
class Spec:
    """One component's resources. A limit needs its request: Kubernetes copies the limit into it anyway."""

    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    disk: str | None = None  # a request only: a limit evicts the pod, and node disk pressure already does

    def to_flyte(self) -> flyte.Resources:
        """Return the SDK's resources for this spec; an empty spec is `flyte.Resources()` (no requests)."""
        import flyte

        return flyte.Resources(
            cpu=_pair(self.cpu_request, self.cpu_limit),
            memory=_pair(self.memory_request, self.memory_limit),
            disk=self.disk,
        )

    @property
    def cpu(self) -> float:
        """The CPU request in cores."""
        return cpu_cores(self.cpu_request) if self.cpu_request else 0.0

    @property
    def memory_gib(self) -> float:
        """The memory request in GiB."""
        return memory_bytes(self.memory_request) / 2**30 if self.memory_request else 0.0

    @property
    def disk_gib(self) -> float:
        """The ephemeral-storage request in GiB."""
        return memory_bytes(self.disk) / 2**30 if self.disk else 0.0


def _pair(request: str | None, limit: str | None) -> str | tuple[str, str] | None:
    return (request, limit) if request and limit else request


def cpu_cores(value: str) -> float:
    if not _CPU.fullmatch(value):
        raise ValueError(f"not a CPU quantity: {value!r}")
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def memory_bytes(value: str) -> float:
    match = _MEMORY.fullmatch(value)
    if not match:
        raise ValueError(f"not a memory or disk quantity: {value!r}")
    return float(match[1]) * _MEMORY_UNIT[match[3] or ""]


def parse(data: Mapping[str, Any]) -> Mapping[str, Spec]:
    """Return the `[resources.<component>]` tables of a profile as specs holding only the keys they set.

    Raises `ResourcesError` for an unknown component or key, or a malformed quantity.
    """
    out = {}
    for component, table in data.items():
        if component not in COMPONENTS:
            raise ResourcesError(f"unknown [resources.{component}]; one of {', '.join(COMPONENTS)}")
        if not isinstance(table, Mapping):
            raise ResourcesError(f"[resources.{component}] must be a table")
        if unknown := sorted(set(table) - set(KEYS)):
            raise ResourcesError(
                f"[resources.{component}] unknown key(s): {', '.join(unknown)}; one of {', '.join(KEYS)}"
            )
        fields: dict[str, str | None] = {}
        for key, value in table.items():
            at = f"[resources.{component}] {key}"
            request, limit = _request_limit(key, value, at)
            if key == "disk":
                fields["disk"] = request
            else:
                fields.update({f"{key}_request": request, f"{key}_limit": limit})
        out[component] = Spec(**fields)
    return out


def _request_limit(key: str, value: Any, where: str) -> tuple[str, str | None]:
    pair = isinstance(value, list)
    if pair and (key == "disk" or len(value) != 2):
        raise ResourcesError(
            f"{where} must be one quantity"
            if key == "disk"
            else f"{where} must be a quantity or [request, limit]"
        )
    parts = [str(v) for v in (value if pair else [value])]
    if any(isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in (value if pair else [value])):
        raise ResourcesError(f"{where} must be a quantity such as 500m or 2Gi, not {value!r}")
    amounts = []
    for part in parts:
        try:
            amount = cpu_cores(part) if key == "cpu" else memory_bytes(part)
        except ValueError as exc:
            raise ResourcesError(f"{where}: {exc}") from exc
        if amount <= 0:
            raise ResourcesError(f"{where} must be positive, not {part!r}")
        amounts.append(amount)
    if len(amounts) == 2 and amounts[0] > amounts[1]:
        raise ResourcesError(f"{where}: the request {parts[0]} is above the limit {parts[1]}")
    return parts[0], parts[1] if pair else None
