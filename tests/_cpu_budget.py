"""How many CPUs this process may use: the cgroup's CPU quota where one is set, else the CPUs it sees.

A pod sees every CPU of its node (`os.cpu_count()` is 16 on a 4-CPU pod), so `-n auto` sized from that
starts four times the workers the quota runs; the kernel then throttles them all, and a test that runs
a subprocess against a deadline (the benchmark lane's pytest-in-pytest) misses it.
"""

import math
import os
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")


def _quota(root: Path) -> float | None:
    """Return the CPUs the quota under `root` grants (v2 `cpu.max`, else v1 `cpu/cpu.cfs_*`), or None."""
    try:
        if (root / "cpu.max").is_file():
            quota, period = (root / "cpu.max").read_text().split()[:2]
        else:
            quota = (root / "cpu" / "cpu.cfs_quota_us").read_text().strip()
            period = (root / "cpu" / "cpu.cfs_period_us").read_text().strip()
        if quota in ("max", "-1"):
            return None
        return int(quota) / int(period)
    except (OSError, ValueError):
        return None


def visible() -> int:
    """Return the CPUs this process may be scheduled on."""
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1


def cpus(visible: int, root: Path = CGROUP) -> int:
    """Return `visible` capped by the quota under `root`, rounded up, and at least 1."""
    quota = _quota(root)
    return max(1, min(visible, math.ceil(quota)) if quota else visible)
