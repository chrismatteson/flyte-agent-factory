"""Decide the author session's permission requests in code.

The session runs unattended (`bypassPermissions` by default, with the factory's deny rules and its
`PreToolUse` hooks still enforced by Claude Code itself). What still reaches us is a `can_use_tool`
control request: a call Claude Code will not make without someone saying yes. Nobody is at the session to
say it, so the answer is no, with a reason the model can act on. On the pty these were menus answered by
matching screen text and pressing Escape (`_process.permission_prompt`); here each request carries the
tool and its input, and the answer is a structured `deny`.

A call denied again and again is the session retrying instead of changing approach: from
`PROMPT_REPEAT_AFTER` denials of one tool within `PROMPT_DECLINE_WINDOW_S`, the denial also interrupts the
turn, so the supervisor's idle handling takes over instead of a loop of refusals.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import _session_config

#: Tools that exist to ask a person something. The session's person is reached through `ask-human`.
_ASKS_A_PERSON = {
    "AskUserQuestion": (
        "Nobody is at this session to answer an in-session question. Decide it yourself if it is "
        "engineering judgement; if it is owner-reserved, ask with `ask-human` (with a timeout and a "
        "default) and carry on."
    ),
    "ExitPlanMode": (
        "Nobody is at this session to approve a plan. Record the plan with `flyte-note` and carry on "
        "building it."
    ),
}

_DEFAULT = (
    "This session runs unattended and nobody can approve this call, so it was denied. Use a different "
    "approach that needs no approval; if the call is genuinely required, ask the owner with `ask-human`."
)

_REPEATED = " It has now been denied {count} times: stop retrying it, and change approach."


@dataclass(frozen=True)
class Decision:
    """One answer to a `can_use_tool` request."""

    behavior: str
    message: str = ""
    interrupt: bool = False
    updated_input: dict[str, Any] | None = None

    def to_control_response(self) -> dict[str, Any]:
        """Return the control response body Claude Code expects."""
        if self.behavior == "allow":
            return {"behavior": "allow", "updatedInput": self.updated_input or {}}
        body: dict[str, Any] = {"behavior": "deny", "message": self.message}
        if self.interrupt:
            body["interrupt"] = True
        return body


@dataclass
class Policy:
    """The factory's policy: allow only the tools in `allow`, deny the rest with guidance.

    `allow` is empty by default: under `bypassPermissions` Claude Code asks only about calls it would not
    make unattended, and those are the ones nobody here can approve.
    """

    allow: frozenset[str] = frozenset()
    repeat_after: int = _session_config.PROMPT_REPEAT_AFTER
    window_s: float = _session_config.PROMPT_DECLINE_WINDOW_S
    _denials: dict[str, deque[float]] = field(default_factory=dict)

    def decide(self, tool: str, tool_input: dict[str, Any]) -> Decision:
        """Return the decision for one request."""
        if tool in self.allow:
            return Decision("allow", updated_input=tool_input)
        now = time.monotonic()
        recent = self._denials.setdefault(tool, deque(maxlen=50))
        while recent and now - recent[0] > self.window_s:
            recent.popleft()
        recent.append(now)
        message = _ASKS_A_PERSON.get(tool, _DEFAULT)
        repeated = len(recent) >= self.repeat_after
        if repeated:
            message += _REPEATED.format(count=len(recent))
        return Decision("deny", message, interrupt=repeated)
