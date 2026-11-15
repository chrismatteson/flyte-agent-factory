"""The load-adjusted clock: waiting for CPU the tree was due is forgiven, nothing else is."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from flyte_agent_factory import _load_clock

NS = 1_000_000_000


class Fake:
    """A scripted node: each `step` moves time on and adds CPU and run-queue wait to one thread."""

    def __init__(self, cpus: float = 4, load: float = 1.0) -> None:
        self.t = 0.0
        self.cpu = 0
        self.wait = 0
        self.load = load
        self.available = True

    def clock(self, limit_s: float = 900, **kwargs) -> _load_clock.LoadClock:
        return _load_clock.LoadClock(
            limit_s,
            1,
            sampler=lambda _pgid: {"1/1": (self.cpu, self.wait)} if self.available else None,
            now=lambda: self.t,
            cpus=kwargs.pop("cpus", 4),
            loadavg=lambda: (self.load, 0.0, 0.0),
            **kwargs,
        )

    def step(self, clock: _load_clock.LoadClock, seconds: float, cpu: float, wait: float) -> bool:
        self.t += seconds
        self.cpu += int(cpu * NS)
        self.wait += int(wait * NS)
        return clock.poll()


def run(node: Fake, clock: _load_clock.LoadClock, wall: int, cpu_per_s: float, wait_per_s: float) -> bool:
    return any(node.step(clock, 1, cpu_per_s, wait_per_s) for _ in range(wall))


def test_without_contention_adjusted_time_is_wall_time():
    node = Fake()
    clock = node.clock()
    assert not run(node, clock, 600, cpu_per_s=1.0, wait_per_s=0.0)
    assert clock.adjusted == pytest.approx(600)
    assert clock.wall == 600


def test_a_check_needing_800s_finishes_in_1500s_of_wall_under_load():
    node = Fake()
    clock = node.clock()
    # One busy thread getting 800/1500 of a CPU: it waited for the rest, and 4 CPUs were due to it.
    assert not run(node, clock, 1500, cpu_per_s=800 / 1500, wait_per_s=1 - 800 / 1500)
    assert clock.adjusted == pytest.approx(800, abs=2)
    assert clock.wait_s == pytest.approx(700, abs=2)


def test_starved_check_times_out_on_adjusted_time_and_blames_the_load():
    node = Fake(load=19.4)
    clock = node.clock()
    assert run(node, clock, 2000, cpu_per_s=0.5, wait_per_s=0.5)
    assert clock.adjusted >= 900
    assert 1800 <= clock.wall <= 1802
    assert "the node was busy" in clock.attribution()
    assert "19.4" in clock.attribution()


def test_self_inflicted_oversubscription_is_not_forgiven():
    node = Fake()
    clock = node.clock(cpus=4)
    # 8 runnable threads on 4 CPUs, alone on the node: they got all 4 CPUs they could have had.
    run(node, clock, 600, cpu_per_s=4.0, wait_per_s=4.0)
    assert clock.adjusted == pytest.approx(600)


def test_part_of_the_demand_over_the_pod_allowance_is_forgiven_when_it_got_less_than_the_allowance():
    node = Fake()
    clock = node.clock(cpus=4)
    # Asked for 8 CPUs, got 2 of the 4 it was entitled to: rate 2/4.
    run(node, clock, 600, cpu_per_s=2.0, wait_per_s=6.0)
    assert clock.adjusted == pytest.approx(300, abs=2)


def test_idle_intervals_count_in_full():
    node = Fake()
    clock = node.clock()
    run(node, clock, 600, cpu_per_s=0.0, wait_per_s=0.0)
    assert clock.adjusted == pytest.approx(600)


def test_a_hung_check_times_out_at_the_unchanged_limit_and_is_called_slow():
    node = Fake()
    clock = node.clock(limit_s=100)
    assert run(node, clock, 200, cpu_per_s=0.0, wait_per_s=0.0)
    assert clock.wall == pytest.approx(100, abs=1)
    assert "the checks themselves are slow" in clock.attribution()


def test_wall_backstop_kills_a_check_that_barely_runs():
    node = Fake()
    clock = node.clock(limit_s=100)
    # 1% of a CPU: adjusted time crawls, but the run is over at 3x the limit of wall time.
    assert run(node, clock, 1000, cpu_per_s=0.01, wait_per_s=0.99)
    assert clock.adjusted < 100
    assert clock.wall == pytest.approx(300, abs=1)


def test_a_new_thread_does_not_bring_its_earlier_life_into_the_interval():
    node = Fake()
    samples = [{"1/1": (0, 0)}, {"1/1": (NS, 0), "1/2": (50 * NS, 0)}]
    clock = _load_clock.LoadClock(
        900, 1, sampler=lambda _p: samples.pop(0), now=lambda: node.t, cpus=4, loadavg=lambda: (0, 0, 0)
    )
    node.t = 1.0
    clock.poll()
    assert clock.cpu_s == pytest.approx(1.0)


@pytest.mark.parametrize("fails_at_start", [True, False])
def test_without_schedstat_the_clock_is_wall_time_and_says_so(fails_at_start):
    node = Fake()
    node.available = not fails_at_start
    clock = node.clock(limit_s=100)
    if not fails_at_start:
        run(node, clock, 10, cpu_per_s=1.0, wait_per_s=0.0)
        node.available = False
    assert run(node, clock, 200, cpu_per_s=0.5, wait_per_s=0.5)
    assert not clock.measured
    assert clock.wall == pytest.approx(100, abs=1)
    assert "not measured" in clock.attribution()


def test_attribution_names_load_or_slowness_clearly():
    busy = Fake(load=19.4)
    clock = busy.clock()
    run(busy, clock, 1830, cpu_per_s=0.31, wait_per_s=0.69)
    text = clock.attribution()
    assert text.startswith("the node was busy")
    assert "waited 1" in text and "31%" in text and "on 4 CPUs" in text

    slow = Fake(load=1.2)
    clock = slow.clock(limit_s=900)
    run(slow, clock, 901, cpu_per_s=3.7, wait_per_s=0.1)
    text = clock.attribution()
    assert text.startswith("the checks themselves are slow")
    assert "CPU-seconds used" in text and "1.2 on 4 CPUs" in text


def test_summary_is_empty_unless_load_made_a_difference():
    node = Fake()
    clock = node.clock()
    run(node, clock, 100, cpu_per_s=1.0, wait_per_s=0.0)
    assert clock.summary() == ""
    clock = node.clock()
    run(node, clock, 100, cpu_per_s=0.5, wait_per_s=0.5)
    assert "load-adjusted" in clock.summary()


def test_cpu_allowance_is_the_smaller_of_affinity_and_cgroup_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    cpu_max = tmp_path / "cpu.max"
    monkeypatch.setattr(_load_clock, "_CGROUP_CPU_MAX", cpu_max)
    cpu_max.write_text("200000 100000\n")
    assert _load_clock.cpu_allowance() == 2
    cpu_max.write_text("max 100000\n")
    assert _load_clock.cpu_allowance() == 4
    cpu_max.unlink()
    assert _load_clock.cpu_allowance() == 4


_BUSY = "while True: pass"


def test_a_thread_that_began_in_the_interval_counts_its_whole_life():
    # A short subprocess is seen in one sample only; its wait is forgiven like a long-lived thread's.
    samples = iter(
        [{"1/1": (0, 0)}, {"1/1": (int(0.25 * NS), int(0.25 * NS)), "2/2": (int(0.25 * NS), int(0.25 * NS))}]
    )
    t = [0.0]
    clock = _load_clock.LoadClock(
        900, 1, sampler=lambda _pgid: next(samples), now=lambda: t[0], cpus=4, loadavg=lambda: (1.0, 0, 0)
    )
    t[0] = 1.0
    clock.poll()
    assert clock.cpu_s == pytest.approx(0.5)
    assert clock.wait_s == pytest.approx(0.5)
    assert clock.adjusted == pytest.approx(0.5)


@pytest.mark.slow
def test_a_real_process_group_is_forgiven_only_for_rivals_outside_it():
    # Every process is pinned to one CPU and the child does a fixed amount of CPU work, so what the clock
    # counts depends only on who the rivals are, not on what else the node is running.
    cpu = min(os.sched_getaffinity(0))
    pin = f"import os, subprocess, sys, time; os.sched_setaffinity(0, {{{cpu}}})\n"
    busy = [sys.executable, "-c", f"{pin}{_BUSY}"]
    work = "end = time.process_time() + 1.0\nwhile time.process_time() < end: pass\n"

    def measure(outside: int, inside: int) -> _load_clock.LoadClock:
        rivals = [subprocess.Popen(busy, start_new_session=True) for _ in range(outside)]
        child_code = (
            f"{pin}rivals = [subprocess.Popen({busy!r}) for _ in range({inside})]\n"
            f"{work}for r in rivals: r.kill()\n"
        )
        try:
            child = subprocess.Popen([sys.executable, "-c", child_code], start_new_session=True)
            clock = _load_clock.LoadClock(1e9, child.pid, cpus=1, interval_s=0.2)
            while child.poll() is None:
                time.sleep(0.05)
                clock.poll()
            return clock
        finally:
            for rival in rivals:
                rival.kill()
                rival.wait()

    # Startup and the unsampled tail add a little to `adjusted` on a busy node, never take from it.
    alone = measure(outside=0, inside=0)
    assert alone.cpu_s >= 0.5, "the clock saw the child's work (all but its last interval)"
    assert alone.adjusted >= 0.9 * alone.cpu_s, "a tree is charged at least the CPU it used"
    contended = measure(outside=3, inside=0)
    assert contended.wall >= 2.5, "three rivals on the child's CPU slow it down"
    assert contended.adjusted >= 0.9 * contended.cpu_s
    assert contended.adjusted < 0.5 * contended.wall, "waiting for rivals outside is forgiven"
    own = measure(outside=0, inside=3)
    assert own.wall >= 2.5
    assert own.adjusted >= 2.5, "the CPU the tree's own rivals took is charged"
