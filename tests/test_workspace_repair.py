"""A workspace keeps its POSIX modes across runs, and a restored one is repaired when it did not."""

import os
import shutil
import stat
import subprocess
import sys
import tarfile
import venv
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _factory_recreate,
    _gate_base,
    _state,
    _timeline,
    _workspace_archive,
    _workspace_repair,
)

EXEC = 0o111


class _StoredDir:
    """What object storage hands back: the file bytes, never the modes."""

    downloads = 0

    def __init__(self, root: Path, path: str = "s3://bucket/run"):
        self.root = root
        self.path = path

    async def download(self, target):
        type(self).downloads += 1
        shutil.copytree(self.root, target, copy_function=shutil.copyfile, dirs_exist_ok=True)
        for file in Path(target).rglob("*"):
            if file.is_file() and not file.is_symlink():
                file.chmod(0o644)


@pytest.fixture
def object_store(tmp_path, monkeypatch):
    from flyte.io import Dir

    uploads: list[Path] = []
    _StoredDir.downloads = 0

    async def from_local(path):
        dest = tmp_path / f"object-{len(uploads)}"
        uploads.append(dest)
        shutil.copytree(path, dest, copy_function=shutil.copyfile)
        return _StoredDir(dest)

    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(_state, "_checkpoint", lambda: None)
    return uploads


def _workspace_with_script(tmp_path: Path) -> Path:
    workspace = tmp_path / "published"
    (workspace / ".venv/bin").mkdir(parents=True)
    (workspace / "run.sh").write_text("#!/bin/sh\n")
    (workspace / "run.sh").chmod(0o755)
    (workspace / ".venv/bin/python").write_text("#!/bin/sh\n")
    (workspace / ".venv/bin/python").chmod(0o755)
    (workspace / ".venv/bin/python3").symlink_to("/usr/bin/env")  # absolute, like the image's interpreter
    return workspace


async def test_publish_then_restore_keeps_modes_with_one_upload_and_one_download(tmp_path, object_store):
    published = _state.ProjectState(root=tmp_path, resumed_from="")
    shutil.copytree(_workspace_with_script(tmp_path), published.workspace, symlinks=True)

    dir_ = await _state.publish(published)
    assert len(object_store) == 1
    assert [p.name for p in object_store[0].iterdir()] == [_workspace_archive.WORKSPACE_ARCHIVE]

    restored = await _state.restore(root=tmp_path / "next", workspace_in=dir_)
    assert _StoredDir.downloads == 1
    assert (restored.workspace / "run.sh").stat().st_mode & EXEC
    assert (restored.workspace / ".venv/bin/python").stat().st_mode & EXEC
    assert os.readlink(restored.workspace / ".venv/bin/python3") == "/usr/bin/env"
    assert not (restored.workspace / _workspace_archive.WORKSPACE_ARCHIVE).exists()


async def test_restore_still_accepts_a_workspace_published_file_by_file(tmp_path, object_store):
    old = tmp_path / "old"
    (old / "src").mkdir(parents=True)
    (old / "src/app.py").write_text("x = 1\n")

    restored = await _state.restore(root=tmp_path / "next", workspace_in=_StoredDir(old))  # type: ignore[arg-type]

    assert (restored.workspace / "src/app.py").read_text() == "x = 1\n"
    assert _StoredDir.downloads == 1


def test_a_loaded_checkpoint_is_republished_with_its_modes(tmp_path, monkeypatch):
    import flyte
    import flyte.io

    tree = tmp_path / "tree"
    shutil.copytree(_workspace_with_script(tmp_path), tree / "workspace", symlinks=True)
    sent: list[list[str]] = []
    published = object()

    class Checkpoint:
        def __init__(self, **kwargs):
            pass

        def load_sync(self):
            return tree

    def from_local(path):  # syncified in the SDK: it blocks
        sent.append([p.name for p in Path(path).iterdir()])
        with tarfile.open(Path(path) / _workspace_archive.WORKSPACE_ARCHIVE) as archive:
            assert archive.getmember("./run.sh").mode & EXEC
        return published

    monkeypatch.setattr(flyte, "Checkpoint", Checkpoint)
    monkeypatch.setattr(flyte.io.Dir, "from_local", from_local)

    assert _factory_recreate._load_checkpoint_workspace("s3://x") is published
    assert sent == [[_workspace_archive.WORKSPACE_ARCHIVE]]


def _strip_exec(root: Path) -> None:
    for file in root.rglob("*"):
        if file.is_file() and not file.is_symlink():
            file.chmod(file.stat().st_mode & ~EXEC)


async def test_repair_restores_exec_bits_on_the_venv_and_on_files_git_records_as_executable(tmp_path):
    workspace = tmp_path / "ws"
    venv.create(workspace / ".venv", with_pip=False, symlinks=False)
    (workspace / "tool.sh").write_text("#!/bin/sh\nexit 0\n")
    (workspace / "tool.sh").chmod(0o755)
    (workspace / "plain.txt").write_text("not a script\n")
    for args in (["init", "-q"], ["add", "tool.sh", "plain.txt"]):
        subprocess.run(["git", *args], cwd=workspace, check=True, timeout=30)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        cwd=workspace,
        check=True,
        timeout=30,
    )
    _strip_exec(workspace)
    python = workspace / ".venv/bin/python"
    assert _gate_base._run([str(python), "-c", ""], workspace)[0] == 126

    timeline = _timeline._Timeline()
    await _workspace_repair.repair_workspace(workspace, timeline)

    assert _gate_base._run([str(python), "-c", "print(1)"], workspace) == (0, "1\n")
    assert (workspace / "tool.sh").stat().st_mode & stat.S_IXUSR
    assert not (workspace / "plain.txt").stat().st_mode & stat.S_IXUSR
    rows = [text for _, _, text in timeline._rows]
    assert len(rows) == 1 and rows[0].startswith("restored exec bits on ")


async def test_an_environment_that_stays_unusable_is_reported_without_raising(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".venv/bin").mkdir(parents=True)
    broken = workspace / ".venv/bin/python"
    broken.write_text("not an executable\n")  # chmod +x makes it a script the kernel cannot run
    broken.chmod(0o644)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    timeline = _timeline._Timeline()

    await _workspace_repair.repair_workspace(workspace, timeline)

    rows = [(icon, text) for _, icon, text in timeline._rows]
    assert any(icon == "🔧" for icon, _ in rows)  # the bit was restored, but the file is not a program
    assert any(
        icon == "⚠️"
        and text.startswith("project environment unusable: .venv/bin/python")
        and "uv sync" in text
        for icon, text in rows
    ), rows


async def test_a_venv_whose_interpreter_was_lost_is_reported(tmp_path, monkeypatch):
    # A per-file upload that skips symlinks restores `.venv/bin` without `python`.
    workspace = tmp_path / "ws"
    (workspace / ".venv/bin").mkdir(parents=True)
    (workspace / ".venv/bin/pytest").write_text("#!/ws/.venv/bin/python\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    timeline = _timeline._Timeline()

    await _workspace_repair.repair_workspace(workspace, timeline)

    assert any(
        icon == "⚠️" and text.startswith("project environment unusable: .venv/bin/python")
        for _, icon, text in timeline._rows
    ), timeline._rows


async def test_a_healthy_venv_is_restored_without_a_repair_row(tmp_path):
    workspace = tmp_path / "ws"
    venv.create(workspace / ".venv", with_pip=False, symlinks=True)
    for name in ("activate", "activate.fish", "activate_this.py"):
        (workspace / ".venv/bin" / name).write_text("# sourced, not run\n")
        (workspace / ".venv/bin" / name).chmod(0o644)
    timeline = _timeline._Timeline()

    await _workspace_repair.repair_workspace(workspace, timeline)

    assert timeline._rows == []
    assert not (workspace / ".venv/bin/activate").stat().st_mode & stat.S_IXUSR


async def test_a_project_without_a_venv_reports_nothing(tmp_path):
    timeline = _timeline._Timeline()
    await _workspace_repair.repair_workspace(tmp_path, timeline)
    assert timeline._rows == []


def test_run_names_the_tool_it_could_not_start(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)
    code, message = _gate_base._run([str(tool)], tmp_path)
    assert code == 126
    assert str(tool) in message and "not executable" in message


def test_run_reports_an_unrunnable_binary_not_an_exception(tmp_path):
    tool = tmp_path / "tool"
    tool.write_bytes(b"\x7fELF-not-really")
    tool.chmod(0o755)
    code, message = _gate_base._run([str(tool)], tmp_path)
    assert code == 126 and str(tool) in message and "could not start" in message


def test_the_helper_module_is_the_only_place_that_builds_the_archive():
    # Not duplicated: `_checkpoint` and `_state` both go through the one module.
    source = Path(sys.modules["flyte_agent_factory._state"].__file__ or "").read_text()
    assert "tarfile" not in source
