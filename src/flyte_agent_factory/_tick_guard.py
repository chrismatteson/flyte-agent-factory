"""Scheduled ticks skip themselves while an earlier run of the same task is still live.

`flyte.Trigger` has no skip-if-running option, and nothing verifies that OSS Flyte enforces
`max_queued_time`, so on a cluster too small to start a tick's pod each new tick queued behind the last.
Each scheduled task asks `skip_reason` first and returns what it says. The run listing is the same
call on OSS and Union.

This is a best-effort skip, not mutual exclusion (`_deployer.RUN_LISTING_CONSISTENT` is False): two
ticks started together may both run, so work that must be exclusive keeps its own lease.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import flyte
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import _deploy
from flyte_agent_factory._errors import REMOTE_ERRORS

log = logging.getLogger(__name__)

#: A tick only needs the oldest live run, and its own if that is older; the rest of the page is slack.
LISTING_LIMIT = 10
#: A live run created longer ago than this no longer blocks. No guarded tick may run for longer (the longest,
#: `garden_weekly` and `collect_labels`, stop at 2 hours plus their queue bound), so an older run is stuck: a
#: pod Pending on a cluster that does not enforce `max_queued_time`, or one left from an earlier deploy that
#: asked for more than the node has. Without the bound it would skip every later tick for good.
STALE_AFTER = timedelta(hours=3)


async def blocking_run(task_name: str, *, project: str, domain: str) -> tuple[str, str] | None:
    """Return `(name, phase)` of an earlier live run of `task_name`, or None.

    The SDK's Run protobuf omits creation time (see `_deployer._failed_attempts`), so the order is the
    server's: the listing is sorted oldest first and only a run listed before this one blocks. Two ticks
    started at the same moment therefore never both skip: the older one sees no one ahead of it. Runs
    created more than `STALE_AFTER` ago are left out of the listing (by the server's `created_at` filter).
    """
    # Outside a remote task (a local run, a test) there is no context or no run name, and no schedule to
    # guard. `flyte.ctx()` is typed as always set, so this reads it defensively.
    own = getattr(getattr(flyte.ctx(), "action", None), "run_name", None)
    if not own:
        return None
    try:
        async for run in remote.Run.listall.aio(
            task_name=task_name,
            in_phase=_deploy.LIVE_PHASES,
            project=project,
            domain=domain,
            sort_by=("created_at", "asc"),
            created_at=remote.TimeFilter(after=datetime.now(timezone.utc) - STALE_AFTER),
            limit=LISTING_LIMIT,
        ):
            if run.name == own:
                return None
            return run.name, str(run.phase).split(".")[-1]
    except ConnectError as exc:
        # As `_deploy.delete_retired_triggers`: bad credentials fail every call, so say so.
        if exc.code in (Code.UNAUTHENTICATED, Code.PERMISSION_DENIED):
            raise
        log.warning("Could not list runs of %s (%s); running this tick anyway", task_name, exc.code)
    except REMOTE_ERRORS as exc:
        # Fail open: a broken listing must not stop the factory's queue.
        log.warning("Could not list runs of %s (%s); running this tick anyway", task_name, type(exc).__name__)
    return None


async def skip_reason(task_name: str, *, project: str, domain: str) -> str | None:
    """Return the JSON `{"skipped": reason}` a tick returns when it must not run, else None."""
    blocker = await blocking_run(task_name, project=project, domain=domain)
    if blocker is None:
        return None
    reason = json.dumps({"skipped": f"previous run {blocker[0]} is {blocker[1]}"})
    print(f"[tick] {task_name} {reason}", flush=True)
    return reason
