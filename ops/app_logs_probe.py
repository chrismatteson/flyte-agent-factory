"""Check the app tools against a live app, read-only: logs, status history and health, as an MCP call would.

The tools read app logs with the caller's own request headers (`_app_tools._build_caller_headers`). This
passes this pod's Union token the same way, as the `Authorization: Bearer` header of a stand-in request
context, so the connect route (SelectCluster, then TailLogs on the data plane it names) is exercised exactly
as the deployed server would exercise it. Nothing is deployed, restarted or written to the app.

    PYTHONPATH=src uv run python ops/app_logs_probe.py                 # the profile's factory MCP app
    PYTHONPATH=src uv run python ops/app_logs_probe.py --app <name> --out evidence/union-app-logs.json

The evidence is redacted (`_mcp_redact`) and the deployment's own names (org, endpoint and app hosts, the
factory's project) are replaced by placeholders, so the file can be committed. Log messages are kept only as
a few samples per call; counts, replicas, revisions and timings are kept whole.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

# Run under `uv run`, which does not install this project (`package = false`), as in ops/deploy_status.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flyte_agent_factory import _profile

_profile.select_from_argv()

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402 -- after select_from_argv, as every ops script
from starlette.datastructures import Headers  # noqa: E402 -- after select_from_argv, as every ops script

from flyte_agent_factory import _app_tools, _errors, _factory, _mcp_redact, _union_identity  # noqa: E402 -- after select_from_argv
from ops import personal_literals  # noqa: E402 -- after select_from_argv

REPO = Path(__file__).resolve().parent.parent
SAMPLE_LINES = 5
#: What a tool call raises: a refusal or a wrapped failure (`ToolError`), or the remote call's own error.
PROBE_ERRORS = (*_errors.REMOTE_ERRORS, ToolError)
_REGISTRY = re.compile(r"\b\d{12}\.dkr\.ecr\.[\w-]+\.amazonaws\.com\b")


def read_bearer_token() -> str:
    """Return this pod's Union access token, from the command the SDK uses; never printed or written."""
    done = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._union_token"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise SystemExit(f"no Union token: {done.stderr.strip()[-300:]}")
    return done.stdout.strip()


def build_request_context(token: str) -> Any:
    """Return a stand-in for FastMCP's Context: the tools read only `request_context.request.headers`."""
    request = SimpleNamespace(headers=Headers({"authorization": f"Bearer {token}"}))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


async def time_call(label: str, call: Any) -> dict[str, Any]:
    """Await one tool call and record its wall time and its result or error."""
    began = time.monotonic()
    try:
        result = await call
    except PROBE_ERRORS as exc:  # a probe records each failure as evidence
        return {
            "call": label,
            "ok": False,
            "elapsed_s": round(time.monotonic() - began, 2),
            "error": repr(exc),
        }
    return {"call": label, "ok": True, "elapsed_s": round(time.monotonic() - began, 2), "result": result}


def _sample_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep every field but `lines`, of which the oldest and newest few stand for the rest."""
    lines = result.get("lines")
    if not isinstance(lines, list) or len(lines) <= 2 * SAMPLE_LINES:
        return result
    out = {k: v for k, v in result.items() if k != "lines"}
    out["lines_oldest"] = lines[:SAMPLE_LINES]
    out["lines_newest"] = lines[-SAMPLE_LINES:]
    out["line_span"] = {"first_ts": lines[0]["ts"], "last_ts": lines[-1]["ts"]}
    out["originators"] = {
        o: sum(1 for e in lines if e["originator"] == o) for o in ("user", "system", "unknown")
    }
    return out


_OTHER_APP_HOST = re.compile(r"[a-z0-9][a-z0-9-]*\.apps\.<endpoint>")


def make_placeholders(app_url: str) -> list[tuple[str, str]]:
    """Return the deployment's own names in what a tool returns, each with the placeholder for it."""
    pairs = [
        (os.environ.get("_U_ORG_NAME", ""), "<org>"),
        (
            urlparse("//" + os.environ.get("_U_EP_OVERRIDE", "").removeprefix("dns:///")).hostname or "",
            "<endpoint>",
        ),
        (urlparse(app_url).hostname or "", "<app-host>"),
        (_factory.PROJECT, "<project>"),
    ]
    # Longest first, so a host is replaced whole before an org name inside it is.
    return sorted([(lit, ph) for lit, ph in pairs if len(lit) >= 3], key=lambda p: -len(p[0]))


def scrub(text: str, pairs: list[tuple[str, str]], token: str) -> str:
    """Replace the token, the deployment's names and any owner literal the guard knows in `text`."""
    text = text.replace(token, _mcp_redact.MARKER)
    for literal, placeholder in pairs:
        text = text.replace(literal, placeholder)
    # Any other app's host under the endpoint: a status of production names its URL.
    text = _OTHER_APP_HOST.sub("<app-host>", text)
    # The image registry names the AWS account; then any other tenant host or owner literal the guard knows.
    text = _REGISTRY.sub("<registry>", text)
    return personal_literals.pattern().sub("<app-host>", text)


def summarize_headline(calls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the numbers the write-up reports, per call."""
    out: dict[str, Any] = {}
    for label, entry in calls.items():
        result = entry.get("result") or {}
        row: dict[str, Any] = {"ok": entry["ok"], "elapsed_s": entry["elapsed_s"]}
        for key in ("returned", "truncated", "stopped_because", "replicas", "revisions", "gaps"):
            if key in result:
                row[key] = result[key]
        if label == "get_app_status":
            row["conditions"] = len(result.get("history", []))
            row["last_failure"] = result.get("last_failure")
        if label == "factory_mcp_health":
            row["restarts"] = result.get("restarts")
            row["errors_last_hour"] = result.get("errors_last_hour", {}).get("count")
        out[label] = row
    return out


async def probe_app_logs(app: str, token: str) -> dict[str, Any]:
    """Call each app tool against `app` as an MCP request bearing `token` would, one after another."""
    ctx = build_request_context(token)
    calls: dict[str, dict[str, Any]] = {}
    for label, call in (
        ("get_app_logs_newest_200", _app_tools.get_app_logs(app, max_lines=200, ctx=ctx)),
        ("get_app_logs_last_1h", _app_tools.get_app_logs(app, since="1h", max_lines=1000, ctx=ctx)),
        ("get_app_logs_persisted", _app_tools.get_app_logs(app, source="persisted", ctx=ctx)),
        ("get_app_status", _app_tools.get_app_status(app, history=40, ctx=ctx)),
    ):
        calls[label] = await time_call(label, call)

    # The health tool reports on the server it runs in: its app name from the environment and its replica
    # from HOSTNAME. Here that is the probed app and its newest live replica, as it would be in that pod.
    os.environ[_app_tools.OWN_APP_ENV] = app
    live = (calls["get_app_logs_newest_200"].get("result") or {}).get("lines") or []
    live_replicas = [e["replica"] for e in live if e["source"] == "live" and e["replica"]]
    if live_replicas:
        os.environ["HOSTNAME"] = live_replicas[-1]
    calls["factory_mcp_health"] = await time_call(
        "factory_mcp_health", _app_tools.make_factory_mcp_health(_app_tools.HealthInputs())(ctx=ctx)
    )
    return calls


def write_evidence(path: Path, app: str, calls: dict[str, dict[str, Any]], token: str) -> None:
    """Write the redacted, placeholder-scrubbed evidence; refuse when a deployment name survives."""
    status = (calls["get_app_status"].get("result") or {}).get("summary") or {}
    pairs = make_placeholders(status.get("url", ""))
    evidence = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app": app,
        "how": "ops/app_logs_probe.py: tools called in-process with this pod's token as the bearer header",
        "headline": summarize_headline(calls),
        "calls": {
            label: {**e, "result": _sample_result(e["result"])} if e["ok"] else e
            for label, e in calls.items()
        },
    }
    text = json.dumps(_mcp_redact.redact_result(evidence), indent=2, default=str)
    text = scrub(text, pairs, token)
    leaks = [ph for lit, ph in pairs if lit in text] + (["token"] if token in text else [])
    if leaks:
        raise SystemExit(f"refusing to write evidence: still holds {leaks}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")


def main() -> int:
    """Probe the app and write the evidence."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile (read before argparse runs)")
    parser.add_argument(
        "--app", default=_app_tools.read_own_app_name(), help="app to probe (default: %(default)s)"
    )
    parser.add_argument("--out", type=Path, default=REPO / "evidence" / "union-app-logs.json")
    args = parser.parse_args()

    profile = _profile.require()
    _union_identity.init_for_script(profile, key_env=_union_identity.KEY_ENV)
    token = read_bearer_token()
    calls = asyncio.run(probe_app_logs(args.app, token))
    write_evidence(args.out, args.app, calls, token)
    summary = json.dumps(summarize_headline(calls), indent=2, default=str)
    print(scrub(summary, make_placeholders(""), token))
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
