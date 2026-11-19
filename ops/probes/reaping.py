"""Check that PID 1 in a slot pod reaps orphans, and that the load clock still keeps time after many of them.

    uv run python ops/probes/reaping.py [--profile NAME] [--orphans N] [--control] [--in-run]

Runs one Flyte task on the slot image (`agent_image()`, which carries `_images.with_init`). The task starts N
orphans the way the Bash tool does (`sh -c "sleep 0.2 &"`: the shell exits, its sleep goes to PID 1), counts
the zombies left, times one scan of `/proc` by the load clock, and runs a 0.3 s gate command under the load
clock, as `test_a_command_is_judged_by_the_load_clock_with_its_own_limit` in tests/test_gates.py does. Then,
in the same pod, it runs that test and tests/test_load_clock.py with pytest (`uv run --frozen`, on a copy of
this checkout's tracked `src`, `tests`, `ops`, `pyproject.toml` and `uv.lock` passed in as a file). It prints
`PASS` or `FAIL` and the measurements, with pytest's summary line. `--control` clears `_F_EXEC_WRAPPER` for
the task, so `a0` is PID 1 as before issue #34, and the zombies remain: that run is expected to print `FAIL`.

By default it runs as your own Flyte login, building the image with the profile's builder (`remote` on
Union), as a deploy does. `--in-run` submits it from inside a slot instead, as a child action of the slot's
run (`flyte_agent_factory.child.attach`). It needs an image build, so it is a coordinator probe, never part
of the offline checks. Results are in docs/process-reaping.md.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import flyte
import flyte.errors
from flyte.io import File

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from flyte_agent_factory import _profile, _profile_oss, _union_identity
from flyte_agent_factory._images import agent_image

ROOT = Path(__file__).resolve().parents[2]
env = flyte.TaskEnvironment(
    name="reaping-probe", image=agent_image(), resources=flyte.Resources(cpu=2, memory="4Gi")
)

# The tests the issue's acceptance names: the load clock's, and the gate test that failed in a burdened slot.
LOAD_CLOCK_TESTS = [
    "tests/test_load_clock.py",
    "tests/test_gates.py::test_a_command_is_judged_by_the_load_clock_with_its_own_limit",
]
PYTEST_FLAGS = ["-q", "-p", "no:cacheprovider"]
SOURCES = ["src", "tests", "ops", "pyproject.toml", "uv.lock"]


def _gate_command_is_polled() -> tuple[bool, str]:
    """Run a 0.3 s command under the load clock; True when it was polled with its own limit and passed."""
    from flyte_agent_factory import _gate_base, _load_clock

    limits: list[float] = []

    class Spy(_load_clock.LoadClock):
        def poll(self) -> bool:
            limits.append(self.limit_s)
            return super().poll()

    real = _load_clock.LoadClock
    _load_clock.LoadClock = Spy  # type: ignore[misc]
    try:
        command = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        result = _gate_base._run(command, Path.cwd(), timeout=30)
    finally:
        _load_clock.LoadClock = real  # type: ignore[misc]
    return result == (0, "") and set(limits) == {30}, f"result {result}, polled with {sorted(set(limits))}"


def _run_load_clock_tests(checkout: Path) -> tuple[bool, str]:
    """Run LOAD_CLOCK_TESTS in an unpacked checkout; True when they pass, with pytest's last line."""
    result = subprocess.run(
        ["uv", "run", "--frozen", "python", "-m", "pytest", *LOAD_CLOCK_TESTS, *PYTEST_FLAGS],
        cwd=checkout,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=900,
    )
    # pytest's summary is the last line it writes to stdout; uv writes its own lines to stderr.
    lines = result.stdout.strip().splitlines() or result.stderr.strip().splitlines()
    return result.returncode == 0, lines[-1] if lines else f"exit {result.returncode}, no output"


def _measure(orphans: int, checkout: Path) -> str:
    """Start the orphans, measure, run the tests; return the `PASS ...` or `FAIL ...` line."""
    from flyte_agent_factory import _load_clock, _proc_health

    before = _proc_health.snapshot()
    for _ in range(orphans):
        subprocess.run(["sh", "-c", "sleep 0.2 &"], check=True, timeout=10)
    time.sleep(2)
    after = _proc_health.snapshot()
    start = time.monotonic()
    _load_clock.sample_group(os.getpgrp())
    scan = time.monotonic() - start
    polled, gate = _gate_command_is_polled()
    passed, tests = _run_load_clock_tests(checkout)
    facts = {
        "uid": os.getuid(),
        "before": before.describe(),
        "after": after.describe(),
        "scan_ms": round(scan * 1000, 1),
        "gate": gate,
        "tests": tests,
    }
    ok = after.zombies == 0 and polled and passed and os.getuid() != 0
    return f"{'PASS' if ok else 'FAIL'} {json.dumps(facts)}"


@env.task
async def probe(orphans: int, checkout: File) -> str:
    """Return the `PASS ...` or `FAIL ...` line for N orphans in this pod.

    The engine is imported from the shipped checkout, not the code bundle: a child action submitted from a
    slot runs on its parent's bundle, which need not hold this checkout's `src`.
    """
    unpacked = Path(tempfile.mkdtemp())
    with tarfile.open(await checkout.download()) as tar:
        tar.extractall(unpacked, filter="data")
    sys.path.insert(0, str(unpacked / "src"))
    for name in [
        m for m in sys.modules if m == "flyte_agent_factory" or m.startswith("flyte_agent_factory.")
    ]:
        del sys.modules[name]
    return _measure(orphans, unpacked)


def pack_checkout(dest: Path) -> Path:
    """Write the tracked SOURCES of this checkout to a tarball in `dest` and return its path."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *SOURCES], cwd=ROOT, check=True, capture_output=True, timeout=60
    )
    tarball = dest / "checkout.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for name in listed.stdout.decode().split("\0"):
            if name:
                tar.add(ROOT / name, arcname=name)
    return tarball


async def submit_from_this_run(task: flyte.TaskTemplate[Any, Any, Any], orphans: int, checkout: File) -> str:
    """Run the probe as a child action of this slot's run: spot first, on-demand when spot is not placed.

    A child action does not build its image (only a run's own images are built when it starts), so the image
    is built here first, with the in-cluster builder `attach` configures, and the task is pointed at it.
    """
    built = await flyte.build.aio(env.image)
    task = dataclasses.replace(task, image=built.uri)
    bound = flyte.Timeout(max_queued_time=timedelta(minutes=5))
    try:
        return str(await task.override(interruptible=True, timeout=bound).aio(orphans, checkout))
    except flyte.errors.MaxQueuedTimeExceededError:
        return str(await task.override(interruptible=False, timeout=flyte.Timeout()).aio(orphans, checkout))


def main(argv: list[str] | None = None) -> None:
    """Submit the probe task and print its result line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    parser.add_argument("--orphans", type=int, default=3200, help="orphans to start (default: 3200)")
    parser.add_argument("--control", action="store_true", help="run with a0 as PID 1, as before issue #34")
    parser.add_argument("--in-run", action="store_true", help="submit as a child action of this slot's run")
    args = parser.parse_args(argv)
    task = probe.override(env_vars={"_F_EXEC_WRAPPER": ""}) if args.control else probe
    with tempfile.TemporaryDirectory() as scratch:
        tarball = pack_checkout(Path(scratch))
        if args.in_run:
            from flyte_agent_factory.child import attach

            attach()
            checkout = File.from_local_sync(str(tarball))
            print(asyncio.run(submit_from_this_run(task, args.orphans, checkout)))
            return
        _profile.select_from_argv(argv)
        profile = _profile.require()
        _union_identity.init_for_script(profile, image_builder=_profile_oss.builder(profile.backend))
        run = flyte.run(task, args.orphans, File.from_local_sync(str(tarball)))
        print(run.url)
        run.wait()
        print(run.outputs()[0])


if __name__ == "__main__":
    main()
