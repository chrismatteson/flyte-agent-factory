"""Per-role record of a sealed run: layer 5 of docs/sealed-runs.md.

Layout under a record root:

    manifest.json                    {"spec_sha256", "roles": [{"role_id", "kind", "registered_at"}]}
    roles/<role_id>/role.json        what the role was and how it was sealed
    roles/<role_id>/transcript/...   the engine's own session files, copied after the role exits
    roles/<role_id>/proxy.jsonl      the network proxy's log (written by the proxy)
    roles/<role_id>/guard.jsonl      the guard hook's log (written by the hook)
    roles/<role_id>/end.json         exit code and the sha256 + line count of every other file, written LAST

The launcher registers a role before it starts, so a role that never comes back is visible as a manifest
entry with no `end.json`. A sub-agent bundles its finished role dir into the `Dir` it returns, under
`.seal-record/roles/<role_id>/`, and its caller merges that into its own root; a role that was registered
but never merged stays incomplete, which is how a lost sub-agent shows up in the audit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KINDS = ("author", "subagent", "reviewer", "judge", "delegate", "plain")
ENGINES = ("claude", "codex")
BUNDLE_DIR = ".seal-record"
_ROLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SEAL_KEYS = ("net", "guard", "tools_off", "fs_readonly", "spec_sha256")


@dataclasses.dataclass(frozen=True)
class RolePaths:
    dir: Path
    proxy_log: Path
    guard_log: Path
    transcript_dir: Path
    end: Path


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_role_id(kind: str) -> str:
    return f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def role_paths(root: Path, role_id: str) -> RolePaths:
    # The id becomes a directory name, and a bundled one comes from another pod: refuse `..` and `/`.
    if not _ROLE_ID.match(role_id):
        raise ValueError(f"invalid role id {role_id!r}")
    role_dir = Path(root) / "roles" / role_id
    return RolePaths(
        dir=role_dir,
        proxy_log=role_dir / "proxy.jsonl",
        guard_log=role_dir / "guard.jsonl",
        transcript_dir=role_dir / "transcript",
        end=role_dir / "end.json",
    )


def _write_json(path: Path, data: Any) -> None:
    """Write atomically: a reader sees the old file or the whole new one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked_manifest(root: Path) -> Iterator[dict[str, Any]]:
    """Hold the record's lock, yield the manifest, and write it back if the caller changed it."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".manifest.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "manifest.json"
        manifest = json.loads(path.read_text()) if path.exists() else {"spec_sha256": "", "roles": []}
        before = json.dumps(manifest, sort_keys=True)
        yield manifest
        if json.dumps(manifest, sort_keys=True) != before:
            _write_json(path, manifest)


def _add_to_manifest(manifest: dict[str, Any], role_id: str, kind: str, registered_at: str) -> None:
    if all(role["role_id"] != role_id for role in manifest["roles"]):
        manifest["roles"].append({"role_id": role_id, "kind": kind, "registered_at": registered_at})


def register(
    root: Path,
    role_id: str,
    kind: str,
    *,
    engine: str,
    parent: str = "",
    action: str = "",
    seal: Mapping[str, Any],
    create_dir: bool = True,
) -> Path:
    """Register a role before it starts: a manifest entry, and `roles/<id>/role.json` with empty logs.

    The logs start empty so that a missing log later means someone removed it. With `create_dir=False` the
    caller only registers a role another pod will run, and the role has no dir until its bundle is merged.
    Return the role dir (which then does not exist yet).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown role kind {kind!r}; expected one of {KINDS}")
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")
    if missing := [key for key in _SEAL_KEYS if key not in seal]:
        raise ValueError(f"seal is missing {missing}")
    paths = role_paths(root, role_id)
    started_at = _now()
    with _locked_manifest(Path(root)) as manifest:
        if paths.end.exists():
            raise ValueError(f"role {role_id} is finished; it cannot be registered again")
        manifest["spec_sha256"] = manifest["spec_sha256"] or seal["spec_sha256"]
        _add_to_manifest(manifest, role_id, kind, started_at)
        if create_dir:
            paths.transcript_dir.mkdir(parents=True, exist_ok=True)
            for log in (paths.proxy_log, paths.guard_log):
                log.touch()
            _write_json(
                paths.dir / "role.json",
                {
                    "role_id": role_id,
                    "kind": kind,
                    "engine": engine,
                    "parent": parent,
                    "action": action,
                    "started_at": started_at,
                    "seal": {key: seal[key] for key in _SEAL_KEYS},
                },
            )
    return paths.dir


def copy_regular(source: Path, target: Path, within: Path) -> bool:
    """Copy `source` to `target` only if it is a regular file below `within` reached through no link.

    The role writes `within` (its config dir, its guard log directory) and the harness copies from it
    outside the seal: a link planted there would copy a hidden file, or any file of this uid, into the
    record, where the role can read it. The last component is opened with `O_NOFOLLOW` and checked on the
    descriptor; a link above it is refused by comparing the resolved parent. False when refused.
    """
    try:
        if source.parent.resolve() != (within.resolve() / source.parent.relative_to(within)):
            return False
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return False
    # On the descriptor, before `fdopen`, which raises for a directory the role put at a transcript's name.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return False
    with os.fdopen(fd, "rb") as handle:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            shutil.copyfileobj(handle, out)
    return True


def _copy_into(files: list[tuple[Path, Path]], source_root: Path, dest_root: Path) -> list[Path]:
    copied = []
    for source, relative in files:
        target = dest_root / relative
        if copy_regular(source, target, source_root):
            copied.append(target)
    return copied


def collect_claude(config_dir: Path, role_dir: Path) -> list[Path]:
    """Copy Claude Code's session files into `transcript/`, keeping their paths relative to `config_dir`.

    That is every `projects/**/*.jsonl` (the session, and `<session>/subagents/agent-*.jsonl`) and every
    `subagents/*.meta.json`, which ties a sub-agent file to the Agent call that started it.
    """
    config_dir = Path(config_dir)
    found = {*config_dir.glob("projects/**/*.jsonl"), *config_dir.glob("**/subagents/*.meta.json")}
    files = [(p, p.relative_to(config_dir)) for p in sorted(found)]
    return _copy_into(files, config_dir, Path(role_dir) / "transcript")


def collect_codex(codex_home: Path, role_dir: Path) -> list[Path]:
    """Copy Codex's rollouts (`sessions/**/*.jsonl`) into `transcript/`, keeping their relative paths."""
    codex_home = Path(codex_home)
    files = [(p, p.relative_to(codex_home)) for p in sorted(codex_home.glob("sessions/**/*.jsonl"))]
    return _copy_into(files, codex_home, Path(role_dir) / "transcript")


def _describe(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "lines": data.count(b"\n") + (not data.endswith(b"\n") and bool(data)),
    }


def _describe_tree(role_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        p.relative_to(role_dir).as_posix(): _describe(p)
        for p in sorted(role_dir.rglob("*"))
        if p.is_file() and p != role_dir / "end.json" and not p.name.endswith(".tmp")
    }


def finish(root: Path, role_id: str, exit_code: int) -> Path:
    """Write `end.json` last: when the role ended and the hash and line count of every file it left."""
    paths = role_paths(root, role_id)
    if not (paths.dir / "role.json").is_file():
        raise FileNotFoundError(f"role {role_id} was never registered under {root}")
    if paths.end.exists():
        raise ValueError(f"role {role_id} is already finished")
    _write_json(paths.end, {"ended_at": _now(), "exit_code": exit_code, "files": _describe_tree(paths.dir)})
    return paths.end


def bundle(root: Path, role_id: str, dest_dir: Path) -> Path:
    """Copy a finished role dir to `dest_dir/.seal-record/roles/<role_id>/` and return that path."""
    paths = role_paths(root, role_id)
    if not paths.end.is_file():
        raise FileNotFoundError(f"role {role_id} is not finished; call finish() before bundling")
    target = role_paths(Path(dest_dir) / BUNDLE_DIR, role_id).dir
    shutil.copytree(paths.dir, target, dirs_exist_ok=True)
    return target


def _same_tree(a: Path, b: Path) -> bool:
    return (
        _describe_tree(a) == _describe_tree(b)
        and (a / "end.json").read_bytes() == (b / "end.json").read_bytes()
    )


def merge(src_dir: Path, root: Path) -> list[str]:
    """Copy the role dirs bundled under `src_dir/.seal-record/roles/` into `root/roles/`; return their ids.

    A role the caller registered but whose bundle never arrives is left as it is. A finished role dir
    already in `root` is never overwritten with different content (ValueError); an unfinished one is a
    placeholder from `register` and is replaced by the bundle.
    """
    bundled = Path(src_dir) / BUNDLE_DIR / "roles"
    if not bundled.is_dir():
        return []
    merged = []
    with _locked_manifest(Path(root)) as manifest:
        for source in sorted(p for p in bundled.iterdir() if p.is_dir()):
            paths = role_paths(root, source.name)
            if not (source / "end.json").is_file():
                raise ValueError(f"bundled role {source.name} has no end.json")
            if paths.end.exists():
                if not _same_tree(source, paths.dir):
                    raise ValueError(f"role {source.name} already exists in {root} with different content")
            else:
                shutil.rmtree(paths.dir, ignore_errors=True)
                shutil.copytree(source, paths.dir)
            role = json.loads((paths.dir / "role.json").read_text())
            _add_to_manifest(manifest, source.name, role["kind"], role["started_at"])
            manifest["spec_sha256"] = manifest["spec_sha256"] or role["seal"]["spec_sha256"]
            merged.append(source.name)
    return merged
