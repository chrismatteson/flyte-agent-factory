"""What every deployed component asks Kubernetes for, and the arithmetic that says it fits one small node.

The single source of truth: each environment declaration calls `for_component(profile, name).to_flyte()`, the
profile's `[resources.<name>]` tables raise or lower a default, and `budget(profile)` adds up the same specs.
A test compares both against the real environment objects, so the table in docs/small-nodes.md cannot drift.

    PYTHONPATH=src python -m flyte_agent_factory._resources [--profile NAME]    # the budget, as markdown

Flyte semantics this relies on: a single value (`cpu="500m"`) is a request only; a pair is (request, limit).
The SDK writes nothing for an all-None `flyte.Resources()`, and Kubernetes then makes the pod BestEffort.
"""

from __future__ import annotations

import math
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

# Nothing else from the factory is imported here: `_slow_lane` reads its resources through this module, and
# the launcher may import only test-harness files (`_gates._TEST_HARNESS`), of which this and
# `_profile_resources` are two.
from flyte_agent_factory import _profile_resources
from flyte_agent_factory._profile_resources import Spec as Spec

if TYPE_CHECKING:
    from . import _profile

#: The variable a pod carries its profile's text in (`_profile.TOML_ENV`; a test keeps the two equal).
PROFILE_TOML_ENV = "AGENT_FACTORY_PROFILE_TOML"


#: The node the defaults are sized for: one e2-standard-4 (4 vCPU, 16 GiB). GKE allocatable on it is about
#: 3.92 CPU and 13 GiB; kube-system's DaemonSets and the kubelet take the rest, so the factory plans on 3 CPU
#: and 12 GiB of requests and leaves the difference as headroom.
BUDGET_CPU = 3.0
BUDGET_MEMORY_GIB = 12.0
#: A default GKE boot disk is 100 GB; the images and the OS take half. Ephemeral-storage requests are counted
#: against it.
BUDGET_DISK_GIB = 50.0

#: CPUs per test-lane shard unless `AGENT_FACTORY_LANE_CPUS` says otherwise (`_slow_lane.CPUS_DEFAULT`).
LANE_CPUS_DEFAULT = 2


def lane_shard(cpus: int) -> Spec:
    """Return a test-lane shard's resources: the limit is `cpus` (the quota a test sees), the request 3/4.

    A node's allocatable CPU is below its size (system pods hold some), so a request of the whole size can
    never place on a node of exactly that size: 2 CPUs requested stay unplaced on a 2-CPU node, 1.5 place.
    Memory: 1.5 GiB per CPU requested, 2 per CPU plus 1 as the limit, so one xdist worker per CPU, each with
    its own Terraform and pytest-in-pytest children, fits. A guess to be tuned from the peak memory every
    shard prints at the end of its tail.
    """
    return Spec(f"{round(0.75 * cpus * 1000)}m", str(cpus), f"{math.ceil(1.5 * cpus)}Gi", f"{2 * cpus + 1}Gi")


# Each default is sized from the measurements in docs/small-nodes.md (Evidence). The request is what schedules
# and what the budget adds up; the limit is what a burst may use, and a limit above the node is harmless.
DEFAULTS: Mapping[str, Spec] = {
    # A slot pod holds a Claude Code session: 0.27 CPU on average and ~0.5 GiB anonymous memory over a live
    # session's first minutes, the Flyte runtime process alone peaking near 0.9 GiB. Heavy work runs in a
    # child.
    # Disk was 120Gi, which a 100GB default boot disk cannot place. It stays a request only, so exceeding it
    # is not an eviction by itself; the LJSpeech eviction that raised it from 30Gi was node disk pressure.
    "slot": Spec("500m", "4", "1536Mi", "8Gi", "20Gi"),
    "subagent": Spec("500m", "4", "1536Mi", "8Gi", "10Gi"),
    # Setup tasks are a few API calls.
    "setup": Spec("250m", "1", "256Mi", "1Gi"),
    # Nothing at all (BestEffort): 104 MiB idle, 1.5 CPU-seconds at startup. Under node pressure a BestEffort
    # pod is killed first (oom_score_adj 1000), instead of starving the pods that hold work; it restarts
    # cheaply.
    "mcp": Spec(),
    # A FastAPI process forwarding deliveries (~80 MiB): as `mcp`.
    "webhook": Spec(),
    # Chromium is unbounded and a limit forces a request (Kubernetes copies it), so a 256Mi request goes with
    # the 4Gi cap. No CPU request: it bursts. Disk was 20Gi with no measurement behind it.
    "browser": Spec(None, None, "256Mi", "4Gi", "10Gi"),
    # A scheduled tick is the Flyte runtime plus one module import (70-90 MiB, under 1.5 CPU-seconds) and a
    # few HTTP calls: 100-300 MiB, CPU-bound only while importing.
    "ticks": Spec("100m", "1", "256Mi", "1Gi"),
    # The ticks' floor, plus subprocesses that import the agent and submit a remote image build.
    "deployer": Spec("250m", "2", "512Mi", "2Gi"),
    # One standards-gate scan of one repository; `_factory.scan_targets` runs them one at a time.
    "scan": Spec("250m", "2", "512Mi", "4Gi"),
    # A test lane runs as shards (`_slow_lane`, `_lane_shards`), each a child of `AGENT_FACTORY_LANE_CPUS`
    # CPUs sized by `lane_shard`; this is one default-sized shard. The profile's `[resources.lanes]` applies
    # on top of a shard of any size (`for_lane_shard`).
    "lanes": lane_shard(LANE_CPUS_DEFAULT),
    # The benchmark's hidden checks (pytest, scikit-learn, Terraform init). Heavy and optional: kept at what
    # it was and left out of the default budget.
    "bench": Spec("1", None, "2Gi", None),
}


def for_component(profile: _profile.Profile | None, name: str) -> Spec:
    """Return `name`'s default spec with the profile's `[resources.<name>]` keys laid over it.

    A key the table sets replaces both its request and its limit; a key it leaves out keeps the default.
    """
    return _overlay(DEFAULTS[name], profile.resources.get(name) if profile else None)


def for_profile_text(text: str, name: str) -> Spec:
    """Return `for_component` for a profile given as TOML text (a pod's `AGENT_FACTORY_PROFILE_TOML`).

    For `_slow_lane`, which cannot import `_profile`; the text was validated when the profile was deployed.
    """
    return _overlay(DEFAULTS[name], _table_from_text(text, name))


def for_lane_shard(text: str, cpus: int) -> Spec:
    """Return a `cpus`-CPU shard's resources with the profile text's `[resources.lanes]` keys laid over them.

    Without that table it is `lane_shard(cpus)`: the shard size decides. A key the table sets wins at every
    shard size, as `for_component` does for any other component.
    """
    return _overlay(lane_shard(cpus), _table_from_text(text, "lanes"))


def _table_from_text(text: str, name: str) -> Spec | None:
    tables = tomllib.loads(text).get("resources", {}) if text.strip() else {}
    return _profile_resources.parse(tables).get(name)


def _overlay(spec: Spec, override: Spec | None) -> Spec:
    if override is None:
        return spec
    changes: dict[str, str | None] = {}
    if override.cpu_request:
        changes.update(cpu_request=override.cpu_request, cpu_limit=override.cpu_limit)
    if override.memory_request:
        changes.update(memory_request=override.memory_request, memory_limit=override.memory_limit)
    if override.disk:
        changes["disk"] = override.disk
    return replace(spec, **changes)


@dataclass(frozen=True)
class Line:
    component: str
    name: str  # the environment or app as deployed
    spec: Spec


@dataclass(frozen=True)
class Totals:
    cpu: float
    memory_gib: float
    disk_gib: float

    @property
    def fits(self) -> bool:
        return (
            self.cpu <= BUDGET_CPU
            and self.memory_gib <= BUDGET_MEMORY_GIB
            and self.disk_gib <= BUDGET_DISK_GIB
        )


def _totals(lines: Iterable[Line]) -> Totals:
    lines = list(lines)
    return Totals(
        sum(line.spec.cpu for line in lines),
        sum(line.spec.memory_gib for line in lines),
        sum(line.spec.disk_gib for line in lines),
    )


@dataclass(frozen=True)
class Budget:
    idle: tuple[Line, ...]  # the always-on apps and every scheduled tick, running at once
    live: tuple[Line, ...]  # what one live project adds
    optional: tuple[Line, ...]  # heavy and not counted
    shard: Spec  # one test-lane shard as the profile sizes it

    @property
    def idle_total(self) -> Totals:
        return _totals(self.idle)

    @property
    def live_total(self) -> Totals:
        return _totals(self.idle + self.live)

    @property
    def shards_at_once(self) -> int:
        """How many lane shards fit at once beside the idle factory and one slot; the rest wait for room."""
        floor = _totals(self.idle + tuple(line for line in self.live if line.component == "slot"))
        rooms = (
            (BUDGET_CPU - floor.cpu, self.shard.cpu),
            (BUDGET_MEMORY_GIB - floor.memory_gib, self.shard.memory_gib),
            (BUDGET_DISK_GIB - floor.disk_gib, self.shard.disk_gib),
        )
        return max(0, min(int(room // each + 1e-9) for room, each in rooms if each))


def budget(profile: _profile.Profile) -> Budget:
    """Return every request the profile deploys, and the totals for an idle factory and one live project."""

    def line(component: str, name: str) -> Line:
        return Line(component, name, for_component(profile, component))

    idle = [line("mcp", "factory-mcp"), line("ticks", "factory"), line("deployer", "factory-deployer")]
    if profile.github.webhook_secret:
        idle.append(line("webhook", "factory-webhook"))
        # As `_webhook.env` lists it: intake needs maintainers too.
        if profile.github.maintainers:
            idle.append(line("ticks", "factory-issues"))
    idle += [line("browser", identity.app) for identity in profile.browser.identities]
    # As `_deploy.FACTORY_CHILD` deploys them.
    if profile.board or profile.github.configured:
        idle.append(line("ticks", "factory-reconcile"))
    if profile.loop.enabled:
        idle.append(line("ticks", "factory-loop"))
    if profile.bench:
        idle.append(line("ticks", "factory-bench"))
    # `_factory.scan_targets` runs one scan at a time, so the weekly gardening counts one.
    idle.append(line("scan", "gardening-scan"))

    # A project's own children: its checks' test lanes or one sub-agent, whichever asks for more. `checks.sh`
    # runs the fast and slow lanes at once and `_slow_lane` submits every shard of both at once, but a shard
    # that finds no room waits for one (up to 2 min on spot, then 3 min on-demand, before it is a capacity
    # outcome), so what must fit beside the slot is one shard; `Budget.shards_at_once` says how many the node
    # holds together. Further sub-agents wait likewise. Setup tasks (`factory-setup`) are an owner's one-off
    # bootstrap, not part of a project's run, so they are not counted; the headroom left by the totals covers
    # one anyway.
    shard = (line("lanes", "test-lanes"),)
    subagent = (line("subagent", "subagent"),)
    children = max(shard, subagent, key=lambda group: (_totals(group).cpu, _totals(group).memory_gib))
    live = (line("slot", "slot"), *children)
    optional = (line("bench", "factory-bench-check"),) if profile.bench else ()
    return Budget(tuple(idle), live, optional, shard[0].spec)


def _fmt_pair(request: str | None, limit: str | None) -> str:
    if not request:
        return "none"
    return f"{request} / {limit}" if limit else f"{request} / none"


def markdown(profile: _profile.Profile) -> str:
    """Return `budget(profile)` as the tables docs/small-nodes.md shows."""
    result = budget(profile)
    rows = [
        "| Component | Environment | CPU request / limit | Memory request / limit | Disk request |",
        "|---|---|---|---|---|",
    ]
    for title, lines in (
        ("always on", result.idle),
        ("one live project", result.live),
        ("optional", result.optional),
    ):
        if lines:
            rows.append(f"| **{title}** | | | | |")
        for entry in lines:
            spec = entry.spec
            rows.append(
                f"| {entry.component} | {entry.name} | {_fmt_pair(spec.cpu_request, spec.cpu_limit)} "
                f"| {_fmt_pair(spec.memory_request, spec.memory_limit)} | {spec.disk or 'none'} |"
            )
    rows += ["", "| Scenario | CPU | Memory | Ephemeral disk |", "|---|---|---|---|"]
    for title, total in (("idle", result.idle_total), ("one project live", result.live_total)):
        rows.append(f"| {title} | {total.cpu:.2f} | {total.memory_gib:.2f} Gi | {total.disk_gib:.0f} Gi |")
    rows.append(f"| budget | {BUDGET_CPU:.2f} | {BUDGET_MEMORY_GIB:.2f} Gi | {BUDGET_DISK_GIB:.0f} Gi |")
    rows += ["", f"Lane shards that fit at once beside one live slot: {result.shards_at_once}."]
    return "\n".join(rows)


if __name__ == "__main__":
    from . import _profile

    _profile.select_from_argv()
    print(markdown(_profile.require()))
    sys.exit(0)
