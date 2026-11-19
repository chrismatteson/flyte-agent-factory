"""The audit over records from real sealed runs (`ops/seal_e2e.py`, kept in `docs/seal-e2e/`).

The corpus (`tests/seal_corpus.py`) measures the rates on built records; these are records a real Claude
left in a child action, through the deployed entry points (`sealed_plain`, `subagent`), so a change to the
record or the audit that breaks on the real shapes shows here.
"""

import shutil
from pathlib import Path

import pytest

from flyte_agent_factory import _seal_audit

E2E = Path(__file__).resolve().parent.parent / "docs" / "seal-e2e"
GOLD = _seal_audit.Gold.load(E2E / "gold.json")


def _audit(root: Path) -> _seal_audit.Verdict:
    return _seal_audit.audit(root, GOLD)


def _copy(case: str, tmp_path: Path) -> Path:
    return Path(shutil.copytree(E2E / case / "record", tmp_path / case))


@pytest.mark.parametrize("case", ["clean", "reviewer"])
def test_a_real_clean_run_with_a_sub_agent_is_clean_and_lists_its_blocked_attempts(case):
    verdict = _audit(E2E / case / "record")
    assert verdict.verdict == "clean", verdict.to_json()
    assert not verdict.reasons
    # Every route the brief tried, from the role and from its sub-agent, is in the record as an attempt.
    assert len(verdict.attempts) >= 9
    kinds = {a.kind for a in verdict.attempts}
    assert "guard-denied" in kinds


def test_a_real_run_that_read_another_attempts_patch_is_void_with_the_evidence_line():
    verdict = _audit(E2E / "seeded" / "record")
    assert verdict.verdict == "void"
    reached = [r for r in verdict.reasons if r.kind == "reached"]
    assert reached and "Fix compatibility with proxy objects" in reached[0].evidence
    assert reached[0].file.startswith("roles/") and reached[0].line > 0


def test_a_real_record_without_its_end_marker_is_incomplete(tmp_path):
    root = _copy("clean", tmp_path)
    for end in root.glob("roles/*/end.json"):
        end.unlink()
    verdict = _audit(root)
    assert verdict.verdict == "void"
    assert any(r.kind == "incomplete" and "end.json" in r.evidence for r in verdict.reasons)


def test_a_real_record_with_a_truncated_sub_agent_transcript_is_incomplete(tmp_path):
    root = _copy("clean", tmp_path)
    (sub,) = root.glob("roles/*/transcript/**/subagents/*.jsonl")
    lines = sub.read_text().splitlines(keepends=True)
    sub.write_text("".join(lines[: len(lines) // 2]))
    verdict = _audit(root)
    assert verdict.verdict == "void"
    assert any(r.kind == "incomplete" for r in verdict.reasons)
