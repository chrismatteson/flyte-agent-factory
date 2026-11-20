"""What to tell the owner when no reviewer could run: the cause, as far as it can be read, and what to do.

A review nobody ran is an infrastructure failure, and retrying it is the owner's to decide
(`_reserved.OWNER_RESERVED_FLOOR`). It still reaches them with a diagnosis and a recommendation, not as a
bare decision (#53). The reasons are the reviewers' own errors (`_review._raise_undecided`); the backend kind
is the supervisor's, which has the profile even when the reviewers' pods did not.

The advice goes into an `ask-human "..."` command line the session runs, so it holds no backtick or double
quote (`advise` is tested for that).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import _union_identity

#: Characters a double-quoted shell argument expands or ends on, and what replaces each.
_SHELL_UNSAFE = str.maketrans({"`": "'", '"': "'", "$": "", "\\": "/", "!": "."})
#: How much of one reviewer's error the escalation quotes; the rest is in the reviewer's own log.
REASON_CHARS = 400


@dataclass(frozen=True)
class Advice:
    diagnosis: str
    recommendation: str
    default: str


#: What the session does if the owner does not answer: never a skipped review, never a pass.
_WAIT = "I wait, unreviewed, and declare DONE again only when a human says to retry"


def summarise(missing: Mapping[str, str]) -> str:
    """Return each distinct reason once, with the reviewers it stopped, quoted up to `REASON_CHARS`."""
    by_reason: dict[str, list[str]] = {}
    for judge, reason in sorted(missing.items()):
        by_reason.setdefault(reason.strip(), []).append(judge)
    text = "; ".join(
        f"{', '.join(judges)}: {reason[:REASON_CHARS]}{'...' if len(reason) > REASON_CHARS else ''}"
        for reason, judges in by_reason.items()
    )
    # The session quotes this in `ask-human "..."`: nothing the shell would expand or end the string on.
    return text.translate(_SHELL_UNSAFE)


def advise(reasons: str, backend_kind: str) -> Advice:
    """Return the diagnosis and recommendation for reviewers that failed with `reasons` on `backend_kind`."""
    if "cannot determine this pod's identity" in reasons and backend_kind == "union":
        return Advice(
            "the reviewers' pods could not tell which backend they run on: their environment was deployed by "
            "an engine that does not give every pod the backend kind (AGENT_FACTORY_BACKEND_KIND)",
            "redeploy the factory with the current engine (docs/setup.md#deploying), then retry the review",
            _WAIT,
        )
    if "cannot determine this pod's identity" in reasons or (
        backend_kind == "oss" and "no Union identity for tier" in reasons
    ):
        return Advice(
            "the reviewers' pods could not tell that they run on an OSS backend, so they asked for a Union "
            "key OSS never issues. Their environment was deployed by an engine that does not give every "
            "pod the backend kind (AGENT_FACTORY_BACKEND_KIND); no Union key is needed or missing",
            "redeploy the factory with the current engine (docs/setup.md#deploying), then retry the review. "
            "Do not run ops/bootstrap.py --union-keys: OSS Flyte reads no keys",
            _WAIT,
        )
    if "on an OSS backend: the profile lists [union] keys" in reasons:
        return Advice(
            "the profile is OSS but lists [union] keys, so the reviewers' pods expect a key that is not "
            "mounted",
            "remove the [union] table from the profile and redeploy (OSS runs keyless), then retry the "
            "review",
            _WAIT,
        )
    if _union_identity.BAD_KEY in reasons and backend_kind != "union":
        return Advice(
            "a reviewer pod has a key mounted that is not an API key, on a backend that is not Union",
            "store the key the backend issued in the secret the error names, or remove the [union] table "
            "from an OSS profile and redeploy, then retry the review",
            _WAIT,
        )
    if "no Union identity for tier" in reasons or _union_identity.BAD_KEY in reasons:
        return Advice(
            "the reviewers' pods have no valid Union key for their tier",
            "store the tier's key with python ops/bootstrap.py --union-keys (docs/setup.md#union-keys) and "
            "redeploy, then retry the review",
            _WAIT,
        )
    if "Union refused" in reasons:
        return Advice(
            "Union refused the reviewers' identity an action it needs",
            "bind the role named in the error to that project/domain (docs/setup.md#union-keys), then retry",
            _WAIT,
        )
    return Advice(
        "the reviewers' pods failed before giving a verdict, for the reason quoted",
        "retry the review once; if it fails the same way, read a reviewer pod's log for the cause",
        _WAIT,
    )


def own_backend_kind() -> str:
    """Return this process's backend kind: its profile's, else its `BACKEND_ENV`; "" when neither says."""
    return _union_identity.backend_kind(_union_identity._load_profile_or_none())


def escalation(why: str, backend_kind: str | None = None) -> tuple[str, str]:
    """Return the diagnosis sentence for the session and the `ask-human` text it is told to send.

    `backend_kind` defaults to this process's (`own_backend_kind`).
    """
    advice = advise(why, own_backend_kind() if backend_kind is None else backend_kind)
    diagnosis = f"Diagnosis: {advice.diagnosis}. Recommendation: {advice.recommendation}."
    ask = (
        f"Review could not run (<reason>). Diagnosis: {advice.diagnosis}. My recommendation: "
        f"{advice.recommendation}. Summary of what was delivered: <...>. Default if no answer: "
        f"{advice.default}."
    )
    return diagnosis, ask
