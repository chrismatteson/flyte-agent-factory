"""A workspace launch is judged in the folder its brief names, or refused at launch: never an empty folder.

Only entry points the engine had before routing existed (`_prepare_workspace`, `_project_dir`, `_checks_rel`),
so the old behaviour (judging the empty `projects/<launch name>/`) fails here rather than failing to import.
"""

import asyncio

import flyte.errors
import pytest

from flyte_agent_factory import _session_config, _session_setup, _timeline


@pytest.fixture
def launch(tmp_path, monkeypatch):
    """Launch `fix-x` into a workspace that holds the imported `projects/engine/` and no `projects/fix-x/`."""
    (tmp_path / "projects/engine/src").mkdir(parents=True)
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "_project_name", "fix-x")
    monkeypatch.setattr(_session_config, "_project_folder", "", raising=False)  # old code has none
    monkeypatch.setattr(_session_config, "_layout", lambda: "workspace")

    def prepare(brief):
        asyncio.run(
            _session_setup._prepare_workspace(
                brief=brief, repo="", branch="", resuming=False, timeline=_timeline._Timeline()
            )
        )

    return prepare


def test_a_launch_is_judged_in_the_folder_its_brief_names(launch, tmp_path):
    launch("Fix double in the engine.\nProject folder: projects/engine/\nMay edit: projects/engine/**\n")
    assert _session_config._project_dir() == tmp_path / "projects/engine"
    assert _session_config._checks_rel("fix-x", "workspace") == "projects/engine/checks.sh"


def test_a_launch_that_would_be_judged_in_an_empty_folder_is_refused_at_launch(launch):
    with pytest.raises(
        flyte.errors.NonRecoverableError, match=r"projects/fix-x/ does not exist.*projects/engine/"
    ):
        launch("Fix double in the engine.\nMay edit: projects/engine/**\n")


def test_a_launch_without_a_folder_line_is_still_judged_in_its_own_folder(launch, tmp_path):
    (tmp_path / "projects/fix-x").mkdir()
    launch("Fix it.\nMay edit: projects/fix-x/**\n")
    assert _session_config._project_dir() == tmp_path / "projects/fix-x"
