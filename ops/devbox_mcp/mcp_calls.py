"""Call the factory MCP tools in-process against the devbox about a live probe run; write what they return.

Run inside the privileged pod, from the shipped engine tree under its own environment, with the devbox profile
selected (AGENT_FACTORY_PROFILE) and kubectl pointed at the devbox:

    uv run python ops/devbox_mcp/mcp_calls.py --run <probe run> --out <evidence.json>

The server is the factory's own (`_factory_mcp.app.mcp`, every wrapper included), built as the tests build it,
and every call goes through FastMCP's public `list_tools` and `call_tool`. Nothing is deployed for it: the
devbox cannot host the factory MCP app on this engine (oss-setup-selftest REPORT, gap 1), and in-process the
calls exercise the same tool code against the same Flyte API.

`factory.status` is a task, not a tool, so it is reached the way chat reaches it: `run_task`, `wait_for_run`,
`get_run_io`. It then runs in a devbox pod, as it does for the operator. `read_status` is also called here, in
this process, to tell a failure inside the cluster from one anywhere.

Every result is recorded whole or cut to its newest lines; an error is recorded by type and message. The
devbox holds no secret of ours (its two secrets are a placeholder and a key generated in the pod).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # as in ops/deploy_factory.py

from mcp.server.fastmcp.exceptions import ToolError
from probe_run import (  # type: ignore[import-not-found]  # sibling script, on sys.path only when run as a script
    A0_LINE,
    LEAF_LINE,
    PROJECT_NAME,
)

from flyte_agent_factory import _errors

CONDITION_WAIT_S = 600
STATUS_WAIT_S = 600
CONSOLE = "http://localhost:30080"
LOG_LINES = 20
#: What a served tool raises in process: `ToolError`, which FastMCP wraps every failure in, or a remote error.
CALL_ERRORS = (*_errors.REMOTE_ERRORS, ToolError)


class Recorder:
    """Calls tools on a FastMCP server and keeps a record of each call."""

    def __init__(self, server: Any) -> None:
        """Record calls made on `server`."""
        self.server = server
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool: str, arguments: dict[str, Any], label: str = "") -> Any:
        """Call `tool`; record arguments, seconds and result (or error). Return the result, None on error."""
        record: dict[str, Any] = {"label": label or tool, "tool": tool, "arguments": arguments}
        start = time.monotonic()
        try:
            result = _unwrap(await self.server.call_tool(tool, arguments))
        except CALL_ERRORS as e:  # a failed call is the evidence; the run goes on to the next
            record.update(ok=False, error=f"{type(e).__name__}: {e}")
            result = None
        else:
            record.update(ok=True, result=result)
        record["seconds"] = round(time.monotonic() - start, 2)
        self.calls.append(record)
        print(f"{record['label']}: {'ok' if record['ok'] else record['error'][:300]}", flush=True)
        return result

    def note(self, label: str, **fields: Any) -> None:
        """Record something that is not a tool call (ground truth, an SDK read)."""
        self.calls.append({"label": label, **fields})


def _unwrap(out: Any) -> Any:
    """Return a `call_tool` result as plain data.

    With an output schema FastMCP returns `(content, structured)`, wrapping a non-object return value as
    `{"result": value}`; without one, only content blocks, whose text is the JSON.
    """
    if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], dict):
        structured = out[1]
        return structured["result"] if set(structured) == {"result"} else structured
    texts = [getattr(block, "text", "") for block in out]
    joined = "".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


def _run_shell(*cmd: str, timeout: float = 60) -> dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return {
        "cmd": " ".join(cmd),
        "exit": p.returncode,
        "stdout": p.stdout[-4000:],
        "stderr": p.stderr[-2000:],
    }


def _find_newest(lines: list[str], marker: str) -> str:
    return next((line for line in reversed(lines) if marker in line), "")


def _read_log_lines(result: Any) -> list[str]:
    """Return the lines of a `get_logs` result (its `logs` text), oldest first."""
    return str(result.get("logs") or "").splitlines() if isinstance(result, dict) else []


def _read_attempts(run: str) -> dict[str, Any]:
    """Read a0's attempts from its ActionDetails protobuf: what the attempt-0 hypothesis turns on."""
    import flyte.remote as remote
    from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]  # no stubs installed

    action = remote.Action.get(run_name=run, name="a0")
    details = action.details()
    return {
        "details_attempts": details.attempts,
        "pb2_attempts": [MessageToDict(a) for a in details.pb2.attempts],
    }


def _list_pods(attempts: dict[str, Any]) -> list[dict[str, str]]:
    """Namespace, pod and primary container of each attempt's log context."""
    out = []
    for attempt in attempts.get("pb2_attempts", []):
        context = attempt.get("logContext") or {}
        for pod in context.get("pods") or []:
            out.append(
                {
                    "attempt": str(attempt.get("attempt", "")),
                    "namespace": pod.get("namespace", ""),
                    "pod": pod.get("podName", ""),
                    "container": pod.get("primaryContainerName", ""),
                }
            )
    return out


def _read_labelled(run: str) -> dict[str, Any]:
    """Return the probe runs `factory.status` finds by label, and the newest runs unfiltered."""
    import flyte.remote as remote

    from flyte_agent_factory import _factory_base

    by_label = _factory_base.list_runs({_factory_base.LABEL_PROJECT: PROJECT_NAME}, limit=5)
    newest = list(itertools.islice(remote.Run.listall(sort_by=("created_at", "desc")), 10))
    return {
        "by_label": [r.name for r in by_label],
        "probe_run_listed_unfiltered": any(r.name == run for r in newest),
        "unfiltered": [{"name": r.name, "labels": _factory_base.run_labels(r)} for r in newest],
    }


async def collect(server: Any, run: str, project: str, domain: str) -> dict[str, Any]:
    """Make every call the harness records, in order, and return the evidence."""
    rec = Recorder(server)
    scope = {"project": project, "domain": domain}
    evidence: dict[str, Any] = {"run": run, "project": project, "domain": domain, "started": time.time()}

    tools = await server.list_tools()
    evidence["tools"] = sorted(t.name for t in tools)

    # The run is live once its question is open; until then nothing below has anything to read.
    deadline = time.monotonic() + CONDITION_WAIT_S
    conditions: Any = None
    while time.monotonic() < deadline:
        try:
            conditions = _unwrap(await server.call_tool("list_conditions", {"run_name": run, **scope}))
        except CALL_ERRORS as e:  # the run may not be listed yet; the last error is kept
            evidence["condition_wait_error"] = f"{type(e).__name__}: {e}"
        else:
            if conditions:
                break
        await asyncio.sleep(10)
    evidence["condition_wait_s"] = round(CONDITION_WAIT_S - (deadline - time.monotonic()), 1)
    await asyncio.sleep(3 * 5)  # a few more ticks in the log after the question opened

    await rec.call("get_run", {"name": run, **scope})
    actions = await rec.call("list_actions", {"run_name": run, **scope})
    # The leaf is the child that is not a0 and not the question (a condition is an action too, `paused`).
    asked = {c.get("name") for c in conditions or [] if isinstance(c, dict)}
    leaves = [
        a.get("name")
        for a in actions or []
        if isinstance(a, dict) and a.get("name") not in ("a0", None, *asked)
    ]
    await rec.call("get_action", {"run_name": run, "action_name": "a0", **scope})
    try:
        attempts = await asyncio.to_thread(_read_attempts, run)
    except _errors.REMOTE_ERRORS as e:  # recorded; the tool calls below do not depend on it
        attempts = {"error": f"{type(e).__name__}: {e}"}
    rec.note("a0 attempts (ActionDetails.pb2)", **attempts)

    run_logs = await rec.call("get_logs", {"run_name": run, "max_lines": LOG_LINES, **scope}, "get_logs run")
    a0_logs = await rec.call(
        "get_logs", {"run_name": run, "action_name": "a0", "max_lines": LOG_LINES, **scope}, "get_logs a0"
    )
    real = max((int(a.get("attempt", 0)) for a in attempts.get("pb2_attempts", [])), default=0)
    real_logs = None
    if real:
        real_logs = await rec.call(
            "get_logs",
            {"run_name": run, "action_name": "a0", "attempt": real, "max_lines": LOG_LINES, **scope},
            f"get_logs a0 attempt={real}",
        )
    leaf_logs = None
    if leaves:
        await rec.call("get_action", {"run_name": run, "action_name": leaves[0], **scope}, "get_action leaf")
        leaf_logs = await rec.call(
            "get_logs",
            {"run_name": run, "action_name": leaves[0], "max_lines": LOG_LINES, **scope},
            f"get_logs leaf {leaves[0]}",
        )

    # Ground truth: the pod's own newest line, read with kubectl as the tools' answer is compared to it.
    truth = []
    for pod in _list_pods(attempts):
        kubectl = _run_shell(
            "kubectl", "logs", "-n", pod["namespace"], pod["pod"], "-c", pod["container"], "--tail=3"
        )
        truth.append({**pod, **kubectl})
    rec.note(
        "kubectl logs --tail=3 of a0's pod",
        pods=truth,
        newest_tool_line_run=_find_newest(_read_log_lines(run_logs), A0_LINE),
        newest_tool_line_a0=_find_newest(_read_log_lines(a0_logs), A0_LINE),
        newest_tool_line_a0_real_attempt=_find_newest(_read_log_lines(real_logs), A0_LINE),
        newest_tool_line_leaf=_find_newest(_read_log_lines(leaf_logs), LEAF_LINE),
    )
    rec.note("kubectl get pods -A", **_run_shell("kubectl", "get", "pods", "-A", "-o", "wide"))

    conditions = await rec.call("list_conditions", {"run_name": run, **scope})
    condition = next((c.get("name") for c in conditions or [] if isinstance(c, dict) and c.get("name")), None)
    if condition:
        await rec.call("get_condition", {"name": condition, "run_name": run, **scope})

    # How factory.status finds a project's run: by its label. Recorded so a "not found" can be told apart
    # from a label filter the server ignores or does not match.
    try:
        labelled = await asyncio.to_thread(_read_labelled, run)
    except _errors.REMOTE_ERRORS as e:  # recorded as the evidence
        labelled = {"error": f"{type(e).__name__}: {e}"}
    rec.note("runs by label (_factory_base.list_runs) and unfiltered", **labelled)

    # factory.status by project name and by run name (read_status falls back to the run id, which reaches the
    # report without the label lookup): here, outside the cluster, and as chat runs it, in a devbox pod.
    from flyte_agent_factory import _factory

    evidence["factory_status"] = {}
    for key in (PROJECT_NAME, run):
        start = time.monotonic()
        try:
            status_here: dict[str, Any] = {
                "ok": True,
                "result": await asyncio.to_thread(_factory.read_status, key),
            }
        except (*_errors.REMOTE_ERRORS, ImportError) as e:  # recorded; an import failure is how run 1 broke
            status_here = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        rec.note(
            f"read_status({key}) in-process (outside the cluster)",
            seconds=round(time.monotonic() - start, 2),
            **status_here,
        )
        started = await rec.call(
            "run_task",
            {"name": "factory.status", "inputs": {"name": key}, **scope},
            f"run_task factory.status {key}",
        )
        if not (isinstance(started, dict) and started.get("name")):
            continue
        status_run = started["name"]
        await rec.call(
            "wait_for_run",
            {"name": status_run, "timeout_s": STATUS_WAIT_S, **scope},
            f"wait_for_run status {key}",
        )
        io = await rec.call(
            "get_run_io", {"name": status_run, "outputs_only": True, **scope}, f"get_run_io {key}"
        )
        evidence["factory_status"][key] = _read_status_output(io)
        if evidence["factory_status"][key] is None:
            await rec.call(
                "get_logs",
                {"run_name": status_run, "max_lines": 60, **scope},
                f"get_logs status run {key} (no output)",
            )

    path = f"/v2/domain/{domain}/project/{project}/runs/{run}"
    rec.note("console route", url=f"{CONSOLE}{path}", **_read_http_status(f"{CONSOLE}{path}"))

    # Answer the question, let the run end, and ask for a0's logs once its pod may be gone.
    if condition:
        await rec.call(
            "signal_condition",
            {"name": condition, "run_name": run, "value": "devbox harness: done", **scope},
        )
        await rec.call("wait_for_run", {"name": run, "timeout_s": 300, **scope}, "wait_for_run probe")
        await rec.call(
            "get_logs",
            {"run_name": run, "action_name": "a0", "max_lines": LOG_LINES, **scope},
            "get_logs a0 after the run ended",
        )
    evidence["calls"] = rec.calls
    evidence["ended"] = time.time()
    return evidence


def _read_status_output(io: Any) -> Any:
    """`factory.status`'s JSON output parsed, from a `get_run_io` result; None if there is none."""
    outputs = io.get("outputs") if isinstance(io, dict) else None
    if not outputs:
        return None
    value = next(iter(outputs.values())) if isinstance(outputs, dict) else outputs
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def _read_http_status(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return {"http_status": resp.status}
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main(argv: list[str] | None = None) -> int:
    """Collect the evidence for `--run` and write it to `--out`."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True, help="the probe run (probe_run.py prints it)")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    from flyte_agent_factory import _factory, _factory_mcp, _profile
    from flyte_agent_factory._union_identity import init_for_script

    profile = _profile.require()
    init_for_script(profile)
    # What the deployed app has in its environment, so an omitted project or domain means the factory's.
    env_vars = _factory_mcp.app.env_vars or {}
    for name in ("FLYTE_MCP_PROJECT", "FLYTE_MCP_DOMAIN"):
        os.environ[name] = env_vars[name]
    server = _factory_mcp.app.mcp
    assert server is not None  # built in the environment's __post_init__
    evidence = asyncio.run(collect(server, args.run, _factory.PROJECT, _factory.DOMAIN))
    args.out.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
    print(f"wrote {args.out}: {len(evidence['calls'])} records", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
