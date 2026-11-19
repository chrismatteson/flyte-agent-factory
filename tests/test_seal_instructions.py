"""The generated CLAUDE.md of a sealed session: the push recipe, the sealed note, nothing else changed."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _instructions
from tests._seal_fakes import SPEC

SNAPSHOT = Path(__file__).parent / "snapshots" / "seal_unsealed_memory.json"
# The three renders the snapshot hashes: a project branch, no branch, and a Codex author on a branch.
RENDERS: dict[str, dict[str, Any]] = {
    "branch": {"branch": "factory/x", "name": "x"},
    "nobranch": {"name": "x"},
    "codex": {"branch": "factory/x", "name": "x", "engine": "codex"},
}


@pytest.mark.parametrize("key", sorted(RENDERS))
def test_an_unsealed_memory_is_byte_identical_to_before_sealing_existed(key):
    memory = _instructions._project_memory(False, **RENDERS[key])
    expected = json.loads(SNAPSHOT.read_text())[key]
    assert hashlib.sha256(memory.encode()).hexdigest() == expected


def _sealed(**kw):
    return _instructions._project_memory(False, name="x", branch="factory/x", sealed=SPEC, **kw)


def test_the_sealed_push_recipe_is_a_plain_push():
    memory = _sealed()
    assert "git push -u origin HEAD:refs/heads/factory/x" in memory
    assert "git fetch" not in memory
    assert "git merge" not in memory


def test_the_sealed_note_says_what_the_run_can_reach():
    memory = _sealed()
    assert "**Sealed run.**" in memory
    assert "2025-03-01" in memory
    assert "`delegate`" in memory
    assert "recorded and audited" in memory


def test_a_sealed_memory_does_not_suggest_calling_subagent_directly():
    assert "import subagent" not in _sealed()
    assert "import subagent" in _instructions._project_memory(False, name="x", branch="factory/x")


def test_an_unsealed_memory_has_no_sealed_note():
    assert "Sealed run" not in _instructions._project_memory(False, name="x", branch="factory/x")


def test_the_task_dir_in_the_note_is_under_the_project_folder():
    memory = _instructions._project_memory(
        False, name="x", branch="factory/x", sealed=SPEC, layout="workspace", folder="proj/x"
    )
    assert "`projects/proj/x/repo/`" in memory
