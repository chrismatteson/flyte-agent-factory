"""A workspace carried as one tar inside a `Dir`, so object storage cannot drop its POSIX modes.

Object storage uploads file by file and keeps bytes, not modes: a workspace shipped as plain files
comes back with `.venv/bin/python` and every script non-executable. One tar keeps the modes, and the
upload stays a single native `Dir` upload. Used by the project's published workspace (`_state`), the
factory's recreate path and the sub-agent snapshots (`_checkpoint`).
"""

from __future__ import annotations

import asyncio
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flyte.io import Dir

WORKSPACE_ARCHIVE = "agent-factory-workspace.tar"
#: What an engine from before the rename wrote; still restored, for a workspace uploaded by one still running.
LEGACY_WORKSPACE_ARCHIVE = "claude-flyte-workspace.tar"

Filter = Callable[[tarfile.TarInfo], tarfile.TarInfo | None]


def pack(workspace: Path, target: Path, member_filter: Filter | None = None) -> None:
    """Write `workspace` into `target / WORKSPACE_ARCHIVE`, modes included."""
    # Git's index alone cannot restore executable bits on uncommitted files or preserve mode-only changes.
    with tarfile.open(target / WORKSPACE_ARCHIVE, "w") as archive:
        archive.add(workspace, arcname=".", filter=member_filter)


async def upload(workspace: Path, member_filter: Filter | None = None) -> Dir:
    """One native `Dir` upload holding the workspace as a single tar."""
    from flyte.io import Dir

    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging)
        await asyncio.to_thread(pack, workspace, staged, member_filter)
        return await Dir.from_local(str(staged))


async def download(workdir: Dir, workspace: Path) -> None:
    """One native `Dir` download, unpacked into `workspace`.

    Accepts a plain directory too: older snapshots and published workspaces were file by file.
    """
    await workdir.download(str(workspace))
    archive_path = next(
        (p for p in (workspace / WORKSPACE_ARCHIVE, workspace / LEGACY_WORKSPACE_ARCHIVE) if p.is_file()),
        None,
    )
    if archive_path is not None:

        def unpack() -> None:
            with tarfile.open(archive_path, "r:") as archive:
                # "tar", not "data": a workspace .venv links to the image's interpreter by absolute
                # path, which "data" refuses. "tar" still keeps members inside the workspace.
                archive.extractall(workspace, filter="tar")
            archive_path.unlink()

        await asyncio.to_thread(unpack)
