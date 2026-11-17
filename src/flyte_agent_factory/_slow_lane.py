"""Run a pytest lane (`slow` by default) of the project in raw-container child actions, sharded by test file.

With `AGENT_FACTORY_SLOW_LANE=child` the pre-review's `checks.sh` and the gate send their pytest lanes to
child actions, so that the suite does not contend for the session pod's CPU:

    python -m flyte_agent_factory._slow_lane --junit <path> [pytest args...]

The lane is the `slow` marker unless the pytest args say otherwise (`-m "not slow"` makes it the fast
lane). Without that variable, or when this process cannot attach to its run, the lane runs here as one pytest
process (with `PYTEST_XDIST_AUTO_NUM_WORKERS` set from the CPU quota unless already set), so CI and local
runs are unchanged. `--result` (default `<junit path>.result` when `--junit` is given) is the outcome record,
written atomically and removed at the start: `{"completed": true, "code": N}` when pytest ran to the end
with code 0-5, else `{"completed": false, "outcome": "ran out" | "out of memory" | "setup" | "upload" |
"capacity", "code": ..., "reason": ...}`. A reader that finds no record may assume nothing ran to completion.

Attached, the lane is split by test file into shards of `AGENT_FACTORY_LANE_CPUS` CPUs each, because a big
child does not place on a cluster whose nodes are small or busy (measured: 2 CPUs placed in 5 s, 4 in 12 s,
8 in 160 s on spot, and a size no node has waits its whole queue bound before it fails). A file is never
split (the Terraform benchmark file caps its own concurrency with machine-wide locks), and files are spread
by their durations from the last runs (`_lane_shards`), else by size. The tree and the history are uploaded
once; every shard is submitted at once on spot (queue bound `QUEUE_BOUND`), and a shard that finds no spot
capacity goes on-demand (queue bound `ON_DEMAND_QUEUE_BOUND`; the gate's launcher never passes
`--on-demand`, which is accepted and changes nothing). A shard whose child ends without reporting, or whose
container could not set up the lane (`SETUP_FAILED`), is retried once on on-demand. A shard expected to run
longer than on-demand would have left after a full spot wait (its seconds over its CPUs, but no less than its
longest file) goes the other way: on-demand first with the runtime of the whole budget, then spot. Knobs,
read when the lane starts; a value that is not a whole number at or above its minimum is reported and the
default used:

- `AGENT_FACTORY_LANE_CPUS` (default 2): CPUs per shard.
- `AGENT_FACTORY_LANE_MAX_SHARDS` (default 8): the most shards.
- `AGENT_FACTORY_LANE_TARGET_S` (default 300): the runtime a shard is planned for; with the estimated total
  it sets the shard count.
- `AGENT_FACTORY_LANE_BUDGET_S` (default 1100): the caller's wall budget for the whole lane (checks.sh
  allows 900 s, a gate 1200 s). Each attempt's `max_runtime` is what remains of it, when the attempt is
  submitted, after that attempt's queue bound, so that no attempt ends past the budget; an attempt that
  would have less than `MIN_RUNTIME_S` is not submitted.

When every shard reported, the exit code is pytest's own over the merged shards (the highest of the codes
other than 5, which is "no tests selected": 5 only when every shard had none), and `--junit` is every shard's
report in one file. Otherwise the first of these that applies decides, and a line on stderr names each shard
that did not report and why:

1. A shard that reported pytest code 1-4: the change failed. The exit is pytest's code over the shards that
   reported, `--junit` holds their cases, and completion is recorded. No `.capacity` file.
2. A shard that ran out of its own `max_runtime` on its first attempt, or whose child was killed for its
   memory limit (`OOMError`): the change made it slow or big, and another child would take as long or be
   killed as well. Exit 1, `--junit` holds the shards that reported, the record says "ran out" or "out of
   memory", and no `.capacity` file is written.
3. `SETUP_UNAVAILABLE` (78): a shard's child was placed but could not set up the lane (no git, no
   `uv sync`), on its retry on-demand too. It is the environment, not capacity, so waiting for room does not
   help. No `.capacity` file; the record says "setup".
4. `CAPACITY` (69): a shard was not placed on spot and on-demand, did not report after its retry, ran out of
   the shorter runtime its retry had left after waiting for room, or had no budget left for its retry. The
   reason is one line in `<junit path>.capacity` (and on stderr) and in the record.

In none of these does anything run here, with or without `--no-local`: a lane that cannot be placed is told
apart from a failing one so the caller decides, rather than overrunning on the shared pod. Two more codes:

- `UPLOAD_FAILED` (74): the tree could not be uploaded after its retries.
- `NOT_RUN` (75): `--no-local` and this process could not attach to its run.

The child is a `ContainerTask`, not a Python task: a child action runs the *deployed* engine's code bundle,
which lacks this module until the engine is redeployed, so a Python task died on `ModuleNotFoundError`. A raw
container needs nothing from the bundle. It gets the packed working tree and a git bundle of HEAD (so tests
that read git see the repository they see locally), builds the locked environment with `uv sync --frozen`,
installs a pinned Terraform when the image has none (its tests skip without it), uses the gitleaks and tflint
the image bakes, and runs pytest on its shard's files.

Any project can use it for any command: `python -m flyte_agent_factory._slow_lane [--junit PATH] -- <command>`
runs the command in one child instead of pytest (the pre-review hands `checks.sh` a launcher for it, as
`AGENT_FACTORY_TEST_LANE`). A command cannot be split, so it is a lane of one shard: the same ladder (spot,
then on-demand, one retry of a child that did not report), the same four outcomes and records, and the same
bound, the caller's budget. Nothing of it runs here when attached; `--on-demand` is accepted and ignored. A
command that wrote no junit leaves the empty file a pytest lane does. The child runs
`uv sync --frozen` first only when the tree has a `uv.lock`, puts the venv's `bin` on `PATH`, and exports
the junit path as `AGENT_FACTORY_LANE_JUNIT` for a command that writes one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import dataclasses
import functools
import importlib.util
import inspect
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypeVar, cast
from xml.etree import ElementTree

import flyte
import flyte.errors
import httpx
from connectrpc.errors import ConnectError
from flyte.extras import ContainerTask
from flyte.io import File
from obstore.exceptions import BaseError as ObstoreError

from flyte_agent_factory import _lane_shards, _resources

#: Same name as `_gate_tests.SLOW_LANE_ENV`, which sets it for the pre-review.
ENV_VAR = "AGENT_FACTORY_SLOW_LANE"
#: How long a shard's spot placement may queue before it goes on-demand. Short on purpose: spot places
#: quickly or not at all (2 CPUs in 5 s, 4 in 12 s, 8 in 160 s on this cluster).
QUEUE_BOUND = timedelta(minutes=2)
#: The on-demand placement after that: the scarce pool, so a longer wait, but still bounded.
ON_DEMAND_QUEUE_BOUND = timedelta(minutes=3)
#: The longest a shard may run in its container when no budget says less.
MAX_RUNTIME = timedelta(minutes=30)
#: A shard always gets at least this long to run, whatever the budget has left.
MIN_RUNTIME_S = 60
#: How much of pytest's output the child returns; the junit file carries the detail.
TAIL_CHARS = 12000
#: The container's exit code when it could not set up the lane (no `uv sync`, no Terraform). Pytest's own
#: codes are 0-5, so this cannot be mistaken for a test result.
SETUP_FAILED = 70
#: The lane's exit code when a shard's child was placed but could not set up the lane, on its retry too: the
#: environment (an image without git, no network), neither capacity nor the change. 78 is `EX_CONFIG`.
SETUP_UNAVAILABLE = 78
#: The lane's exit code when a shard found no capacity (or never reported even on its retry): nothing is run
#: here. 69 is `EX_UNAVAILABLE`; outside pytest's 0-5, apart from 70, 74, 75 and 78.
CAPACITY = 69
#: What `--no-local` exits with when the lane could not run in a child action: nothing ran. 75 is
#: `EX_TEMPFAIL`; pytest's own codes are 0-5, so a caller can tell it from a test result.
NOT_RUN = 75
#: With an upload that failed `UPLOAD_ATTEMPTS` times, the exit code: the lane did not run, and is not run
#: here either, with or without `--no-local` (EX_IOERR; outside pytest's 0-5, apart from 70 and 75).
UPLOAD_FAILED = 74
#: Attempts at uploading the packed tree, and the wait before the next (times the attempt). Each asks for
#: a fresh presigned URL, so a refused one (an HTTP 400 seen on this cluster) or an expired one is retried;
#: when every attempt fails the lane fails (`UPLOAD_FAILED`) rather than move to the shared pod.
UPLOAD_ATTEMPTS = 4
UPLOAD_BACKOFF_S = 5.0
#: How often the launcher asks the cluster whether its child has ended, and how long it then still waits for
#: the run's watch to say so. The watch can drop and reconnect without the completion: a pre-review's slow
#: child succeeded at 13:41 while its launcher waited until checks.sh's 900 s ran out at 13:48.
POLL_S = 30.0
GRACE_S = 30.0
#: What the container is asked to run: the project's pytest lane, or the command after `--`.
MODE_PYTEST = "pytest"
MODE_COMMAND = "command"
#: Trees are never packed from these, whatever git lists.
_EXCLUDED_DIRS = {".git", ".venv"}

#: The lane's knobs: each variable, its default and its minimum.
CPUS_VAR, CPUS_DEFAULT, CPUS_MIN = "AGENT_FACTORY_LANE_CPUS", _resources.LANE_CPUS_DEFAULT, 1
MAX_SHARDS_VAR, MAX_SHARDS_DEFAULT, MAX_SHARDS_MIN = "AGENT_FACTORY_LANE_MAX_SHARDS", 8, 1
TARGET_S_VAR, TARGET_S_DEFAULT, TARGET_S_MIN = "AGENT_FACTORY_LANE_TARGET_S", 300, 30
BUDGET_S_VAR, BUDGET_S_DEFAULT, BUDGET_S_MIN = "AGENT_FACTORY_LANE_BUDGET_S", 1100, 120
#: Where the container script reads the CPU quota; a test points it at a fake file.
CPU_MAX_VAR = "AGENT_FACTORY_CPU_MAX_FILE"

#: The Terraform the Terraform tests expect (`~> 1.9.0`). The same pin as `_images.TERRAFORM_VERSION` and
#: `TERRAFORM_SHA256`, copied because importing `_images` here would put another module ahead of the junit
#: file (`_gates._TEST_HARNESS`); a test keeps the two equal.
TERRAFORM_VERSION = "1.9.8"
TERRAFORM_SHA256 = "186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8"
TERRAFORM_URL = f"https://releases.hashicorp.com/terraform/{TERRAFORM_VERSION}/terraform_{TERRAFORM_VERSION}_linux_amd64.zip"

#: The container's whole job. Arguments: inputs dir, outputs dir, Terraform zip URL and sha256, and tail size.
#: The pytest arguments (`pytest_args`) start with the shard's selection (`_lane_shards.build_shard_args`);
#: with none, the shard is the whole lane.
#: gitleaks and tflint come baked into the image (`_scanners`): the lane never fetches them.
#: It always exits 0 so that the result is reported; the lane's own exit code is the `code` output.
#: A task input arrives as the file /var/inputs/<name>, an output is the file /var/outputs/<name>.
_LANE_SCRIPT = f"""
in=$1 out=$2 tf_url=$3 tf_sha=$4 tail_bytes=$5
mode=pytest
[[ ! -s $in/mode ]] || mode=$(<"$in/mode")
mkdir -p "$out"
work=$(mktemp -d "${{TMPDIR:-/tmp}}/test-lane-XXXXXX")
export PATH="$work/bin:$PATH" UV_CACHE_DIR="$work/uv-cache" UV_PYTHON_DOWNLOADS=never
# The benchmark check uses this cache when the directory exists. The image has none, and without one every
# `terraform init` downloads the AWS provider again.
cache=${{TF_PLUGIN_CACHE_DIR:-/opt/bench-terraform/plugin-cache}}
[[ -d $cache ]] || {{ mkdir -p "$work/plugin-cache"; export TF_PLUGIN_CACHE_DIR="$work/plugin-cache"; }}

install_terraform() {{
  command -v terraform >/dev/null && return 0
  python3 - "$tf_url" "$tf_sha" "$work/bin" <<'PY'
import hashlib, io, os, sys, urllib.request, zipfile
url, sha, dest = sys.argv[1:]
data = urllib.request.urlopen(url, timeout=120).read()
if hashlib.sha256(data).hexdigest() != sha:
    sys.exit("terraform zip: checksum differs from the pinned one")
os.makedirs(dest, exist_ok=True)
zipfile.ZipFile(io.BytesIO(data)).extract("terraform", dest)
os.chmod(os.path.join(dest, "terraform"), 0o755)
PY
}}

# The tree tar carries no `.git`; the history bundle gives the tree its HEAD back, index at HEAD and the
# working tree as packed, so tests that read git (tracked files, HEAD) see what they see in the pod.
restore_history() {{
  git init -q && git fetch -q "$in/history" HEAD && git update-ref HEAD FETCH_HEAD && git reset -q
}}

# Without a history (no bundle, or a checkout with no commit), tests still read the repository: one commit
# tracking every packed file, forced past ignore rules, as the checkout tracks them. No user config.
commit_packed_tree() {{
  local git=(git -c user.name=test-lane -c user.email=test-lane@invalid -c commit.gpgsign=false)
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" init -q &&
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" add -A -f &&
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" commit -q --no-verify -m "test lane tree"
}}

setup() {{
  mkdir "$work/tree" && tar xzf "$in/tree" -C "$work/tree" && cd "$work/tree" &&
    {{ if [[ -s $in/history ]]; then restore_history; else commit_packed_tree; fi; }} &&
    {{
      if [[ $mode == pytest ]]; then
        uv sync --frozen && install_terraform
      elif [[ -f uv.lock ]]; then
        uv sync --frozen
      fi
    }}
}}

# `-n auto` counts the node's CPUs (16 here), not the pod's quota: 16 workers on 2 CPUs. xdist reads this.
# Keep in step with `_lane_shards.read_cpu_allowance`; the module is not importable before the venv exists.
if [[ -z ${{PYTEST_XDIST_AUTO_NUM_WORKERS:-}} ]]; then
  workers=$(python3 - "${{{CPU_MAX_VAR}:-/sys/fs/cgroup/cpu.max}}" <<'PY'
import os, sys
count = len(os.sched_getaffinity(0))
try:
    quota, period = open(sys.argv[1]).read().split()[:2]
    if quota != "max":
        count = min(count, int(int(quota) / int(period) + 0.5))
except (OSError, ValueError, ZeroDivisionError):
    pass
print(max(1, count))
PY
  ) && [[ -n $workers ]] && export PYTEST_XDIST_AUTO_NUM_WORKERS=$workers
fi

# A shard keeps its own files of what pytest collects through this plugin (`_lane_shards.build_shard_args`).
mkdir -p "$work/plugin"
cat >"$work/plugin/{_lane_shards.PLUGIN_MODULE}.py" <<'PY'
{_lane_shards.PLUGIN_SOURCE}PY

# Null-separated, so an argument with a space or a quote reaches pytest (or the command) as it was given.
mapfile -d '' args < <(
  python3 -c 'import json, sys; sys.stdout.write(chr(0).join(json.load(open(sys.argv[1]))))' "$in/pytest_args"
)
{{
  setup || {{ echo "test-lane: setup failed" >&2; exit {SETUP_FAILED}; }}
  if [[ $mode == pytest ]]; then
    PYTHONPATH="src:$work/plugin" .venv/bin/python -m pytest "${{args[@]}}" -n auto -p no:cacheprovider \\
      "--junitxml=$out/junit"
  else
    [[ ! -d .venv/bin ]] || export PATH="$PWD/.venv/bin:$PATH" VIRTUAL_ENV="$PWD/.venv"
    AGENT_FACTORY_LANE_JUNIT="$out/junit" "${{args[@]}}"
  fi
}} 2>&1 | tee "$work/log"
code=${{PIPESTATUS[0]}}
# For sizing the shards from a real run: SECONDS is the script's own wall time.
peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null) && peak="$((peak / 1048576)) MiB" || peak="n/a"
echo "test-lane: wall ${{SECONDS}}s, peak memory $peak, ${{PYTEST_XDIST_AUTO_NUM_WORKERS:-?}} workers" |
  tee -a "$work/log"
printf '%s' "$code" >"$out/code"
tail -c "$tail_bytes" "$work/log" >"$out/tail"
[[ -e $out/junit ]] || : >"$out/junit"
exit 0
"""
LANE_COMMAND = ["bash", "-c", _LANE_SCRIPT, "test-lane"]


def _memory_limit(cpus: int) -> str:
    """Return a shard's memory limit; the one place it is read, so a message cannot drift from it."""
    return _lane_spec(cpus).memory_limit or "unlimited"


def _lane_spec(cpus: int) -> _resources.Spec:
    # The profile's text, not `_profile`: the launcher may import only test-harness modules
    # (`_gates._TEST_HARNESS`).
    return _resources.for_lane_shard(os.environ.get(_resources.PROFILE_TOML_ENV, ""), cpus)


def make_lane_resources(cpus: int) -> flyte.Resources:
    """Return a `cpus`-CPU shard's resources: `_resources.lane_shard` under the profile's `[resources.lanes]`.

    By default the limit is `cpus` (the quota a test sees) and the request 3/4 of it, because a request of a
    node's whole size never places on a node of that size; see `_resources.lane_shard` for memory.
    """
    return _lane_spec(cpus).to_flyte()


def _name_task(cpus: int, interruptible: bool, max_runtime: timedelta) -> tuple[str, str]:
    """Return the task's name and its environment's: they differ for every setting a task is built with."""
    suffix = ""
    if cpus != CPUS_DEFAULT:
        suffix += f"-{cpus}cpu"
    if max_runtime != MAX_RUNTIME:
        suffix += f"-{int(max_runtime.total_seconds())}s"
    if not interruptible:
        suffix += "-on-demand"
    return f"test-lane{suffix}", f"test-lanes{suffix}"


_ENVIRONMENTS: dict[str, flyte.TaskEnvironment] = {}


@functools.cache
def make_lane_task(cpus: int, interruptible: bool, max_runtime: timedelta) -> ContainerTask:
    """Return the lane's container task of `cpus` CPUs; all is set here, as `.override()` drops its command.

    A task per setting, built once and registered in an environment of its own, as a shard's size, spot or
    not, and runtime bound can each differ from the last shard's: the runtime is what the lane's budget has
    left when the attempt is submitted, so a lane's attempts may use several tasks of each kind.
    """
    name, env_name = _name_task(cpus, interruptible, max_runtime)
    lane = ContainerTask(
        name=name,
        image=os.environ.get("FLYTE_INTERNAL_IMAGE", "auto"),
        command=LANE_COMMAND,
        arguments=[
            "/var/inputs",
            "/var/outputs",
            TERRAFORM_URL,
            TERRAFORM_SHA256,
            str(TAIL_CHARS),
        ],
        # `pytest_args` is the JSON list of pytest's arguments, or of the command's when `mode` is "command".
        inputs={"tree": File, "history": File, "pytest_args": str, "mode": str},
        # The junit XML is a File: a large suite's report would not fit a scalar output.
        # A scalar output is capped at 1 KiB on the platform, so the tail is a file like the junit.
        outputs={"code": str, "tail": File, "junit": File},
        resources=make_lane_resources(cpus),
        interruptible=interruptible,
        # Each attempt is bounded, so a full cluster ends in the next attempt instead of an unbounded wait.
        timeout=flyte.Timeout(
            max_queued_time=QUEUE_BOUND if interruptible else ON_DEMAND_QUEUE_BOUND, max_runtime=max_runtime
        ),
    )
    # A task only holds a weak reference to its environment: keep it alive here.
    _ENVIRONMENTS[env_name] = flyte.TaskEnvironment.from_task(env_name, lane)
    return lane


#: The default-sized lane, spot and on-demand, with their environments.
task = make_lane_task(CPUS_DEFAULT, True, MAX_RUNTIME)
env = _ENVIRONMENTS["test-lanes"]
task_on_demand = make_lane_task(CPUS_DEFAULT, False, MAX_RUNTIME)
env_on_demand = _ENVIRONMENTS["test-lanes-on-demand"]


class Outcome(NamedTuple):
    """What a shard's child reported: pytest's exit code, the output tail, the junit XML, the action name."""

    code: int
    tail: str
    junit: str
    action: str = ""


#: Runs one shard on spot (True) or on-demand, given pytest's arguments (the shard's files first) or a command
#: lane's command; returns an `Outcome` or a plain `(code, tail, junit)`, directly or awaited. A plain
#: function is run in a thread.
Submit = Callable[[list[str], bool], Any]


def _listed_files(root: Path) -> list[str]:
    """Return the tracked and untracked-but-not-ignored files, relative to `root`."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [name for name in out.decode().split("\0") if name]


def pack_tree(root: Path) -> bytes:
    """Return a gzipped tar of the repository's working tree, without VCS data or virtualenvs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in _listed_files(root):
            path = root / name
            # `ls-files` still lists a tracked file that was deleted from the working tree.
            if _EXCLUDED_DIRS & set(Path(name).parts) or not path.exists():
                continue
            tar.add(path, arcname=name, recursive=False)
    return buffer.getvalue()


def pack_history(root: Path) -> bytes:
    """Return a git bundle of the repository's HEAD, or nothing when it has no commit.

    Nothing too when `root` is a subdirectory of the repository: HEAD holds the other paths as well, and the
    container commits the packed tree instead (`commit_packed_tree`).
    """
    if subprocess.run(
        ["git", "-C", str(root), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True
    ).returncode:
        return b""
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if Path(top).resolve() != root.resolve():
        return b""
    with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
        bundle = Path(tmp) / "history.bundle"
        subprocess.run(
            ["git", "-C", str(root), "bundle", "create", "-q", str(bundle), "HEAD"],
            check=True,
            capture_output=True,
        )
        return bundle.read_bytes()


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:]


def run_in_tree(root: Path, pytest_args: Sequence[str], python: str = sys.executable) -> tuple[int, str, str]:
    """Run the slow lane in the unpacked tree `root`; return exit code, output tail and junit text."""
    junit = root / ".slow-lane-junit.xml"
    proc = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-m",
            "slow",
            "-n",
            "auto",
            f"--junitxml={junit}",
            *pytest_args,
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": "src"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode, _tail(proc.stdout), junit.read_text() if junit.exists() else ""


def _run_context_dir(start: Path) -> Path | None:
    """Return the directory holding the run context `attach` reads, None when there is none.

    The supervisor writes it at the workspace root, while a project's checks run from its own directory below
    that (`projects/<name>/`): looking only in the working directory sent the lane to run here every time.
    The walk stops at the repository root, so a context outside the repository is never used.
    """
    from flyte_agent_factory.child import context_path

    for base in (start, *start.parents):
        if context_path(base).is_file():
            return base
        if (base / ".git").exists():
            break
    workspace = os.environ.get("AGENT_FACTORY_WORKSPACE")
    if workspace and context_path(workspace).is_file():
        return Path(workspace)
    return None


def _attach() -> None:
    from flyte_agent_factory.child import attach

    attach(_run_context_dir(Path.cwd()))


class UploadFailed(flyte.errors.RuntimeSystemError):
    """The packed tree could not be uploaded in `UPLOAD_ATTEMPTS` attempts."""


async def _upload_tree(
    tarball: Path,
    upload: Callable[[Path], Awaitable[tuple[str, str]]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Upload the packed tree; return its remote URI, trying up to `UPLOAD_ATTEMPTS` times."""
    if upload is None:
        from flyte import remote

        # A syncified function: `.aio` is its coroutine form, plain calls would block the loop.
        upload = remote.upload_file.aio
    for attempt in range(1, UPLOAD_ATTEMPTS):
        try:
            _, uri = await upload(tarball)
            return uri
        except flyte.errors.BaseRuntimeError as exc:
            print(
                f"slow lane: upload of the tree failed (attempt {attempt}/{UPLOAD_ATTEMPTS}: {exc}); "
                "retrying",
                file=sys.stderr,
            )
            await sleep(UPLOAD_BACKOFF_S * attempt)
    try:
        _, uri = await upload(tarball)
    except flyte.errors.BaseRuntimeError as exc:
        raise UploadFailed(
            "UploadFailed", f"upload of the tree failed {UPLOAD_ATTEMPTS} times, last: {exc}"
        ) from exc
    return uri


_T = TypeVar("_T")
#: What a remote call raises when the cluster or the network fails: `_errors.REMOTE_ERRORS`, copied because
#: importing `_errors` here would put another module ahead of the junit file (`_gates._TEST_HARNESS`); a test
#: keeps the two equal.
_POLL_ERRORS = (ConnectError, RuntimeError, OSError, ObstoreError, httpx.HTTPError, ValueError, KeyError)


class LaneEnded(flyte.errors.RuntimeSystemError):
    """The child action ended without succeeding, and the watch did not say so.

    `phase` is the action's phase (an `ActionPhase`, None when unknown) and `error_code` the server's code
    for it, so the shard ladder can tell the change's own timeout or OOM kill from a child that merely died
    without reading the message. The watch would have raised the matching error itself.
    """

    def __init__(self, code: str, message: str, phase: Any = None, error_code: str = "") -> None:
        super().__init__(code, message)
        self.phase = phase
        self.error_code = error_code

    def as_watch_error(self) -> flyte.errors.BaseRuntimeError | None:
        """Return the error the watch raises for this ending, or None when it raises nothing specific."""
        from flyte.models import ActionPhase

        if self.phase == ActionPhase.TIMED_OUT:
            # A queue bound that fired is capacity, not the change: the server says which bound it was.
            if "QUEUED" in self.error_code.upper():
                return flyte.errors.MaxQueuedTimeExceededError(str(self))
            return flyte.errors.MaxRuntimeExceededError(str(self))
        if "OOM" in self.error_code.upper():
            return flyte.errors.OOMError(self.error_code, str(self))
        return None


async def _settle(
    result: Awaitable[_T],
    finished: Callable[[], Awaitable[_T | None]],
    poll_s: float = POLL_S,
    grace_s: float = GRACE_S,
) -> _T:
    """Await `result`; poll `finished` every `poll_s` and, once it reports the end, wait `grace_s` more.

    `finished` returns None while the child runs, its outputs once it succeeded, and raises `LaneEnded` when
    it ended otherwise. A poll that fails (the network, the API) is not an answer: the wait goes on.
    """
    task = asyncio.ensure_future(result)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_s)
            if done:
                return task.result()
            try:
                found = await finished()
            except LaneEnded:
                done, _ = await asyncio.wait({task}, timeout=grace_s)
                if done:
                    return task.result()
                raise
            except _POLL_ERRORS as exc:
                print(f"slow lane: could not read the child's state ({exc}); still waiting", file=sys.stderr)
                continue
            if found is None:
                continue
            done, _ = await asyncio.wait({task}, timeout=grace_s)
            if done:
                return task.result()
            print(
                "slow lane: the child ended but the run's watch did not say so; using its outputs",
                file=sys.stderr,
            )
            return found
    finally:
        if not task.done():
            task.cancel()


def _read_lane_outputs(literals: dict[str, Any]) -> tuple[str, File, File]:
    """Return the lane's outputs (code, tail, junit) from the child action's output literals."""
    return (
        literals["code"].scalar.primitive.string_value,
        File.from_existing_remote(literals["tail"].scalar.blob.uri),
        File.from_existing_remote(literals["junit"].scalar.blob.uri),
    )


async def _poll_child_outputs(names: list[str]) -> tuple[str, File, File] | None:
    """Ask the cluster about the lane's child action: None while it runs (or is not submitted yet)."""
    from flyte import remote
    from flyte.models import ActionPhase

    if not names:
        return None
    details = await remote.ActionDetails.get.aio(run_name=flyte.ctx().action.run_name, name=names[-1])
    if not details.done():
        return None
    if details.phase != ActionPhase.SUCCEEDED:
        error = details.error_info
        raise LaneEnded(
            "LaneEnded",
            f"child action {names[-1]} ended {details.phase.value}",
            phase=details.phase,
            error_code=error.code if error else "",
        )
    return _read_lane_outputs(await details.output_literals.aio())


@dataclasses.dataclass(frozen=True)
class Knobs:
    """The lane's settings, from the environment."""

    cpus: int
    max_shards: int
    target_s: int
    budget_s: int


def _read_knob(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        print(
            f"slow lane: {name} must be a whole number of at least {minimum}; using {default}",
            file=sys.stderr,
        )
        return default
    return value


def read_knobs(env: Mapping[str, str] | None = None) -> Knobs:
    """Read the knobs when the lane starts.

    A value that is not a whole number at its minimum or more is reported and replaced by the default, as a
    typo in an environment variable should not end a pre-review.
    """
    env = os.environ if env is None else env
    return Knobs(
        cpus=_read_knob(env, CPUS_VAR, CPUS_DEFAULT, CPUS_MIN),
        max_shards=_read_knob(env, MAX_SHARDS_VAR, MAX_SHARDS_DEFAULT, MAX_SHARDS_MIN),
        target_s=_read_knob(env, TARGET_S_VAR, TARGET_S_DEFAULT, TARGET_S_MIN),
        budget_s=_read_knob(env, BUDGET_S_VAR, BUDGET_S_DEFAULT, BUDGET_S_MIN),
    )


class Budget:
    """The lane's wall budget; a placement's `max_runtime` is what is left of it after its queue bound."""

    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.total_s = total_s
        self._end = clock() + total_s
        self._clock = clock

    def left_s(self) -> float:
        """Return the seconds left of the budget now."""
        return self._end - self._clock()

    def runtime_s(self, spot: bool) -> int:
        """Return the seconds an attempt placed on spot (or on-demand) now may run, in whole minutes.

        What is left now less the attempt's queue bound, so that an attempt that waits its whole bound and
        then runs its whole runtime still ends within the budget. Below `MIN_RUNTIME_S` (even negative) there
        is no time for the attempt: the caller does not submit it.
        """
        bound = QUEUE_BOUND if spot else ON_DEMAND_QUEUE_BOUND
        return int((self.left_s() - bound.total_seconds()) // 60) * 60

    def runtime_after_spot_wait_s(self) -> int:
        """Return the seconds an on-demand attempt would get if it were submitted after a full spot wait."""
        waited = QUEUE_BOUND + ON_DEMAND_QUEUE_BOUND
        return int((self.left_s() - waited.total_seconds()) // 60) * 60


#: The runtime `_run_shard` chose for the attempt it is making; each shard runs as its own asyncio task, so
#: this is per shard. Without it (a direct call) the submit asks the budget itself.
_ATTEMPT_RUNTIME_S: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_ATTEMPT_RUNTIME_S", default=None
)


async def _upload_lane(root: Path) -> tuple[File, File]:
    """Pack and upload the tree and its history, once for every shard of the lane."""
    with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
        tarball = Path(tmp) / "tree.tar.gz"
        tarball.write_bytes(pack_tree(root))
        history = Path(tmp) / "history.bundle"
        history.write_bytes(pack_history(root))
        uri = await _upload_tree(tarball)
        history_uri = await _upload_tree(history)
    return File.from_existing_remote(uri), File.from_existing_remote(history_uri)


def _make_child_submit(
    cpus: int, budget: Budget, tree: File, history: File, mode: str = MODE_PYTEST
) -> Submit:
    """Return the submit that runs one shard (or, in `MODE_COMMAND`, the command) in a child action."""

    async def submit(pytest_args: list[str], interruptible: bool) -> Outcome:
        from flyte_agent_factory import child

        runtime_s = _ATTEMPT_RUNTIME_S.get()
        if runtime_s is None:
            runtime_s = budget.runtime_s(interruptible)
        lane = make_lane_task(cpus, interruptible, timedelta(seconds=runtime_s))
        # A lane that names no marker is the slow lane, as in the local run; a later `-m` wins in pytest.
        args = json.dumps(pytest_args if mode == MODE_COMMAND else ["-m", "slow", *pytest_args])
        names: list[str] = []
        with child.track_submissions(names.append):
            try:
                # The remote task's declared outputs are not visible to the typechecker.
                code, tail, junit = cast(
                    tuple[str, File, File],
                    await _settle(
                        lane.aio(tree=tree, history=history, pytest_args=args, mode=mode),
                        lambda: _poll_child_outputs(names),
                    ),
                )
            except flyte.errors.BaseRuntimeError as exc:
                for name in names:
                    print(f"slow lane: child action {name} ended with {type(exc).__name__}", file=sys.stderr)
                raise
        action = names[-1] if names else ""
        # The pytest code is in hand: a report that cannot be fetched costs that shard's cases in the junit
        # file (the gate then runs them itself), not the lane's result.
        try:
            with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
                await junit.download(str(Path(tmp) / "junit"))
                await tail.download(str(Path(tmp) / "tail"))
                text = (Path(tmp) / "tail").read_text(errors="replace")
                return Outcome(int(code), text, (Path(tmp) / "junit").read_text(), action)
        except _POLL_ERRORS as exc:
            print(f"slow lane: could not fetch the report of child action {action} ({exc})", file=sys.stderr)
            return Outcome(int(code), f"(report of child action {action} could not be fetched)\n", "", action)

    return submit


def _write_record(result: Path | None, fields: dict[str, Any]) -> None:
    if result is None:
        return
    # A reader must see either no record or the whole record, even if the launcher is killed.
    result.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=result.parent, delete=False) as record:
        temporary = Path(record.name)
        try:
            json.dump(fields, record)
            record.close()
            temporary.replace(result)
        finally:
            temporary.unlink(missing_ok=True)


def _write_result(result: Path | None, code: int) -> None:
    """Record that pytest ran to completion with `code`; any other code is no completion and not recorded."""
    if type(code) is int and code in range(6):
        _write_record(result, {"completed": True, "code": code})


def _write_outcome(result: Path | None, outcome: str, code: int, reason: str) -> None:
    """Record that the lane did not complete, why (`outcome`), and the code it exits with."""
    _write_record(result, {"completed": False, "outcome": outcome, "code": code, "reason": reason})


def _run_local(junit: Path | None, pytest_args: Sequence[str], result: Path | None = None) -> int:
    record = [f"--junitxml={junit}"] if junit is not None else []
    # The launcher may run on the supervisor's interpreter (it can attach), which has no pytest: the
    # project's own environment runs the tests then.
    python = [sys.executable] if importlib.util.find_spec("pytest") else ["uv", "run", "python"]
    cmd = [*python, "-m", "pytest", "tests", "-q", "-m", "slow", *record, *pytest_args]
    # `-n auto` (in pyproject's addopts) counts the node's CPUs, not this pod's quota.
    env = {"PYTEST_XDIST_AUTO_NUM_WORKERS": str(_lane_shards.read_cpu_allowance()), **os.environ}
    code = subprocess.run(cmd, env=env).returncode
    _write_result(result, code)
    return code


@dataclasses.dataclass
class ShardEnd:
    """How one shard ended: `problem` is empty when the child reported pytest's `code`."""

    index: int
    code: int = 0
    tail: str = ""
    junit: str = ""
    kind: str = ""
    action: str = ""
    seconds: float = 0.0
    #: "unplaced" (no capacity on spot or on-demand), "unreported" (no result even on its retry), "setup"
    #: (its child ran but could not set up the lane, on its retry too), "ran out" (hit its own max_runtime on
    #: spot), "out of memory" (its child hit its memory limit, the change's doing: the limit is the same
    #: on-demand), "short" (hit the shorter max_runtime its retry had left after the first attempt) or
    #: "no time left" (the budget had too little left to submit its retry after waiting for capacity).
    problem: str = ""
    runtime_s: int = 0
    memory: str = ""
    #: For "short": how the first attempt ended ("unplaced", "unreported" or "setup"), which the report names.
    first: str = ""


def _count_minutes(bound: timedelta) -> int:
    return int(bound.total_seconds() // 60)


async def _call(submit: Submit, pytest_args: list[str], spot: bool) -> Outcome:
    if inspect.iscoroutinefunction(submit):
        found = await submit(pytest_args, spot)
    else:
        found = await asyncio.to_thread(submit, pytest_args, spot)
    return Outcome(*found)


async def _run_shard(
    index: int,
    count: int,
    files: list[str],
    selection: list[str],
    pytest_args: list[str],
    submit: Submit,
    knobs: Knobs,
    budget: Budget,
    expected_s: float = 0.0,
) -> ShardEnd:
    """Run one shard: spot, then on-demand when spot has no capacity or the child did not report.

    A shard expected to take longer than on-demand would have left after a full spot wait (`expected_s`)
    goes on-demand first, with the runtime of the whole budget, and to spot only when on-demand has no room:
    on a cluster without spot capacity it would otherwise wait for spot and then always run out.
    """
    label = f"slow lane: shard {index}/{count}"
    problem = tail = ""
    after_wait_s = budget.runtime_after_spot_wait_s()
    ladder = (True, False)
    if expected_s > after_wait_s:
        ladder = (False, True)
        print(
            f"{label} ({_name_files(files)}): about {expected_s:.0f}s, more than the {max(0, after_wait_s)}s "
            "on-demand would have left after waiting for spot; on-demand first",
            file=sys.stderr,
        )
    for attempt, spot in enumerate(ladder):
        kind = _name_kind(spot)
        bound = QUEUE_BOUND if spot else ON_DEMAND_QUEUE_BOUND
        runtime_s = budget.runtime_s(spot)
        if runtime_s < MIN_RUNTIME_S:
            # Submitting anyway would end past the budget, where the caller's limit kills everything.
            print(
                f"{label}: {budget.total_s:.0f}s budget has {max(0.0, budget.left_s()):.0f}s left, "
                f"not enough for a {kind} attempt ({bound.total_seconds():.0f}s queue + {MIN_RUNTIME_S}s)",
                file=sys.stderr,
            )
            problem = "no time left"
            break
        _ATTEMPT_RUNTIME_S.set(runtime_s)
        started = time.monotonic()
        try:
            try:
                outcome = await _call(submit, [*selection, *pytest_args], spot)
            except LaneEnded as ended:
                # The watch was lost and the poll found the end: the change's timeout or OOM kill is the
                # change as the watch's own error would have said, never "unreported" and so capacity.
                watch_error = ended.as_watch_error()
                if watch_error is None:
                    raise
                raise watch_error from ended
        except flyte.errors.MaxQueuedTimeExceededError:
            print(f"{label}: no {kind} capacity within {bound}", file=sys.stderr)
            problem = "unplaced"
            continue
        except flyte.errors.MaxRuntimeExceededError:
            if attempt:
                # The retry had only what the first attempt (unplaced, or lost) left of the budget: capacity,
                # said with how long it had and why, not the change (with the whole budget, it would be).
                short = ShardEnd(
                    index,
                    1,
                    kind=kind,
                    seconds=time.monotonic() - started,
                    problem="short",
                    runtime_s=runtime_s,
                    first=problem,
                )
                print(f"{label} ({_name_files(files)}) {_explain_short(short)}", file=sys.stderr)
                return short
            # The change makes the shard slow; another child would take as long.
            print(
                f"slow lane: shard {index}/{count} ({_name_files(files)}) ran out of its {runtime_s}s "
                f"in its own {knobs.cpus}-CPU child",
                file=sys.stderr,
            )
            return ShardEnd(
                index,
                1,
                kind=kind,
                seconds=time.monotonic() - started,
                problem="ran out",
                runtime_s=runtime_s,
            )
        except UploadFailed:
            raise
        except flyte.errors.OOMError:
            # The limit is the shard's own, the same on-demand: another child would be killed as well.
            memory = _memory_limit(knobs.cpus)
            print(
                f"{label} ({_name_files(files)}) ran out of its {memory} memory in its own "
                f"{knobs.cpus}-CPU child",
                file=sys.stderr,
            )
            return ShardEnd(
                index,
                1,
                kind=kind,
                seconds=time.monotonic() - started,
                problem="out of memory",
                memory=memory,
            )
        except flyte.errors.BaseRuntimeError as exc:
            # The child died before it reported (image pull, a preempted spot node, a lost watch).
            print(
                f"{label}: the child action failed before reporting ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            problem = "unreported"
            continue
        seconds = time.monotonic() - started
        if outcome.code == SETUP_FAILED:
            # The container could not build the environment: nothing ran; pytest never exits with this code.
            print(f"{label}: setup failed in the child action ({kind})\n{outcome.tail}", file=sys.stderr)
            problem = "setup"
            tail = outcome.tail
            continue
        print(
            f"{label} {kind} child action {outcome.action or '?'} {seconds:.0f}s code {outcome.code}",
            file=sys.stderr,
        )
        return ShardEnd(index, outcome.code, outcome.tail, outcome.junit, kind, outcome.action, seconds)
    return ShardEnd(index, kind=kind, tail=tail if problem == "setup" else "", problem=problem)


def _name_kind(spot: bool) -> str:
    return "spot" if spot else "on-demand"


def _explain_capacity(ends: list[ShardEnd], cpus: int) -> str:
    unplaced = sum(e.problem == "unplaced" for e in ends)
    unreported = sum(e.problem == "unreported" for e in ends)
    parts = []
    if unplaced:
        parts.append(
            f"{unplaced} of {len(ends)} shards ({cpus} CPUs) not placed within "
            f"{_count_minutes(QUEUE_BOUND)}m spot + {_count_minutes(ON_DEMAND_QUEUE_BOUND)}m on-demand"
        )
    if unreported:
        parts.append(
            f"{unreported} of {len(ends)} shards ({cpus} CPUs) did not report, even on a retry "
            + next(e.kind for e in ends if e.problem == "unreported")
        )
    short = [e for e in ends if e.problem == "short"]
    if short:
        parts.append(f"{len(short)} of {len(ends)} shards ({cpus} CPUs) " + _explain_short(short[0]))
    late = sum(e.problem == "no time left" for e in ends)
    if late:
        parts.append(
            f"{late} of {len(ends)} shards had no budget left for a retry after waiting for capacity"
        )
    return "no capacity: " + "; ".join(parts)


def _explain_short(end: ShardEnd) -> str:
    """Say how long a shard's retry had, and after what, for a shard that ran out of it."""
    before = "on-demand" if end.kind == "spot" else "spot"
    after = {
        "unreported": f"its {before} child failed before reporting",
        "setup": f"its {before} child could not set up the lane",
    }.get(end.first, f"no {before} capacity")
    return f"ran out of the {end.runtime_s}s {end.kind} left after {after}"


def _name_files(files: Sequence[str], most: int = 3) -> str:
    """Name a shard's files for a line of output: the first `most`, then how many more."""
    named = " ".join(files[:most])
    return named + (f" and {len(files) - most} more" if len(files) > most else "")


def _explain_not_run(end: ShardEnd, cpus: int) -> str:
    """Say why a shard did not report pytest's result, for the line that names it."""
    return {
        "unplaced": f"not placed within {_count_minutes(QUEUE_BOUND)}m spot + "
        f"{_count_minutes(ON_DEMAND_QUEUE_BOUND)}m on-demand",
        "unreported": f"no report, even on a retry {end.kind}",
        "setup": "could not set up the lane in its child action",
        "ran out": f"ran out of its {end.runtime_s}s in its own {cpus}-CPU child",
        "out of memory": f"ran out of its {end.memory} memory in its own {cpus}-CPU child",
        "short": _explain_short(end),
        "no time left": "no budget left for a retry after waiting for capacity",
    }.get(end.problem, end.problem)


def _find_marker(pytest_args: Sequence[str]) -> str:
    """Return the lane's marker expression: the last `-m`, as pytest takes it, else "slow"."""
    marker = "slow"
    for flag, value in zip(pytest_args, pytest_args[1:], strict=False):
        if flag == "-m":
            marker = value
    return marker


def _combine_lane_codes(codes: list[int]) -> int:
    """Return pytest's code for shards that ended with `codes`: the highest but 5 ("nothing selected")."""
    real = [c for c in codes if c != 5]
    return max(real) if real else 5


async def _run_lane(
    junit: Path | None, pytest_args: list[str], submit: Submit | None, result: Path | None, knobs: Knobs
) -> int:
    budget = Budget(knobs.budget_s)
    root = Path.cwd()
    if submit is None:
        tree, history = await _upload_lane(root)
        submit = _make_child_submit(knobs.cpus, budget, tree, history)

    files = _lane_shards.discover_test_files(root)
    lane = _find_marker(pytest_args)
    durations = {**_lane_shards.read_seed_durations(root, lane), **_lane_shards.read_durations(root, lane)}
    # A file expected to outrun even the longest child the budget allows (on-demand first, at once) is split
    # by test: on a busy node the Terraform benchmark file (about 830 test-seconds) ran past a 540 s child.
    units, weights = _lane_shards.split_heavy_files(
        files, _lane_shards.weigh_files(root, files, durations), budget.runtime_s(False) * _lane_shards.FILL
    )
    count = _lane_shards.count_shards(sum(weights.values()), knobs.cpus, knobs.target_s, knobs.max_shards)
    # No test file found (a tree without `tests/`): one shard that leaves pytest to its own defaults.
    shards = _lane_shards.plan_shards(units, weights, count) or [[]]
    print(
        f"slow lane: {len(files)} test files, about {sum(weights.values()):.0f}s, in {len(shards)} shards of "
        f"{knobs.cpus} CPUs (budget {knobs.budget_s}s)",
        file=sys.stderr,
    )
    # Collected from `tests/` as the local run is, unless the caller named what to run: a project without
    # `testpaths` would otherwise be collected from its whole tree in a shard.
    named = any(not a.startswith("-") and (root / a.split("::")[0]).exists() for a in pytest_args)
    where = ["tests"] if files and not named else []
    ends = await asyncio.gather(
        *(
            _run_shard(
                i,
                len(shards),
                shard,
                [*where, *selection],
                pytest_args,
                submit,
                knobs,
                budget,
                _lane_shards.estimate_shard_seconds(shard, weights, knobs.cpus),
            )
            for i, (shard, selection) in enumerate(
                zip(shards, _lane_shards.build_shard_args(shards), strict=True), 1
            )
        )
    )

    code = _end_lane(ends, shards, junit, result, knobs)
    # Only a lane that every shard ran to a pass says anything about how long its files take.
    if all(not e.problem and e.code not in range(1, 5) for e in ends):
        _lane_shards.record_durations(_lane_shards.merge_junit(e.junit for e in ends), root, lane)
    return code


def _end_lane(
    ends: list[ShardEnd], names: list[list[str]], junit: Path | None, result: Path | None, knobs: Knobs
) -> int:
    """Settle the lane, then print its verdict last: a log kept only by its end still says why it failed."""
    code = _settle_lane(ends, names, junit, result, knobs)
    _print_verdict(ends, code, knobs)
    return code


def _print_verdict(ends: Sequence[ShardEnd], code: int, knobs: Knobs) -> None:
    """Print one line per shard (its pytest summary, failures or why it did not run), then the lane's code."""
    sys.stderr.flush()
    print(f"=== lane verdict: exit {code} ===")
    for e in ends:
        where = f"shard {e.index}/{len(ends)} ({e.kind or 'not placed'}, child action {e.action or '?'})"
        if e.problem:
            print(f"{where}: did not run: {_explain_not_run(e, knobs.cpus)}")
            continue
        *failures, summary = _lane_shards.summarize_tail(e.tail) or ["no pytest summary"]
        print(f"{where}: exit {e.code}: {summary}")
        for line in failures:
            print(f"  {line}")
    sys.stdout.flush()


def _settle_lane(
    ends: list[ShardEnd], names: list[list[str]], junit: Path | None, result: Path | None, knobs: Knobs
) -> int:
    """Report how a lane ended, from how each shard did, and return its exit code.

    `names[i]` is what shard `i + 1` is called in a message: its files, or a command lane's command. The
    junit file is written for every ending that has cases, and for a command that wrote none it is the empty
    file a pytest lane that wrote none leaves: the gate reads that as "no junit".
    """
    reported = [e for e in ends if not e.problem]
    failed = any(e.code in range(1, 5) for e in reported)
    if failed or any(e.problem for e in ends):
        for e, shard in zip(ends, names, strict=True):
            if e.problem:
                named_files = f" ({_name_files(shard)})" if shard else ""
                print(
                    f"slow lane: shard {e.index}/{len(ends)}{named_files} did not run: "
                    f"{_explain_not_run(e, knobs.cpus)}",
                    file=sys.stderr,
                )
    if failed:
        # A test that failed is the change, whatever else kept other shards from running: the lane fails
        # with pytest's code and the cases that ran, not as capacity.
        code = _combine_lane_codes([e.code for e in reported])
        _write_result(result, code)
        _write_junit(junit, reported)
        _print_tails(reported, len(ends))
        return code
    ran_out = [e for e in ends if e.problem in ("ran out", "out of memory")]
    if ran_out:
        reason = "; ".join(
            f"shard {e.index}/{len(ends)} ({_name_files(names[e.index - 1])}) "
            f"{_explain_not_run(e, knobs.cpus)}"
            for e in ran_out
        )
        # Recorded, so that a reader does not take the missing completion for a lane that never reported.
        _write_outcome(result, ran_out[0].problem, 1, reason)
        _write_junit(junit, reported)
        _print_tails(reported, len(ends))
        return 1
    setup = [e for e in ends if e.problem == "setup"]
    if setup:
        # The children were placed and ran: this is the environment, not capacity, and waiting will not help.
        last = setup[-1].tail.strip().splitlines() or ["no output"]
        print(
            f"slow lane: setup failed: {len(setup)} of {len(ends)} shards could not set up the lane in "
            f"their child action, even on a retry on-demand ({last[-1]}); the lane did not run, "
            "and is not run here",
            file=sys.stderr,
        )
        _write_outcome(result, "setup", SETUP_UNAVAILABLE, f"{len(setup)} of {len(ends)} shards: {last[-1]}")
        return SETUP_UNAVAILABLE
    if any(e.problem for e in ends):
        reason = _explain_capacity(ends, knobs.cpus)
        print(f"slow lane: {reason}; the lane did not run to the end, and is not run here", file=sys.stderr)
        if junit is not None:
            junit.parent.mkdir(parents=True, exist_ok=True)
            _locate_capacity_file(junit).write_text(reason + "\n")
        _write_outcome(result, "capacity", CAPACITY, reason)
        return CAPACITY
    code = _combine_lane_codes([e.code for e in ends])
    _write_result(result, code)
    _write_junit(junit, ends)
    _print_tails(ends, len(ends))
    return code


async def _run_command_lane(
    junit: Path | None, command: list[str], submit: Submit | None, result: Path | None, knobs: Knobs
) -> int:
    """Run a command as a lane of one shard: the ladder, the outcomes and the records of a pytest lane."""
    budget = Budget(knobs.budget_s)
    if submit is None:
        tree, history = await _upload_lane(Path.cwd())
        submit = _make_child_submit(knobs.cpus, budget, tree, history, MODE_COMMAND)
    name = shlex.join(command)
    end = await _run_shard(1, 1, [name], [], command, submit, knobs, budget)
    return _end_lane([end], [[name]], junit, result, knobs)


def _write_junit(junit: Path | None, ends: Sequence[ShardEnd]) -> str:
    """Write the merged junit of the shards that reported, and return it."""
    merged = _lane_shards.merge_junit(e.junit for e in ends)
    # No shard left a report (pytest died before writing one): the file stays empty, as a single lane's was,
    # so that the gate sees "no junit" rather than a run of no tests.
    report = merged if len(ElementTree.fromstring(merged)) else ""
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(report)
    return report


def _print_tails(ends: Sequence[ShardEnd], count: int) -> None:
    for e in ends:
        print(f"=== shard {e.index}/{count} ({e.kind}, child action {e.action or '?'}) ===")
        print(e.tail, end="" if e.tail.endswith("\n") else "\n")


def _locate_capacity_file(junit: Path) -> Path:
    """Return where the reason for a `CAPACITY` exit goes: beside the junit file."""
    return junit.with_name(junit.name + ".capacity")


def _run_local_command(junit: Path | None, command: Sequence[str], result: Path | None = None) -> int:
    env = {**os.environ, **({"AGENT_FACTORY_LANE_JUNIT": str(junit)} if junit is not None else {})}
    try:
        code = subprocess.run(command, env=env).returncode
    except OSError as exc:
        print(f"slow lane: cannot run {command[0]}: {exc}", file=sys.stderr)
        code = 127
    _write_result(result, code)
    return code


def _run_remote(
    junit: Path | None,
    pytest_args: list[str],
    submit: Submit | None = None,
    result: Path | None = None,
    command: list[str] | None = None,
) -> int:
    """Run the lane (or the `command`) in child actions; return its exit code. Nothing falls back to here."""
    knobs = read_knobs()
    try:
        if command:
            return asyncio.run(_run_command_lane(junit, command, submit, result, knobs))
        return asyncio.run(_run_lane(junit, pytest_args, submit, result, knobs))
    except UploadFailed as exc:
        # Nothing was submitted, and a lane whose tree could not reach the cluster does not move to the
        # shared pod either.
        print(f"slow lane: {exc}; the lane did not run", file=sys.stderr)
        # A record, so the supervisor can tell the infrastructure from the change: a missing one is what a
        # lane killed by the checks' time limit leaves too.
        _write_outcome(result, "upload", UPLOAD_FAILED, str(exc))
        return UPLOAD_FAILED


def main(argv: Sequence[str] | None = None, submit: Submit | None = None) -> int:
    """Run the `python -m flyte_agent_factory._slow_lane` command line; return pytest's exit status."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._slow_lane")
    parser.add_argument("--junit", type=Path, help="where to write the lane's junit XML")
    parser.add_argument(
        "--result", type=Path, help="where to atomically record pytest completion and exit code"
    )
    parser.add_argument(
        "--no-local",
        action="store_true",
        help=f"when this process cannot attach to its run, exit {NOT_RUN} instead of running the lane here",
    )
    parser.add_argument(
        "--on-demand",
        action="store_true",
        help="accepted and ignored: a shard without spot capacity always goes on-demand now",
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after `--` is a command to run instead of pytest; argparse would hand it back mixed in with
    # the pytest arguments.
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
        if not command:
            parser.error("-- needs a command")
    args, pytest_args = parser.parse_known_args(argv)

    if args.junit is not None:
        # The outcome is recorded beside the junit file unless the caller says where.
        args.result = args.result or args.junit.with_name(args.junit.name + ".result")
        # A reason left by an earlier run would be taken for this one's.
        _locate_capacity_file(args.junit).unlink(missing_ok=True)
    if args.result is not None:
        args.result.unlink(missing_ok=True)
    if os.environ.get(ENV_VAR) == "child":
        try:
            _attach()
        # No run context, no controller extension in this interpreter, or no API key: each means run here.
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"slow lane: running locally, cannot attach to a Flyte run ({exc})", file=sys.stderr)
        else:
            return _run_remote(args.junit, pytest_args, submit, args.result, command)
    if args.no_local:
        # The message above says "running locally", the default; with this flag the caller decides.
        print(f"slow lane: --no-local, so it was not run here (exit {NOT_RUN})", file=sys.stderr)
        return NOT_RUN
    if command:
        return _run_local_command(args.junit, command, args.result)
    return _run_local(args.junit, pytest_args, args.result)


if __name__ == "__main__":
    sys.exit(main())
