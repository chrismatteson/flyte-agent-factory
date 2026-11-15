"""A workspace project is judged against its own start tree, as a root-layout project is.

Pre-existing debt in `projects/<name>/` (an over-long module, a lint finding) is not the project's to fix;
what it adds or grows is.
"""

import json

import pytest

from flyte_agent_factory import _gate_base, _gate_start, _gates, _timeline
from tests._git import git

pytestmark = pytest.mark.real_factory_gates

LAYOUT = "workspace"
LIMIT = 1500
SIZE_RULES = {"engineering.md": "## SIZE-1 MUST\nEnforced: check factory:module-size\n"}
RUFF_RULES = {"languages/python.md": "## LINT-1 MUST\nEnforced: tool ruff check --select F .\n"}


def _make_lines(count):
    return "".join(f"x{i} = {i}\n" for i in range(count))


def _make_workspace(tmp_path, files):
    """A workspace repo whose `projects/app/` holds `files` at the start; return the project folder."""
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects/.gitkeep").write_text("")
    project = tmp_path / "projects/app"
    for rel, text in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/HEAD", "HEAD")  # a workspace clone's default branch
    _gates.capture_base(tmp_path, "app")
    git(tmp_path, "config", "user.email", "app@agent-factory.localhost")
    return project


def _commit_all(workspace, message="change"):
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", message)


def _judge_rule(workspace, rules, rule):
    note = "refactor: no behaviour change"
    _gates.run(workspace, "app", "", "", note, rules, _timeline._Timeline(), LAYOUT)
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return {r["rule"]: r for r in results}[rule]


def _judge_size(workspace):
    return _judge_rule(workspace, SIZE_RULES, "factory:module-size")


def test_the_start_folder_is_the_projects_folder_of_the_start_tree(tmp_path):
    folder = _gate_start.make_start_folder(tmp_path, "projects/app")
    assert folder == tmp_path / "projects/app" and folder.is_dir()
    (folder / "a.py").write_text("")
    assert _gate_start.make_start_folder(tmp_path, "projects/app") == folder
    assert (folder / "a.py").is_file(), "an existing folder is returned as it is"


def test_a_module_over_the_limit_at_the_start_is_not_charged_to_an_unrelated_change(tmp_path):
    project = _make_workspace(tmp_path, {"big.py": _make_lines(LIMIT + 20), "small.py": "y = 1\n"})
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)
    result = _judge_size(tmp_path)
    assert result["status"] == "PASS", result["output"]
    assert f"over the limit but not grown: big.py ({LIMIT + 20} lines, was {LIMIT + 20})" in result["output"]


def test_growing_a_module_that_was_over_the_limit_fails(tmp_path):
    project = _make_workspace(tmp_path, {"big.py": _make_lines(LIMIT + 20)})
    (project / "big.py").write_text(_make_lines(LIMIT + 21))
    _commit_all(tmp_path)
    result = _judge_size(tmp_path)
    assert result["status"] == "FAIL" and f"big.py: {LIMIT + 21} lines, was {LIMIT + 20}" in result["output"]


def test_a_new_module_over_the_limit_fails_beside_old_debt(tmp_path):
    project = _make_workspace(tmp_path, {"big.py": _make_lines(LIMIT + 20)})
    (project / "other.py").write_text(_make_lines(LIMIT + 1))
    _commit_all(tmp_path)
    result = _judge_size(tmp_path)
    assert result["status"] == "FAIL" and f"other.py: {LIMIT + 1} lines, new file" in result["output"]


def test_a_brand_new_project_folder_is_judged_on_everything_it_adds(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects/.gitkeep").write_text("")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/HEAD", "HEAD")
    _gates.capture_base(tmp_path, "app")
    git(tmp_path, "config", "user.email", "app@agent-factory.localhost")
    (tmp_path / "projects/app").mkdir()
    (tmp_path / "projects/app/big.py").write_text(_make_lines(LIMIT + 1))
    _commit_all(tmp_path)
    result = _judge_size(tmp_path)
    assert result["status"] == "FAIL" and "big.py" in result["output"]


def test_a_lint_finding_the_start_already_had_passes_and_a_new_one_fails(tmp_path):
    old = "import os\n"  # F401, unused
    project = _make_workspace(tmp_path, {"old.py": old, "pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    (project / "fine.py").write_text("z = 1\n")
    _commit_all(tmp_path)
    result = _judge_rule(tmp_path, RUFF_RULES, "LINT-1")
    assert result["status"] == "PASS" and "pre-existing" in result["output"], result["output"]
    (project / "new.py").write_text("import sys\n")
    _commit_all(tmp_path, "adds a finding")
    result = _judge_rule(tmp_path, RUFF_RULES, "LINT-1")
    assert result["status"] == "FAIL" and "new.py" in result["output"], result["output"]


def test_the_start_tree_failure_is_reported_like_the_root_layouts(tmp_path, monkeypatch):
    project = _make_workspace(tmp_path, {"old.py": "x = 1\n"})
    (project / "old.py").write_text("x = 2\n")
    _commit_all(tmp_path)

    def raise_start_error(*args, **kwargs):
        raise _gate_start._GateError("boom")

    monkeypatch.setattr(_gate_start, "_start_tree", raise_start_error)
    result = _judge_rule(tmp_path, SIZE_RULES, "scope")
    # As for the root layout since upstream #6: the factory's fault, said as such, never the project's.
    assert result["status"] == _gate_base.Status.HARNESS_FAULT.value
    assert "harness fault (the factory's, not the project's)" in result["output"]
    assert "boom" in result["output"]


def test_a_routed_launch_is_judged_against_its_folder_of_the_start_tree(tmp_path):
    # Launch `fix` works in `projects/app/` (`Project folder: projects/app/`): the start is that folder's.
    project = _make_workspace(tmp_path, {"big.py": _make_lines(LIMIT + 20), "small.py": "y = 1\n"})
    _gates.capture_base(tmp_path, "fix")
    git(tmp_path, "config", "user.email", "fix@agent-factory.localhost")
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)

    def judge_size(folder):
        note = "refactor: no behaviour change"
        _gates.run(tmp_path, "fix", "", "", note, SIZE_RULES, _timeline._Timeline(), LAYOUT, folder=folder)
        results = json.loads((tmp_path / ".claude-flyte/fix/standards-results.json").read_text())
        return {r["rule"]: r for r in results}["factory:module-size"]

    result = judge_size("app")
    assert result["status"] == "PASS", result["output"]
    assert "big.py" in result["output"], "the debt stays listed"
    (project / "big.py").write_text(_make_lines(LIMIT + 21))
    _commit_all(tmp_path)
    assert judge_size("app")["status"] == "FAIL"


def _judge_all(workspace, rules):
    note = "refactor: no behaviour change"
    _gates.run(workspace, "app", "", "", note, rules, _timeline._Timeline(), LAYOUT)
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return {r["rule"]: r for r in results}


@pytest.mark.parametrize(
    ("added", "rules"),
    [
        ({"huge.py": _make_lines(LIMIT + 100)}, SIZE_RULES),
        ({"bad.py": "import os\n", "pyproject.toml": "[tool.ruff]\n"}, RUFF_RULES),
    ],
)
def test_a_commit_under_another_author_is_not_laundered_into_the_start(tmp_path, added, rules):
    """`git commit --author=someone` cannot make the project's own additions count as inherited debt."""
    project = _make_workspace(tmp_path, {"small.py": "y = 1\n"})
    for rel, text in added.items():
        (project / rel).write_text(text)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--author=Someone <someone@example.com>", "-m", "not mine, honest")
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)
    results = _judge_all(tmp_path, rules)
    attribution = results["attribution"]
    assert attribution["status"] == "FAIL" and "someone@example.com" in attribution["output"], attribution


def test_commits_merged_from_the_default_branch_are_not_the_projects(tmp_path):
    project = _make_workspace(tmp_path, {"small.py": "y = 1\n"})
    git(tmp_path, "checkout", "-q", "-b", "upstream")
    (tmp_path / "projects/other").mkdir()
    (tmp_path / "projects/other/big.py").write_text(_make_lines(LIMIT + 1))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--author=Other <other@example.com>", "-m", "another project")
    git(tmp_path, "update-ref", "refs/remotes/origin/HEAD", "HEAD")
    git(tmp_path, "checkout", "-q", "-")
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)
    git(tmp_path, "merge", "-q", "--no-edit", "upstream")
    results = _judge_all(tmp_path, SIZE_RULES)
    assert results["attribution"]["status"] == "PASS", results["attribution"]
    assert results["scope"]["status"] == "PASS", results["scope"]


@pytest.mark.parametrize(
    ("added", "rules", "rule"),
    [
        ({"huge.py": _make_lines(LIMIT + 100)}, SIZE_RULES, "factory:module-size"),
        ({"bad.py": "import os\n", "pyproject.toml": "[tool.ruff]\n"}, RUFF_RULES, "LINT-1"),
    ],
)
def test_a_merge_the_project_authored_cannot_launder_additions_into_the_start(tmp_path, added, rules, rule):
    """What the project's own merge commit adds is its own: no commit of it is reversed to find the start."""
    project = _make_workspace(tmp_path, {"small.py": "y = 1\n"})
    git(tmp_path, "checkout", "-q", "-b", "side")
    (project / "side.py").write_text("s = 1\n")
    _commit_all(tmp_path)
    git(tmp_path, "checkout", "-q", "-")
    git(tmp_path, "merge", "-q", "--no-ff", "--no-commit", "side")
    for rel, text in added.items():
        (project / rel).write_text(text)
    _commit_all(tmp_path, "merge side")
    result = _judge_all(tmp_path, rules)[rule]
    assert result["status"] == "FAIL" and next(iter(added)) in result["output"], result


def test_debt_merged_from_the_default_branch_into_the_folder_is_not_the_projects(tmp_path):
    project = _make_workspace(tmp_path, {"small.py": "y = 1\n"})
    git(tmp_path, "checkout", "-q", "-b", "upstream")
    (project / "big.py").write_text(_make_lines(LIMIT + 1))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--author=Other <other@example.com>", "-m", "upstream debt")
    git(tmp_path, "update-ref", "refs/remotes/origin/HEAD", "HEAD")
    git(tmp_path, "checkout", "-q", "-")
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)
    git(tmp_path, "merge", "-q", "--no-edit", "upstream")
    result = _judge_size(tmp_path)
    assert result["status"] == "PASS" and f"big.py ({LIMIT + 1} lines, was {LIMIT + 1})" in result["output"]


def test_a_merge_the_project_authored_cannot_hide_edits_outside_its_scope(tmp_path):
    project = _make_workspace(tmp_path, {"small.py": "y = 1\n"})
    git(tmp_path, "checkout", "-q", "-b", "side")
    (project / "side.py").write_text("s = 1\n")
    _commit_all(tmp_path)
    git(tmp_path, "checkout", "-q", "-")
    (project / "small.py").write_text("y = 2\n")
    _commit_all(tmp_path)
    git(tmp_path, "merge", "-q", "--no-ff", "--no-commit", "side")
    (tmp_path / "projects/other").mkdir()
    (tmp_path / "projects/other/evil.py").write_text("e = 1\n")
    (tmp_path / "ROOT.txt").write_text("r\n")
    _commit_all(tmp_path, "merge side")
    scope = _judge_all(tmp_path, SIZE_RULES)["scope"]
    assert scope["status"] == "FAIL", scope
    assert "projects/other/evil.py" in scope["output"] and "ROOT.txt" in scope["output"], scope
