"""A stand-in factory project run for the devbox harness: it logs, starts a child and asks a question.

Run inside the privileged pod, from the shipped engine tree under its own environment, against the devbox:

    uv run python ops/devbox_mcp/probe_run.py --minutes 40

It prints `<run name> <run url>` and returns at once; the run stays live until its question is answered or
`--minutes` pass.

Why a stand-in and not `ops/operate.py launch`: a real project needs a Claude token, the devbox is given none
of ours, and an agent that cannot sign in neither logs steadily nor pauses on a question on cue. What the MCP
tools read is made the way a real a0 makes it:
- the report through the engine's own `_timeline._Timeline`, so `factory.status` parses it as it would a
  project's;
- the question through `_conditions.open_condition`, as `_bridge` asks one, and listed in the report's open
  questions as the bridge lists it;
- a child action, because on OSS starting one resets a0's attempt count (REPORT.md, devbox section), the
  suspected cause of `Log stream not available for action a0`;
- the run labelled with its project name, as the queue labels a launch, so `factory.status <name>` finds it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

# Run under `uv run`, which does not install the engine (as in ops/deploy_factory.py). The same line makes the
# import work in the task pod, where the code bundle holds this file and `src/` side by side.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import flyte  # noqa: E402 -- after the sys.path line

from flyte_agent_factory import _conditions, _profile, _timeline  # noqa: E402 -- after the sys.path line
from flyte_agent_factory._images import agent_image  # noqa: E402 -- after the sys.path line

PROJECT_NAME = "devbox-mcp-probe"
QUESTION = "Devbox MCP harness: is the factory MCP able to follow this run? (answered by the harness)"
TICK_S = 5
#: Both lines are matched by the harness (mcp_calls.py) to compare the tools' newest line with the pod's own.
A0_LINE = "[devbox-probe a0] tick"
LEAF_LINE = "[devbox-probe leaf] tick"

env = flyte.TaskEnvironment(
    name="devbox-mcp-probe",
    # The factory's own image: deploy_factory.py has just built it into the devbox registry, so this is a
    # cache hit, and the engine modules the report and the question come from are importable in the pod.
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="1Gi"),
    env_vars=_profile.pod_env(),
)


def _read_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _sample_tick(prefix: str, seconds: float) -> int:
    """Print a numbered, timestamped line every TICK_S seconds for `seconds`; return how many."""
    n = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        n += 1
        print(f"{prefix} {n} at {_read_now()}", flush=True)
        await asyncio.sleep(TICK_S)
    return n


@env.task
async def find_leaf_action(seconds: int) -> int:
    """Log for `seconds`: a live action with no children of its own, to compare with a0."""
    return await _sample_tick(LEAF_LINE, seconds)


@env.task(report=True)
async def project(name: str, minutes: int) -> str:
    """Log, start `find_leaf_action`, publish a report with one open question, and wait for its answer."""
    print(f"[devbox-probe a0] started {name} at {_read_now()}", flush=True)
    child = asyncio.ensure_future(find_leaf_action(minutes * 60))
    timeline = _timeline._Timeline()
    timeline.header("project", name)
    timeline.row("🚀", "devbox probe run started; a child action is running")
    condition, _ = await _conditions.open_condition(
        "ask", 1, prompt=QUESTION, prompt_type="markdown", data_type=str, timeout=timedelta(minutes=minutes)
    )
    opened = time.time()
    condition_name = getattr(condition, "name", "")
    # The fields `_bridge_triage._new_entry` records; the report reads the bridge's open questions only.
    question = {
        "id": "devbox-probe-1",
        "kind": "ask",
        "text": QUESTION,
        "opened_at": opened,
        "applies_at": opened + minutes * 60,
        "deadline_at": opened + minutes * 60,
        "default": "",
        "condition": condition_name,
        "timed_out": False,
        "payload": "str",
    }
    cast(Any, timeline).bridge = SimpleNamespace(questions={question["id"]: question})
    timeline.row("🙋", f"asked: {QUESTION}")
    timeline.activity("waiting for an answer to the devbox probe question")
    await timeline.flush(force=True)
    ticker = asyncio.ensure_future(_sample_tick(A0_LINE, minutes * 60))
    try:
        answer = await condition.wait.aio()
    finally:
        ticker.cancel()
        child.cancel()
    print(f"[devbox-probe a0] answered at {_read_now()}: {answer!r}", flush=True)
    timeline.row("✅", f"answered: {answer}")
    await timeline.flush(force=True)
    return str(answer)


def main(argv: list[str] | None = None) -> int:
    """Launch `project` against the profile's backend and print `<run name> <run url>`."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default=PROJECT_NAME, help="the project label (default: %(default)s)")
    ap.add_argument("--minutes", type=int, default=40, help="how long the run waits for its answer")
    args = ap.parse_args(argv)

    from flyte_agent_factory import _factory_base, _profile_oss
    from flyte_agent_factory._union_identity import init_for_script

    profile = _profile.require()
    init_for_script(profile, root_dir=ROOT, image_builder=_profile_oss.builder(profile.backend))
    run = flyte.with_runcontext(labels={_factory_base.LABEL_PROJECT: args.name}).run(
        project, name=args.name, minutes=args.minutes
    )
    print(f"{run.name} {run.url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
