"""Every ops entry script runs as documented, `uv run python ops/<x>.py`: no PYTHONPATH, no login, no network.

`pyproject.toml` keeps `package = false`, so nothing puts `src` on the path for a script; each one does it
itself (as `ops/bootstrap.py` does). `--help` stands in for any invocation: it imports everything the script
imports at the top, and must neither log in nor reach the network.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Modules under ops/ that are not commands, with why. Everything else found by the glob is one.
NOT_ENTRY_POINTS = {
    "ops/personal_literals.py": "the pattern module `ops/scan_public.py` and the profile guard import",
}

#: Run a script the way `python <script>` does (its directory first on sys.path) with every socket refused,
#: so a network call fails the run instead of passing silently. gRPC's own sockets are not Python's: the
#: profile's Flyte config does not exist (see `_env`), so a login fails loudly before one is opened.
_RUNNER = """
import os, runpy, socket, sys

def refuse(*args, **kwargs):
    raise OSError("network access while printing --help")

socket.socket.connect = refuse
socket.create_connection = refuse
socket.getaddrinfo = refuse
target = sys.argv[1]
sys.argv = [target, "--help"]
if target.endswith(".py"):
    sys.path[0] = os.path.dirname(os.path.abspath(target))
    runpy.run_path(target, run_name="__main__")
else:
    sys.path[0] = os.getcwd()
    runpy.run_module(target, run_name="__main__", alter_sys=True)
"""


def _entry_points() -> list[str]:
    scripts = sorted(ROOT.glob("ops/*.py")) + sorted(ROOT.glob("ops/probes/*.py"))
    found = [str(p.relative_to(ROOT)) for p in scripts if not p.name.startswith("_")]
    return [p for p in found if p not in NOT_ENTRY_POINTS] + ["ops.model_eval"]


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # Neither the person's Flyte config nor the pod's; the suite's profile names a config file that does not
    # exist, so any login attempt raises.
    env["HOME"] = str(home)
    for name in ("FLYTECTL_CONFIG", "UNION_CONFIG", "FLYTE_CONFIG"):
        env.pop(name, None)
    return env


def test_the_exclusions_still_exist():
    for path in NOT_ENTRY_POINTS:
        assert (ROOT / path).is_file(), f"{path} is gone: drop it from NOT_ENTRY_POINTS"


@pytest.mark.parametrize("target", _entry_points())
def test_help_runs_without_pythonpath_or_network(target, tmp_path):
    done = subprocess.run(
        [sys.executable, "-c", _RUNNER, target],
        cwd=ROOT,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "usage:" in done.stdout


@pytest.mark.parametrize("target", _entry_points())
def test_help_runs_with_no_profile_selected(target, tmp_path):
    # `--help` is how someone finds out what a script needs, so it cannot itself need a profile (#223).
    env = _env(tmp_path)
    for name in ("AGENT_FACTORY_PROFILE", "AGENT_FACTORY_PROFILE_TOML"):
        env.pop(name, None)
    done = subprocess.run(
        [sys.executable, "-c", _RUNNER, target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "usage:" in done.stdout


@pytest.mark.parametrize("target", _entry_points())
def test_help_runs_under_a_profile_from_before_union(target, tmp_path):
    # A shell or pod still carrying a pre-[union] profile: usage prints, then says why the profile is refused.
    env = _env(tmp_path)
    env.pop("AGENT_FACTORY_PROFILE", None)
    profile = (ROOT / "tests" / "profiles" / "work.toml").read_text()  # a Union backend
    env["AGENT_FACTORY_PROFILE_TOML"] = re.sub(
        r"(?ms)^\[union\]\n.*?(?=^\[)", "", profile
    )  # no [union] table
    done = subprocess.run(
        [sys.executable, "-c", _RUNNER, target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "usage:" in done.stdout
