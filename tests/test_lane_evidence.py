"""ops/lane_evidence.py: what it runs for each scenario, and what its evidence file records."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory._gate_tests import JunitCase
from ops import lane_evidence


def _git(cwd: Path, *args: str) -> str:
    # No stdin: an xdist worker's stdin is its channel to the controller.
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace repository with a project folder and a run context at its root, as in a session pod."""
    top = tmp_path / "ws"
    project = top / "projects" / "p"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "mod.py").write_text("X = 1\n")
    (project / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n")
    (project / "checks.sh").write_text("true\n")
    _git(tmp_path, "init", "-q", str(top))
    for key, value in (("user.name", "t"), ("user.email", "t@localhost")):
        _git(top, "config", key, value)
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "code")
    (project / "README.md").write_text("docs only\n")
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "docs")
    (top / ".flyte").mkdir()
    (top / ".flyte" / "run_context.json").write_text("{}")
    monkeypatch.setattr(lane_evidence, "HERE", project)
    monkeypatch.setenv("FLYTE_INTERNAL_EXECUTION_ID", "run-under-test")
    # main() sets these for the lanes it starts and puts the clone's src first; each test gets them back.
    monkeypatch.setenv("AGENT_FACTORY_LANE_CPUS", "unset-by-test")
    monkeypatch.setattr(sys, "path", list(sys.path))
    return top


def test_without_a_run_it_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)

    assert lane_evidence.main(["gate-2cpu", "--out", str(tmp_path / "out")]) == 2
    assert "needs a Flyte run" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_the_clone_is_the_commit_with_the_run_context_and_names_the_last_code_commit(workspace, tmp_path):
    first = _git(workspace, "rev-parse", "HEAD~1")
    (tmp_path / "scratch").mkdir()

    clone, project, sha, code = lane_evidence._clone("HEAD", tmp_path / "scratch")

    assert sha == _git(workspace, "rev-parse", "HEAD")
    assert code == first  # the docs commit did not change the code
    assert project == clone / "projects" / "p" and (project / "src" / "mod.py").is_file()
    assert (clone / ".flyte" / "run_context.json").is_file()


def test_the_failing_test_is_committed_in_the_clone_only(workspace, tmp_path):
    (tmp_path / "scratch").mkdir()
    clone, project, _, _ = lane_evidence._clone("HEAD", tmp_path / "scratch")

    lane_evidence._add_failing_test(clone, project)

    assert _git(clone, "status", "--porcelain") == ""
    assert "tests/test_zz_demo_failure.py" in _git(clone, "show", "--name-only", "HEAD")
    assert not (workspace / "projects" / "p" / "tests" / "test_zz_demo_failure.py").exists()
    # Run in this process: a nested pytest under an xdist worker is what a shard cannot afford to hang on.
    namespace: dict[str, Any] = {}
    exec((project / "tests" / "test_zz_demo_failure.py").read_text(), namespace)
    with pytest.raises(AssertionError, match="deliberately failing"):
        namespace["test_a_deliberately_failing_test_for_the_demo"]()


def test_a_gate_scenario_runs_the_head_run_at_its_size_and_records_what_it_said(
    workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_FACTORY_LANE_RUN_HERE", "1")
    seen: dict[str, Any] = {}
    merged = "flyte_controller_base::informer: Merged action: ===>"
    output = "\n".join(
        [
            f"2026-11-14T15:47:11.386187Z  WARN {merged} abc123 Action {{" + "x" * 500,
            f"2026-11-14T15:47:12.000000Z  WARN {merged} def456 Action {{",
            "an unrelated line",
            "=== lane verdict: exit 1 ===",
            "shard 1/2 (spot, child action abc123): exit 1: 1 failed, 10 passed " + "y" * 400,
            "FAILED tests/test_a.py::test_b - assert False",
        ]
    )

    def run_gate(project):
        seen["project"] = project
        seen["cpus"] = os.environ.get("AGENT_FACTORY_LANE_CPUS")
        seen["run_here"] = os.environ.get("AGENT_FACTORY_LANE_RUN_HERE")
        cases = [JunitCase("tests.test_a", "test_b", False), JunitCase("tests.test_a", "test_c", True)]
        return "FAIL (exit 1)", output, cases, ""

    monkeypatch.setattr(lane_evidence, "_run_gate", run_gate)

    assert lane_evidence.main(["gate-4cpu", "--out", str(tmp_path / "out")]) == 0

    assert seen["cpus"] == "4" and seen["run_here"] is None  # the opt-in to run here is never on
    assert (
        seen["project"].name == "p" and seen["project"] != workspace / "projects" / "p"
    )  # the scratch clone
    text = (tmp_path / "out" / "gate-4cpu.txt").read_text()
    assert "scenario: gate-4cpu (gate, 4-CPU shards)" in text
    assert f"commit: {_git(workspace, 'rev-parse', 'HEAD')}" in text
    assert "run: run-under-test" in text and "limit: 1200 s" in text
    assert "outcome: FAIL (exit 1)" in text
    assert "junit cases: 2, failed: 1" in text and "failed: tests.test_a::test_b" in text
    assert "capacity: -" in text and "ran here (session pod): no" in text
    assert "child actions launched: abc123 def456" in text
    assert "WARN flyte_controller_base" not in text and "an unrelated line" not in text
    assert "=== lane verdict: exit 1 ===" in text and "FAILED tests/test_a.py::test_b" in text
    shard = next(line for line in text.splitlines() if line.startswith("shard 1/2"))
    assert len(shard) == 300 and shard.endswith("...")


def test_a_capacity_outcome_records_its_reason_and_no_cases(workspace, tmp_path, monkeypatch):
    reason = "fast lane: no capacity: 1 of 1 shards (64 CPUs) not placed within 2m spot + 3m on-demand"
    monkeypatch.setattr(lane_evidence, "_run_gate", lambda project: ("NO_CAPACITY (exit 0)", "", [], reason))

    assert lane_evidence.main(["gate-64cpu", "--out", str(tmp_path / "out")]) == 0

    text = (tmp_path / "out" / "gate-64cpu.txt").read_text()
    assert "64-CPU shards" in text and "outcome: NO_CAPACITY (exit 0)" in text
    assert "junit cases: 0, failed: 0" in text and f"capacity: {reason}" in text


def test_a_checks_scenario_runs_the_supervisors_step_in_the_clone(workspace, tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def run_checks(clone, project, scratch):
        seen.update(clone=clone, project=project, cpus=os.environ.get("AGENT_FACTORY_LANE_CPUS"))
        return "PASS", "a lane ran here, labelled ran here", [JunitCase("t", "x", True)], ""

    monkeypatch.setattr(lane_evidence, "_run_checks", run_checks)
    monkeypatch.setattr(lane_evidence, "_run_gate", lambda project: pytest.fail("not the gate"))

    assert lane_evidence.main(["checks-2cpu", "--out", str(tmp_path / "out")]) == 0

    assert seen["cpus"] == "2" and seen["project"] == seen["clone"] / "projects" / "p"
    text = (tmp_path / "out" / "checks-2cpu.txt").read_text()
    assert "limit: 900 s" in text and "outcome: PASS" in text
    assert "ran here (session pod): yes" in text  # anything run on the session pod is called out


def test_the_failing_scenario_adds_its_test_before_the_run(workspace, tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def run_gate(project):
        seen["has_failing_test"] = (project / "tests" / "test_zz_demo_failure.py").is_file()
        seen["cpus"] = os.environ.get("AGENT_FACTORY_LANE_CPUS")
        return "FAIL (exit 1)", "", [], ""

    monkeypatch.setattr(lane_evidence, "_run_gate", run_gate)

    assert lane_evidence.main(["gate-2cpu-failing", "--out", str(tmp_path / "out")]) == 0

    assert seen == {"has_failing_test": True, "cpus": "2"}
    assert "one deliberately failing test" in (tmp_path / "out" / "gate-2cpu-failing.txt").read_text()


def test_every_scenario_of_the_doc_has_its_step_and_size():
    assert lane_evidence.SCENARIOS == {
        "checks-2cpu": ("checks", 2, False),
        "checks-4cpu": ("checks", 4, False),
        "gate-2cpu": ("gate", 2, False),
        "gate-4cpu": ("gate", 4, False),
        "gate-2cpu-failing": ("gate", 2, True),
        "gate-64cpu": ("gate", 64, False),
    }
