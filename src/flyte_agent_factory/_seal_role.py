"""Run one role (author, sub-agent, plain arm) sealed: proxy, guard, launcher and record in one place.

`sealed_role` is the one thing every sealed launch goes through. It starts the role's proxy outside the
role, hands back the launcher argv, the environment and the guard settings to start the engine with, and
on exit copies the engine's transcripts into the record and writes `end.json`. The role's caller owns the
rest (how the engine is started, what it is told); this module only decides what the role can reach and
what is kept.

Also here: the caller's side of the record for a sub-agent (`register_subagent`, `merge_result`), and
`close_author_record`, the hook the DONE path calls before review.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import tempfile
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _seal_guard, _seal_net, _seal_proxy, _seal_record
from ._seal_spec import SealSpec

#: The port the role reaches its proxy on. Each role has its own network namespace, so one number serves all.
PROXY_PORT = 8118
#: Where the proxy's push route is for a role: git's `pushInsteadOf` rewrites the workspace URL to this.
PUSH_ROUTE = f"http://127.0.0.1:{PROXY_PORT}/push/"
INDEX_URL = f"http://127.0.0.1:{PROXY_PORT}/index/simple/"
#: Turns off Codex's web search. `web_search` is Codex's own config key (`disabled`, `cached`, `indexed`,
#: `live`; checked against codex-cli 0.163: an unknown value is refused when the config loads), and it is
#: what `tools_off` means for a Codex role. Codex has no other tool that reaches the network by itself.
CODEX_TOOLS_OFF_ARGS = ("-c", 'web_search="disabled"')

#: The role's `/tmp`: a fresh tmpfs in its mount namespace, so it neither sees nor writes the harness's.
PRIVATE_TMP = "/tmp"

#: What a sub-agent's `purpose` (a judge name, `planner`, ...) is recorded as; anything else is `subagent`.
_KIND_FOR_PURPOSE = {"delegate": "delegate", "judgement": "judge", "arbiter": "reviewer"}
_REVIEWER_PURPOSES = ("correctness", "completeness", "craft")


@dataclass
class SealedRole:
    """What a caller needs to start an engine sealed. Set `exit_code` before leaving the `with` block."""

    role_id: str
    argv_prefix: list[str]
    env: dict[str, str]
    settings_extra: dict[str, Any]
    exit_code: int | None = None
    role_dir: Path = field(default_factory=Path)
    #: Where the guard hook writes while the role runs (writable to the role), and the record's copy of it.
    live_guard_log: Path | None = None
    record_guard_log: Path | None = None

    def sync_guard_log(self) -> None:
        """Copy the guard hook's log into the record. Only the harness, outside the seal, writes the record.

        A role that made no tool call has no live log, and keeps the empty `guard.jsonl` `register` made.
        A live log the role replaced with a link or anything but a regular file is not followed: the record
        gets a line saying so, which is not JSON, so the audit calls the record incomplete (void).
        """
        live, record = self.live_guard_log, self.record_guard_log
        if live is None or record is None or not os.path.lexists(live):
            return
        if not _seal_record.copy_regular(live, record, live.parent):
            record.write_text("guard log refused: the live log was not a regular file\n")

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """Return `argv` run through the launcher; a `claude` command also gets the web tools taken away."""
        tail = _seal_guard.cli_deny_args() if argv and Path(argv[0]).name == "claude" else []
        return [*self.argv_prefix, *argv, *tail]


def kind_for(purpose: str) -> str:
    """Return the record kind of a sub-agent called for `purpose`."""
    if purpose in _KIND_FOR_PURPOSE:
        return _KIND_FOR_PURPOSE[purpose]
    if purpose in _REVIEWER_PURPOSES or purpose.endswith("-check"):
        return "reviewer"
    return "subagent"


def seal_flags(spec: SealSpec, engine: str) -> dict[str, Any]:
    """Return the `seal` entry of a role's record: what protected it. `guard` is Claude's hook only."""
    return {
        "net": True,
        "guard": engine == "claude",
        "tools_off": True,
        "fs_readonly": True,
        "spec_sha256": spec.sha256(),
    }


def _cache_env(cache: Path) -> dict[str, str]:
    """Return the environment that points every package manager's cache into `cache` (made here).

    `$HOME` is read-only in the seal, so a tool that caches under it (`~/.cache`, `~/.npm`) would fail.
    """
    dirs = {
        "XDG_CACHE_HOME": "xdg",
        "UV_CACHE_DIR": "uv",
        "PIP_CACHE_DIR": "pip",
        "npm_config_cache": "npm",
    }
    for name in dirs.values():
        (cache / name).mkdir(parents=True, exist_ok=True)
    return {var: str(cache / name) for var, name in dirs.items()}


def _python_path() -> str:
    """Return the directory the launcher and the guard hook import this package from."""
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    return f"{package_parent}{os.pathsep}{existing}" if existing else package_parent


def _collect(root: Path, role_id: str, engine: str, config_dir: Path | None) -> None:
    role_dir = _seal_record.role_paths(root, role_id).dir
    if config_dir is None:
        return
    if engine == "codex":
        _seal_record.collect_codex(config_dir, role_dir)
    else:
        _seal_record.collect_claude(config_dir, role_dir)


def _check_record_apart(root: Path, workdir_roots: Sequence[Path | str]) -> None:
    """Refuse a record root that holds a workdir: read-only on the record would make the workdir read-only.

    A record root inside a workdir is fine (the tests do that): only that subtree becomes read-only.
    """
    record = root.resolve()
    for workdir in workdir_roots:
        work = Path(workdir).resolve()
        if work.is_relative_to(record):
            raise RuntimeError(
                f"the role's workdir {work} is inside the record root {record}; making the record "
                "read-only would make the workdir read-only"
            )


def _expand_forbidden(forbidden_paths: Sequence[str]) -> list[str]:
    """Return the existing paths the forbidden globs name, to hide in the seal.

    The guard still matches the globs themselves, so a path created later is denied and recorded even
    though it is not hidden.
    """
    found = sorted({p for pattern in forbidden_paths for p in glob.glob(pattern, recursive=True)})
    # Sorted, so a directory comes before what is below it; the tmpfs covers that, and a `**` over a big
    # tree must not become thousands of launcher arguments.
    kept: list[str] = []
    for path in found:
        if not any(os.path.isdir(d) and path.startswith(d.rstrip("/") + "/") for d in kept):
            kept.append(path)
    return kept


@contextlib.contextmanager
def sealed_role(
    spec: SealSpec,
    record_root: Path,
    role_id: str,
    kind: str,
    engine: str,
    *,
    workdir_roots: Sequence[Path | str],
    push_auth: Callable[[], str] | None = None,
    config_dir: Path | None = None,
    parent: str = "",
    action: str = "",
    extra_writable: Sequence[Path | str] = (),
) -> Iterator[SealedRole]:
    """Seal a role for the length of the `with` block, or raise `RuntimeError` when the seal is impossible.

    `config_dir` is Claude's `CLAUDE_CONFIG_DIR` or Codex's `CODEX_HOME`; its transcripts are copied into
    the record on exit, however the block ends. A role the caller registered before launching it (the
    usual case for a sub-agent) is not registered again. `extra_writable` adds paths the role may write
    besides its own (the author's bridge to the supervisor); like a workdir, none may hold the record.
    """
    if reason := _seal_net.unavailable_reason():
        raise RuntimeError(
            "this sealed run needs unprivileged user and network namespaces, which this pod does not "
            f"allow ({reason}); refusing to run the role unsealed"
        )
    root = Path(record_root)
    _check_record_apart(root, [*workdir_roots, *extra_writable])
    for path in extra_writable:
        Path(path).mkdir(parents=True, exist_ok=True)
    if config_dir is not None:
        # A writable path must exist for the seal to bind it; the engine would create this one anyway.
        config_dir.mkdir(parents=True, exist_ok=True)
    paths = _seal_record.role_paths(root, role_id)
    if not (paths.dir / "role.json").is_file():
        _seal_record.register(
            root, role_id, kind, engine=engine, parent=parent, action=action, seal=seal_flags(spec, engine)
        )
    # A short, private directory: a unix socket path is limited to ~108 bytes, and the guard's config is
    # kept out of the record (it is neither a transcript nor a log).
    private = Path(tempfile.mkdtemp(prefix="seal"))
    socket_path = private / "proxy.sock"
    # The guard hook runs inside the seal and must be able to append its log, so the log cannot be in the
    # record (read-only to the role) or in `private` (read-only, it holds the guard config). It is written
    # here and copied into the record from outside. Seeded with what the record already has: a restarted
    # author is the same role, and its earlier guard lines must stay.
    guard_dir = Path(tempfile.mkdtemp(prefix="sealg"))
    # The role's package-manager caches; writable to it, deleted with the rest of its scratch at close.
    scratch = Path(tempfile.mkdtemp(prefix="sealc"))
    cache = scratch / "cache"
    live_guard_log = guard_dir / "guard.jsonl"
    if paths.guard_log.is_file():
        shutil.copyfile(paths.guard_log, live_guard_log)
    settings: dict[str, Any] = {}
    if engine == "claude":
        config = _seal_guard.write_config(
            private / "guard.json",
            task_project=spec.task_project,
            forbidden_paths=spec.forbidden_paths,
            local_roots=[str(p) for p in workdir_roots],
            guard_log=live_guard_log,
            role_id=role_id,
        )
        settings = _seal_guard.settings_extra(config)
    hidden = _expand_forbidden(spec.forbidden_paths)
    role = SealedRole(
        role_id=role_id,
        argv_prefix=_seal_net.launcher_argv(
            str(socket_path),
            PROXY_PORT,
            INDEX_URL,
            spec.index_cutoff,
            hidden,
            # The guard config must not be rewritable by the role it judges (same uid, so a mode bit would
            # not do). The proxy socket lives here too; connecting to a socket needs no write access to
            # the mount. The record root is read-only for every role, so the role cannot edit its own or
            # another role's logs, or the manifest.
            readonly=[str(root), *([str(private)] if engine == "claude" else [])],
            # Everything else is read-only (the harness, same uid, reads what a role could plant in
            # `$HOME` or `/tmp`): the workdir and the engine's own config, the guard's log, the private
            # tmp and the caches, and nothing more.
            # Resolved here, where nothing the role does has happened yet: the launcher refuses a path with a
            # link in it, so a link the role swaps in later fails its next launch instead of being followed.
            writable=[
                os.path.realpath(p)
                for p in [
                    *workdir_roots,
                    *([config_dir] if config_dir is not None else []),
                    *extra_writable,
                    guard_dir,
                    PRIVATE_TMP,
                    cache,
                ]
            ],
            private_tmp=PRIVATE_TMP,
        ),
        env={"PYTHONPATH": _python_path(), **_cache_env(cache)},
        settings_extra=settings,
        role_dir=paths.dir,
        live_guard_log=live_guard_log,
        record_guard_log=paths.guard_log,
    )
    try:
        with _seal_proxy.run_in_thread(
            spec, socket_path, paths.proxy_log, port=PROXY_PORT, push_auth=push_auth
        ):
            try:
                yield role
            except BaseException:
                if role.exit_code is None:
                    role.exit_code = 1
                raise
    finally:
        # After the proxy has stopped and closed its log, so `end.json` hashes the final proxy.jsonl.
        shutil.rmtree(private, ignore_errors=True)
        role.sync_guard_log()
        shutil.rmtree(guard_dir, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        _collect(root, role_id, engine, config_dir)
        _finish(root, role_id, role.exit_code if role.exit_code is not None else 0)


def _finish(root: Path, role_id: str, exit_code: int) -> None:
    """Write `end.json` for the role, replacing one an earlier close wrote."""
    _seal_record.role_paths(root, role_id).end.unlink(missing_ok=True)
    _seal_record.finish(root, role_id, exit_code)


def close_author_record(record_root: Path, config_dir: Path, role: SealedRole | None = None) -> None:
    """Copy the author's transcripts (and guard log, from `role`) into its role dir; (re)write `end.json`.

    Called from the DONE path before review, and again when the project ends: the author's session is
    still running at DONE, so each call records what exists then. The audit is added next to this call.
    """
    root = Path(record_root)
    paths = _seal_record.role_paths(root, "author")
    if role is not None:
        role.sync_guard_log()
    engine = json.loads((paths.dir / "role.json").read_text())["engine"]
    _collect(root, "author", engine, Path(config_dir))
    _finish(root, "author", 0)


# --- the caller's side of a sub-agent ---------------------------------------------------------- #


def register_subagent(
    spec: SealSpec, record_root: Path, kind: str, engine: str, *, action: str = "", parent: str = "author"
) -> str:
    """Register a sub-agent's role before it is launched, and return its id.

    There is no dir yet: a role whose bundle never comes back stays this way, which is how the audit sees
    a lost sub-agent as incomplete.
    """
    role_id = _seal_record.new_role_id(kind)
    _seal_record.register(
        record_root,
        role_id,
        kind,
        engine=engine,
        parent=parent,
        action=action,
        seal=seal_flags(spec, engine),
        create_dir=False,
    )
    return role_id


async def fetch_bundle(out: Any, dest: Path) -> None:
    """Download only the `.seal-record/` part of a sub-agent's returned `Dir` into `dest`.

    The rest of the `Dir` is a whole workspace, and a caller that wants it (a delegate) downloads it on its
    own: a reviewer's result is never downloaded at all.
    """
    marker = f"/{_seal_record.BUNDLE_DIR}/"
    async for file in out.walk(recursive=True):
        path = urllib.parse.unquote(str(file.path))
        if marker not in path:
            continue
        target = Path(dest) / _seal_record.BUNDLE_DIR / path.split(marker, 1)[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        await file.download(str(target))


def merge_result(downloaded: Path, record_root: Path) -> list[str]:
    """Merge the `.seal-record/` inside a downloaded sub-agent result into the project's record."""
    return _seal_record.merge(downloaded, record_root)
