"""PID 1 reaps orphans: the factory pods run under tini, and these tests run a real one."""

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import flyte
import pytest
from flyte._image import _CREATE_FLYTE_USER_CMD

from flyte_agent_factory import _gate_base, _images, _load_clock, _slow_lane
from tests import _tini as _tini_fetch
from tests.test_deploy_personal_browser import PERSONAL, WORK

ORPHANS = 200
WORKLOAD_EXIT = 7

# Makes ORPHANS orphans: each middle process starts a grandchild and exits at once, so the grandchild is
# re-parented to the nearest subreaper (tini) and becomes its zombie when it ends. The middles are waited for
# here, so they are not zombies. Then it counts zombies whose parent is the one that started it.
_WORKLOAD = f"""
import os, sys, time
parent = os.getppid()
middles = []
for _ in range({ORPHANS}):
    pid = os.fork()
    if pid == 0:
        if os.fork() == 0:
            time.sleep(0.2)
            os._exit(0)
        os._exit(0)
    middles.append(pid)
for pid in middles:
    os.waitpid(pid, 0)

def zombies():
    count = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = open(f"/proc/{{name}}/stat").read().rpartition(")")[2].split()
        except OSError:
            continue
        count += fields[0] == "Z" and int(fields[1]) == parent
    return count

time.sleep(1)
deadline = time.monotonic() + 5
count = zombies()
while count and time.monotonic() < deadline:
    time.sleep(0.1)
    count = zombies()
print(count)
sys.exit({WORKLOAD_EXIT})
"""


def _tini() -> str:
    path = shutil.which("tini")
    if path is None:
        pytest.fail(
            "tini is not on PATH and could not be fetched (tests/_tini.py: "
            f"{_tini_fetch.FETCH_FAILURE or 'not attempted'}): the factory images install it "
            "(apt-get install tini)"
        )
    return path


def _under_tini() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_tini(), "-s", "--", sys.executable, "-c", _WORKLOAD], capture_output=True, text=True, timeout=60
    )


# A subreaper that does not wait for what it adopts while the workload counts, as the a0 process never did.
# It collects them before it exits: otherwise they pass to the pod's own PID 1, which may not reap either.
_NEGLIGENT = f"""
import ctypes, os, subprocess, sys
ctypes.CDLL(None).prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
child = subprocess.Popen([sys.executable, "-c", {_WORKLOAD!r}])
try:
    child.wait(timeout=50)
except subprocess.TimeoutExpired:
    child.kill()
    child.wait()
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
"""


def test_tini_leaves_no_zombie_behind_orphans():
    result = _under_tini()

    assert result.returncode == WORKLOAD_EXIT, result.stderr
    assert result.stdout.strip() == "0"


def test_a_parent_that_never_waits_does_collect_zombies():
    """The control: without it a count of 0 above could be the counting, not the reaping."""
    result = subprocess.run([sys.executable, "-c", _NEGLIGENT], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) >= ORPHANS


def test_tini_forwards_sigterm_and_passes_the_exit_status_on(tmp_path):
    marker = tmp_path / "got-sigterm"
    script = (
        "import signal, sys, time\n"
        "def stop(*_):\n"
        f"    open({str(marker)!r}, 'w').write('x')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [_tini(), "-s", "--", sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"

        proc.send_signal(signal.SIGTERM)

        assert proc.wait(timeout=10) == 0
        assert marker.read_text() == "x"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_load_clock_is_still_polled_after_a_burst_of_orphans(tmp_path, monkeypatch):
    assert _under_tini().returncode == WORKLOAD_EXIT
    limits = []
    polled = tmp_path / "polled"

    class Spy(_load_clock.LoadClock):
        def poll(self):
            limits.append(self.limit_s)
            polled.touch()
            return super().poll()

    monkeypatch.setattr(_load_clock, "LoadClock", Spy)
    # The command runs until the clock has polled it, not for a fixed 0.3 s: on a loaded host the clock's
    # first scan of /proc can outlast a short command, which is the race #34 is about, not what this test
    # measures. Never polled, it gives up after 20 s and fails with status 1.
    wait = "import os, sys, time\nfor _ in range(400):\n    if os.path.exists(sys.argv[1]): sys.exit(0)\n"
    command = [sys.executable, "-c", wait + "    time.sleep(0.05)\nsys.exit(1)", str(polled)]

    assert _gate_base._run(command, tmp_path, timeout=30) == (0, "")
    assert limits and set(limits) == {30}


# --- configuration: the pods really start under it ---


def _layers_before_the_user(image: flyte.Image) -> tuple[Any, ...]:
    layers = image._layers
    last = max(
        i for i, layer in enumerate(layers) if _CREATE_FLYTE_USER_CMD in getattr(layer, "commands", ())
    )
    assert last == len(layers) - 1, "run_as_flyte must stay the last layer"
    return layers[:last]


@pytest.mark.parametrize(
    "image",
    [
        lambda: _images.agent_image(),
        lambda: _images.agent_image(browser=True),
        lambda: _images.agent_image(tailscale=True),
        lambda: _images.agent_image(browser=True, tailscale=True),
        lambda: _images.browser_image(),
        lambda: _images.browser_image(tailscale=True),
    ],
    ids=[
        "agent",
        "agent-browser",
        "agent-tailscale",
        "agent-browser-tailscale",
        "browser",
        "browser-tailscale",
    ],
)
def test_every_image_installs_tini_and_runs_the_task_under_it(image):
    before_user = _layers_before_the_user(image())

    packages = [p for layer in before_user for p in getattr(layer, "packages", ())]
    wrappers = [
        dict(layer.env_vars)["_F_EXEC_WRAPPER"]
        for layer in before_user
        if "_F_EXEC_WRAPPER" in dict(getattr(layer, "env_vars", None) or ())
    ]

    assert "tini" in packages
    assert wrappers == [_images.INIT_WRAPPER]


def test_the_bench_check_image_runs_under_the_init():
    """`_bench_check` builds its own image, not through `agent_image`; its `a0` task gets the init too."""
    from flyte_agent_factory import _bench_check

    image = _bench_check.env.image
    assert isinstance(image, flyte.Image)
    before_user = _layers_before_the_user(image)

    assert "tini" in [p for layer in before_user for p in getattr(layer, "packages", ())]
    assert [
        dict(layer.env_vars)["_F_EXEC_WRAPPER"]
        for layer in before_user
        if "_F_EXEC_WRAPPER" in dict(getattr(layer, "env_vars", None) or ())
    ] == [_images.INIT_WRAPPER]


def test_the_init_command_is_the_wrapper_split_into_words():
    assert _images.INIT_COMMAND == ["tini", "-s", "--"]
    assert _slow_lane._INIT_COMMAND == _images.INIT_COMMAND


def test_the_lane_command_starts_with_the_init_command():
    assert _slow_lane.LANE_COMMAND[:4] == [*_images.INIT_COMMAND, "bash"]


def test_the_browser_apps_start_under_the_init_command():
    from flyte_agent_factory import _profile, browser

    start = [*_images.INIT_COMMAND, "/bin/bash", "/opt/browser/start.sh"]
    profile = _profile.parse(WORK + PERSONAL)
    (identity,) = profile.browser.identities

    assert browser.browser_env.command == start
    assert browser.personal_app(profile, identity, "").command == start


def test_the_sdk_re_execs_the_task_under_the_wrapper(monkeypatch):
    from flyte._bin import runtime

    calls = []
    monkeypatch.setattr(os, "execvp", lambda file, args: calls.append((file, args)))
    monkeypatch.setenv("_F_EXEC_WRAPPER", _images.INIT_WRAPPER)
    monkeypatch.delenv("_F_EXEC_WRAPPED", raising=False)
    monkeypatch.delenv("_F_EXEC_WRAPPER_CLUSTERED", raising=False)
    monkeypatch.delenv("FLYTE_CLUSTERED_WORKER", raising=False)
    monkeypatch.delenv("TORCHELASTIC_RUN_ID", raising=False)

    runtime._maybe_reexec_under_wrapper()

    # The SDK execs the init script, which execs `tini -s --` with the same arguments (keeping the PID).
    assert calls == [(_images.INIT_WRAPPER, [_images.INIT_WRAPPER, *sys.argv])]
    assert os.environ["_F_EXEC_WRAPPED"] == "1"


def test_a_command_is_judged_even_when_the_first_scan_outlasts_it(tmp_path, monkeypatch):
    """In a pod with a large process table the clock's first scan of /proc took longer than a 0.3 s command,
    which then ended unjudged (#34); the verdict must not depend on that."""
    limits = []

    class SlowFirstScan(_load_clock.LoadClock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            time.sleep(0.6)

        def poll(self):
            limits.append(self.limit_s)
            return super().poll()

    monkeypatch.setattr(_load_clock, "LoadClock", SlowFirstScan)
    command = [sys.executable, "-c", "import time; time.sleep(0.3)"]

    assert _gate_base._run(command, tmp_path, timeout=30) == (0, "")
    assert limits and set(limits) == {30}


def test_every_image_env_value_is_one_word_for_the_local_docker_builder():
    """The SDK's local builder writes `ENV key=value` unquoted: a value with a space breaks the Dockerfile."""
    import flyte

    image = _images.with_init(flyte.Image.from_debian_base(name="x", python_version=(3, 12)))
    env = [layer for layer in image._layers if type(layer).__name__ == "Env"]
    assert env, "with_init sets the exec wrapper"
    for layer in env:
        for key, value in layer.env_vars:
            assert not any(ch.isspace() for ch in value), (key, value)


def test_the_init_script_execs_tini_as_a_subreaper(tmp_path):
    """The one-word wrapper is a script that execs the same tini command the apps run."""
    import subprocess

    script = tmp_path / "factory-init"
    subprocess.run(["sh", "-c", f"printf '{_images._INIT_SCRIPT}' > {script}"], check=True)
    assert script.read_text() == '#!/bin/sh\nexec tini -s -- "$@"\n'
    assert _images.INIT_COMMAND == ["tini", "-s", "--"]
