"""Guard hook for sealed runs: layer 4 of docs/sealed-runs.md.

A sealed role has no network except a proxy (layer 3). This hook is the backstop that makes an attempt to
reach the reference fix explicit: a PreToolUse hook (`python -m flyte_agent_factory._seal_guard --config F`)
that denies web tools, the browser MCP, git fetches from a remote (and adding one), `gh`, HTTP clients and
one-liners naming a non-local host, downloads of the task project, and reads of forbidden paths. Every
decision, allow and deny, is appended to the role's `guard.jsonl`.

What a shell command does is read by `_seal_shell`, the same reading the audit (`_seal_audit`) applies to a
recorded call; this module decides: which source is local (under the local roots, from the working
directory a `cd` left), which paths are forbidden, the messages, the hook's input and log.

It is a heuristic over shell text, not a sandbox: the network namespace is what stops a determined command,
and this hook is what records the attempt. So it errs towards denying, and it fails CLOSED: an unreadable
config, malformed input or an unwritable log denies the call.

The hook takes the seal values as a JSON config (`write_config`), not a `SealSpec`, so the module stays
independent of the spec. It is run by `python -m`, so it must stay a statically imported `.py`.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from flyte_agent_factory import _seal_shell

DENIED_TOOLS = ["WebSearch", "WebFetch", "mcp__browser"]


def cli_deny_args() -> list[str]:
    """Return the `claude` flags that take the web tools away for the life of the process.

    The settings' deny list lives in the role's config dir, which the role can write; a command-line flag it
    cannot change. Put last on the command line: the option takes every following word up to the next flag.
    """
    return ["--disallowedTools", *DENIED_TOOLS]


PREFIX = "Sealed run: "
#: Claude Code's documented spelling of "every tool" for a hook matcher.
MATCH_ALL = "*"

_PATH_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "NotebookEdit", "Write", "Edit", "MultiEdit"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_PATH_KEYS = ("file_path", "path", "pattern", "notebook_path")
#: The input key that holds a file glob, per search tool (Grep's `pattern` is a regex, not a path).
_GLOB_KEYS = {"Glob": "pattern", "Grep": "glob"}
#: Commands that walk the whole tree under a directory they are given, with the short options that make
#: them do so (an empty set: always). Naming an ANCESTOR of a forbidden path to one of these reads the
#: forbidden path too, so it is denied. Naming an ancestor to anything else (`ls /tmp`, `cat /tmp/x`) lists
#: or reads only that level, so it stays allowed: `/`, `/tmp` and `/home` are used by ordinary commands.
_RECURSIVE_COMMANDS: dict[str, set[str]] = {
    "find": set(), "fd": set(), "rg": set(), "ag": set(), "ack": set(), "tree": set(), "du": set(),
    "tar": set(), "bsdtar": set(),
    "grep": {"r", "R"}, "egrep": {"r", "R"}, "fgrep": {"r", "R"}, "ls": {"R"}, "cp": {"r", "R", "a"},
    "rsync": {"r", "a"}, "zip": {"r"}, "scp": {"r"},
}  # fmt: skip
_RECURSIVE_LONG_OPTS = {"--recursive", "--dereference-recursive", "--archive", "--directories=recurse"}


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str
    reason: str = ""


ALLOW = Decision(True, "allow")


def _deny(rule: str, detail: str) -> Decision:
    return Decision(False, rule, f"{PREFIX}{detail}")


@dataclasses.dataclass
class _Context:
    task_project: str
    forbidden: list[str]
    local_roots: list[str]
    cwd: str


# --- settings and config -------------------------------------------------------------------- #


def hook_command(config_path: Path) -> str:
    # `sys.executable`, so the module imports in the pod whatever interpreter the supervisor runs on.
    python = shlex.quote(sys.executable)
    return f"{python} -m flyte_agent_factory._seal_guard --config {shlex.quote(str(config_path))}"


def settings_extra(config_path: Path) -> dict[str, Any]:
    """Return the `extra` for `_auth.write_settings` that denies the web tools and installs the hook.

    `mcp__browser` (no tool suffix) is Claude Code's rule for every tool of the `browser` MCP server.
    """
    return {
        "permissions": {"deny": list(DENIED_TOOLS)},
        "hooks": {
            "PreToolUse": [
                {"matcher": MATCH_ALL, "hooks": [{"type": "command", "command": hook_command(config_path)}]}
            ]
        },
    }


def merge_settings(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> dict[str, Any]:
    """Combine two `extra` dicts: dicts merge, lists concatenate (without repeats), other values: `b` wins.

    `None` stands for "no settings" (`_context_guard.settings_extra` returns it when the guard is off).
    """
    merged: dict[str, Any] = dict(a or {})
    for key, value in (b or {}).items():
        old = merged.get(key)
        if isinstance(old, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_settings(old, value)
        elif isinstance(old, list) and isinstance(value, list):
            merged[key] = old + [item for item in value if item not in old]
        else:
            merged[key] = value
    return merged


def write_config(
    path: Path,
    *,
    task_project: str,
    forbidden_paths: Iterable[str],
    local_roots: Iterable[str],
    guard_log: Path,
    role_id: str,
) -> Path:
    """Write the JSON the hook reads: what to deny and where to log."""
    config = {
        "task_project": task_project,
        "forbidden_paths": [str(p) for p in forbidden_paths],
        "local_roots": [str(p) for p in local_roots],
        "guard_log": str(guard_log),
        "role_id": role_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    return path


# --- paths ---------------------------------------------------------------------------------- #


def _resolve(path: str, cwd: str) -> str:
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))


def _glob_base(glob: str) -> str:
    """Return the directory a forbidden glob names: the glob up to its first wildcard component."""
    parts = glob.split("/")
    keep = []
    for part in parts:
        if any(c in part for c in "*?[{"):
            break
        keep.append(part)
    return "/".join(keep).rstrip("/") or "/"


def _is_under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _forbidden(path: str, ctx: _Context, *, ancestors: bool = False) -> bool:
    """Whether `path` is under a forbidden glob (or, with `ancestors`, contains one: a recursive search)."""
    resolved = _resolve(path, ctx.cwd)
    for glob in ctx.forbidden:
        pattern = _resolve(glob, ctx.cwd)
        base = _glob_base(pattern)
        if fnmatch.fnmatchcase(resolved, pattern) or _is_under(resolved, base):
            return True
        if ancestors and _is_under(base, _glob_base(resolved)):
            return True
    return False


def _in_local_roots(path: str, ctx: _Context) -> bool:
    real = os.path.realpath(_resolve(path, ctx.cwd))
    return any(_is_under(real, os.path.realpath(root)) for root in ctx.local_roots)


def _is_local_source(source: str, ctx: _Context) -> bool:
    """Whether a git fetch/clone source is a path or `file://` URL under the local roots.

    A bare word is a named remote, which a sealed repo does not have, so it is not local.
    """
    if "://" in source:
        parsed = urlparse(source)
        return parsed.scheme == "file" and _in_local_roots(unquote(parsed.path), ctx)
    if re.match(r"^[\w.-]+@[\w.-]+:", source):
        return False
    explicit = source.startswith(("/", "./", "../", "~")) or source in {".", ".."}
    if explicit or os.path.exists(_resolve(source, ctx.cwd)):
        return _in_local_roots(source, ctx)
    return False


def _is_recursive(name: str, args: Sequence[str]) -> bool:
    short = _RECURSIVE_COMMANDS.get(name)
    if short is None:
        return False
    if not short:
        return True
    for arg in args:
        if arg == "--":
            break
        if arg in _RECURSIVE_LONG_OPTS or (name != "ls" and arg.startswith("--directories=")):
            return True
        if arg[:1] == "-" and arg[1:2] != "-" and short & set(arg[1:]):
            return True
    return False


def _check_paths(args: Sequence[str], ctx: _Context, name: str = "") -> Decision | None:
    recursive = _is_recursive(name, args)
    for arg in args:
        candidates = [arg, arg.split("=", 1)[1]] if "=" in arg else [arg]
        for candidate in candidates:
            if candidate and _forbidden(candidate, ctx, ancestors=recursive):
                return _deny("forbidden-path", f"{candidate} is not readable in a sealed run.")
    # `find`, `grep -r`, `rg`... with no path walk the working directory.
    if (
        recursive
        and not any("/" in a or a in {".", ".."} for a in args)
        and _forbidden(ctx.cwd, ctx, ancestors=True)
    ):
        return _deny("forbidden-path", f"{ctx.cwd} holds a path that is not readable in a sealed run.")
    return None


# --- commands ------------------------------------------------------------------------------- #

#: The deny message per `_seal_shell.Route.rule`: `{name}` is the command, `{evidence}` the route's evidence.
_ROUTE_MESSAGES = {
    "git-fetch": "`{evidence}` reads from a remote; only local paths are allowed.",
    "git-remote-add": "`{evidence}` names a remote that is not a local path; a sealed repo has none.",
    "gh": "`gh` talks to the code host; the sealed run has no network access.",
    "http-client": "`{name}` names {evidence}; the sealed run has no network access.",
    "script-url": "`{name}` code names {evidence}; the sealed run has no network access.",
    "pip-download": "`{evidence}` fetches packages from the network.",
    "pip-task-project": "`{name}` names the task project ({evidence}); it cannot be installed.",
}


def _requirement_file_specs(path: str, ctx: _Context) -> list[str]:
    try:
        lines = Path(_resolve(path, ctx.cwd)).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", "-"))]


def _shell_rules(ctx: _Context) -> _seal_shell.Rules:
    """Return the classifier's rules for a command run in `ctx.cwd`: local means under the local roots."""
    return _seal_shell.Rules(
        task_project=ctx.task_project,
        is_local_source=lambda source, git_dir: _is_local_source(
            source, dataclasses.replace(ctx, cwd=_resolve(git_dir, ctx.cwd))
        ),
        requirement_lines=lambda path: _requirement_file_specs(path, ctx),
    )


def _check_command(command: str, ctx: _Context) -> Decision | None:
    # A `cd` holds for the rest of its own shell, not after the `bash -c` or `$(...)` it ran in returns.
    cwds = [ctx.cwd]
    for found in _seal_shell.walk_commands(command):
        del cwds[found.depth + 1 :]
        cwds += [cwds[-1]] * (found.depth + 1 - len(cwds))
        here = dataclasses.replace(ctx, cwd=cwds[-1])
        name, args = Path(found.words[0]).name, found.words[1:]
        git_dir = _seal_shell.split_git(args)[0] if name == "git" else ""
        if denied := _check_paths(args, dataclasses.replace(here, cwd=_resolve(git_dir, here.cwd)), name):
            return denied
        if name == "cd" and args:
            cwds[-1] = _resolve(args[0], here.cwd)
        for route in _seal_shell.classify_segment(found, _shell_rules(here)):
            return _deny(route.rule, _ROUTE_MESSAGES[route.rule].format(name=name, evidence=route.evidence))
    return None


# --- the decision --------------------------------------------------------------------------- #


def _check_path_tool(tool_name: str, tool_input: Mapping[str, Any], ctx: _Context) -> Decision | None:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str) or not value:
            continue
        # A glob or search starting above a forbidden directory would read it too.
        if _forbidden(value, ctx, ancestors=tool_name in _SEARCH_TOOLS and key == "path"):
            return _deny("forbidden-path", f"{value} is not readable in a sealed run.")
    glob = tool_input.get(_GLOB_KEYS.get(tool_name, ""))
    # An absolute glob names its own starting directory: judge the fixed part before the first wildcard.
    if isinstance(glob, str) and glob.startswith(("/", "~")):
        if _forbidden(_glob_base(os.path.expanduser(glob)), ctx, ancestors=True):
            return _deny("forbidden-path", f"{glob} is not readable in a sealed run.")
    return None


def decide(tool_name: str, tool_input: object, config: Mapping[str, Any], *, cwd: str = "") -> Decision:
    """Decide one tool call. Pure: no I/O beyond looking at paths and requirement files it is given.

    `cwd` is the session's working directory from the hook payload; a `cwd` in the tool input wins.
    """
    if not isinstance(tool_input, Mapping):
        return _deny("malformed", "the tool input is not an object.")
    if tool_name in {"WebSearch", "WebFetch"}:
        return _deny("web-tool", f"{tool_name} is off; the sealed run has no network access.")
    if tool_name.startswith("mcp__browser"):
        return _deny("browser-mcp", "the browser is off in a sealed run.")
    ctx = _Context(
        task_project=_seal_shell.normalize_project(str(config.get("task_project") or "")),
        forbidden=[str(g) for g in config.get("forbidden_paths") or []],
        local_roots=[str(r) for r in config.get("local_roots") or []],
        cwd=str(tool_input.get("cwd") or cwd or os.getcwd()),
    )
    denied = None
    if tool_name in _PATH_TOOLS:
        denied = _check_path_tool(tool_name, tool_input, ctx)
    elif tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return _deny("malformed", "the Bash command is not a string.")
        denied = _check_command(command, ctx)
    return denied or ALLOW


# --- the hook ------------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(config: Mapping[str, Any], tool: Any, tool_input: Any, decision: Decision) -> None:
    line = {
        "ts": _now(),
        "role_id": config.get("role_id", ""),
        "tool": tool,
        "input": tool_input,
        "decision": "allow" if decision.allow else "deny",
        "rule": decision.rule,
    }
    # One write of one line to an O_APPEND file: concurrent hook processes do not interleave.
    with open(config["guard_log"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _emit_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_seal_guard")
    parser.add_argument("--config", required=True, type=Path)
    config_path = parser.parse_args(argv).config
    config: dict[str, Any] = {}
    tool: Any = None
    tool_input: Any = None
    # Fail CLOSED: the guard is the record of attempted routes, so a call it could not judge or log is not
    # let through. These are what an unreadable config, bad stdin, a payload of the wrong shape or an
    # unwritable log raise; a broad `except Exception` is pinned by tests/test_narrow_excepts.py to the
    # boundary sites, and this is not one.
    try:
        config = json.loads(config_path.read_text())
        payload = json.load(sys.stdin)
        tool, tool_input = payload["tool_name"], payload.get("tool_input") or {}
        decision = decide(tool, tool_input, config, cwd=str(payload.get("cwd") or ""))
        _log(config, tool, tool_input, decision)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        decision = _deny("fail-closed", f"the guard could not judge this call ({type(error).__name__}).")
        try:
            _log(config, tool, tool_input, decision)
        except (OSError, KeyError, TypeError):
            pass  # nowhere to log: the deny below is all there is
    if not decision.allow:
        _emit_deny(decision.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
