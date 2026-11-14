"""Check that an unprivileged slot can `podman build` and `podman run` a two-line Containerfile.

    uv run python ops/probes/podman_rootless.py [--profile NAME]

Runs one Flyte task on the slot image (`agent_image()`) that builds an image whose `RUN` step writes a
marker file, runs it, and prints `PASS` or `FAIL` plus the tail of podman's error. The base image is
pulled from Docker Hub, so the cluster needs egress to it. It runs as your own Flyte login (the profile's
`[backend] config`) and needs an image build, so it is a coordinator probe, never part of the offline checks.

Result on the Union EKS deployment (2026-10-31): FAIL. Pulls work, but the cluster refuses newuidmap's
multi-range map, setgroups in a single-uid namespace (`RUN` under chroot isolation) and mounting /proc in
a nested container (`podman run`). It passes only where pods may use user namespaces and an unmasked
/proc (e.g. `procMount: Unmasked`, or privileged), which is a cluster setting, not an image one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import flyte

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from flyte_agent_factory import _profile, _union_identity
from flyte_agent_factory._images import agent_image

_MARKER = "podman-rootless-ok"
# The RUN step is what needs chroot isolation; a COPY-only build would pass without it.
_CONTAINERFILE = f"""FROM docker.io/library/debian:bookworm-slim
RUN echo {_MARKER} > /marker
"""

env = flyte.TaskEnvironment(
    name="podman-rootless-probe", image=agent_image(), resources=flyte.Resources(cpu=1, memory="2Gi")
)


def _podman(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


@env.task
async def probe() -> str:
    """Return the `PASS ...` or `FAIL ...` line for a build and run as the task's unprivileged user."""
    if os.getuid() == 0:
        return "FAIL running as root; the probe only means something unprivileged"
    with tempfile.TemporaryDirectory(prefix="podman-probe-") as directory:
        context = Path(directory)
        (context / "Containerfile").write_text(_CONTAINERFILE)
        build = _podman("build", "-t", "podman-probe", ".", cwd=context)
        if build.returncode != 0:
            return f"FAIL podman build: {build.stderr.strip()[-500:]}"
        run = _podman("run", "--rm", "podman-probe", "cat", "/marker", cwd=context)
        if run.returncode != 0 or run.stdout.strip() != _MARKER:
            return f"FAIL podman run: {run.stderr.strip()[-500:]}"
        return f"PASS podman build and run as uid {os.getuid()}"


def main(argv: list[str] | None = None) -> None:
    """Submit the probe task and print its result line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    parser.parse_args(argv)
    _profile.select_from_argv(argv)
    _union_identity.init_for_script(_profile.require())
    run = flyte.run(probe)
    print(run.url)
    run.wait()
    print(run.outputs()[0])


if __name__ == "__main__":
    main()
