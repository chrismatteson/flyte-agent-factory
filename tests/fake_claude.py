"""A scripted stand-in for the `claude` CLI, for rehearsing a session offline.

It is run under a real pty by the real `ClaudeProcess`, prints the line Remote
Control prints, reads whatever the supervisor types, and reacts per a script.
It is not a model: it does exactly what the script says, which is the point --
the supervisor's behaviour (nudges, the DONE gate, the review loop, what the
session is told) can then be exercised end to end in seconds, where the real
thing needs a cluster run, a wait, and `flyte get logs`.

Script (JSON, path in $FAKE_CLAUDE_SCRIPT): a list of
    {"when": "<regex matched against a received line>", "run": ["<sh command>", ...]}
Each step fires at most once, in any order. Commands run with the supervisor's
environment, so `flyte-note` is the real wrapper. Every received line is
appended to $FAKE_CLAUDE_LOG so tests can assert on what the session was told.

Started with `--input-format stream-json` (the author's default, `StreamClaudeProcess`) it speaks
stream-json on pipes instead: it reads each user message whole (reporting `system/init` at the first),
answers it with a `result`, and answers control requests. `limited=True` refuses every turn as Claude
Code does on the usage limit: a rejected `rate_limit_event`, an assistant `rate_limit` error and an error
`result`.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SESSION_URL = "https://claude.ai/code/session_FAKE0123456789 "


def main(*, limited: bool = False) -> int:
    with open(os.environ["FAKE_CLAUDE_SCRIPT"]) as fh:
        steps = json.load(fh)
    log = os.environ["FAKE_CLAUDE_LOG"]
    fired: set[int] = set()

    def received(line: str) -> None:
        pointer = re.fullmatch(
            r"New supervisor message\. The complete message is in (\.claude-flyte/inbox/message-\d+\.md); "
            r"read the whole file and act on it\.",
            line,
        )
        if pointer:
            # The scripted session follows the same file instruction as the real session.
            line = Path(pointer.group(1)).read_text()
        with open(log, "a") as fh:
            fh.write(line + "\n")
        if limited:
            return
        for i, step in enumerate(steps):
            if i in fired or not re.search(step["when"], line):
                continue
            fired.add(i)
            for command in step["run"]:
                subprocess.run(command, shell=True, check=False)
            break

    if "--input-format" in sys.argv:
        return _stream(received, limited=limited)
    print(SESSION_URL, flush=True)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "/exit":
            return 0
        received(line)
    return 0


def _emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


def _stream(received, *, limited: bool) -> int:
    session = "fake-session-0001"
    initialised = False
    for raw in sys.stdin:
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("type") == "control_request":
            # Interrupts are acknowledged; a turn here is over as soon as it is answered anyway.
            body = {"subtype": "success", "request_id": event.get("request_id")}
            _emit({"type": "control_response", "response": body})
            continue
        if event.get("type") != "user":
            continue
        if not initialised:
            # As the real `-p` session does: `system/init` comes once it has read its first message.
            _emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake-model"})
            initialised = True
        content = event["message"]["content"]
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        received(content.strip())
        result = {"type": "result", "session_id": session, "num_turns": 1, "total_cost_usd": 0}
        if limited:
            notice = "You've hit your usage limit. Try again later."
            info = {"status": "rejected", "rateLimitType": "five_hour"}
            _emit({"type": "rate_limit_event", "rate_limit_info": info, "session_id": session})
            message = {"role": "assistant", "content": [{"type": "text", "text": notice}]}
            _emit({"type": "assistant", "error": "rate_limit", "message": message, "session_id": session})
            _emit({**result, "subtype": "error_during_execution", "is_error": True, "result": notice})
        else:
            _emit({**result, "subtype": "success", "is_error": False, "result": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
