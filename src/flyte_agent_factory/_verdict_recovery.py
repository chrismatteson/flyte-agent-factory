"""Ask a judge's own session for the verdict its reply left out, instead of re-running the whole review.

A judge that wrote findings and forgot (or mangled) the `VERDICT:` line has done the expensive part. One
cheap turn on the same Claude session (`--resume`, no tools, structured output so the verdict is a field)
gets the verdict for a few cents instead of a second full review. It runs inside the sub-agent, after the
judge's own call, and only for a reply `_no_verdict.classify` calls `unparseable`: no output, a turn limit
and a tool or limit error go through the supervisor's retry as before.

Nothing here can change a finding. The follow-up's verdict is appended after the original text, which stays
byte for byte as the judge wrote it; any failure, timeout or answer that is not exactly PASS or FAIL leaves
the text untouched, and the supervisor falls back to the full re-run. Findings still decide the verdict
(`_findings.decide`): a recovered PASS never overrides an evidenced blocking finding.

Codex works the same way: `_run_codex` takes the `thread_id` from the `thread.started` event and the follow-up
is one `codex exec resume` turn with an output schema. A run that never reported a thread id has nothing to
resume and records `skipped: no session id`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flyte_agent_factory import (
    _auth,
    _checkpoint,
    _findings,
    _no_verdict,
    _policy,
    _seal_guard,
    _session_config,
    _timeline,
    _unprivileged,
    _usage,
)

#: The structured answer: a verdict and nothing else, so a stray word cannot pass for one.
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["PASS", "FAIL"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}
PROMPT = (
    "Your review above ended without its verdict. Do not re-review and do not change any finding. "
    "Reply with your verdict only."
)
#: Longest the follow-up may take; it is a two-turn call with no tools.
FOLLOWUP_TIMEOUT_S = 300
_MAX_TURNS = "2"

#: Takes a session id and the prompt; returns the CLI's result JSON. Raises on any failure to get one.
Runner = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass
class Followup:
    """What `recover` made of a reply: the text to return, and `outcome` (`recovered` or `failed: <why>`)."""

    text: str
    outcome: str
    payload: dict[str, Any] | None = None


def wants_followup(text: str) -> bool:
    """Return whether `text` is a reply with content but no verdict: the one case worth a follow-up.

    A JSON object is the CLI's own payload reaching here in place of a review, never a judge's reply.
    """
    if _is_json_object(text):
        return False
    return _no_verdict.classify(text) == "unparseable" and not _findings.verdict_words(text)


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def read_verdict(payload: Mapping[str, Any]) -> str | None:
    """Return PASS or FAIL from a follow-up's result JSON, or None when it holds anything else.

    The structured field is the answer when the CLI returns one, and then only an exact PASS or FAIL
    counts. Without it (an older CLI that ignored the schema) the `result` text must be one verdict line.
    """
    if payload.get("is_error") or payload.get("subtype", "success") != "success":
        return None
    structured = payload.get("structured_output")
    if structured is not None:
        value = structured.get("verdict") if isinstance(structured, dict) else None
        return value if value in ("PASS", "FAIL") else None
    result = payload.get("result")
    lines = [line for line in str(result).splitlines() if line.strip()] if isinstance(result, str) else []
    found = _findings.verdict_words(lines[0]) if len(lines) == 1 else []
    return found[0].upper() if found else None


async def recover(text: str, payload: Mapping[str, Any], runner: Runner) -> Followup:
    """Return `text` with a same-session verdict appended when it lacks one, else unchanged.

    `payload` is the judge's own result JSON (for its `session_id`). `outcome` is empty when no follow-up
    was due, so a reply that already has a verdict never costs a call.
    """
    if not wants_followup(text):
        return Followup(text, "")
    session = payload.get("session_id")
    if not session:
        return Followup(text, "skipped: no session id")
    if sum(1 for line in text.splitlines() if _findings._FENCE.match(line)) % 2:
        # A verdict line inside an open fence is not a section break: it would join the last finding.
        return Followup(text, "skipped: unclosed code fence")
    try:
        answer = await runner(str(session), PROMPT)
    except asyncio.TimeoutError:
        return Followup(text, "failed: timeout")
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        LookupError,
        AttributeError,
    ) as e:  # a follow-up that cannot run changes nothing
        _timeline._log_line(f"[verdict-followup] {type(e).__name__}: {e}"[:300], err=True)
        return Followup(text, f"failed: {type(e).__name__}")
    verdict = read_verdict(answer)
    if verdict is None:
        return Followup(text, "failed: not a single PASS or FAIL", answer)
    return Followup(f"{text}{_no_verdict.recovered_suffix(verdict)}", "recovered", answer)


def with_followup(trailer: str, followup: Followup) -> str:
    """Return the usage `trailer` carrying the follow-up's outcome and its usage added to the judge's own.

    The supervisor reads the outcome (`_call_reviewer`) and strips it with the trailer, so it never reaches
    the text that is parsed or kept.
    """
    if not followup.outcome:
        return trailer
    _, rec = _usage._split_usage(trailer)
    rec["verdict_followup"] = followup.outcome
    extra = followup.payload or {}
    usage = extra.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    try:
        for key in _session_config._USAGE_FIELDS:
            rec[key] = int(rec.get(key) or 0) + int(usage.get(key) or 0)
        rec["cost_usd"] = float(rec.get("cost_usd") or 0.0) + float(extra.get("total_cost_usd") or 0.0)
        rec["turns"] = int(rec.get("turns") or 0) + int(extra.get("num_turns") or 0)
    except (ValueError, TypeError):  # unreadable usage is no usage; the outcome still goes
        pass
    return f"{_usage._USAGE_MARK}{json.dumps(rec)}-->"


async def run_claude(
    claude: str,
    work: Path,
    env: dict[str, str],
    model: str,
    effort: str,
    session: str,
    prompt: str,
    *,
    launcher: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one `claude -p --resume` turn with the verdict schema and return its result JSON.

    The same credential path and seal as the judge's own call (see `_subagent_run._ask_remaining_work`,
    the other short `--resume` turn). Raises when there is no result: `recover` turns that into "failed".
    """
    with _auth.token_fd(env, os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()) as (child_env, pass_fds):
        proc = await _unprivileged.spawn(
            "a Claude Code sub-agent",
            *launcher,  # a sealed judge's follow-up turn is inside the same seal
            claude,
            "-p",
            prompt,
            "--resume",
            session,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            *_policy.argv(_policy.Setting(model, effort)),
            "--max-turns",
            _MAX_TURNS,
            "--tools",
            "",
            "--permission-mode",
            "bypassPermissions",
            *(_seal_guard.cli_deny_args() if launcher else ()),
            cwd=str(work),
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=FOLLOWUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _checkpoint._reap(proc)
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"follow-up exited {proc.returncode}")
    answer = json.loads(raw.decode(errors="replace"))
    if not isinstance(answer, dict):
        raise ValueError("follow-up result is not an object")
    return answer


async def run_codex(
    work: Path,
    env: dict[str, str],
    session: str,
    prompt: str,
    *,
    model: str,
    effort: str,
    tokens: Callable[[list[dict[str, Any]]], dict[str, int]],
    wrap: Callable[[list[str]], list[str]] = list,
    tools_off: Sequence[str] = (),
    scratch: Path | None = None,
) -> dict[str, Any]:
    """Run one `codex exec resume` turn with the verdict schema and return a payload `read_verdict` reads.

    The same options, `env` (so `CODEX_HOME`) and jail (`wrap`, `tools_off`) as the judge's own call. The
    schema goes in a file because `--output-schema` takes a path; the answer comes back in the `-o` file as
    JSON, or as text when the model ignored the schema. `tokens` maps the `turn.completed` events to the
    usage fields `with_followup` adds. Raises when there is no answer: `recover` turns that into "failed".
    """
    # Beside the judge's own `-o` file (`scratch`, its base), where a sealed run can write it too.
    with tempfile.TemporaryDirectory(prefix="verdict-followup-", dir=scratch) as tmp:
        schema, last = Path(tmp) / "schema.json", Path(tmp) / "last.txt"
        schema.write_text(json.dumps(SCHEMA))
        argv = [
            "codex",
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'cli_auth_credentials_store="file"',
            "-o",
            str(last),
            "--output-schema",
            str(schema),
        ]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        if model:
            argv += ["-m", model]
        # After `--`, the id then the prompt: `resume` takes them in that order.
        argv += [*tools_off, "--", session, prompt]
        proc = await _unprivileged.spawn(
            "a Codex sub-agent",
            *wrap(argv),
            cwd=str(work),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=FOLLOWUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _checkpoint._reap(proc)
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"follow-up exited {proc.returncode}")
        text = last.read_text(errors="replace").strip() if last.is_file() else ""
    if not text:
        raise RuntimeError("follow-up gave no answer")
    events: list[dict[str, Any]] = []
    for line in raw.decode(errors="replace").splitlines():
        try:
            event = json.loads(line) if line.strip().startswith("{") else None
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    answer: dict[str, Any] = {"structured_output": parsed} if isinstance(parsed, dict) else {"result": text}
    answer["usage"] = tokens(events)
    return answer
