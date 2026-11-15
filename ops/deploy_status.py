"""Which commit of main is each agent task actually running, and what is not deployed yet.

The loop stalled twice waiting on a deploy (the deployment's operations log, inbox/007): merged
fixes sat inert because nothing said, where a human would see it, that the
deployed code was behind. Task versions cannot answer that -- the same commit
deployed twice gets two versions -- so this reads the engine commit the deploy
stamped into the task spec (`AGENT_FACTORY_ENGINE_SHA`) and counts what main has
added since. Nothing is downloaded.

    python ops/deploy_status.py            # all agent tasks, human-readable
    python ops/deploy_status.py --json     # for the supervisor / scripts

Every env is deployed automatically on merge (the `factory-deployer` task), so a non-empty
"behind" list is information: a deploy is pending or has failed (see the deployer's run).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _deploy, _profile
from flyte_agent_factory._errors import REMOTE_ERRORS

_profile.select_from_argv()

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "flyte_agent_factory"
PROFILE = _profile.require()
# Only the slots' project tasks: the retired self-agent envs are never listed as behind.
TASKS = PROFILE.tasks
# Written by the deploy into the task spec (agent.py); `_names` has no constant for it.
STAMP = "AGENT_FACTORY_ENGINE_SHA"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout


def _fetch(timeout: float = 60) -> str:
    """Fetch origin, bounded; "" on success, else why (the caller falls back to the local ref).

    Its own process group, so a hung ssh dies with git: `PYTHONPATH=src uv run python ops/deploy_work.py`
    calls this after every deploy and must not hang on it.
    """
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(REPO), "fetch", "-q", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            start_new_session=True,
        )
    except OSError as e:
        return f"git fetch could not start: {e}"
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return f"git fetch timed out after {timeout:.0f}s"
    return "" if rc == 0 else f"git fetch exited {rc}"


def behind(commit: str, head: str) -> list[str]:
    """Return commits on main after `commit` that change the deployed package, newest first."""
    out = _git("log", "--format=%h %s", f"{commit}..{head}", "--", f"src/{PACKAGE}")
    return [line for line in out.splitlines() if line.strip()]


def _deployed_commit(task_name: str) -> tuple[str, str]:
    """(task version, engine commit) from the stamp the deploy put in the task spec; no bundle is read."""
    from flyte import remote

    task = remote.Task.get(task_name, auto_version="latest").fetch()
    env = {kv.key: kv.value for kv in task.pb2.spec.task_template.container.env}
    sha = env.get(STAMP, "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise LookupError(
            f"no {STAMP} stamp in the task spec (deployed from a tree without git, or before the stamp); "
            "redeploy to identify it"
        )
    return task.version, sha


def status(task_name: str, head: str, ref: str) -> dict[str, Any]:
    """Return which commit `task_name` is deployed from and how many deploy-relevant commits it lags."""
    try:
        version, sha = _deployed_commit(task_name)
    except (*REMOTE_ERRORS, LookupError) as e:  # one bad task must not hide the others
        return {"task": task_name, "error": f"{type(e).__name__}: {e}"[:300]}
    row: dict[str, Any] = {"task": task_name, "version": version, "commit": sha}
    if subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
    ).returncode:
        row["commit"] = None
        row["note"] = (
            f"deployed from {sha[:7]}, which is not in the local clone (fetch failed, or an unpushed deploy)"
        )
        return row
    if subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", sha, head], capture_output=True
    ).returncode:
        row["commit"] = None
        row["note"] = f"deployed from {sha[:7]}, which is not on {ref} (an unmerged branch or local deploy)"
        return row
    row["behind"] = behind(sha, head)
    return row


def render(rows: list[dict[str, Any]]) -> str:
    """Return one line per task from `status` rows, or why the task could not be checked."""
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['task']}: could not check ({r['error']})")
        elif r.get("commit") is None:
            lines.append(f"{r['task']} {r['version'][:8]}: {r['note']}")
        elif not r["behind"]:
            lines.append(f"{r['task']} {r['version'][:8]}: up to date (main@{r['commit'][:7]})")
        else:
            lines.append(
                f"{r['task']} {r['version'][:8]}: main@{r['commit'][:7]}, BEHIND by {len(r['behind'])} "
                f"src change(s):"
            )
            lines += [f"    {c}" for c in r["behind"]]
    return "\n".join(lines)


def deployer_runs_24h(now: datetime | None = None) -> int:
    """Count deployer runs in the preceding day, including webhook and reconcile runs."""
    from flyte import remote

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    return sum(
        1
        for _ in remote.Run.listall(
            task_name=_deploy.DEPLOYER_TASK,
            created_at=remote.TimeFilter(after=cutoff),
            limit=1_000_000,
        )
    )


def deployed_pair() -> dict[str, Any]:
    """Return the recorded release, independently of bundle-content matches for individual tasks."""
    from flyte_agent_factory import _deployer

    release = asyncio.run(_deployer.deployed_pair(PROFILE))
    if release is None:
        return {"task": _deploy.DEPLOY_PAIR, "deployed_pair": None}
    run, (sha, direction_sha) = release
    return {
        "task": _deploy.DEPLOY_PAIR,
        "run": run.name,
        "deployed_pair": {"sha": sha, "direction_sha": direction_sha or "none"},
    }


def main(argv: list[str] | None = None) -> int:
    """Print how far each deployed task is behind `--ref`; exit status says whether all are current."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ref", default="origin/main")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import operate  # type: ignore[import-not-found]  # sibling script, found via the sys.path insert above

    operate._init()
    stale = _fetch()
    if stale:
        print(f"warning: {stale}; using the local {args.ref}, which may be stale", file=sys.stderr)
    head = _git("rev-parse", args.ref).strip()
    rows = [status(t, head, args.ref) for t in TASKS]
    try:
        count = deployer_runs_24h()
        measurement = {"task": _deploy.DEPLOYER_TASK, "runs_24h": count}
        measured = f"{_deploy.DEPLOYER_TASK}: {count} runs in the last 24h (5-minute poll baseline: 288/day)"
    except REMOTE_ERRORS as exc:
        measurement = {"task": _deploy.DEPLOYER_TASK, "error": f"could not count runs: {type(exc).__name__}"}
        measured = f"{_deploy.DEPLOYER_TASK}: {measurement['error']}"
    try:
        release = deployed_pair()
        pair = release["deployed_pair"]
        released = f"{_deploy.DEPLOY_PAIR}: {pair if pair else 'unknown (no readable success)'}"
    except REMOTE_ERRORS as exc:
        release = {"task": _deploy.DEPLOY_PAIR, "error": f"could not read release: {type(exc).__name__}"}
        released = f"{_deploy.DEPLOY_PAIR}: {release['error']}"
    print(
        json.dumps([*rows, measurement, release], indent=2)
        if args.json
        else f"{render(rows)}\n{measured}\n{released}"
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
