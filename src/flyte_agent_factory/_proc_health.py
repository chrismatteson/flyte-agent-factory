"""What /proc says about the pod's process table: how many processes, how many zombies, who is PID 1.

A zombie is a finished process nobody has waited for. PID 1 reaps the orphans re-parented to it; the flyte
`a0` process does not, so the pods run under tini (`_images.INIT_WRAPPER`). This is how a pod shows whether
that is in place, and when it is not, why every walk of /proc (`_load_clock.sample_group`) got slow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Below this many zombies a scan of /proc costs nothing measurable, so it is not worth saying.
ZOMBIES_WORTH_NAMING = 50


@dataclass(frozen=True)
class ProcHealth:
    processes: int
    zombies: int
    #: PID 1's command name, None when it cannot be read.
    init: str | None

    @property
    def burdened(self) -> bool:
        return self.zombies >= ZOMBIES_WORTH_NAMING

    def describe(self) -> str:
        return f"PID 1 {self.init or 'unknown'}, {self.processes} processes, {self.zombies} zombies"


def snapshot(proc: Path = Path("/proc")) -> ProcHealth:
    """Return the process counts in `proc`; a process that ends during the scan is skipped."""
    processes = zombies = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The command name is in parentheses and may hold spaces; the state is the first field after it.
            state = (entry / "stat").read_text().rpartition(")")[2].split()[0]
        except (OSError, ValueError, IndexError):
            continue
        processes += 1
        zombies += state == "Z"
    try:
        init = (proc / "1/comm").read_text().strip()
    except OSError:
        init = None
    return ProcHealth(processes, zombies, init)
