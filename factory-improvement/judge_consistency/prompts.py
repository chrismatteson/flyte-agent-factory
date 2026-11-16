"""The judge prompt as the changed engine would write it, for a decision the study already recorded."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any


def build_after_prompt(study_prompt: str, judge: str, findings_mod: ModuleType) -> str:
    """Return the study's stored prompt with only the finding-instruction block swapped for the new one.

    Everything else stays byte-identical: that is what "same inputs" means for the before/after comparison.
    """
    old = findings_mod.CRAFT_FINDING_INSTRUCTIONS
    count = study_prompt.count(old)
    if count != 1:
        raise ValueError(f"the old finding instructions occur {count} times in the stored prompt, not once")
    return str(study_prompt.replace(old, findings_mod.render_finding_instructions(judge)))


def rebuild_check_inputs(
    row: dict[str, Any],
    study_dir: Path,
    inputs_mod: ModuleType,
    briefs: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return the (brief, acceptance) text the decision's judge prompt carried, for the finding check.

    Rebuilt with the study's own `build_prompt_job`, offline: the check gets the same strings the engine's
    `_one_judge` passes it. `briefs` may be passed in so a caller loads the launch briefs once.
    """
    snapshot = json.loads((study_dir / "data" / "snapshots" / f"{row['run']}.json").read_text())
    if briefs is None:
        briefs = inputs_mod.load_launch_briefs()
    job, _fidelity, _source = inputs_mod.build_prompt_job(row, snapshot, briefs)
    return str(job["brief"]), str(job["acceptance"])


def verify_stored_prompts(study: Any, findings_mod: ModuleType, expect: int | None = None) -> list[str]:
    """Return what is wrong with the study's stored prompts as inputs for this harness (empty when sound).

    Every stored prompt must take the swap in `build_after_prompt`, and must carry, verbatim, the brief and
    acceptance text `rebuild_check_inputs` rebuilds, so the judge and the finding check see the same inputs.
    """
    rows = study.recover.read_jsonl(study.dir / "data" / "sample.jsonl")
    problems = [] if expect is None or len(rows) == expect else [f"{len(rows)} decisions, expected {expect}"]
    briefs = study.inputs.load_launch_briefs()
    for row in rows:
        did = row["decision_id"]
        stem = did.replace(":", "__")
        stored = json.loads((study.dir / "data" / "inputs" / f"{stem}.json").read_text())["prompt"]
        try:
            after = build_after_prompt(stored, row["judge"], findings_mod)
        except ValueError as err:
            problems.append(f"{did}: {err}")
            continue
        if findings_mod.CRAFT_FINDING_INSTRUCTIONS in after:
            problems.append(f"{did}: the old finding instructions survive the swap")
        brief, acceptance = rebuild_check_inputs(row, study.dir, study.inputs, briefs)
        problems += [
            f"{did}: the {name} is not in the stored prompt"
            for name, text in (("brief", brief), ("acceptance", acceptance))
            if text not in stored
        ]
    return problems
