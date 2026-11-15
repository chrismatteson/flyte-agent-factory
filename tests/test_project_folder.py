"""A launch's name is its identity; the `projects/<folder>/` its files are judged in is chosen by the launch.

`fix-x` working in the imported `projects/engine/` used to be judged against an empty `projects/fix-x/`.
"""

import flyte.errors
import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _gates,
    _instructions,
    _project_folder,
    _session_config,
)
from tests.test_workspace_layout import OTHER, git, make_origin

pytestmark = pytest.mark.real_factory_gates

CALC = "def double(x):\n    return x\n"
FIXED = "def double(x):\n    return x * 2\n"
TEST = "from calc import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
ROUTED = "Fix double in the engine.\nProject folder: projects/engine/\n"


@pytest.fixture
def engine_work(tmp_path):
    """A workspace whose imported `projects/engine/` has a fix committed by launch `fix-x`."""
    clone = make_origin(tmp_path, ["projects/.gitkeep"])
    (clone / "projects/engine/src").mkdir(parents=True)
    (clone / "projects/engine/src/calc.py").write_text(CALC)
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "import engine")
    _gates.capture_base(clone, "fix-x")
    (clone / "projects/engine/src/calc.py").write_text(FIXED)
    (clone / "projects/engine/tests").mkdir()
    (clone / "projects/engine/tests/test_calc.py").write_text(TEST)
    git(clone, "add", "-A")
    git(clone, "-c", f"user.email={_gates.commit_identity('fix-x')}", "commit", "-m", "fix double")
    return clone


def judge(workspace, brief="", folder=""):
    """Run the project judgement for launch `fix-x`; return the recorded (status, output) by rule."""
    recorded = {}

    def record(rule, status, output):
        recorded[rule] = (status, output)

    base = _gates.resolve_base(workspace, "fix-x")
    kwargs = {"folder": folder} if folder else {}
    _gates._judge_project(
        workspace, "fix-x", base, brief, "", "DONE", [], record, "workspace", None, ["R1"], **kwargs
    )
    return recorded


# --- the reproduction ------------------------------------------------------------------------


def test_without_a_folder_the_gate_judges_the_launch_name_and_blocks_the_edits(engine_work):
    recorded = judge(engine_work)
    status, output = recorded["scope"]
    assert status is _gate_base.Status.FAIL and "projects/engine/src/calc.py" in output
    assert recorded["factory:fail-to-pass"][0].blocks


def test_a_routed_launch_is_judged_in_its_folder(engine_work):
    recorded = judge(engine_work, ROUTED, folder=_project_folder.read_folder_from_brief(ROUTED))
    assert recorded["scope"][0] is _gate_base.Status.PASS
    status, output = recorded["factory:fail-to-pass"]
    assert status is _gate_base.Status.PASS, output
    assert "head tests pass; base tests fail" in output


def test_a_routed_launch_has_its_script_and_directory_in_the_folder(engine_work, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", engine_work)
    monkeypatch.setattr(_session_config, "_project_name", "fix-x")
    monkeypatch.setattr(_session_config, "_project_repo", OTHER)
    monkeypatch.setattr(_session_config, "_project_folder", "engine")
    assert _session_config._checks_rel("fix-x", "workspace") == "projects/engine/checks.sh"
    assert _gate_base.checks_rel("fix-x", "workspace", "engine") == "projects/engine/checks.sh"
    assert _session_config._project_dir() == engine_work / "projects/engine"
    memory = _instructions._project_memory(False, name="fix-x", layout="workspace", folder="engine")
    assert "`projects/engine/checks.sh`" in memory and "projects/fix-x" not in memory


def test_uncommitted_work_is_looked_for_in_the_folder(engine_work):
    (engine_work / "projects/engine/new.py").write_text("x\n")
    assert _gates._uncommitted(engine_work, "fix-x", "workspace") == []
    assert _gates._uncommitted(engine_work, "fix-x", "workspace", "engine") == ["projects/engine/new.py"]


def test_the_folders_promote_toml_stays_protected():
    manifest = ["projects/engine/promote.toml"]
    assert _gate_scope.scope_violations(manifest, "fix-x", ROUTED, None, "workspace", "engine") == manifest
    # The launch name's own folder is not the project's once routed.
    other = ["projects/fix-x/a.py"]
    assert _gate_scope.scope_violations(other, "fix-x", ROUTED, None, "workspace", "engine") == other


# --- the name keeps its meaning ----------------------------------------------------------------


def test_name_keyed_state_does_not_follow_the_folder(engine_work):
    judge(engine_work, ROUTED, folder="engine")
    assert (engine_work / ".claude-flyte/fix-x/base-commit").is_file()
    assert not (engine_work / ".claude-flyte/engine").exists()
    assert _gates.commit_identity("fix-x") == f"fix-x@{_gates.COMMIT_DOMAIN}"
    assert _session_config._acceptance_rel("fix-x") == ".claude-flyte/fix-x/ACCEPTANCE.md"


# --- where the folder may come from --------------------------------------------------------------


@pytest.mark.parametrize("line", ["projects/engine", "projects/engine/", "`projects/engine/`"])
def test_the_brief_line_takes_the_folder_with_or_without_the_trailing_slash(line):
    assert _project_folder.read_folder_from_brief(f"x\nproject FOLDER: {line}\n") == "engine"


def test_the_first_brief_line_wins():
    assert (
        _project_folder.read_folder_from_brief("Project folder: projects/a/\nProject folder: projects/b/\n")
        == "a"
    )


class _Rows:
    def __init__(self):
        self.rows = []

    def row(self, *row):
        self.rows.append(row)


@pytest.fixture
def launched(engine_work, monkeypatch):
    """The session state of a workspace launch `fix-x` once its clone exists (`_prepare_workspace`)."""
    monkeypatch.setattr(_session_config, "WORKSPACE", engine_work)
    monkeypatch.setattr(_session_config, "_project_name", "fix-x")
    monkeypatch.setattr(_session_config, "_layout", lambda: "workspace")
    return _Rows()


def test_the_launch_is_routed_by_its_brief_line(launched):
    assert _project_folder.route("fix-x", ROUTED, launched) == "engine"
    assert _project_folder.route("fix-x", "Fix it.\n", launched) == ""
    assert launched.rows == []


@pytest.mark.parametrize(
    "brief",
    [
        "Fix it.\nMay edit: projects/engine/src\n",  # ambiguous
        "Fix it.\nProject folder: projects/engin/\n",  # missing from the workspace
        "Fix it.\nProject folder: TBD\n",  # not a folder at all
    ],
)
def test_a_launch_that_cannot_be_routed_is_refused_for_good_and_says_why(launched, brief):
    with pytest.raises(flyte.errors.NonRecoverableError) as refused:
        _project_folder.route("fix-x", brief, launched)
    assert [row[1] for row in launched.rows] == [str(refused.value)]


def test_outside_a_workspace_the_launch_is_never_refused_for_its_folder(launched, monkeypatch):
    monkeypatch.setattr(_session_config, "_layout", lambda: "root")
    assert _project_folder.route("fix-x", "May edit: projects/engine/src\n", launched) == ""


def test_outside_a_workspace_the_folder_line_is_not_read(launched, monkeypatch):
    monkeypatch.setattr(_session_config, "_layout", lambda: "root")
    assert _project_folder.route("fix-x", "Project folder: src/\n", launched) == ""
    assert launched.rows == []


@pytest.mark.parametrize(
    "bad", ["/abs", "..", ".", "a/b", "../x", "projects/engine", "a/..", "a/.", "./a", "a/", "a\\b"]
)
def test_an_unsafe_folder_is_refused_naming_the_value(bad):
    with pytest.raises(ValueError, match="project folder"):
        _project_folder.validate(bad)


@pytest.mark.parametrize(
    "bad",
    [
        *["/abs", "projects/..", "projects/.", "projects/a/b", "engine", "../x"],
        *["projects/a/.", "projects/./a/", "projects/a//"],  # other spellings of a folder
    ],
)
def test_an_unsafe_brief_line_is_refused_naming_the_value(bad):
    with pytest.raises(ValueError, match="project folder"):
        _project_folder.read_folder_from_brief(f"Project folder: {bad}\n")


def test_the_working_tree_brief_cannot_change_the_folder(launched):
    """A `Project folder:` line the project writes into its own files does not route its launch."""
    harness = _session_config.WORKSPACE / ".claude-flyte"
    harness.mkdir(exist_ok=True)
    (harness / "BRIEF.md").write_text(ROUTED)
    (_session_config.WORKSPACE / "projects/engine/BRIEF.md").write_text(ROUTED)
    folder = _project_folder.route("fix-x", "Fix double.\n", launched)  # the brief passed at launch
    assert folder == ""
    status, output = judge(_session_config.WORKSPACE, "Fix double.\n", folder=folder)["scope"]
    assert status is _gate_base.Status.FAIL and "projects/engine/src/calc.py" in output, output


# --- the ambiguous launch ------------------------------------------------------------------------


def test_a_launch_naming_another_existing_folder_is_refused(engine_work):
    message = _project_folder.explain_ambiguity(
        engine_work, "fix-x", "Fix it.\nMay edit: projects/engine/src\n"
    )
    assert "projects/fix-x/" in message and "projects/engine/" in message
    assert "Project folder: projects/engine/" in message and "Project folder: projects/fix-x/" in message


def test_the_default_is_unchanged_when_nothing_is_ambiguous(engine_work):
    brief = "Fix it.\nMay edit: projects/engine/src\n"
    assert _project_folder.explain_ambiguity(engine_work, "engine", brief) == ""  # the name's folder exists
    assert _project_folder.explain_ambiguity(engine_work, "fix-x", "Fix it.\n") == ""  # no other folder named
    assert _project_folder.explain_ambiguity(engine_work, "fix-x", "May edit: projects/missing/src\n") == ""
    assert _project_folder.explain_ambiguity(engine_work, "fix-x", "May edit: docs/a.md\n") == ""
    assert _project_folder.explain_ambiguity(engine_work, "fix-x", "May edit: ../projects/engine\n") == ""


@pytest.mark.parametrize(
    "line",
    [
        "**Project folder:** projects/engine/",
        "- Project folder: projects/engine/",
        "1. Project folder: projects/engine/",
        "Project folder: projects/engine/ (the imported engine)",
    ],
)
def test_the_brief_line_may_be_a_list_item_bold_or_followed_by_a_remark(line):
    assert _project_folder.read_folder_from_brief(f"x\n{line}\n") == "engine"


def test_a_routed_folder_missing_from_the_workspace_is_refused(engine_work):
    message = _project_folder.explain_launch_refusal(engine_work, "fix-x", "engin", "")
    assert "projects/engin/" in message and "does not exist" in message


def test_a_routed_folder_that_exists_or_is_the_name_may_run(engine_work):
    assert _project_folder.explain_launch_refusal(engine_work, "fix-x", "engine", "") == ""
    assert _project_folder.explain_launch_refusal(engine_work, "fix-x", "fix-x", "") == ""  # a new project


def test_without_a_routed_folder_the_launch_check_is_the_ambiguity_check(engine_work):
    brief = "May edit: projects/engine/**\n"
    assert _project_folder.explain_launch_refusal(
        engine_work, "fix-x", "", brief
    ) == _project_folder.explain_ambiguity(engine_work, "fix-x", brief)
    assert _project_folder.explain_launch_refusal(engine_work, "fix-x", "", brief)
