"""The deterministic seal audit: is a sealed run's record clean, or void? (design layer 6).

`python -m flyte_agent_factory._seal_audit <record-root> [--gold gold.json] [--json]` reads the record layout
of `docs/sealed-runs.md` (layer 5) and exits 0 for clean, 1 for void, 2 for a usage error and 3 for clean
without `--gold` (`reached` not checked). No model, no network, nothing but the
standard library: the same record gives the same verdict.

The record's claude transcripts and codex rollouts become one timeline of events, merged across roles by
timestamp. The void reasons are `incomplete`, `unsealed`, `reached`, `route-succeeded` and `egress`.
Attempts that were denied or failed are listed in `attempts` and do not void a run.
Adding a remote (`git remote add up URL`) and installing a release file already on disk are not routes
here (`NON_ROUTE_KINDS`): neither retrieves anything, though the guard denies both.

Whether a route call worked is decided from the result's `is_error` (or non-zero exit, for codex) and its
text (`FAILURE_SIGNATURES`), because `is_error` alone is not reliable: `git fetch x | tail -1` fails with
exit status 0. The signatures are anchored to how the tools print a failure, so that a successful result
that merely contains "403" or "proxy" counts as a success. The list is what `ops/seal_audit_rates.py`
measures against the seeded corpus; change it there, not by hand-tuning cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from flyte_agent_factory._seal_proxy import (
    _project_of_filename,
)  # the proxy's own rule for whose file this is
from flyte_agent_factory._seal_shell import (
    GIT_REMOTE_ADD,
    TASK_FILE,
    Rules,
    find_routes,
    normalize_project,
    string_leaves,
)

INCOMPLETE = "incomplete"
UNSEALED = "unsealed"
REACHED = "reached"
ROUTE_SUCCEEDED = "route-succeeded"
EGRESS = "egress"
KINDS = (INCOMPLETE, UNSEALED, REACHED, ROUTE_SUCCEEDED, EGRESS)

#: 2 is argparse's usage error, so a clean verdict that did not check `reached` gets its own code.
EXIT_CLEAN_NO_GOLD = 3
MAX_EVIDENCE = 300
MIN_LINE_LENGTH = 25
MIN_SHA_PREFIX = 10
FULL_SHA = re.compile(r"[0-9a-f]{40}")
GOLD_FIELDS = ("fix_patch", "test_patch", "shas", "task_project", "forbidden_paths")
#: A proxy line that was allowed to reach one of these is the seal working; any other route is egress.
ALLOWED_PROXY_ROUTES = ("model", "index", "files", "push")
ENGINES = ("claude", "codex")
#: What every role must have; a claude role also needs `guard` (Codex has no hook: `_seal_role.seal_flags`).
SEAL_FLAGS = ("net", "tools_off", "fs_readonly")
CLAUDE_SEAL_FLAGS = (*SEAL_FLAGS, "guard")
#: Tools whose input is something a role wrote (a gold line in it was authored, not found).
AUTHOR_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "apply_patch")
AGENT_TOOLS = ("Agent", "Task")
#: The logs every role's directory holds, whatever it did (`_seal_record.register` creates them).
REQUIRED_LOGS = ("proxy.jsonl", "guard.jsonl")
SHELL_NAMES = ("bash", "sh", "zsh")

#: A route call whose result is not an error succeeded unless a line of its text matches one of these. They
#: are what the tools print when they fail, anchored to that shape (a line start, a status line, an
#: exception name), not bare words: a successful API body holding "403", a task about "proxy objects" or
#: a `TypeError: not str` in a clone's output is not a failed route. Case matters (`error:` is uv's, `ERROR:`
#: pip's). `ops/seal_audit_rates.py` measures them against the corpus; the real failures in
#: `docs/seal-e2e/*/record` (the guard's `Sealed run:` denials) are pinned by `test_seal_e2e_records.py`.
FAILURE_SIGNATURES = (
    # git: `fatal: unable to access ...`, `fatal: Could not read from remote repository.`, ...
    r"^fatal: (?:unable to (?:access|connect)|[Cc]ould not read from remote|.*does not appear to be a git"
    r"|repository .* not found|Authentication failed)",
    # curl and wget (`curl: (56) ...`, `wget: unable to resolve host address`)
    r"^curl: \(\d+\)",
    r"^wget: (?:unable to resolve|.*failed|.*connection refused)",
    # the network: resolver, routing, refused or timed-out connections, the proxy's refused CONNECT
    r"Could not resolve host|Temporary failure in name resolution|Name or service not known",
    r"Network is unreachable|Connection refused|Connection timed out|Operation timed out",
    r"\bECONNREFUSED\b|\bENETUNREACH\b|\bENOTFOUND\b|\bETIMEDOUT\b",
    r"CONNECT tunnel failed|^CONNECT \S+ .*\b[45]\d\d\b",
    # an HTTP status line, `gh: HTTP 403: Forbidden`, pip's `HTTP error 403`, a fetch tool's status error
    r"^HTTP(?:/\d(?:\.\d)?)? [45]\d\d\b",
    r"^gh: HTTP [45]\d\d\b",
    r"\bHTTP error [45]\d\d\b",
    r"\bRequest failed with status code [45]\d\d\b",
    # pip and uv: their own error prefixes
    r"^ERROR: ",
    r"^error: ",
    # a Python traceback that ended in a network error
    r"^(?:[\w.]+\.)?(?:URLError|HTTPError|ConnectionError|ConnectionRefusedError|ConnectTimeout"
    r"|ProxyError|MaxRetryError|NewConnectionError|gaierror)\b",
    r"^OSError: \[Errno (?:101|110|111)\]",
    # the guard's denial text (`PreToolUse:Bash hook error: Sealed run: ...`)
    r"Sealed run:",
    # a local path that was refused or is not there (the other-attempt route)
    r": (?:No such file or directory|Permission denied)\s*$",
    r"^File does not exist",
)
FAILURE_PATTERN = re.compile("|".join(f"(?:{sig})" for sig in FAILURE_SIGNATURES), re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    """One thing the audit found: what, in which role's file and line, and the text that shows it."""

    kind: str
    role_id: str
    file: str
    line: int
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        """Return the finding as a JSON-ready dict."""
        return asdict(self)


@dataclass(frozen=True)
class Event:
    """One step of a role's transcript, the same shape for claude and codex."""

    role_id: str
    file: str
    line: int
    ts: datetime
    kind: str  # tool_call | tool_result | assistant_text | user_text
    tool: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    is_error: bool = False
    tool_use_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Gold:
    """What the reference fix looks like; held by the operator and never given to a run."""

    fix_patch: str = ""
    test_patch: str = ""
    shas: tuple[str, ...] = ()
    task_project: str = ""
    forbidden_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Gold:
        """Return the gold of a parsed gold.json; raise `ValueError` for one that would check nothing.

        A misspelt key, or a gold with no distinctive line and no sha, would silently switch off
        `reached`, and the run would be called clean, so each is refused naming the field.
        """
        if not isinstance(data, dict):
            raise ValueError("gold.json must be a JSON object")
        unknown = sorted(set(data) - set(GOLD_FIELDS))
        if unknown:
            hint = " (SWE-bench's 'patch' is 'fix_patch' here)" if "patch" in unknown else ""
            raise ValueError(
                f"gold.json: unknown field(s) {', '.join(map(repr, unknown))}{hint}; "
                f"expected {', '.join(GOLD_FIELDS)}"
            )
        for name in ("fix_patch", "test_patch", "task_project"):
            if not isinstance(data.get(name, ""), str):
                raise ValueError(f"gold.json: {name} must be a string")
        for name in ("shas", "forbidden_paths"):
            values = data.get(name, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"gold.json: {name} must be a list of strings")
        bad = [s for s in data.get("shas", []) if not FULL_SHA.fullmatch(s)]
        if bad:
            raise ValueError(f"gold.json: shas must be 40 lowercase hex characters: {', '.join(bad)}")
        gold = cls(
            fix_patch=data.get("fix_patch", ""),
            test_patch=data.get("test_patch", ""),
            shas=tuple(data.get("shas", [])),
            task_project=data.get("task_project", ""),
            forbidden_paths=tuple(data.get("forbidden_paths", [])),
        )
        if not select_distinctive_lines(gold) and not gold.shas:
            raise ValueError(
                f"gold.json identifies nothing: no added line of >= {MIN_LINE_LENGTH} characters and no sha, "
                "so 'reached' could never fire"
            )
        return gold

    @classmethod
    def load(cls, path: Path) -> Gold:
        """Return the gold of a gold.json file."""
        return cls.from_dict(json.loads(path.read_text()))


@dataclass
class Verdict:
    """The audit's answer: `clean` or `void`, the reasons that void it and the attempts that did not."""

    verdict: str
    reasons: list[Finding]
    attempts: list[Finding]
    counts: dict[str, int]
    gold: bool

    def to_json(self) -> str:
        """Return the verdict as JSON text."""
        return json.dumps(
            {
                "verdict": self.verdict,
                "gold": self.gold,
                "reasons": [f.to_dict() for f in self.reasons],
                "attempts": [f.to_dict() for f in self.attempts],
                "counts": self.counts,
            },
            indent=2,
        )


def clip(text: str) -> str:
    """Return evidence text cut to `MAX_EVIDENCE` characters."""
    return text.strip()[:MAX_EVIDENCE]


def collapse_whitespace(text: str) -> str:
    """Return text with every run of whitespace as one space, trimmed."""
    return " ".join(text.split())


# --- parsing: claude transcripts and codex rollouts into events -------------------------------------


def parse_timestamp(value: Any, previous: datetime) -> datetime:
    """Return an event's time; an absent or unreadable one takes the previous event's, so order holds."""
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return previous
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return previous


def result_text(content: Any) -> str:
    """Return a tool result's text: a string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return ""


def parse_claude_object(obj: dict[str, Any], base: dict[str, Any]) -> list[Event]:
    """Return the events of one claude transcript line (a user or assistant message)."""
    message = obj.get("message")
    if obj.get("type") not in ("user", "assistant") or not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [
            Event(kind="assistant_text" if obj["type"] == "assistant" else "user_text", text=content, **base)
        ]
    events: list[Event] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_kind = "assistant_text" if obj["type"] == "assistant" else "user_text"
            events.append(Event(kind=text_kind, text=str(block.get("text", "")), **base))
        elif kind == "tool_use":
            tool_input = block.get("input")
            events.append(
                Event(
                    kind="tool_call",
                    tool=str(block.get("name", "")),
                    input=tool_input if isinstance(tool_input, dict) else {},
                    tool_use_id=str(block.get("id", "")),
                    **base,
                )
            )
        elif kind == "tool_result":
            meta = obj.get("toolUseResult")
            agent_id = str(meta.get("agentId", "")) if isinstance(meta, dict) else ""
            events.append(
                Event(
                    kind="tool_result",
                    text=result_text(block.get("content")),
                    is_error=bool(block.get("is_error")),
                    tool_use_id=str(block.get("tool_use_id", "")),
                    agent_id=agent_id,
                    **base,
                )
            )
    return events


def codex_command(arguments: dict[str, Any]) -> str:
    """Return the shell command of a codex shell call: `["bash","-lc","cmd"]` is `cmd`, `cmd` stays `cmd`."""
    command = arguments.get("command", arguments.get("cmd", ""))
    if isinstance(command, list):
        words = [str(w) for w in command]
        if len(words) == 3 and words[0].rsplit("/", 1)[-1] in SHELL_NAMES and words[1] in ("-c", "-lc"):
            return words[2]
        return shlex.join(words)
    return str(command)


#: Codex tool names that run a shell command (`exec_command` takes `cmd`, the others `command`).
CODEX_SHELL_TOOLS = ("shell", "shell_command", "exec_command", "container.exec", "local_shell")
#: What Codex puts before a command's output: `Exit code: 1`, or `Process exited with code 1` (exec_command).
CODEX_EXIT = re.compile(r"(?:^|\n)(?:Exit code: |Process exited with code )(-?\d+)")
CODEX_OUTPUT_MARK = "\nOutput:\n"


def codex_output(output: Any) -> tuple[str, bool]:
    """Return a codex tool output's text and whether it failed.

    Codex 0.163 writes a string: an `Exit code: N` header, a `Wall time` line, then `Output:` and the text
    (shell), the same with `Process exited with code N` (exec_command), or just text (apply_patch). Older
    rollouts hold JSON
    `{"output", "metadata": {"exit_code"}}`, and a list of content items is possible too.
    """
    if isinstance(output, list):
        output = "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in output)
    if isinstance(output, dict):
        output = json.dumps(output)
    text = str(output or "")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and "output" in decoded:
        metadata = decoded.get("metadata")
        exit_code = metadata.get("exit_code") if isinstance(metadata, dict) else None
        return str(decoded["output"]), exit_code not in (None, 0)
    exit_match = CODEX_EXIT.search(text)
    if exit_match and CODEX_OUTPUT_MARK in text:
        text_after = text.partition(CODEX_OUTPUT_MARK)[2]
        return text_after, int(exit_match.group(1)) != 0
    return text, False


#: Prefix of the tool_use_id of a web search read from an `event_msg`; see `drop_duplicate_web_searches`.
WEB_EVENT_PREFIX = "event_msg:"
#: Hosted web search statuses that mean the search did not run; the rest (`completed`, `in_progress`, none)
#: count as it having run, since the query has left for OpenAI's servers either way.
WEB_SEARCH_FAILED = ("failed", "error", "cancelled", "canceled")


def codex_web_search(payload: dict[str, Any], base: dict[str, Any], call_id: str) -> list[Event]:
    """Return the call and result events of a codex hosted web search (`web_search_call`, `web_search_end`).

    The search runs on OpenAI's servers, so there is no tool output to read: the result is made from the
    status. A failed status is an attempt, as a denied fetch is; any other status is a search that worked.
    The call is a `WebSearch`, so `find_routes` treats it as the web route, as it does claude's.
    """
    action = payload.get("action")
    detail = dict(action) if isinstance(action, dict) else {}
    detail.pop("type", None)
    for key in ("query", "url"):  # `web_search_end` carries the query at the top level
        if key in payload:
            detail.setdefault(key, payload[key])
    status = str(payload.get("status") or "")
    result = Event(
        kind="tool_result",
        text=status or "completed",
        is_error=status.lower() in WEB_SEARCH_FAILED,
        tool_use_id=call_id,
        **base,
    )
    return [Event(kind="tool_call", tool="WebSearch", input=detail, tool_use_id=call_id, **base), result]


def drop_duplicate_web_searches(events: list[Event]) -> list[Event]:
    """Return events without the `event_msg` web searches of a rollout that also has `response_item` ones.

    Codex writes a search twice, as a `response_item` and as `event_msg` begin/end lines, and the two share
    no id. The `response_item` is the model's own record, so it wins; the `event_msg` ones count only when
    it is missing, and then a begin and its end (one `call_id`) are one search.
    """
    from_item = any(e.tool == "WebSearch" and not e.tool_use_id.startswith(WEB_EVENT_PREFIX) for e in events)
    seen: set[str] = set()
    kept: list[Event] = []
    for event in events:
        if event.tool_use_id.startswith(WEB_EVENT_PREFIX):
            if from_item or (event.kind == "tool_call" and event.tool_use_id in seen):
                continue
            seen.add(event.tool_use_id)
        kept.append(event)
    return kept


def parse_codex_object(obj: dict[str, Any], base: dict[str, Any]) -> list[Event]:
    """Return the events of one codex rollout line.

    Only `response_item` lines carry the conversation (and `event_msg` the web searches, see
    `codex_web_search`); `event_msg` otherwise repeats them for the UI, and
    `session_meta`, `turn_context` and reasoning items hold no tool use, so they give no events.
    The shapes are those of codex-cli 0.163: a command is a `function_call` (`shell`, `exec_command`) or a
    `local_shell_call`, a file edit is a `custom_tool_call` named `apply_patch` whose input is the patch text.
    """
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    call_id = str(payload.get("call_id", ""))
    # The one `event_msg` that is not a copy: a role that only has these still searched the web. Taken
    # defensively (any `web_search*` type), as the begin/end shapes are not pinned down for 0.163.
    if obj.get("type") == "event_msg" and str(kind).startswith("web_search"):
        return codex_web_search(payload, base, WEB_EVENT_PREFIX + (call_id or f"line-{base['line']}"))
    if obj.get("type") != "response_item":
        return []
    if kind == "web_search_call":
        return codex_web_search(payload, base, f"web_search:{base['line']}")
    if kind == "function_call":
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {"arguments": arguments}
        name = str(payload.get("name", ""))
        if name in CODEX_SHELL_TOOLS:
            return [
                Event(
                    kind="tool_call",
                    tool="Bash",
                    input={"command": codex_command(arguments)},
                    tool_use_id=call_id,
                    **base,
                )
            ]
        return [Event(kind="tool_call", tool=name, input=arguments, tool_use_id=call_id, **base)]
    if kind == "local_shell_call":
        action = payload.get("action")
        command = codex_command(action if isinstance(action, dict) else {})
        return [Event(kind="tool_call", tool="Bash", input={"command": command}, tool_use_id=call_id, **base)]
    if kind == "custom_tool_call":
        # apply_patch: the patch text is what the role authored, so it must be a string leaf of the input.
        return [
            Event(
                kind="tool_call",
                tool=str(payload.get("name", "")),
                input={"input": str(payload.get("input", ""))},
                tool_use_id=call_id,
                **base,
            )
        ]
    if kind in ("function_call_output", "custom_tool_call_output"):
        text, is_error = codex_output(payload.get("output"))
        return [Event(kind="tool_result", text=text, is_error=is_error, tool_use_id=call_id, **base)]
    if kind == "message":
        blocks = payload.get("content")
        text = "\n".join(str(b.get("text", "")) for b in blocks or [] if isinstance(b, dict))
        text_kind = "assistant_text" if payload.get("role") == "assistant" else "user_text"
        return [Event(kind=text_kind, text=text, **base)]
    return []


def parse_transcript(role_id: str, path: Path, root: Path) -> tuple[list[Event], list[Finding]]:
    """Return a transcript file's events, and an `incomplete` finding for each line that is not JSON."""
    rel = path.relative_to(root).as_posix()
    events: list[Event] = []
    problems: list[Finding] = []
    previous = datetime.min.replace(tzinfo=UTC)
    for number, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            problems.append(Finding(INCOMPLETE, role_id, rel, number, "line is not JSON: " + clip(raw)))
            continue
        if not isinstance(obj, dict):
            problems.append(
                Finding(INCOMPLETE, role_id, rel, number, "line is not a JSON object: " + clip(raw))
            )
            continue
        previous = parse_timestamp(obj.get("timestamp"), previous)
        base = {"role_id": role_id, "file": rel, "line": number, "ts": previous}
        events += parse_codex_object(obj, base) if "payload" in obj else parse_claude_object(obj, base)
    return drop_duplicate_web_searches(events), problems


# --- the record: manifest, roles, end.json -----------------------------------------------------------


def read_json(path: Path) -> Any:
    """Return a JSON file's content; raise ValueError (naming the file) if it is not JSON."""
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not JSON: {exc}") from exc


def count_lines(data: bytes) -> int:
    """Return how many lines a file has (a last line without a newline counts)."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def check_end_files(role_id: str, role_dir: Path, root: Path) -> list[Finding]:
    """Return the `incomplete` findings of a role's end.json: a missing, changed or unlisted file."""
    end_rel = (role_dir / "end.json").relative_to(root).as_posix()
    try:
        end = read_json(role_dir / "end.json")
    except ValueError as exc:
        return [Finding(INCOMPLETE, role_id, end_rel, 1, str(exc))]
    listed = end.get("files") if isinstance(end, dict) else None
    if not isinstance(listed, dict):
        return [Finding(INCOMPLETE, role_id, end_rel, 1, "end.json has no files table")]
    problems: list[Finding] = []
    for relpath, expect in sorted(listed.items()):
        path = role_dir / relpath
        shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else relpath
        if not path.is_file():
            problems.append(Finding(INCOMPLETE, role_id, end_rel, 1, f"{relpath} is listed but missing"))
            continue
        data = path.read_bytes()
        got = {"sha256": hashlib.sha256(data).hexdigest(), "lines": count_lines(data)}
        if not isinstance(expect, dict) or got != {k: expect.get(k) for k in got}:
            problems.append(Finding(INCOMPLETE, role_id, shown, 1, f"{relpath} differs from end.json: {got}"))
    # A transcript that end.json does not list was added after the role ended.
    for path in sorted((role_dir / "transcript").rglob("*")) if (role_dir / "transcript").is_dir() else []:
        rel = path.relative_to(role_dir).as_posix()
        if path.is_file() and rel not in listed:
            problems.append(
                Finding(
                    INCOMPLETE, role_id, path.relative_to(root).as_posix(), 1, f"{rel} is not in end.json"
                )
            )
    return problems


def list_roles(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return the manifest's role ids in order, then any role dir the manifest does not name."""
    ids = [str(r.get("role_id", "")) for r in manifest.get("roles", []) if isinstance(r, dict)]
    roles_dir = root / "roles"
    extra = sorted(p.name for p in roles_dir.iterdir() if p.is_dir()) if roles_dir.is_dir() else []
    return ids + [name for name in extra if name not in ids]


def check_role(root: Path, manifest: dict[str, Any], role_id: str) -> list[Finding]:
    """Return the `incomplete` and `unsealed` findings of one role's directory and role.json/end.json."""
    listed = {str(r.get("role_id")) for r in manifest.get("roles", []) if isinstance(r, dict)}
    role_dir = root / "roles" / role_id
    rel = role_dir.relative_to(root).as_posix()
    if Path(role_id).name != role_id or not role_id:
        return [Finding(INCOMPLETE, role_id, "manifest.json", 1, f"role id {role_id!r} is not a name")]
    if not role_dir.is_dir():
        return [Finding(INCOMPLETE, role_id, rel, 1, "the manifest names this role but it has no directory")]
    problems: list[Finding] = []
    if role_id not in listed:
        problems.append(Finding(INCOMPLETE, role_id, rel, 1, "role directory is not in the manifest"))
    if not (role_dir / "end.json").is_file():
        problems.append(
            Finding(INCOMPLETE, role_id, rel + "/end.json", 1, "end.json is missing: the role did not finish")
        )
    else:
        problems += check_end_files(role_id, role_dir, root)
    # The harness creates both logs for every role before it starts (Codex too: an empty guard log), so a
    # missing one is a lost record, not a quiet role.
    for log in REQUIRED_LOGS:
        if not (role_dir / log).is_file():
            problems.append(
                Finding(INCOMPLETE, role_id, f"{rel}/{log}", 1, f"{log} is missing: every role has one")
            )
    problems += check_seal(root, manifest, role_id, role_dir)
    return problems


def check_seal(root: Path, manifest: dict[str, Any], role_id: str, role_dir: Path) -> list[Finding]:
    """Return the findings of a role's seal flags: `unsealed` if any is off or the spec hash differs."""
    rel = (role_dir / "role.json").relative_to(root).as_posix()
    if not (role_dir / "role.json").is_file():
        return [Finding(INCOMPLETE, role_id, rel, 1, "role.json is missing")]
    try:
        role = read_json(role_dir / "role.json")
    except ValueError as exc:
        return [Finding(INCOMPLETE, role_id, rel, 1, str(exc))]
    seal = role.get("seal") if isinstance(role, dict) else None
    engine = role.get("engine") if isinstance(role, dict) else None
    # An engine we cannot parse is not "clean by default": its tool calls would never be checked.
    if engine not in ENGINES:
        return [Finding(INCOMPLETE, role_id, rel, 1, f"unknown engine {engine!r}; expected one of {ENGINES}")]
    if not isinstance(seal, dict):
        return [Finding(UNSEALED, role_id, rel, 1, "role.json has no seal flags")]
    required = CLAUDE_SEAL_FLAGS if engine == "claude" else SEAL_FLAGS
    off = [flag for flag in required if seal.get(flag) is not True]
    if off:
        return [Finding(UNSEALED, role_id, rel, 1, f"seal flags not true: {', '.join(off)}")]
    if seal.get("spec_sha256") != manifest.get("spec_sha256"):
        return [
            Finding(
                UNSEALED,
                role_id,
                rel,
                1,
                f"spec hash {seal.get('spec_sha256')!r} differs from the manifest's",
            )
        ]
    return []


def check_agent_links(role_id: str, role_dir: Path, events: list[Event]) -> list[Finding]:
    """Return an `incomplete` finding for each Agent/Task call with no sub-agent transcript beside it."""
    transcript_dir = role_dir / "transcript"
    # A transcript that is gone (or empty) links nothing, whatever a .meta.json says about it.
    has_content = {p for p in transcript_dir.rglob("agent-*.jsonl") if p.stat().st_size > 0}
    linked_calls: set[str] = set()
    for meta in transcript_dir.rglob("*.meta.json"):
        if meta.with_name(meta.name.removesuffix(".meta.json") + ".jsonl") not in has_content:
            continue
        try:
            linked_calls.add(str(json.loads(meta.read_text()).get("toolUseId")))
        except (json.JSONDecodeError, AttributeError):
            continue
    agent_files = {p.stem.removeprefix("agent-") for p in has_content}
    results = {e.tool_use_id: e for e in events if e.kind == "tool_result"}
    problems: list[Finding] = []
    for event in events:
        if event.kind != "tool_call" or event.tool not in AGENT_TOOLS or event.tool_use_id in linked_calls:
            continue
        result = results.get(event.tool_use_id)
        agent_ids = set(re.findall(r"agentId:\s*([\w-]+)", result.text)) if result else set()
        if result and result.agent_id:
            agent_ids.add(result.agent_id)
        if not agent_ids & agent_files:
            evidence = f"{event.tool} call {event.tool_use_id} has no sub-agent transcript"
            problems.append(Finding(INCOMPLETE, role_id, event.file, event.line, evidence))
    return problems


def check_unanswered_calls(role_id: str, events: list[Event], stopped: bool = False) -> list[Finding]:
    """Return an `incomplete` finding for each tool call of one transcript file that has no result.

    A transcript cut before `end.json` was written ends in calls nobody answered; so does a role that was
    killed mid-call (claude's last assistant message), and that role did not finish either. Results are
    paired within the file, as ids are only unique there: a sub-agent's calls are answered in its own file.
    Hosted web searches get their result made from the status (`codex_web_search`), so they always pair.

    `stopped` is for the author: the harness ends its long-lived session (at the project's end, or a pod
    restart that resumes it) whatever it is doing, and records it at DONE while its DONE call is running.
    The calls of the file's final step (nothing but calls after them) are then left alone; an unanswered
    call anywhere earlier is still a cut. A cut exactly after the author's last call is not caught.
    """
    answered = {e.tool_use_id for e in events if e.kind == "tool_result"}
    if stopped:
        tail = len(events)
        while tail and events[tail - 1].kind == "tool_call":
            tail -= 1
        events = events[:tail]
    return [
        Finding(
            INCOMPLETE,
            role_id,
            e.file,
            e.line,
            f"{e.tool} call {e.tool_use_id or '(no id)'} has no result: " + clip(json.dumps(e.input)),
        )
        for e in events
        if e.kind == "tool_call" and e.tool_use_id not in answered
    ]


def load_timeline(
    root: Path, role_ids: list[str], stopped: frozenset[str] = frozenset()
) -> tuple[list[Event], list[Finding]]:
    """Return every role's events merged by timestamp (ties keep file order), and the incompleteness found."""
    events: list[Event] = []
    problems: list[Finding] = []
    for role_id in role_ids:
        role_dir = root / "roles" / role_id
        if not role_dir.is_dir() or Path(role_id).name != role_id:
            continue
        files = (
            sorted((role_dir / "transcript").rglob("*.jsonl")) if (role_dir / "transcript").is_dir() else []
        )
        if not files:
            rel = role_dir.relative_to(root).as_posix()
            problems.append(Finding(INCOMPLETE, role_id, rel, 1, "role has no transcript"))
        role_events: list[Event] = []
        for path in files:
            file_events, file_problems = parse_transcript(role_id, path, root)
            problems += file_problems
            problems += check_unanswered_calls(role_id, file_events, role_id in stopped)
            if not any(e.kind == "assistant_text" or e.kind == "tool_call" for e in file_events):
                rel = path.relative_to(root).as_posix()
                problems.append(Finding(INCOMPLETE, role_id, rel, 1, "transcript has no assistant events"))
            role_events += file_events
        problems += check_agent_links(role_id, role_dir, role_events)
        events += role_events
    # sorted() is stable: events at one timestamp stay in role, then file, then line order.
    return sorted(events, key=lambda e: e.ts), problems


# --- the checks over the timeline --------------------------------------------------------------------


def read_jsonl_lines(
    root: Path, role_id: str, name: str
) -> tuple[list[tuple[int, dict[str, Any]]], list[Finding]]:
    """Return the (line number, object) pairs of a role's proxy.jsonl or guard.jsonl, and its bad lines."""
    path = root / "roles" / role_id / name
    if not path.is_file():
        return [], []
    rel = path.relative_to(root).as_posix()
    rows: list[tuple[int, dict[str, Any]]] = []
    problems: list[Finding] = []
    for number, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            rows.append((number, obj))
        else:
            problems.append(Finding(INCOMPLETE, role_id, rel, number, "line is not JSON: " + clip(raw)))
    return rows, problems


def is_task_project_file(target: str, task_project: str) -> bool:
    """Return whether a proxy line's target (`/files/<name>?..` or a URL) names a file of the task project."""
    if not task_project:
        return False
    name = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
    return _project_of_filename(name) == normalize_project(task_project)


def check_proxy_and_guard(
    root: Path, role_ids: list[str], task_project: str = ""
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Return (problems, egress, attempts) from the proxy and guard logs: allows off-route, denials."""
    problems: list[Finding] = []
    egress: list[Finding] = []
    attempts: list[Finding] = []
    for role_id in role_ids:
        for name, kind in (("proxy.jsonl", "proxy-denied"), ("guard.jsonl", "guard-denied")):
            rows, bad = read_jsonl_lines(root, role_id, name)
            problems += bad
            rel = f"roles/{role_id}/{name}"
            for number, obj in rows:
                decision = obj.get("decision")
                evidence = clip(json.dumps(obj, sort_keys=True))
                route = obj.get("route")
                # The proxy refuses the task project's files; an allow for one means it did not.
                if (
                    decision == "allow"
                    and name == "proxy.jsonl"
                    and (
                        route not in ALLOWED_PROXY_ROUTES
                        or (
                            route == "files"
                            and is_task_project_file(str(obj.get("target", "")), task_project)
                        )
                    )
                ):
                    egress.append(Finding(EGRESS, role_id, rel, number, evidence))
                elif decision == "deny":
                    attempts.append(Finding(kind, role_id, rel, number, evidence))
    return problems, egress, attempts


def route_worked(result: Event | None) -> bool:
    """Return whether a route call's result looks like success: no error flag, no failure signature."""
    if result is None or result.is_error:
        return False
    return FAILURE_PATTERN.search(result.text) is None


#: What `find_routes` finds and the guard denies, but which retrieves nothing: not an attempt, not a route.
NON_ROUTE_KINDS = frozenset({GIT_REMOTE_ADD, TASK_FILE})


def check_routes(events: list[Event], rules: Rules) -> tuple[list[Finding], list[Finding]]:
    """Return (route-succeeded, attempts): the upstream-route calls that worked, and those that did not."""
    results = {(e.role_id, e.tool_use_id): e for e in events if e.kind == "tool_result" and e.tool_use_id}
    succeeded: list[Finding] = []
    attempts: list[Finding] = []
    for event in events:
        if event.kind != "tool_call":
            continue
        for route in find_routes(event.tool, event.input, rules):
            if route.kind in NON_ROUTE_KINDS:
                continue
            worked = route_worked(results.get((event.role_id, event.tool_use_id)))
            kind = ROUTE_SUCCEEDED if worked else "route-failed"
            finding = Finding(kind, event.role_id, event.file, event.line, clip(route.evidence))
            (succeeded if worked else attempts).append(finding)
    return succeeded, attempts


def select_distinctive_lines(gold: Gold) -> list[str]:
    """Return the gold lines that identify the fix: added, >= 25 characters, not also removed or context."""
    added: list[str] = []
    others: set[str] = set()
    for patch in (gold.fix_patch, gold.test_patch):
        for raw in patch.splitlines():
            if raw.startswith(("+++", "---", "diff ", "index ", "@@")):
                continue
            text = collapse_whitespace(raw[1:])
            if raw.startswith("+"):
                added.append(text)
            elif raw.startswith(("-", " ")):
                others.add(text)
    return sorted({t for t in added if len(t) >= MIN_LINE_LENGTH and t not in others})


def find_gold_sha(text: str, shas: tuple[str, ...]) -> str | None:
    """Return the gold sha that `text` shows in full or as a >= 10-character prefix word, if any."""
    for word in re.findall(r"\b[0-9a-f]{%d,40}\b" % MIN_SHA_PREFIX, text.lower()):
        for sha in shas:
            if len(sha) >= MIN_SHA_PREFIX and (sha.startswith(word) or word.startswith(sha)):
                return sha
    return None


def find_authoring_times(events: list[Event], distinctive: list[str]) -> dict[str, datetime]:
    """Return when each distinctive line was first authored: in a write/edit/shell input or assistant text."""
    first: dict[str, datetime] = {}
    for event in events:
        if event.kind == "assistant_text":
            text = event.text
        elif event.kind == "tool_call" and event.tool in AUTHOR_TOOLS:
            text = "\n".join(string_leaves(event.input))
        else:
            continue
        collapsed = collapse_whitespace(text)
        for line in distinctive:
            if line not in first and line in collapsed:
                first[line] = event.ts
    return first


def was_authored(line: str, ts: datetime, authored: dict[str, datetime]) -> bool:
    """Return whether a role wrote the line at or before `ts`."""
    return line in authored and authored[line] <= ts


def check_reached(events: list[Event], gold: Gold) -> list[Finding]:
    """Return a `reached` finding for each tool result that shows the gold before any role authored it."""
    distinctive = select_distinctive_lines(gold)
    authored = find_authoring_times(events, distinctive)
    found: list[Finding] = []
    for event in events:
        if event.kind != "tool_result":
            continue
        hit = ""
        for raw in event.text.splitlines():
            collapsed = collapse_whitespace(raw)
            if any(d in collapsed and not was_authored(d, event.ts, authored) for d in distinctive):
                hit = raw
                break
        if not hit and (sha := find_gold_sha(event.text, gold.shas)):
            hit = f"gold sha {sha[:12]} in: {event.text[:100]}"
        if hit:
            found.append(Finding(REACHED, event.role_id, event.file, event.line, clip(hit)))
    return found


def audit(
    root: Path,
    gold: Gold | None = None,
    *,
    task_project: str = "",
    forbidden: tuple[str, ...] = (),
) -> Verdict:
    """Audit one record root: return `clean` or `void` with the reasons and the attempts that did not void it.

    `task_project` and `forbidden` add to the gold's (the CLI's `--task-project` and `--forbidden`).
    Without `gold`, `reached` cannot be checked and the verdict says so (`gold` False).
    """
    root = Path(root)
    problems: list[Finding] = []
    try:
        manifest = read_json(root / "manifest.json")
    except (ValueError, OSError) as exc:
        problems.append(Finding(INCOMPLETE, "", "manifest.json", 1, f"manifest unreadable: {exc}"))
        manifest = {}
    if not isinstance(manifest, dict):
        problems.append(Finding(INCOMPLETE, "", "manifest.json", 1, "manifest is not an object"))
        manifest = {}
    role_ids = list_roles(root, manifest)
    # Not "no author/plain role": the real reviewer-only record in docs/seal-e2e is a clean one.
    if not any(isinstance(r, dict) for r in manifest.get("roles") or []):
        problems.append(Finding(INCOMPLETE, "", "manifest.json", 1, "manifest lists no roles"))
    for role_id in role_ids:
        problems += check_role(root, manifest, role_id)
    # The author's session is stopped by the harness, not finished by itself (`check_unanswered_calls`).
    stopped = frozenset(
        str(r.get("role_id"))
        for r in manifest.get("roles") or []
        if isinstance(r, dict) and r.get("kind") == "author"
    )
    events, timeline_problems = load_timeline(root, role_ids, stopped)
    problems += timeline_problems
    project = task_project or (gold.task_project if gold else "")
    log_problems, egress, attempts = check_proxy_and_guard(root, role_ids, project)
    problems += log_problems
    rules = Rules(
        task_project=project,
        forbidden=tuple(dict.fromkeys([*forbidden, *(gold.forbidden_paths if gold else ())])),
    )
    succeeded, route_attempts = check_routes(events, rules)
    reached = check_reached(events, gold) if gold else []
    # Reasons in the design's order; `unsealed` findings were found with the role checks.
    reasons = (
        [f for f in problems if f.kind == INCOMPLETE]
        + [f for f in problems if f.kind == UNSEALED]
        + reached
        + succeeded
        + egress
    )
    counts = {
        "roles": len(role_ids),
        "events": len(events),
        "tool_calls": sum(e.kind == "tool_call" for e in events),
        "tool_results": sum(e.kind == "tool_result" for e in events),
        "attempts": len(attempts) + len(route_attempts),
    }
    counts.update({kind: Counter(f.kind for f in reasons)[kind] for kind in KINDS})
    return Verdict(
        "void" if reasons else "clean", reasons, attempts + route_attempts, counts, gold is not None
    )


def main(argv: list[str] | None = None) -> int:
    """Audit the record root named on the command line.

    Exit 0 clean (with gold), 1 void, 2 usage error, `EXIT_CLEAN_NO_GOLD` clean without gold: the one
    check that needs the gold, `reached`, was not made, so a script must not read it as a run that counts.
    """
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._seal_audit", description=__doc__)
    parser.add_argument("root", type=Path, help="the record root (holds manifest.json and roles/)")
    parser.add_argument(
        "--gold", type=Path, help="gold.json: fix_patch, test_patch, shas, task_project, forbidden_paths"
    )
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    parser.add_argument("--task-project", default="", help="the task's PyPI project (default: the gold's)")
    parser.add_argument("--forbidden", action="append", default=[], help="a forbidden path glob (repeatable)")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error(f"record root {args.root} is not a directory")
    gold = None
    if args.gold:
        try:
            gold = Gold.load(args.gold)
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            parser.error(f"cannot read gold {args.gold}: {exc}")
    else:
        print(
            "warning: no --gold: 'reached' (gold lines and shas in results) is not checked", file=sys.stderr
        )
    verdict = audit(args.root, gold, task_project=args.task_project, forbidden=tuple(args.forbidden))
    if args.json:
        print(verdict.to_json())
    else:
        print(f"verdict: {verdict.verdict}")
        for finding in verdict.reasons:
            print(
                f"  {finding.kind}: {finding.role_id} {finding.file}:{finding.line} {finding.evidence[:120]}"
            )
        print(f"  attempts (not void): {len(verdict.attempts)}")
    if verdict.verdict == "void":
        return 1
    return 0 if verdict.gold else EXIT_CLEAN_NO_GOLD


if __name__ == "__main__":
    sys.exit(main())
