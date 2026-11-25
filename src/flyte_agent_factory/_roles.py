"""Role names: the session sub-agent roles with their old aliases, and the review loop's display names.

Roles are named by the verb of what they do (#80). The job-title names stay installed as aliases for the
015 rename window, and reports written before the rename still carry the old names, so both directions
are mapped here rather than at each reader.
"""

from __future__ import annotations

#: The `subagent` task's own name, which a call with no purpose keeps in the console.
SUBAGENT_ACTION = "subagent"
#: Flyte keeps at most this many characters of a short name (`task_serde._MAX_TASK_SHORT_NAME_LENGTH`).
_SHORT_NAME_MAX = 63

#: alias -> the role it stands for. Removing the aliases is a gardening item after the rename window.
ROLE_ALIASES = {"product": "scope", "designer": "shape", "dev": "build", "qa": "verify"}

#: The ledger and the verdict rows are keyed by the reviewer purpose, which also names the policy keys
#: (`JUDGE_ENGINE`, `PLANNER_ENGINE`, ...), so the purposes keep their names and only the report's
#: display changes. Older reports carry the old names; `display_role` maps those too.
_REVIEW_DISPLAY = {
    "correctness": "judge-correctness",
    "completeness": "judge-completeness",
    "craft": "judge-craft",
    "planner": "plan",
    "arbiter": "arbitrate",
}


def canonical_role(agent_type: str) -> str:
    """Return the new role an `agentType` stands for; any other type (`general-purpose`, ...) is unchanged."""
    return ROLE_ALIASES.get(agent_type, agent_type)


def display_role(purpose: str) -> str:
    """Return the name a report shows for a reviewer purpose or a session role."""
    return _REVIEW_DISPLAY.get(purpose) or canonical_role(purpose)


def action_name(purpose: str) -> str:
    """Return the console name of a routed sub-agent call: its report name, or `subagent` with no purpose.

    It is the display-only `short_name` of the one `subagent` task (`_reviewer._call_reviewer_raw`), so
    every judge, planner, arbiter, check and delegate is named in the run's graph by the role it played.
    """
    return (display_role(purpose) or SUBAGENT_ACTION)[:_SHORT_NAME_MAX]
