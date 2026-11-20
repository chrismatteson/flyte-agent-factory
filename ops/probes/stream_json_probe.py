"""Live probe of `claude -p` stream-json with the slot's setup-token.

Runs a battery of short scenarios and prints a redacted summary of the events
each produced. Used for discovery (docs/headless-author-discovery.md); it is
not part of the factory runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MODEL = os.environ.get("PROBE_MODEL", "haiku")

#: One stream-json event as Claude Code printed it.
Event = dict[str, Any]


class Proc:
    """One `claude -p` stream-json process, its events collected by a reader thread."""

    def __init__(self, cwd: str, *extra: str) -> None:
        """Start `claude` in `cwd` with the stream-json flags, the probe model and `extra`."""
        cmd = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            MODEL,
            *extra,
        ]
        self.p = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.events: list[Event] = []
        self.raw: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        self.answered: set[str] = set()

    def _read(self) -> None:
        assert self.p.stdout
        for line in self.p.stdout:
            self.raw.append(line)
            try:
                self.events.append(json.loads(line))
            except ValueError:
                self.events.append({"type": "_unparsed", "line": line[:200]})

    def send(self, obj: Event) -> None:
        """Write one JSON line to the process."""
        assert self.p.stdin
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def user(self, text: str) -> None:
        """Send a user message."""
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def results(self) -> list[Event]:
        """Return the `result` events so far."""
        return [e for e in self.events if e.get("type") == "result"]

    def wait_results(self, n: int, timeout: float, policy: Callable[[Event], Event] | None = None) -> None:
        """Wait for `n` results, answering each control request with `policy` when one is given."""
        end = time.time() + timeout
        while time.time() < end and len(self.results()) < n:
            for e in list(self.events):
                if e.get("type") == "control_request" and e["request_id"] not in self.answered:
                    self.answered.add(e["request_id"])
                    if policy:
                        response = {
                            "subtype": "success",
                            "request_id": e["request_id"],
                            "response": policy(e),
                        }
                        self.send({"type": "control_response", "response": response})
            time.sleep(0.3)

    def close(self) -> str:
        """Close stdin, wait for the exit, and return the start of stderr."""
        assert self.p.stdin
        self.p.stdin.close()
        try:
            self.p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.p.kill()
        return (self.p.stderr.read() if self.p.stderr else "")[:400]


def brief(e: Event) -> str:
    """Return a one-line summary of an event."""
    t = e.get("type")
    if t == "assistant":
        parts = []
        for c in e["message"].get("content", []):
            if c.get("type") == "text":
                parts.append("text:" + c["text"][:120])
            elif c.get("type") == "tool_use":
                parts.append(f"tool_use:{c['name']}:{json.dumps(c.get('input'))[:120]}")
        head = f"assistant parent={e.get('parent_tool_use_id')} err={e.get('error')} "
        return head + " | ".join(parts)
    if t == "user":
        c = e["message"].get("content")
        if isinstance(c, list):
            c = [f"{x.get('type')}:{str(x.get('content'))[:100]}" for x in c if isinstance(x, dict)]
        return f"user parent={e.get('parent_tool_use_id')} {str(c)[:200]}"
    if t == "result":
        fields = (
            "subtype",
            "is_error",
            "num_turns",
            "total_cost_usd",
            "duration_ms",
            "terminal_reason",
            "permission_denials",
            "api_error_status",
            "queued_turn_count",
            "result_index",
            "session_id",
        )
        keep = {k: e.get(k) for k in fields}
        keep["usage"] = {k: v for k, v in (e.get("usage") or {}).items() if "tokens" in k}
        keep["result"] = str(e.get("result"))[:150]
        return "result " + json.dumps(keep)
    if t == "system" and e.get("subtype") == "init":
        return (
            f"system init session={e.get('session_id')} perm={e.get('permissionMode')} "
            f"apiKeySource={e.get('apiKeySource')} version={e.get('claude_code_version')}"
        )
    return json.dumps(e)[:600]


def show(name: str, p: Proc, stderr: str) -> None:
    """Print every event of a scenario except the token-level stream events."""
    print(f"\n===== {name} =====")
    for e in p.events:
        if e.get("type") == "stream_event":
            continue
        print(" ", brief(e))
    print("  stderr:", stderr.strip()[:300])
    sys.stdout.flush()


def main() -> None:
    """Run the scenarios named on the command line, or all of them."""
    every = ["deny", "queue", "interrupt", "resume", "hook", "subagent", "bypass_deny"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="*", choices=[*every, []], help="scenarios to run (default: all)")
    which = parser.parse_args().scenario or every
    work = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", work], check=False)

    if "deny" in which:
        # 1. permission callback over stdio, then a second turn in the same process.
        p = Proc(work, "--permission-mode", "default", "--permission-prompt-tool", "stdio")
        p.send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})
        p.user(
            "Run the bash command `echo hi > x.txt`, then reply with one word.\nThis message has two lines."
        )
        p.wait_results(1, 120, lambda e: {"behavior": "deny", "message": "denied by factory policy"})
        p.user("Reply with exactly: second turn ok")
        p.wait_results(2, 90)
        show("deny + second turn", p, p.close())
        print("  x.txt exists:", Path(work, "x.txt").exists())

    if "queue" in which:
        # 2. a message sent while a turn is running.
        p = Proc(work, "--permission-mode", "bypassPermissions")
        p.user("Run the bash command `sleep 25 && echo slept`, then reply 'first done'.")
        time.sleep(8)
        p.user("NEW DIRECTION arrived mid-turn: also reply with the word PINEAPPLE.")
        p.wait_results(2, 150)
        show("mid-turn message", p, p.close())

    if "interrupt" in which:
        p = Proc(work, "--permission-mode", "bypassPermissions")
        p.user("Run the bash command `sleep 60`, then reply 'never'.")
        time.sleep(10)
        p.send({"type": "control_request", "request_id": "int1", "request": {"subtype": "interrupt"}})
        p.wait_results(1, 60)
        p.user("Reply with exactly: after interrupt")
        p.wait_results(2, 90)
        show("interrupt", p, p.close())

    if "resume" in which:
        p = Proc(work, "--permission-mode", "bypassPermissions")
        p.user("Remember the codeword ZEBRA-42. Reply 'noted'.")
        p.wait_results(1, 90)
        sid = p.results()[0]["session_id"]
        p.close()
        q = Proc(work, "--permission-mode", "bypassPermissions", "--resume", sid)
        q.user("What was the codeword? Reply with it only.")
        q.wait_results(1, 90)
        show(f"resume {sid}", q, q.close())
        r = Proc(work, "--permission-mode", "bypassPermissions", "--continue")
        r.user("Repeat the codeword once more.")
        r.wait_results(1, 90)
        show("continue", r, r.close())

    if "hook" in which:
        settings = Path(work, "hook-settings.json")
        script = Path(work, "deny.sh")
        script.write_text("#!/bin/sh\ncat >/dev/null\necho 'blocked by hook' >&2\nexit 2\n")
        script.chmod(0o755)
        hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": str(script)}]}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [hook]}}))
        p = Proc(
            work,
            "--permission-mode",
            "bypassPermissions",
            "--settings",
            str(settings),
            "--include-hook-events",
        )
        p.user("Run the bash command `echo hook > y.txt`, then reply with one word.")
        p.wait_results(1, 90)
        show("PreToolUse hook in bypass", p, p.close())
        print("  y.txt exists:", Path(work, "y.txt").exists())

    if "bypass_deny" in which:
        settings = Path(work, "deny-settings.json")
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}))
        p = Proc(work, "--permission-mode", "bypassPermissions", "--settings", str(settings))
        p.user("Run the bash command `rm -f nothing.txt`, then reply with one word.")
        p.wait_results(1, 90)
        show("deny rule in bypass", p, p.close())

    if "subagent" in which:
        p = Proc(work, "--permission-mode", "bypassPermissions")
        p.user(
            "Use the Task tool with subagent_type general-purpose to have a subagent reply 'sub ok'. "
            "Then reply 'parent ok'."
        )
        p.wait_results(1, 180)
        show("subagent", p, p.close())


if __name__ == "__main__":
    main()
