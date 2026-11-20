"""Live check of the structured author session on a slot: the engine's own code against the real CLI.

Run inside a child action that has the slot's setup-token as `CLAUDE_CODE_OAUTH_TOKEN` and an image with
`claude` (`ops/probes/run_live_author_session.py` submits it). Unlike `stream_json_probe.py`, which speaks
the protocol by hand, this drives `StreamClaudeProcess` with the argv `_author._author_argv` builds, its
permission policy and its ledger, and prints one JSON object of checks, each with its evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Run from the checkout under `uv run`, which does not install this project (`package = false`), `src` has to
# be put on the path. Not in the child action: there the bundled engine is on PYTHONPATH, and `parents[2]` of
# the unpacked script is `/`, where nothing of ours should be found first.
if Path(__file__).resolve().parent.parent.name == "ops":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

if __name__ == "__main__":
    # Before the engine's imports: importing `_author` loads the profile, and `--help` must not need one.
    argparse.ArgumentParser(description=__doc__).parse_args()

from flyte_agent_factory import _author, _inbox, _session_config, _usage
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._stream_process import StreamClaudeProcess

CODEWORD = "MARMALADE-7"
LONG = "\n".join(f"Line {i}: filler text so the message is long and spans many lines." for i in range(1, 13))

checks: dict[str, dict[str, Any]] = {}


def check(name: str, ok: bool, evidence: Any) -> None:
    """Record one check and print it as it happens."""
    checks[name] = {"ok": bool(ok), "evidence": evidence}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(evidence)[:300]}", flush=True)


def said(proc: StreamClaudeProcess, since: int = 0) -> str:
    """Return the session's lines since tail index `since`, joined."""
    return "\n".join(list(proc.tail)[since:])


def replies(proc: StreamClaudeProcess, since: int = 0) -> str:
    """Return only the model's own text since `since`: no `[sent]` echo of our prompt, no `[tool]` input."""
    return "\n".join(line for line in list(proc.tail)[since:] if not line.startswith("["))


async def until(cond: Any, timeout: float) -> bool:
    """Poll `cond()` until true or `timeout` seconds pass."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.5)
    return bool(cond())


def settings_file(work: Path) -> Path:
    """Write settings with a deny rule and a PreToolUse hook, as the factory's policy does."""
    hook = work / "hook.sh"
    hook.write_text(
        "#!/bin/sh\ncat | grep -q FORBIDDEN-HOOK && { echo 'blocked by policy hook' >&2; exit 2; }\nexit 0\n"
    )
    hook.chmod(0o755)
    path = work / "settings.json"
    path.write_text(
        json.dumps(
            {
                "permissions": {"deny": ["Bash(rm:*)"]},
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": str(hook)}]}]
                },
            }
        )
    )
    return path


def new_proc(
    argv: list[str], work: Path, ledger: Path, notices: list[tuple[str, str]]
) -> StreamClaudeProcess:
    """Build the process as `_author._start_session` does."""
    return StreamClaudeProcess(
        argv,
        cwd=work,
        env={k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"},
        on_notice=lambda emoji, text: notices.append((emoji, text)),
        ledger=ledger,
        token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
    )


async def main() -> None:
    """Run every check in order; exit 1 if any failed."""
    work = Path(tempfile.mkdtemp())
    ledger = work / "author-turns.jsonl"
    notices: list[tuple[str, str]] = []
    sid = str(uuid.uuid4())
    base = _author._author_argv("live-probe", "bypassPermissions", "sonnet", "")
    check("argv is the structured session", "--input-format" in base and "-p" in base, base)
    argv = [*base, "--settings", str(settings_file(work)), "--session-id", sid]

    proc = new_proc(argv, work, ledger, notices)
    await proc.start()

    # 1. Long, multi-line direction goes in whole.
    mark = len(proc.tail)
    await proc.submit(
        f"Remember the codeword {CODEWORD}. Count the lines that start with 'Line' below and reply with only "
        f"'COUNT=<n>'.\n{LONG}"
    )
    done = await until(lambda: proc.results >= 1, 180)
    # `-p` reports `system/init` once it has read its first message.
    check("init reported", proc.reached_prompt and proc.session_id == sid, proc.session_id)
    check(
        "long multi-line message answered",
        done and "COUNT=12" in replies(proc, mark),
        replies(proc, mark)[-300:],
    )

    # 2. A message written mid-turn is queued and answered; liveness reads busy while the tool runs silent.
    mark, before = len(proc.tail), proc.results
    await proc.submit(
        "Run the shell command `python3 -c 'import time; time.sleep(25)'` "
        "with the Bash tool in the foreground (run_in_background false), "
        "then reply 'SLEPT'."
    )
    await until(lambda: "[tool]" in said(proc, mark) or "Bash" in said(proc, mark), 60)
    await proc.submit("Also include the word 'QUEUED-OK' in your reply.")
    await asyncio.sleep(12)
    now = time.monotonic()
    live = proc.last_output is not None and now - proc.last_output < 2
    # Silent: the session has sent nothing for a while, and liveness still reads it as working.
    quiet = now - (proc._last_event or now)
    check(
        "busy while a tool runs silently",
        live and proc.busy and quiet > 5,
        {"busy": proc.busy, "seconds since the last event": round(quiet, 1)},
    )
    # The queued message can be answered in the same turn or as its own: wait for the answer, then idle.
    done = await until(lambda: not proc.busy and "QUEUED-OK" in replies(proc, mark), 240)
    # Delivered: the transcript holds it as a message of the conversation (folded into the running turn, or
    # a turn of its own), whatever the model then does with it.
    entries = [
        json.loads(line)
        for path in Path.home().glob(f".claude/projects/*/{sid}.jsonl")
        for line in path.read_text().splitlines()
    ]
    delivered = [
        e.get("type")
        for e in entries
        if e.get("type") in ("user", "attachment") and "QUEUED-OK" in json.dumps(e)
    ]
    check(
        "mid-turn message queued and answered",
        done and bool(delivered) and proc.results >= before + 1,
        {"delivered as": delivered, "replies": replies(proc, mark)[-300:], "results": proc.results - before},
    )
    idle = proc.last_output
    await asyncio.sleep(3)
    check("idle once the turn ended", not proc.busy and proc.last_output == idle, {"last_output": idle})

    # 3. Policy: a settings deny rule and a PreToolUse hook still enforce in bypassPermissions.
    target = work / "keep.txt"
    target.write_text("keep")
    mark, before = len(proc.tail), proc.results
    await proc.submit(
        f"Run exactly `rm {target}` with Bash. Then run exactly "
        f"`echo FORBIDDEN-HOOK > {work}/hooked.txt` with "
        "Bash. Do not try any other way. Reply 'TRIED'."
    )
    await until(lambda: proc.results >= before + 1, 240)
    text = said(proc, mark)
    check("deny rule enforced", target.exists(), text[-400:])
    check("PreToolUse hook enforced", not (work / "hooked.txt").exists(), text[-400:])
    check(
        "hook events reported",
        "[hook]" in text,
        [line for line in text.splitlines() if line.startswith("[hook]")],
    )

    # 4. Sub-agent through the Agent tool.
    mark, before = len(proc.tail), proc.results
    await proc.submit(
        "Use the Agent tool (subagent_type general-purpose) with the prompt 'Reply with exactly: SUB-OK'. "
        "Then reply with what it returned."
    )
    await until(lambda: proc.results >= before + 1, 300)
    text = said(proc, mark)
    check("sub-agent ran", "SUB-OK" in replies(proc, mark) and "[task]" in text, text[-400:])

    # 5. /stop mid-turn: interrupt, and the session takes the next message.
    mark, before = len(proc.tail), proc.results
    await proc.submit(
        "Run `python3 -c 'import time; time.sleep(120)'` "
        "with the Bash tool in the foreground (run_in_background false), then reply 'LATE'."
    )
    await asyncio.sleep(10)
    started = time.monotonic()
    await proc.interrupt()
    await until(lambda: proc.results >= before + 1, 60)
    answer = str((proc.last_result or {}).get("result", ""))
    check(
        "interrupt ended the turn early",
        time.monotonic() - started < 60 and "LATE" not in answer,
        {k: (proc.last_result or {}).get(k) for k in ("subtype", "terminal_reason", "result")},
    )
    await proc.submit("Reply with only 'AFTER-STOP'.")
    await until(lambda: proc.results >= before + 2, 120)
    check("session continues after interrupt", "AFTER-STOP" in replies(proc, mark), said(proc, mark)[-200:])

    # 6. /compact is accepted in the structured session.
    before = proc.results
    await proc.submit("/compact")
    await until(lambda: proc.results >= before + 1, 300)
    last = proc.last_result or {}
    check("/compact accepted", not last.get("is_error"), {k: last.get(k) for k in ("subtype", "is_error")})

    # 7. Structured limits, cost and turns.
    check("rate limit events read", bool(proc.rate_limits), proc.rate_limits)
    check(
        "cost and turns from results",
        proc.turns > 0 and proc.cost_usd > 0,
        {"turns": proc.turns, "cost": proc.cost_usd, "usage": proc.usage},
    )
    await proc.stop(grace=30)
    check("stop exits cleanly", proc.returncode == 0, proc.returncode)

    # 8. Restart: a new process resumes the same conversation.
    resumed = new_proc([*base, "--resume", sid], work, ledger, notices)
    await resumed.start()
    await resumed.submit("What was the codeword I asked you to remember? Reply with it only.")
    # A background task the first process left behind can be reported first, as a turn of its own.
    await until(lambda: CODEWORD in replies(resumed), 180)
    check(
        "resume keeps the conversation",
        CODEWORD in replies(resumed) and resumed.session_id == sid,
        {"session": resumed.session_id, "text": said(resumed)[-200:]},
    )

    # 8b. Inbox direction through the `AuthorSession` wrapper a dual-engine slot uses: it arrives whole
    # (the text, not a pointer to its file), and its marker goes only once it was taken.
    _session_config.WORKSPACE = work
    inbox = work / ".claude-flyte" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "900.md").write_text(
        "Line one of the direction.\nReply with only the word on the last line.\nINBOX-WHOLE-OK\n"
    )
    marker = inbox / "900.pending"
    marker.touch()
    wrapped = AuthorSession(resumed, work / "author-pending.json")
    mark = len(resumed.tail)
    await _inbox._deliver_direction(marker)(wrapped)
    await until(lambda: "INBOX-WHOLE-OK" in replies(resumed, mark), 180)
    check(
        "inbox direction reaches a wrapped session whole",
        "INBOX-WHOLE-OK" in replies(resumed, mark)
        and not marker.exists()
        and "INBOX-WHOLE-OK" in (resumed.sent_texts[-1] if resumed.sent_texts else ""),
        {"sent": (resumed.sent_texts[-1] if resumed.sent_texts else "")[:200], "marker": marker.exists()},
    )
    await resumed.stop(grace=30)

    # 9. The usage report reads the ledger.
    read = _usage.read_author_ledger(ledger)
    check(
        "ledger feeds the usage report",
        read["results"] >= 8 and read["cost_usd"] > 0 and sid in read["sessions"],
        {k: v for k, v in read.items() if k != "sessions"},
    )

    # The transcript the report's `[session]` lines and the inbox watcher read: what marks its entries.
    found = sorted(Path.home().glob(f".claude/projects/*/{sid}.jsonl"))
    marks: dict[str, int] = {}
    for line in found[0].read_text().splitlines() if found else []:
        entry = json.loads(line)
        if entry.get("type") == "user":
            key = f"{entry.get('entrypoint')}/{(entry.get('origin') or {}).get('kind')}"
            marks[key] = marks.get(key, 0) + 1
    check("transcript written", bool(found), {"path": [str(p) for p in found], "user entries": marks})

    # 10. Permission callbacks: in `default` mode an approval request reaches the policy and is denied.
    denied_file = work / "denied.txt"
    gate = new_proc(
        _author._author_argv("live-probe", "default", "sonnet", ""), work, work / "g.jsonl", notices
    )
    await gate.start()
    await gate.submit(f"Run exactly `echo hi > {denied_file}` with Bash. Reply 'DONE' after.")
    await until(lambda: gate.results >= 1, 180)
    check(
        "permission request denied by policy",
        bool(gate.permission_denials) and not denied_file.exists(),
        {"denials": list(gate.permission_denials), "notices": notices[-3:]},
    )
    await gate.stop(grace=30)

    failed = [n for n, c in checks.items() if not c["ok"]]
    print("RESULT " + json.dumps({"failed": failed, "checks": checks}, default=str))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
