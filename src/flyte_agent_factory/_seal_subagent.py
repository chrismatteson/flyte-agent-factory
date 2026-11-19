"""The sub-agent's side of a sealed run: seal each engine attempt, and return the record with the result.

`agent.subagent` calls `SubagentSeal.open` where it starts an engine, and `result_dir` where it returns its
workspace. The role is registered by the caller before the call (`_seal_role.register_subagent`); here it
is finished and bundled into the returned `Dir` under `.seal-record/`, which the caller merges.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

from flyte.io import Dir

from . import _auth, _seal_guard, _seal_record, _seal_role, _session_config
from ._seal_spec import SealSpec


def check_call(seal: str) -> SealSpec | None:
    """Return the seal of this call, or None when it is unsealed.

    A process of a sealed project (`_session_config._seal` is set only there) must never start an unsealed
    sub-agent: a forgotten argument would otherwise be a silent hole.
    """
    if seal:
        return SealSpec.from_json(seal)
    if _session_config._seal is not None:
        raise RuntimeError("refusing an unsealed sub-agent call from a sealed project")
    return None


class SubagentSeal:
    """Seals each engine attempt of one sub-agent call.

    A second attempt (the engine moved after a usage limit) is its own role, `<id>-a1`, so every engine
    that touched the work has a transcript.
    """

    def __init__(self, spec: SealSpec, role_id: str, base: Path, work: Path) -> None:
        self._spec = spec
        self._role_id = role_id or _seal_record.new_role_id("subagent")
        # The caller made the id from its kind; a direct call has none and is a plain sub-agent.
        kind = self._role_id.split("-", 1)[0]
        self._kind = kind if kind in _seal_record.KINDS else "subagent"
        self._base = base
        self._record = base / "seal-record"
        self._work = work
        self._stack = contextlib.ExitStack()
        self._ids: list[str] = []
        self.role: _seal_role.SealedRole | None = None
        self.exit_code: int | None = None

    def config_dir(self, name: str) -> Path:
        """Return the engine config dir of the next attempt: its own, so a transcript is in one role only."""
        return self._base / (name if not self._ids else f"{name}-a{len(self._ids)}")

    def open(self, engine: str, config_dir: Path) -> _seal_role.SealedRole:
        """Seal an attempt on `engine`; `config_dir` is where its transcripts are written."""
        # An attempt still open here ended without a result (the engine refused), so it failed.
        self.close(failed=True)
        role_id = self._role_id if not self._ids else f"{self._role_id}-a{len(self._ids)}"
        self._ids.append(role_id)
        self.role = self._stack.enter_context(
            _seal_role.sealed_role(
                self._spec,
                self._record,
                role_id,
                self._kind,
                engine,
                workdir_roots=[self._work],
                config_dir=config_dir,
            )
        )
        return self.role

    def close(self, failed: bool = False) -> None:
        """Finish the open attempt's record (idempotent)."""
        if self.role is None:
            return
        self.role.exit_code = 1 if failed and self.exit_code is None else self.exit_code
        self.role = None
        self._stack.close()

    async def result_dir(self, *, whole: bool = False) -> Dir:
        """Finish the record, bundle every attempt into the workspace and upload it.

        `whole` also bundles the manifest, so `.seal-record/` is a record root the audit reads as it is: for a
        call nobody merges into a project's record (the plain arm). A sub-agent's caller merges the roles into
        its own manifest instead.
        """
        self.close()
        for role_id in self._ids:
            _seal_record.bundle(self._record, role_id, self._work)
        if whole:
            bundled = self._work / _seal_record.BUNDLE_DIR
            shutil.copy2(self._record / "manifest.json", bundled / "manifest.json")
        return await Dir.from_local(str(self._work))


def claude_settings(role: _seal_role.SealedRole) -> dict[str, Any]:
    """Return the `extra` for `write_settings` of a sealed Claude sub-agent.

    `write_settings` replaces `permissions` with what `extra` has, so the harness's own deny rules are
    merged in here rather than lost to the guard's web-tool rules.
    """
    return _seal_guard.merge_settings(
        {"permissions": {"deny": list(_auth.DENIED_TOOLS)}}, role.settings_extra
    )


async def result_dir(sealed: SubagentSeal | None, work: Path) -> Dir:
    """`Dir.from_local(work)`, with the record bundled in first when the call is sealed."""
    return await (sealed.result_dir() if sealed else Dir.from_local(str(work)))
