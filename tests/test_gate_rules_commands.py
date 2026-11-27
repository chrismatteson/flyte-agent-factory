"""The gate runs a rule's command with the project's own tools and, for tsc, a baseline config."""

import json
import shutil
from pathlib import Path

from flyte_agent_factory import _gate_rules


def test_tsc_without_a_tsconfig_checks_the_projects_ts_files_with_the_baseline(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"strict": True}, "include": ["*"]})
    )
    project = tmp_path / "project"
    (project / "node_modules/dep").mkdir(parents=True)
    (project / "a.ts").write_text("const a: number = 1;\n")
    (project / "node_modules/dep/b.ts").write_text("")
    with _gate_rules.prepare_command(["tsc", "--noEmit"], project, configs, None) as (command, _, _):
        assert command[:3] == ["tsc", "--noEmit", "--project"]
        baseline = Path(command[3])
        assert baseline.parent == project
        config = json.loads(baseline.read_text())
    assert config == {"compilerOptions": {"strict": True}, "files": [str(project / "a.ts")]}
    assert not baseline.exists()


def test_tsc_with_its_own_tsconfig_runs_unchanged(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    with _gate_rules.prepare_command(["tsc", "--noEmit"], tmp_path, tmp_path, None) as (command, _, _):
        assert command == ["tsc", "--noEmit"]


def test_a_project_tool_replaces_the_images_and_pyright_replaces_mypy(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    assert _gate_rules.resolve_command(["python", "-m", "pytest", "-q"], tmp_path) == (
        ["pytest", "-q"],
        "pytest",
    )
    (tmp_path / "node_modules/.bin").mkdir(parents=True)
    (tmp_path / "node_modules/.bin/eslint").write_text("")
    assert _gate_rules.resolve_command(["eslint", "."], tmp_path) == (
        [str(tmp_path / "node_modules/.bin/eslint"), "."],
        "eslint",
    )
    (tmp_path / "pyrightconfig.json").write_text("{}")
    assert _gate_rules.resolve_command(["mypy", "src"], tmp_path) == (["pyright"], "pyright")
