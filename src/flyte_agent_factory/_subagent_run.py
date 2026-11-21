"""The sub-agent task's helpers: its result record, the direct meters, the Codex run, the hand-back."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox
from flyte.io import Dir

from flyte_agent_factory import (
    _auth,
    _author,
    _checkpoint,
    _codex_auth,
    _meter,
    _names,
    _policy,
    _reviewer,
    _roles,
    _router,
    _seal_guard,
    _seal_role,
    _seal_subagent,
    _session_config,
    _session_setup,
    _timeline,
    _unprivileged,
    _usage,
    _verdict_recovery,
)
from flyte_agent_factory._bridge import (
    Bridge,
    ensure_dirs,
    redact_tokens,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._codex_process import CodexAuthor, _Event
from flyte_agent_factory._delegation import (
    PARTIAL_PATCH,
    PLAN,
    work_diff,
)
from flyte_agent_factory._errors import REMOTE_ERRORS


async def _subagent_result(
    text: str,
    route: dict[str, Any] | None,
    *,
    routes: list[dict[str, Any]] | None = None,
    refusals: dict[str, float] | None = None,
    unusable: list[str] | None = None,
) -> str:
    if route is None:
        return redact_tokens(text)
    import flyte.report

    # The task interface stays (text, workspace). Reports carry telemetry without putting an
    # internal trailer in the author's tool response; the supervisor reads completed child reports.
    clean, rec = _usage._split_usage(text)
    rec["route"] = route
    if routes:
        rec["routes"] = routes
    if refusals:
        rec["refusals"] = refusals
    if unusable:
        rec["unusable"] = unusable
    await flyte.report.log.aio(f"{_usage._USAGE_MARK}{json.dumps(rec)}-->", do_flush=True)
    return redact_tokens(clean)  # what the coordinator reads, and may quote in its report


#: Plain Flyte tasks need a fresh process: the image lacks the actor bridge required for reusable
#: tasks (see subagent_env). Each direct child therefore probes each configured login at most once;
#: its result then supplies the supervisor's meters. Reviewer calls use the supervisor's cache and
#: never probe here. Within a process, cache failures too and serialize concurrent readers.
_direct_meters: tuple[float, dict[str, _meter.Meter | None]] | None = None
_direct_meters_lock = asyncio.Lock()


async def _read_direct_meters() -> dict[str, _meter.Meter | None]:
    """Return the meters a sub-agent can read from its own tokens, for a call nobody routed.

    An unreadable
    meter is None, which the router treats as unknown. Read at most once per `_meter.MIN_INTERVAL_S`
    in this process, failures included. Fresh child processes pay at most two probes: Claude is
    bounded at 20 seconds, Codex at 30 seconds.
    """
    global _direct_meters
    async with _direct_meters_lock:
        if _direct_meters and time.time() - _direct_meters[0] < _meter.MIN_INTERVAL_S:
            return dict(_direct_meters[1])
        readers: dict[str, Callable[[], Awaitable[_meter.Meter]]] = {}
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if token:
            # Until the probe confirms the endpoint takes a setup-token, the meter stays unknown here
            # and CLI rate-limit observations are the headroom signal.
            if _meter.SETUP_TOKEN_USAGE_SUPPORTED:
                readers["claude"] = lambda: _meter.read_claude(
                    token, _session_config._claude_account(), setup_token=True
                )
        found: dict[str, _meter.Meter | None] = {}
        for name, read in readers.items():
            try:
                found[name] = await read()
            except _usage._METER_ERRORS as exc:
                _timeline._log_line(f"[meter] {name}: {type(exc).__name__}", err=True)
        _direct_meters = (time.time(), found)
        return dict(found)


def _available_engines() -> tuple[str, ...]:
    """Return the engines whose credential this process holds now."""
    return tuple(
        name
        for name, variable in (
            ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
            ("codex", _session_config.CODEX_TOKEN_ENV_VAR),
        )
        if os.environ.get(variable, "").strip()
    )


async def _route_direct_subagent(
    role: str,
    engine: str,
    model: str,
    effort: str,
    *,
    hard_pin: bool = False,
    available: tuple[str, ...] | None = None,
    refusals: dict[str, float] | None = None,
    unusable: list[str] | None = None,
    routes: list[dict[str, Any]] | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    """Route a `subagent(...)` call that did not come through `_call_reviewer`.

    A given engine is a pin and is logged as one. The Claude `model` and `effort` the task defaults
    to are replaced by the role's Codex setting when the router moves the call to Codex. The last item
    is the decision as a record, for the sub-agent to hand back to its parent (`_with_route`).
    """
    available = available if available is not None else _available_engines()
    purpose = role or "subagent"
    meters = await _read_direct_meters()
    for refused, until in (refusals or {}).items():
        meter = meters.get(refused) or _meter.Meter(refused, "limit-text", time.time())
        meters[refused] = replace(meter, limited_until=until)
    decision = _router.route(
        purpose,
        meters,
        available=available,
        now=time.time(),
        preferred=engine or "claude",
        pin=engine,
        hard_pin=hard_pin,
        exclude=tuple(refusals or ()),
        builders=(_names.getenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", "claude"),)
        if purpose in (*_usage._QA_ROLES, "qa", "verify") and _usage._qa_differs()
        else (),
    )
    if not decision.deferred:
        if refusals or decision.engine != (engine or "claude"):
            setting = (
                _policy.resolve_codex(purpose) if decision.engine == "codex" else _policy.DEFAULTS["author"]
            )
            model, effort = setting.model, setting.effort
        if decision.engine == "codex" and _policy.is_claude_model(model):
            model, effort = _policy.resolve_codex(purpose)
        decision = replace(decision, model=model, effort=effort)
    _usage._record_route(decision, None)
    record = {**decision.record(), "row": decision.row()}
    if decision.deferred:
        await _subagent_result(
            "", record, routes=[*(routes or []), record], refusals=refusals, unusable=unusable
        )
        raise _usage._deferred(decision)
    # Not None once it is not deferred (`Decision.deferred` is `engine is None`).
    return cast(str, decision.engine), model, effort, record


#: Longest the follow-up that asks a turn-limited worker for its remaining-work plan may take.
_PLAN_FOLLOWUP_TIMEOUT_S = 300

_PLAN_PROMPT = (
    "You ran out of turns before finishing. Do not use any tools. Reply with the plan of the work that "
    "remains, as a short numbered list: for each step the files and what to do, and how to check it. "
    "Say first what is already done."
)


async def _ask_remaining_work(
    claude: str,
    work: Path,
    env: dict[str, str],
    payload: dict[str, Any],
    model: str,
    effort: str,
    *,
    launcher: Sequence[str] = (),
) -> str:
    """Return the worker's own plan for what remains: one short `--resume` turn on its session."""
    session = payload.get("session_id")
    if not session:
        return ""
    # The same credential path as the worker's own call: in token deployments `env` alone has no login.
    with _auth.token_fd(env, os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()) as (child_env, pass_fds):
        proc = await _unprivileged.spawn(
            "a Claude Code sub-agent",
            *launcher,  # a sealed worker's follow-up turn is inside the same seal
            claude,
            "-p",
            _PLAN_PROMPT,
            "--resume",
            str(session),
            "--output-format",
            "stream-json",
            "--verbose",  # stream-json requires it; the stream carries the `rate_limit_event`s
            *_policy.argv(_policy.Setting(model, effort)),
            "--max-turns",
            "2",
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
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=_PLAN_FOLLOWUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _checkpoint._reap(proc)
        return ""
    return _final_text(raw.decode(errors="replace")) if proc.returncode == 0 else ""


async def _partial_handback(
    work: Path,
    base: str,
    max_turns: int,
    payload: dict[str, Any],
    ask_plan: Callable[[], Awaitable[str]],
    *,
    cause: str = "",
) -> str:
    """Write a stopped delegate's work to `work` and return its summary.

    `factory.partial.patch` is everything changed since `base`, untracked files included (`add -N`
    records them as intent-to-add so `git diff` shows them), and `plan.md` is what remains. The plan
    is the final assistant text when the CLI gave one, else what `ask_plan()` returns; a worker
    that cannot produce one still hands its patch back. `cause` opens the summary when the worker
    stopped for something other than its turn limit.
    """
    stopped = cause or f"the worker hit its {max_turns}-turn limit"
    repo = work / "repo"
    if not repo.is_dir():
        # The worker stopped before it extracted the repo: there is no tree to diff.
        return (
            f"PARTIAL: {stopped} before it had extracted the repo, "
            "so there is no work to hand back. Run it again, with a narrower brief if it is large."
        )
    # Disarms the tree: the worker is finished, and `Dir.from_local(work)` ships this copy back.
    diff = await asyncio.to_thread(work_diff, repo, base)
    plan = str(payload.get("result") or "").strip()
    if not plan:
        try:
            plan = (await ask_plan()).strip()
        except (OSError, RuntimeError) as exc:
            _timeline._log_line(f"[delegate] no remaining-work plan: {type(exc).__name__}", err=True)
    plan = plan or "(the worker gave no plan; read the partial patch and the brief to see what is left)"
    (work / PLAN).write_text(plan + "\n")
    (work / PARTIAL_PATCH).write_bytes(diff)
    return f"PARTIAL: {stopped} before finishing.\n\nRemaining work:\n{plan}"


async def _no_plan() -> str:
    """Answer `ask_plan` for a usage-limited worker: its engine cannot be asked; its refusal is no plan."""
    return ""


async def _reroute_after_limit(
    role: str,
    pin: str,
    model: str,
    effort: str,
    refused: str,
    error: str,
    until: float,
    **route: Any,
) -> tuple[str, str, str, dict[str, Any]]:
    """Record `refused`'s usage limit and route a direct sub-agent call again, away from it.

    `route` is `_route_direct_subagent`'s keywords; its `refusals`, `unusable` and `routes` are updated
    in place. Raises `RouterDeferred` when no engine is left, as routing does.
    """
    refusals, unusable = route["refusals"], route["unusable"]
    refusals[refused] = until
    if refused == "codex" and _usage._CODEX_LOGIN_UNUSABLE in error:
        # The supervisor's meters are where a dead login is kept out of routing; this child's
        # router forgets it on exit, so the report says so (`_ingest_subagent_usage`).
        refusals[refused] = time.time() + _usage._CODEX_BROKEN_S
        unusable.append(refused)
    engine, model, effort, route_blob = await _route_direct_subagent(role, pin, model, effort, **route)
    route["routes"].append(route_blob)
    await _subagent_result("", route_blob, routes=route["routes"], refusals=refusals, unusable=unusable)
    return engine, model, effort, route_blob


async def _limit_handback(
    work: Path, base: str, engine: str, until: float | None, routed: bool = False
) -> str | None:
    """Hand back a usage-limited delegate's work as `_partial_handback` does; None if it changed nothing.

    A limit used to fail the worker, and its caller re-ran the brief from scratch, so the work done
    until then was lost. With nothing to keep (or a tree that cannot be diffed) the caller re-raises the
    limit, and the job waits or re-routes. A `routed` caller notes a limit it sees raised; this one is
    returned instead, so it rides in the usage trailer the caller ingests (`_ingest_subagent_usage`).
    """
    repo = work / "repo"
    try:
        changed = repo.is_dir() and bool(await asyncio.to_thread(work_diff, repo, base))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # The limit error must reach the caller intact: it is what makes the job wait, not fail.
        _timeline._log_line(f"[delegate] no partial patch at the limit: {type(exc).__name__}", err=True)
        return None
    if not changed:
        return None
    resets = f" (resets {_router._reset_text(until)})" if until is not None else ""
    # The refusal's own text is no plan, and the limited engine cannot be asked for one.
    summary = await _partial_handback(
        work, base, 0, {}, _no_plan, cause=f"usage limit on {engine}{resets}: the worker stopped"
    )
    if routed and until is not None:
        summary += f"{_usage._USAGE_MARK}{json.dumps({'refusals': {engine: until}})}-->"
    return summary


# "You've hit your limit" is the subscription refusal; older CLIs say "Claude AI usage limit reached".
_CLAUDE_LIMIT = _meter._CLAUDE_LIMIT


def _raise_on_cli_error(stdout: str, *, only_limit: bool = False) -> None:
    """`claude -p --output-format json` can exit 0 with `is_error`; that is a failure to run, not an answer.

    Otherwise an auth error becomes the "result", the reviewer gives no verdict, and a
    review round is spent on an infrastructure failure.
    """
    events, payload = _usage.parse_cli_output(stdout)
    if payload.get("is_error"):
        result = str(payload.get("result", ""))[:300]
        # Not `_CODEX_LIMIT`: "quota" or a bare "429" in an ordinary error is not a usage refusal.
        meter = _meter.from_claude_events(events, _session_config._claude_account())
        if meter and meter.limited_until:
            # Carry any observed reset to the supervisor's refusal cache: a failed action has no outputs.
            meter.limited_until = _usage._keep_later_reset(result, meter.limited_until)
            observation = f" {_meter.report_comment(meter)}"
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {result}{observation}")
        if not only_limit:
            raise RuntimeError(f"sub-agent reported an error: {result}")


def _final_text(stdout: str) -> str:
    """Pull the final message out of `claude -p` output (`_usage.parse_cli_output`).

    Falls back to raw stdout: a sub-agent that produced a useful answer in an
    unexpected shape should not be reported as having produced nothing. A
    successful result payload whose message is empty is the CLI saying the agent
    wrote nothing, so it gives "" rather than the payload itself.
    """
    _, payload = _usage.parse_cli_output(stdout)
    for key in ("result", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if (
        payload.get("type") == "result"
        and payload.get("subtype") == "success"
        and not payload.get("is_error")
    ):
        return ""
    return stdout.strip()


_CODEX_LIMIT = _meter._CODEX_LIMIT


def _codex_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return token usage summed over `turn.completed` events of `codex exec --json`.

    Probed shape: input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens,
    reasoning_output_tokens.
    """
    total: dict[str, int] = {}
    for e in events:
        if e.get("type") != "turn.completed":
            continue
        for k, v in (e.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + int(v)
    return total


def _codex_tokens(events: list[dict[str, Any]]) -> dict[str, int]:
    """Return `_codex_usage` under the usage-record field names (`_session_config._USAGE_FIELDS`)."""
    usage = _codex_usage(events)
    return {
        "input_tokens": int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cached_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_write_input_tokens") or 0),
    }


async def _codex_home(base: Path) -> Path:
    """Return a private `CODEX_HOME` for the sub-agent working under `base`, logged in with its credential."""
    credential = os.environ.get(_session_config.CODEX_TOKEN_ENV_VAR, "").strip()
    if not credential:
        raise RuntimeError(
            "no Codex credential available for the sub-agent "
            "([secrets] codex_token or the slot's codex_login)"
        )
    login = _codex_auth.is_login(credential)
    if login and _codex_auth.expires_soon(credential):
        # A sub-agent gets no refresh token (below), so its copy cannot outlive the access token; a
        # short-lived one would die mid-task, so the call waits or goes to Claude now.
        raise RuntimeError(
            f"{_usage.USAGE_LIMIT_MARK} codex: the mounted access token expires within "
            f"{_codex_auth.MIN_ACCESS_LIFE_S // 60:.0f} minutes and a sub-agent cannot refresh it"
        )
    # Under $HOME, not /tmp: Codex refuses to set up its helper links under a temp dir.
    home = Path.home() / ".codex-subagents" / base.name
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    if login:
        # No refresh token: the first holder to refresh invalidates every other copy, and the author is
        # the one that keeps the chain (`_author._persist_codex_login`).
        _auth.write_private(home / "auth.json", _codex_auth.strip_refresh_token(credential))
    else:
        await asyncio.to_thread(_author._login_with_access_token, home, credential)
    return home


async def _run_codex(
    work: Path,
    base: Path,
    brief: str,
    *,
    answer_only: bool,
    role: str,
    model: str,
    effort: str = "medium",
    extra_env: dict[str, str] | None = None,
    sealed: _seal_subagent.SubagentSeal | None = None,
    verdict_followup: bool = False,
) -> str:
    """Run one Codex sub-agent in `work`; its final message, plus the usage trailer.

    Same contract as the Claude path: the brief is in `.claude-flyte/BRIEF.md`, the house rules
    in `AGENTS.md` (Codex's CLAUDE.md), a role file prepended when one is named. A usage-limit
    refusal raises with USAGE_LIMIT_MARK so the caller waits or falls back like any limit.
    """
    home = await _codex_home(base)
    _session_setup._write_managed_block(work / "AGENTS.md", _subagent_memory(answer_only=answer_only))
    role_text = ""
    if role:
        # The image holds only the new roles; an old name runs as the role it is an alias of.
        role_file = _session_setup._roles_dir() / f"{_roles.canonical_role(role)}.md"
        if not role_file.is_file():  # a roles directory from before the rename still has the old file
            role_file = _session_setup._roles_dir() / f"{role}.md"
        if not role_file.is_file():
            raise ValueError(f"unknown role {role!r}")
        text = role_file.read_text(errors="replace")
        # Drop the YAML header Claude Code's agent files carry: Codex has no use for it.
        if text.startswith("---"):
            end = text.find("\n---", 3)
            text = text[end + 4 :] if end != -1 else text
        role_text = text.strip() + "\n\n"
    prompt = role_text + (
        f"Read {_session_config._BRIEF_FILE} and carry it out. Your final message is your entire answer: "
        "nothing you write to files is kept, so put everything the brief asks for in it."
        if answer_only
        else f"Read {_session_config._BRIEF_FILE} and carry it out. Finish with a short report of what you "
        f"did."
    )
    last = base / "codex-last.txt"
    jail = sealed.open("codex", home) if sealed else None
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        # Its own container and a throwaway copy, like the Claude sub-agent's bypassPermissions.
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'cli_auth_credentials_store="file"',
        "-o",
        str(last),
    ]
    # Both optional: a direct `subagent(engine="codex")` call arrives with `effort=""` and the
    # Claude author default as `model`, and Codex refuses an empty effort and has no "sonnet".
    if model and _policy.is_claude_model(model):
        model = ""
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    if model:
        argv += ["-m", model]
    # After `--`: a prompt starting with "-" (a role file's `---` header did) was read as an option.
    if jail:
        argv += _seal_role.CODEX_TOOLS_OFF_ARGS
    argv += ["--", prompt]
    env = {
        **_session_setup._session_env({**os.environ, **(extra_env or {})}),
        "CODEX_HOME": str(home),
        **(jail.env if jail else {}),
    }
    proc = await _unprivileged.spawn(
        "a Codex sub-agent",
        *(jail.wrap(argv) if jail else argv),
        cwd=str(work),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # it otherwise waits for more input on stdin
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if sealed:
        sealed.exit_code = proc.returncode
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    events: list[Any] = []  # parsed JSON lines
    for line in stdout.splitlines():
        with contextlib.suppress(ValueError):
            if line.strip().startswith("{"):
                events.append(json.loads(line))
    answer = last.read_text(errors="replace").strip() if last.is_file() else ""
    observed = _meter.from_events(events, _session_config._codex_account())
    if proc.returncode != 0 or not answer:
        # Codex reports refusals (its usage limit included) as JSON events on stdout; stderr
        # then holds only "Reading additional input from stdin...", so a limit read as a crash.
        reported = [
            str(e.get("message") or (e.get("error") or {}).get("message") or "")
            for e in events
            if isinstance(e, dict) and e.get("type") in ("error", "turn.failed")
        ]
        detail = ("\n".join(m for m in reported if m) or stderr or stdout)[-600:]
        if (
            _codex_auth.is_auth_failure(stderr)
            or any(_codex_auth.is_auth_failure(m) for m in reported)
            or any(
                CodexAuthor._auth_failure(cast("_Event", e))  # parsed JSON; read defensively
                for e in events
                if isinstance(e, dict)
            )
        ):
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {_usage._CODEX_LOGIN_UNUSABLE}: {detail[-200:]}")
        if observed:
            if _CODEX_LIMIT.search(detail):
                observed.limited_until = (
                    _usage._keep_later_reset(detail, observed.limited_until)
                    or time.time() + _session_config.LIMIT_RETRY_S
                )
            # A failed action has no return value; its report must retain the last backend observation.
            try:
                await flyte.report.log.aio(_meter.report_comment(observed), do_flush=True)
            except (*_usage._METER_ERRORS, *REMOTE_ERRORS, RuntimeError, ValueError) as exc:
                # Reporting the meter must never replace the real error raised below.
                _timeline._log_line(f"[meter] codex: {type(exc).__name__}", err=True)
        if _CODEX_LIMIT.search(detail):
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} codex: {detail[-200:]}")
        raise RuntimeError(f"codex sub-agent exited {proc.returncode}: {detail}")
    rec = {
        **_codex_tokens(events),
        "cost_usd": 0.0,
        "models": [model or "codex:default"],
        "engine": "codex",
        "effort": effort,
        "turns": 0,
        **({"rate_limits": observed.to_dict()} if observed else {}),
    }
    trailer = f"{_usage._USAGE_MARK}{json.dumps(rec)}-->"
    if verdict_followup:
        thread_id = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        followup = await _verdict_recovery.recover(
            answer,
            {"session_id": thread_id if isinstance(thread_id, str) else ""},
            functools.partial(
                _verdict_recovery.run_codex,
                work,
                env,
                model=model,
                effort=effort,
                tokens=_codex_tokens,
                wrap=jail.wrap if jail else list,
                tools_off=_seal_role.CODEX_TOOLS_OFF_ARGS if jail else (),
                scratch=base,
            ),
        )
        answer, trailer = followup.text, _verdict_recovery.with_followup(trailer, followup)
    return answer + trailer


def _subagent_memory(answer_only: bool = False) -> str:
    # A past run: the planner, told its files are handed back and to report briefly, wrote
    # PLAN.md in a copy that is discarded and replied "see PLAN.md".
    hand_back = (
        "- Your working directory is a throwaway copy: nothing you write there is kept or seen. "
        "Your final message is your entire answer -- put everything the brief asks for in it, "
        "and never refer to a file you wrote.\n"
        if answer_only
        else "- Everything in your working directory is handed back to the parent agent when you "
        "finish. Leave it in a state someone can read: commit, or leave a clear note.\n"
    )
    finish = "give your full answer as your final message" if answer_only else "finish with a short report"
    return (
        "## You are a Flyte sub-agent\n\n"
        "You were delegated a slice of a larger project and run in your own container, as a child "
        "action of that project's Flyte run.\n\n"
        f"- Your instruction is in `{_session_config._BRIEF_FILE}`. Carry it out and {finish}.\n"
        '- Ask with `ask-human "<question>"` if you are blocked on a decision; it opens a question '
        "in Union against your own action and waits.\n"
        + hand_back
        + "- You cannot spin off further jobs. Finish your slice and report back.\n"
    )


async def _run_codex_session(
    work: Path,
    base: Path,
    bridge_dir: Path,
    brief: str,
    *,
    sealed: Any,
    role: str,
    model: str,
    effort: str,
    answer_only: bool,
    verdict_followup: bool = False,
) -> str:
    """Run one Codex sub-agent session, with the brief written, the harness excluded and the bridge up.

    The same setup as the Claude path before Codex starts. Returning without it left Codex with no brief,
    or a parent's stale one, and no `ask-human` (found by Codex reviewing this very change).
    """
    _session_setup._exclude_harness(work)
    (work / _session_config._BRIEF_FILE).parent.mkdir(parents=True, exist_ok=True)
    (work / _session_config._BRIEF_FILE).write_text(brief.strip() + "\n")
    bridge = Bridge(ensure_dirs(bridge_dir))
    _session_setup._install_bridge_commands()
    bridge_task = asyncio.create_task(bridge.run(), name="subagent-bridge")
    try:
        return await _run_codex(
            work,
            base,
            brief,
            answer_only=answer_only,
            role=role,
            model=model,
            effort=effort,
            sealed=sealed,
            verdict_followup=verdict_followup,
            extra_env={
                **_session_setup._session_env(_session_setup._export_runtime_env()),
                "AGENT_FACTORY_WORKSPACE": str(work),
                "AGENT_FACTORY_BRIDGE_DIR": str(bridge_dir),
            },
        )
    finally:
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)


async def _report_claude_meter(events: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Log the usage meter a Claude run's events carry to the report; a failure to log is only noted."""
    observed = _meter.from_claude_events(events, _session_config._claude_account())
    if observed:
        if observed.limited_until:
            observed.limited_until = _usage._keep_later_reset(
                str(payload.get("result") or ""), observed.limited_until
            )
        try:
            await flyte.report.log.aio(_meter.report_comment(observed), do_flush=True)
        except (*_usage._METER_ERRORS, *REMOTE_ERRORS, RuntimeError) as exc:
            _timeline._log_line(f"[meter] claude: {type(exc).__name__}", err=True)


async def _claude_summary(
    raw: bytes,
    err: bytes,
    returncode: int | None,
    work: Path,
    argv: list[str],
    env: dict[str, str],
    *,
    brief: str,
    role: str,
    model: str,
    effort: str,
    max_turns: int,
    answer_only: bool,
    partial_base: str,
    jail: Any,
    verdict_followup: bool = False,
) -> str:
    """Return the text a finished `claude -p` sub-agent leaves, or raise the error its exit stands for."""
    out = raw.decode(errors="replace")
    stderr = err.decode(errors="replace")
    events, payload = _usage.parse_cli_output(out)
    await _report_claude_meter(events, payload)
    if partial_base and not answer_only and payload.get("subtype") == "error_max_turns":
        # Checked before the exit code: the CLI exits non-zero at the turn limit.
        summary = await _partial_handback(
            work,
            partial_base,
            max_turns,
            payload,
            functools.partial(
                _ask_remaining_work,
                argv[0],
                work,
                env,
                payload,
                model,
                effort,
                launcher=jail.argv_prefix if jail else (),
            ),
        )
        return summary + _usage._usage_trailer(out)
    if returncode != 0:
        # A usage-limit refusal exits 1 with the result JSON on stdout: name it as such.
        _raise_on_cli_error(out, only_limit=True)
        if not payload and 0 < (returncode or 0) < 128:  # it never started; a signal is a crash
            raise _unprivileged.no_result_error(returncode, out, stderr)
        # stdout too: `claude -p` puts its own errors there, not on stderr.
        raise RuntimeError(
            f"sub-agent exited {returncode}.\n"
            f"stdout: {out[-1500:] or '(empty)'}\n"
            f"stderr: {stderr[-1500:] or '(empty)'}"
        )

    _raise_on_cli_error(out)
    summary = _final_text(out) or _reviewer._NO_FINAL_MESSAGE
    trailer = _usage._usage_trailer(out)
    if verdict_followup and isinstance(payload, dict):
        followup = await _verdict_recovery.recover(
            summary,
            payload,
            functools.partial(
                _verdict_recovery.run_claude,
                argv[0],
                work,
                env,
                model,
                effort,
                launcher=jail.argv_prefix if jail else (),
            ),
        )
        summary, trailer = followup.text, _verdict_recovery.with_followup(trailer, followup)
    await flyte.report.log.aio(
        f"<h3>Sub-agent{f' · {html.escape(role)}' if role else ''}</h3>"
        f"<pre>{html.escape(brief[:1000])}</pre><hr><pre>{html.escape(summary[:4000])}</pre>",
        do_flush=True,
    )
    # Added after the report log so the log stays clean; `_call_reviewer` strips it.
    return summary + trailer


async def _subagent_workspace(workdir: Dir | None, repo: str, branch: str) -> tuple[Path, Path]:
    """Make this call's scratch directory; fill its `workspace/` from `workdir` or a clone of `repo`."""
    # Per-invocation paths. `reusable` shares the container *and* the Python
    # process between concurrent sub-agents, so a fixed path here would let two
    # of them overwrite each other's workspace and cross-wire their questions.
    base = Path(tempfile.mkdtemp(prefix="subagent-"))
    work = base / "workspace"
    work.mkdir(parents=True, exist_ok=True)

    if workdir is not None:
        await _checkpoint._download_workspace(workdir, work)
    elif repo:
        await _session_setup._clone(repo, branch, work)
    return base, work
