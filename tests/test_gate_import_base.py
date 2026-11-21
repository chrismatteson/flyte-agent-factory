"""A workspace project is measured from its import, though the import commit is only on the project's branch.

`promote.py import` commits (as the operator) an upstream copy into `projects/<name>/`; when that commit is
not on `origin/main`, the merge-base with `origin/HEAD` has no project folder and every upstream file would
count as the project's own new code (upstream issue #54).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gate_import, _gate_start, _gates, _standards, _timeline

pytestmark = pytest.mark.real_factory_gates

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "operator@example.invalid"
PROJECT = _gates.commit_identity("proj")
SKIP_TEST = "import pytest\n\n\ndef test_auth():\n    pytest.skip('needs a server')\n"
PROMOTE = 'repo = "acme/widgets"\nbase = "' + "a" * 40 + '"\npath = "."\n'
FILES = {
    "promote.toml": PROMOTE,
    "src/lib.py": "def one():\n    return 1\n",
    "tests/test_x.py": SKIP_TEST,
}
MESSAGE = "tests removed, deleted or skipped without brief authorization"


def git(path, *args, email="t@example.com"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", f"user.email={email}", "-c", "user.name=t", *args],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(root, files, email=PROJECT, message="work"):
    for rel, text in files.items():
        path = root / rel
        if text is None:
            git(root, "rm", "-q", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(root, "add", "-f", rel)
    git(root, "commit", "-q", "-m", message, email=email)
    return git(root, "rev-parse", "HEAD")


def prefixed(files, prefix="projects/proj/"):
    return {prefix + rel: text for rel, text in files.items()}


def make_clone(tmp_path):
    """A clone of an origin whose `main` holds a workspace (`projects/`), as the harness clones it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    commit(origin, {"projects/.gitkeep": "\n", "README.md": "r\n"}, message="init")
    clone = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / ".git/info/exclude").write_text("/.claude-flyte/\n")
    git(clone, "checkout", "-q", "-b", "factory/proj")
    return origin, clone


def record_base(clone):
    """The supervisor records the base at creation: the file, and its own in-process copy."""
    _gates.capture_base(clone, "proj")


def project_work(clone):
    """What the project does after the import: a small source change and a test that needs it."""
    return commit(
        clone,
        prefixed({"src/lib.py": "def one():\n    return 1\n\n\ndef two():\n    return 2\n"})
        | prefixed(
            {"tests/test_two.py": "from lib import two\n\n\ndef test_two():\n    assert two() == 2\n"}
        ),
    )


@pytest.fixture
def branch_import(tmp_path):
    """The import is on the project's branch only; the base is recorded at it; then the project works."""
    origin, clone = make_clone(tmp_path)
    imported = commit(clone, prefixed(FILES), email=OPERATOR, message="import acme/widgets")
    record_base(clone)
    project_work(clone)
    return clone, imported


def _start(clone):
    return _gate_import.find_workspace_start(clone, "projects/proj", _gates._captured_base(clone, "proj"))


def judge(clone, brief="May edit: projects/proj/"):
    rows = {}
    _gates._judge_project(
        clone,
        "proj",
        _gates.resolve_base(clone, "proj"),
        brief,
        "",
        "DONE",
        [],
        lambda rule, status, output: rows.update({rule: (status, output)}),
        "workspace",
        fail_to_pass_rules=["ENG-X"],
    )
    return rows


def test_an_import_only_on_the_branch_is_the_baseline_not_the_projects_work(branch_import):
    clone, _ = branch_import
    rows = judge(clone)
    status, output = rows[_standards.FAIL_TO_PASS]
    assert MESSAGE not in output, output
    assert status is _gate_base.Status.PASS, output
    assert rows["attribution"][0] is _gate_base.Status.PASS
    assert rows["scope"][0] is _gate_base.Status.PASS


def test_a_new_skip_in_an_imported_test_still_fails(branch_import):
    clone, _ = branch_import
    commit(clone, {"projects/proj/tests/test_x.py": SKIP_TEST + "\n\ndef test_b():\n    pytest.skip('b')\n"})
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL
    assert MESSAGE in output and "tests/test_x.py" in output


def test_a_new_test_file_with_a_skip_fails(branch_import):
    clone, _ = branch_import
    commit(clone, {"projects/proj/tests/test_new.py": SKIP_TEST})
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL
    assert MESSAGE in output and "tests/test_new.py" in output


def test_deleting_an_imported_test_fails(branch_import):
    clone, _ = branch_import
    commit(clone, {"projects/proj/tests/test_x.py": None})
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL
    assert MESSAGE in output and "tests/test_x.py" in output


@pytest.mark.parametrize("identity", [PROJECT, OPERATOR])
def test_a_promote_toml_added_after_the_base_is_not_trusted(tmp_path, identity):
    """The session cannot move the baseline by committing its own `promote.toml`: behaviour is as before."""
    origin, clone = make_clone(tmp_path)
    commit(clone, {"projects/proj/NOTES.md": "n\n"}, email=OPERATOR, message="project created")
    record_base(clone)
    commit(clone, prefixed(FILES), email=identity, message="import, later")
    project_work(clone)
    assert _start(clone).import_commit is None
    rows = judge(clone)
    if identity == PROJECT:
        status, output = rows[_standards.FAIL_TO_PASS]
        assert status is _gate_base.Status.FAIL and MESSAGE in output and "tests/test_x.py" in output
    else:
        assert rows["attribution"][0] is _gate_base.Status.FAIL
    assert "+ import" not in rows.get("scope", (None, ""))[1]


def test_an_import_already_on_origin_main_changes_nothing(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(origin, prefixed(FILES), email=OPERATOR, message="import acme/widgets")
    git(clone, "pull", "-q", "--ff-only", "origin", "main")
    record_base(clone)
    project_work(clone)
    assert _start(clone).import_commit is None
    rows = judge(clone)
    assert MESSAGE not in rows[_standards.FAIL_TO_PASS][1]
    assert rows[_standards.FAIL_TO_PASS][0] is _gate_base.Status.PASS
    assert "+ import" not in rows["scope"][1]


def test_the_scope_record_names_what_it_measured_from(branch_import):
    clone, imported = branch_import
    upstream = git(clone, "merge-base", "HEAD", "origin/HEAD")
    status, output = judge(clone)["scope"]
    assert status is _gate_base.Status.PASS
    measured = f"measured from {upstream[:12]} (merge-base with origin/HEAD)"
    assert f"{measured} + import {imported[:12]} of projects/proj" in output


def test_the_scope_record_names_the_merge_base_alone_without_an_import(tmp_path):
    origin, clone = make_clone(tmp_path)
    record_base(clone)
    commit(clone, {"projects/proj/a.py": "x = 1\n"})
    upstream = git(clone, "merge-base", "HEAD", "origin/HEAD")
    assert f"measured from {upstream[:12]} (merge-base with origin/HEAD)" in judge(clone)["scope"][1]
    assert "+ import" not in judge(clone)["scope"][1]


def test_a_later_change_to_an_imported_checks_script_is_a_harness_change(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(clone, prefixed(FILES | {"checks.sh": "true\n"}), email=OPERATOR, message="import")
    record_base(clone)
    base = _gates.resolve_base(clone, "proj")
    assert _gates.changed_test_harness(clone, "proj", base) == []
    commit(clone, {"projects/proj/checks.sh": "exit 0\n"})
    assert _gates.changed_test_harness(clone, "proj", base) == ["projects/proj/checks.sh"]


def test_the_start_tree_for_the_language_rules_holds_the_imported_folder(branch_import):
    clone, imported = branch_import
    start = _start(clone)
    assert start.import_commit == imported
    with _gate_start._start_tree(clone, start.upstream, None, start) as built:
        folder = _gate_start.make_start_folder(built.path, "projects/proj")
        assert (folder / "tests/test_x.py").read_text() == SKIP_TEST
        assert (folder / "src/lib.py").read_text() == FILES["src/lib.py"]
        assert not (folder / "tests/test_two.py").exists()
        assert (built.path / "README.md").exists()


@pytest.mark.parametrize("profile", ["example.toml", "oss-example.toml"])
def test_the_verdict_is_the_same_under_both_deployment_profiles(branch_import, monkeypatch, profile):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(ROOT / "profiles" / profile))
    clone, _ = branch_import
    rows = judge(clone)
    assert {rule: status for rule, (status, _) in rows.items()} == {
        "attribution": _gate_base.Status.PASS,
        "scope": _gate_base.Status.PASS,
        _standards.FAIL_TO_PASS: _gate_base.Status.PASS,
    }


def test_run_records_the_measured_base(branch_import):
    clone, imported = branch_import
    _gates.run(clone, "proj", "May edit: projects/proj/", "", "DONE", {}, _timeline._Timeline(), "workspace")
    recorded = json.loads((clone / ".claude-flyte/proj/standards-results.json").read_text())
    scope = next(r for r in recorded if r["rule"] == "scope")
    assert f"+ import {imported[:12]} of projects/proj" in scope["output"]


def test_what_main_put_in_the_project_folder_stays_in_the_start(branch_import):
    clone, _ = branch_import
    origin = clone.parent / "origin"
    ops = {"projects/proj/tests/test_ops.py": SKIP_TEST.replace("test_auth", "test_ops")}
    commit(origin, ops, email=OPERATOR, message="operator adds a test to the folder on main")
    git(clone, "fetch", "-q", "origin")
    git(clone, "merge", "-q", "--no-edit", "origin/main", email=PROJECT)
    assert _start(clone).import_commit is not None
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert MESSAGE not in output, output
    assert status is _gate_base.Status.PASS, output


def test_a_rewritten_base_commit_file_cannot_make_a_later_commit_the_import(tmp_path, monkeypatch):
    monkeypatch.setattr(_gates, "_CAPTURED_BASES", {})
    origin, clone = make_clone(tmp_path)
    commit(clone, {"projects/proj/NOTES.md": "n\n"}, email=OPERATOR, message="project created")
    (clone / ".claude-flyte/proj").mkdir(parents=True)
    _gates.capture_base(clone, "proj")
    commit(clone, prefixed(FILES), message="a copy the session made")
    project_work(clone)
    (clone / ".claude-flyte/proj/base-commit").write_text(git(clone, "rev-parse", "HEAD") + "\n")
    measured = _gate_import.find_workspace_start(clone, "projects/proj", _gates._captured_base(clone, "proj"))
    assert measured is not None and measured.import_commit is None
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL and MESSAGE in output and "tests/test_x.py" in output


def test_an_import_an_agent_identity_authored_is_not_trusted(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(clone, prefixed(FILES), email=PROJECT, message="import, as the project")
    record_base(clone)
    project_work(clone)
    assert _start(clone).import_commit is None
    assert "+ import" not in judge(clone)["scope"][1]


def test_after_a_restart_a_rewritten_base_file_cannot_make_an_operator_looking_copy_the_import(
    tmp_path, monkeypatch
):
    origin, clone = make_clone(tmp_path)
    record_base(clone)
    commit(clone, prefixed(FILES), email=OPERATOR, message="a copy the session made, under an operator mail")
    project_work(clone)
    monkeypatch.setattr(_gates, "_CAPTURED_BASES", {})  # a supervisor restart: only the file is left
    (clone / ".claude-flyte/proj/base-commit").write_text(git(clone, "rev-parse", "HEAD") + "\n")
    assert _start(clone).import_commit is None
    rows = judge(clone)
    status, output = rows[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL and MESSAGE in output and "tests/test_x.py" in output
    assert "import not used" in rows["scope"][1]
