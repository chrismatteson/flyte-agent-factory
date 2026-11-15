"""Test lanes the cluster had no room for: neither a pass nor a failure of the project's change.

The lane launcher (`_slow_lane`) exits `LANE_NO_CAPACITY` when it could not place a lane's shards, and
leaves `<junit path>.capacity` holding a one-line reason. The gate and the supervisor read that here, so
"the cluster was full" is retried later instead of bouncing a finished project.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

#: `_slow_lane.CAPACITY`, copied (as `_gate_tests._LANE_NOT_RUN` is): importing `_slow_lane` declares a
#: task environment as a side effect. On this exit the launcher recorded no completion and ran nothing
#: locally.
LANE_NO_CAPACITY = 69

#: The launcher's wall budget for a lane, in seconds (it defaults to 1100).
LANE_BUDGET_ENV = "AGENT_FACTORY_LANE_BUDGET_S"

#: Seconds to wait before re-running a pre-review that found no capacity: the first, second and third retry.
#: A fourth capacity outcome escalates to the human.
RETRY_BACKOFF_S = (5 * 60, 10 * 60, 20 * 60)


def read_reason(junit: Path) -> str:
    """Return the reason in `<junit>.capacity`, or "" when the launcher left none."""
    try:
        return junit.with_name(junit.name + ".capacity").read_text(errors="replace").strip()
    except OSError:
        return ""


def read_reasons(directory: Path) -> str:
    """Return the reasons in the `*.capacity` files `checks.sh` left in `directory`, joined by `; `."""
    try:
        files = sorted(directory.glob("*.capacity"))
    except OSError:
        return ""
    reasons = [
        f"{f.name.removesuffix('.xml.capacity')} lane: {read_reason(f.with_suffix(''))}" for f in files
    ]
    return "; ".join(r for r in reasons if not r.endswith(": "))


def _read_record(path: Path) -> dict[str, object] | None:
    """Return the outcome record at `path`, or None when it is missing or not a JSON object."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def decide_checks_outcome(directory: Path) -> str:
    """Return the capacity reasons when a failed `checks.sh` is down to capacity alone, else "".

    Decided lane by lane, a lane being a name (`fast`, `slow`) that left a `<lane>.xml*` file in
    `directory`. A lane is fine when it has a `.capacity` file or a record of a pytest run that found nothing
    wrong (code 0 or 5).
    Capacity needs at least one reason file, every lane fine, and no evidence against it: a record of a lane
    that did not complete for another reason (it ran out of its own time, or could not set up), or that
    completed with failing tests (its report may be missing; its code is not), or a failed case in a junit
    file. A lane with neither file, or only a record that cannot be read, is no evidence of capacity: the
    checks' own time limit most likely killed it.
    """
    reasons = read_reasons(directory)
    if not reasons or count_junit_failures(directory):
        return ""
    # The supervisor keeps its own files in this directory too: only `<lane>.xml*` name a lane.
    lanes = {path.name.partition(".xml")[0] for path in directory.glob("*.xml*")}
    for lane in lanes:
        record = _read_record(directory / f"{lane}.xml.result")
        if record is not None and record.get("completed") is True and record.get("code") not in (0, 5):
            return ""
        if record is not None and record.get("completed") is False and record.get("outcome") != "capacity":
            return ""
        passed = record is not None and record.get("completed") is True
        if not passed and not (directory / f"{lane}.xml.capacity").is_file():
            return ""
    return reasons


#: What the launcher's records say of a lane that could not start for want of its environment, by `outcome`.
_INFRASTRUCTURE = {
    "setup": "could not set up its environment (infrastructure, not the change's tests)",
    "upload": "could not upload the tree to the cluster (infrastructure, not the change's tests)",
}


def label_infrastructure_failures(directory: Path) -> str:
    """Return a sentence naming each lane that failed on setup or upload, or "" when none did.

    These are not capacity (waiting does not help) and still fail the checks, but the agent that reads the
    bounce must not take them for failing tests.
    """
    labels = []
    for path in sorted(directory.glob("*.xml.result")):
        record = _read_record(path)
        label = _INFRASTRUCTURE.get(str(record.get("outcome"))) if record else None
        if label and record and record.get("completed") is False:
            labels.append(f"{path.name.removesuffix('.xml.result')} lane {label}")
    return "; ".join(labels)


def count_junit_failures(directory: Path) -> int:
    """Return how many tests failed in the junit files a lane that did run left in `directory`.

    A lane that ran and failed is the project's failure even when the other lane found no capacity: the
    outcome is then a failure, not a wait.
    """
    failed = 0
    for path in sorted(directory.glob("*.xml")):
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        cases = root.iter("testcase")
        failed += sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    return failed


class ChecksNoCapacity(str):
    """What `_run_declared_checks` returns when `checks.sh` failed for want of capacity: its problem text.

    A `str` so that a caller that only tests it for truth, or lists it, keeps working; `capacity` is the
    reason the launcher gave.
    """

    capacity: str = ""

    def __new__(cls, text: str, capacity: str) -> ChecksNoCapacity:
        self = super().__new__(cls, text)
        self.capacity = capacity
        return self


def explain(reason: str) -> str:
    """Return the sentence a gate result or a problem list gives for a capacity outcome."""
    said = reason if "no capacity" in reason else f"no capacity: {reason}"
    return (
        f"{said}. The test lanes could not be placed on the cluster; this is not a failure "
        "of the project's change, and the full suite must still run before review"
    )


@dataclasses.dataclass
class CapacityWait:
    """The pre-review's retries after no capacity: how many so far, and when the next one is due.

    `at` is a monotonic deadline, checked by the supervisor loop on every turn, so waiting never blocks it.
    """

    attempts: int = 0
    at: float | None = None
    #: The DONE the retry re-runs the pre-review for.
    note: str = ""

    def exhausted(self) -> bool:
        return self.attempts >= len(RETRY_BACKOFF_S)

    def schedule(self, now: float) -> float:
        """Count one more capacity outcome and return the seconds until the retry."""
        delay = RETRY_BACKOFF_S[self.attempts]
        self.attempts += 1
        self.at = now + delay
        return delay

    def retry(
        self, notes: list[str], now: float, idle: bool, declares_done: Callable[[str], bool]
    ) -> list[str]:
        """Return the DONE to run the pre-review for again, or [] when no retry is due.

        A new DONE from the agent cancels a waiting retry (the normal path wins: that DONE runs the
        pre-review itself). Other notes do not: a progress note while waiting must not strand the retry,
        since nothing else would run the pre-review again. The count stays until a pre-review gets past.
        `idle` is False while a review runs or the work is escalated: the retry then waits.
        """
        if any(declares_done(note) for note in notes):
            self.at = None
        if self.at is None or now < self.at or not idle:
            return []
        self.at = None
        return [*notes, self.note]

    def reset(self) -> None:
        self.attempts, self.at = 0, None
