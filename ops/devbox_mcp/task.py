"""The privileged child action: runs `steps.run_all` in a dind pod, and `submit`, which starts it from here.

Modelled on projects/oss-setup-selftest/selftest/task.py (the image, the informer workaround and the abort on
interruption are its). What differs: the engine travels as a `File` input, the pod gets no secret of ours,
and the spot attempt is bounded in the queue with an on-demand fallback.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

import flyte
import flyte.errors
import flyte.remote as remote
from flyte.io import File

# Module level, not inside the task: the package travels by value (see `submit`) and cannot be imported by
# name in the container.
from . import steps
from .pod import WORK
from .runner import Redactor

logger = logging.getLogger(__name__)

MAX_RUNTIME = timedelta(hours=3)
MAX_QUEUED = timedelta(minutes=20)
POLL_S = 60
DEFAULT_BASE_IMAGE = "ghcr.io/flyteorg/flyte:py3.12-v2.13.5"

# The task runtime imports `kubernetes` to apply the pod template, and the steps need git, ssh and uv; the
# plain flyte base image has none of them. Built through the platform's builder by `submit`: a bare image
# name has no registry in a pod.
IMAGE = (
    flyte.Image.from_base(os.environ.get("FLYTE_INTERNAL_IMAGE", DEFAULT_BASE_IMAGE))
    .clone(name="devbox-mcp", extendable=True)
    # One layer with its own `apt-get update`: the builder's cached package index went stale (selftest).
    .with_commands(
        [
            "apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates"
            " && rm -rf /var/lib/apt/lists/*"
        ]
    )
    .with_pip_packages("kubernetes==37.0.2", "uv==0.9.5")
)

env = flyte.TaskEnvironment(
    name="devbox-mcp",
    image=IMAGE,
    # The primary container only drives CLIs; the dind sidecar (pod.py) holds the devbox's resources.
    resources=flyte.Resources(cpu=2, memory="4Gi"),
    # Spot: the demo cluster's on-demand CPU nodes (4 CPU / 16 GiB) are too small for the sidecar, which is
    # sized for a c5.4xlarge. A preempted attempt starts again from nothing, which this harness tolerates.
    interruptible=True,
)


@env.task
async def devbox_mcp(engine: File) -> str:
    """Run every step in this pod against the shipped engine tarball; return the records as JSON.

    No retries, on purpose: this is a one-shot diagnostic. A retry would double the ~11-minute devbox cost
    and re-run a probe that already paused on a question.
    """
    redact = Redactor()
    for name in steps.list_secret_env_names():
        redact.add(os.environ.get(name, ""))
    tarball = Path(WORK) / "engine.tgz"
    await engine.download(str(tarball))
    ctx = steps.Ctx(tarball=tarball)
    records, facts = steps.run_all(ctx, redact)
    return json.dumps({"engine_sha": facts.get("engine_sha", ""), "steps": records, "facts": facts})


class ChildEnded(RuntimeError):
    """The child reached a failed terminal phase on its own."""


TERMINAL_FAIL = ("FAILED", "ABORTED", "TIMED_OUT")


def submit(tarball: Path, workspace: Path) -> tuple[list[str], str]:
    """Submit the child from the agent session and wait for it. Returns (child action names, result JSON).

    Spot first with the queue bounded; if no spot node is placed in MAX_QUEUED, once more on demand with the
    bound cleared (DESIGN.md). Both submissions are recorded, so the caller can report each.
    """
    # The agent session's own engine; untyped, and absent where only the offline checks run.
    import cloudpickle  # type: ignore[import-untyped,import-not-found,unused-ignore]

    pkg = importlib.import_module(__package__ or "devbox_mcp")
    from flyte_agent_factory.child import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
        attach,
    )

    from .pod import to_pod_template

    attach(workspace)
    # A child submitted through `attach` ships as a cloudpickle bundle, so this package must travel by value.
    cloudpickle.register_pickle_by_value(pkg)
    devbox_mcp.image = flyte.build(IMAGE).uri
    engine = File.from_local_sync(str(tarball))
    names: list[str] = []
    spot = devbox_mcp.override(
        pod_template=to_pod_template(),
        timeout=flyte.Timeout(max_runtime=MAX_RUNTIME, max_queued_time=MAX_QUEUED),
    )
    on_demand = devbox_mcp.override(
        pod_template=to_pod_template(),
        interruptible=False,
        timeout=flyte.Timeout(max_runtime=MAX_RUNTIME),
    )

    async def run_probe() -> str:
        try:
            return await _wait_for_child(spot, engine, names)
        except flyte.errors.MaxQueuedTimeExceededError:
            logger.warning("no spot node within %s; trying on demand", MAX_QUEUED)
            return await _wait_for_child(on_demand, engine, names)

    try:
        return names, asyncio.run(run_probe())
    except ChildEnded:
        raise  # already terminal: nothing left to abort
    except BaseException:
        # Ctrl-C, SIGTERM (see run.py), cancellation or a wait error: don't leave the privileged child alone.
        _abort_owned(names, workspace)
        raise


async def _wait_for_child(task: object, engine: File, names: list[str]) -> str:
    """Submit `task` and wait for its output, polling its phase as well (the informer can miss completion)."""
    from flyte_agent_factory.child import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
        track_submissions,
    )

    before = len(names)
    with track_submissions(names.append):
        waiting = asyncio.ensure_future(task.aio(engine=engine))  # type: ignore[attr-defined]
    while True:
        done, _ = await asyncio.wait({waiting}, timeout=POLL_S)
        if done:
            return str(waiting.result())
        if len(names) == before:
            continue
        try:
            action = await remote.Action.get.aio(run_name=flyte.ctx().action.run_name, name=names[-1])
        except Exception:
            # Poll boundary: a failed lookup is reported and retried on the next poll.
            logger.exception("could not read child action %s; retrying", names[-1])
            continue
        phase = str(action.phase)
        if phase.endswith("SUCCEEDED"):
            waiting.cancel()
            details = await action.details.aio()
            return str((await details.outputs.aio())[0])
        if phase.endswith(TERMINAL_FAIL) and not waiting.done():
            # Leave a queue timeout to `aio`, which raises MaxQueuedTimeExceededError for the fallback.
            await asyncio.wait({waiting}, timeout=2 * POLL_S)
            if waiting.done():
                return str(waiting.result())
            raise ChildEnded(f"child action {action.name} ended {phase}")


def _abort_owned(names: list[str], workspace: Path) -> None:
    from flyte_agent_factory.child import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
        abort_children,
    )

    # abort_children refuses anything not recorded as this action's own child. A failed abort is reported,
    # not allowed to mask the error being propagated.
    for name in names[-1:]:
        try:
            abort_children([name], "devbox MCP harness interrupted", base_dir=workspace)
        except Exception:
            logger.exception("could not abort child action %s", name)
