"""A load-adjusted clock for a check's process tree: waiting for CPU the tree was due is not counted.

The pre-review checks run on a node shared with other pods. When it is busy the tree is runnable but not
running, and a wall-clock limit then bounces a project whose tests all pass for time it does not control. This
clock reads how long each thread of the tree waited on a run queue (`/proc/<pid>/task/<tid>/schedstat`) and
advances more slowly by that share. A hung or genuinely slow check still advances at wall speed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from flyte_agent_factory import _lane_shards

#: How often the tree is sampled, in seconds.
SAMPLE_EVERY_S = 1.0

#: The run is killed at this many times the limit of wall time whatever the load: a check that is starved
#: of CPU cannot keep the supervisor (and the run's budget) waiting forever, and 3x is more than any
#: node load seen so far has cost (905 s of work took ~1800 s at load 19 on 4 CPUs).
WALL_BACKSTOP = 3

#: Waiting for CPU for more than this share of the wall time is what makes a timeout "the node was busy".
_LOAD_SHARE = 0.25

#: Cumulative (ns on CPU, ns waiting on the run queue) per "pid/tid" of the processes in the tree.
Sample = dict[str, tuple[int, int]]
Sampler = Callable[[int], Sample | None]

_CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def cpu_allowance() -> float:
    """Return how many CPUs this pod may use: its affinity, cut by a cgroup v2 quota when it has one."""
    return _lane_shards.read_cpu_quota(_CGROUP_CPU_MAX)


def sample_group(pgid: int) -> Sample | None:
    """Return the schedstat of every thread of every live process in process group or session `pgid`.

    None when the kernel does not provide schedstat (no /proc, or built without it). A process that
    started and ended between two samples is never seen; that only makes the clock closer to wall time.
    Neither is one that left the group with `setsid` (the gate's own tools start their own session).
    """
    proc = Path("/proc")
    if not (proc / "self/schedstat").exists():
        return None
    found: Sample = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The command name is in parentheses and may hold spaces; the fields after it are fixed.
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
            if pgid not in (int(fields[2]), int(fields[3])):  # process group, session
                continue
            for task in (entry / "task").iterdir():
                cpu, wait = (task / "schedstat").read_text().split()[:2]
                found[f"{entry.name}/{task.name}"] = (int(cpu), int(wait))
        except (OSError, ValueError, IndexError):
            continue  # it exited while being read
    return found


class LoadClock:
    """Measures how much uncontended time a process group has used. Call `poll()` until it returns True.

    Each interval of wall time `dt` advances `adjusted` by `dt * rate`. With `cpu` and `wait` the CPU time and
    run-queue wait the tree's threads accumulated in that interval, demand is `p = (cpu + wait) / dt` and the
    tree got `g = cpu / dt`; `rate = min(1, g / min(p, n))` for `n` CPUs in the pod, or 1 when the tree
    asked for nothing (sleeping, hung). Capping the demand at `n` means a tree running 8 workers on 4 CPUs
    by itself gets g = 4 and rate 1: waiting caused by its own oversubscription is not forgiven.
    """

    def __init__(
        self,
        limit_s: float,
        pgid: int,
        *,
        sampler: Sampler = sample_group,
        now: Callable[[], float] = time.monotonic,
        cpus: float | None = None,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
        interval_s: float = SAMPLE_EVERY_S,
    ) -> None:
        self.limit_s = limit_s
        self.pgid = pgid
        self.cpus = cpus if cpus is not None else cpu_allowance()
        self._sampler = sampler
        self._now = now
        self._loadavg = loadavg
        self._interval_s = interval_s
        self._started = now()
        self._last_at = self._started
        self._adjusted = 0.0
        self.cpu_s = 0.0
        self.wait_s = 0.0
        self.measured = True
        self._previous = self._sample()
        if self._previous is None:
            self.measured = False
        self.load_start = self._load()
        self.load_end = self.load_start
        self.load_max = self.load_start

    def _sample(self) -> Sample | None:
        try:
            return self._sampler(self.pgid)
        except OSError:
            return None

    def _load(self) -> float | None:
        try:
            return self._loadavg()[0]
        except OSError:
            return None

    @property
    def wall(self) -> float:
        return self._now() - self._started

    @property
    def adjusted(self) -> float:
        """The uncontended time used so far; the part since the last sample counts at wall speed."""
        return self._adjusted + (self._now() - self._last_at)

    def poll(self) -> bool:
        """Sample the tree when it is due, and return True when the limit or the wall backstop is reached."""
        now = self._now()
        if self.measured and now - self._last_at >= self._interval_s:
            self._advance(now)
        return self.adjusted >= self.limit_s or self.wall >= WALL_BACKSTOP * self.limit_s

    def _advance(self, now: float) -> None:
        dt = now - self._last_at
        sample = self._sample()
        if sample is None:
            # schedstat went away: the rest of the run is wall time, and the numbers are no longer whole.
            self.measured = False
            return
        assert self._previous is not None
        # A thread missing from the previous sample whose whole life fits in this interval began in it: a
        # suite of short subprocesses (git, CLIs) is then forgiven its wait too, not charged at wall speed.
        # One with more on its counters than the interval holds (it joined the group, or a read failed) has
        # a life from before, so it waits for the next sample like any other.
        before = dict(self._previous)
        for key, (c, w) in sample.items():
            if key not in before and c + w <= dt * 1e9:
                before[key] = (0, 0)
        shared = before.keys() & sample.keys()
        cpu = sum(sample[k][0] - before[k][0] for k in shared) / 1e9
        wait = sum(sample[k][1] - before[k][1] for k in shared) / 1e9
        demand = (cpu + wait) / dt
        rate = 1.0 if demand <= 0 else min(1.0, (cpu / dt) / min(demand, self.cpus))
        self._adjusted += dt * rate
        self._last_at = now
        self._previous = sample
        self.cpu_s += cpu
        self.wait_s += wait
        load = self._load()
        if load is not None:
            self.load_end = load
            self.load_max = load if self.load_max is None else max(self.load_max, load)

    def _got(self) -> float:
        total = self.cpu_s + self.wait_s
        return self.cpu_s / total if total else 1.0

    def _load_text(self) -> str:
        if self.load_max is None:
            return f"{self.cpus:g} CPUs"
        return f"load average up to {self.load_max:.1f} on {self.cpus:g} CPUs"

    @property
    def lost_to_load(self) -> bool:
        """True when the node, not the checks, took a large share of the wall time."""
        return self.measured and self.wall - self.adjusted > _LOAD_SHARE * self.wall

    def attribution(self) -> str:
        """Return one line saying whether a timeout was the node's load or the checks being slow."""
        if not self.measured:
            return "load was not measured: this is wall-clock time"
        if self.lost_to_load:
            return (
                f"the node was busy: the checks waited {self.wait_s:.0f} s for CPU "
                f"(got {self._got():.0%} of the CPU they asked for; {self._load_text()}); "
                f"{self.adjusted:.0f} s of uncontended time used over {self.wall:.0f} s wall"
            )
        return (
            f"the checks themselves are slow: {self.adjusted:.0f} s with little CPU wait "
            f"(got {self._got():.0%} of the CPU they asked for; {self._load_text()}; "
            f"{self.cpu_s:.0f} CPU-seconds used)"
        )

    def summary(self) -> str:
        """Return a short note for a finished run when load made it differ from wall time, else ''."""
        if not self.measured or self.wall - self.adjusted <= max(10.0, 0.1 * self.wall):
            return ""
        return f"{self.adjusted:.0f}s load-adjusted, {self._load_text()}"
