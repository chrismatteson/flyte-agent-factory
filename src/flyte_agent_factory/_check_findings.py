"""The finding check: re-run a judge's findings in a fresh copy, so one that does not reproduce is dropped.

Its own module so `_review.py` stays under its line limit; `_findings` holds the pure parts.
"""

from __future__ import annotations

import traceback

from flyte.io import Dir

from flyte_agent_factory import _findings, _policy, _reviewer, _roles, _timeline


async def run(
    judge: str,
    brief: str,
    acceptance: str,
    text: str,
    round_: int,
    timeline: _timeline._Timeline,
    snapshot: Dir,
) -> str | None:
    """Return the finding check's reply, or None when none was needed or it could not run.

    None keeps the judge's findings (`_findings.decide` fails closed): a check that cannot run removes
    nothing. A bare FAIL has no finding to keep, so with no check it is a PASS.
    """
    blocking = _findings.select_blocking(_findings.parse_findings(text))
    if not _findings.needs_check(text):
        return None
    prompt = _findings.build_check_prompt(judge, brief, acceptance, blocking, reply=text)
    setting = _policy.resolve("finding_check")
    for attempt in (1, 2):
        try:
            reply, _ = await _reviewer._call_reviewer(
                timeline=timeline,
                purpose=f"{judge}-check",
                round_=round_,
                answer_only=True,
                brief=prompt,
                workdir=snapshot,
                role="verify",
                model=setting.model,
                effort=setting.effort,
                max_turns=40,
            )
            return reply
        except Exception as e:  # noqa: BLE001 -- a broken check keeps the findings, it does not sink the round
            traceback.print_exc()
            if attempt == 2:
                consequence = "keeping the findings" if blocking else _findings.NOTHING_TO_KEEP_REASON
                timeline.row(
                    "⚠️",
                    f"{_roles.display_role(judge)}: finding check could not run ({type(e).__name__}: {e}); "
                    f"{consequence}",
                )
    return None
