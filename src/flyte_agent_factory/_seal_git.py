"""Harness git on a repository a sealed role can write: no hooks, and a `.git/config` the role did not shape.

The sealed role runs inside network and mount namespaces, but its workspace clone (`.git/` included) is
writable by it, and the harness runs git there outside the seal, with network: `git fetch`, `status`, the
delegation snapshot. A `reference-transaction` hook, `core.fsmonitor`, a `filter.*` driver, `include.path`,
`url.*.insteadOf`, `credential.helper`, `remote.origin.url` or `uploadpack` is a program or a redirect git
would run for the harness. Two parts, both required:

1. `harness_env` / `config_args`: hooks and fsmonitor off, no system or global config, a credential helper
   the harness names. Command-line and environment config outrank the file.
2. `sanitize`: before a harness git call, `.git/config` is rewritten to an allow-list of keys. A key that
   is not listed is dropped, so a config key git learns next year is dropped too (rejected: a blocklist of
   the dangerous ones, which fails open). `remote.origin.url` is the harness's URL, and `remote.origin.fetch`
   the refspecs it set, whatever the role wrote.

Which repositories: `arm` registers the workspace clone and the nested task repo for a sealed project, and
nothing else is touched; an ordinary project never arms, so these functions then change nothing. The role's
own `pre-push` hook (`_push_guard.install_hook`) stays in `.git/hooks`, but harness git never runs hooks.

An armed repository whose `.git` is missing or not a git directory raises `UnsafeRepo`: git would otherwise
walk up and use the worktree root, which the role can make a bare repository with its own config. Calls that
name the repository get `GIT_DIR` and `GIT_WORK_TREE` (`harness_env`); `safe.bareRepository=explicit` covers
the rest.

Residual: the role runs concurrently, so it can rewrite the config between a `sanitize` and the git call
that follows. `prepare` runs as the call is built, which makes the window milliseconds wide, not zero. The
copies the delegation ships are sanitised after copying (`sanitize_copy`), and that copy is the harness's.

Stdlib only, and no import from the engine: `_secret_env`, `_workspace` and `_judge_inputs` import this.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Config that makes git run no program of the repo's choosing, as `(key, value)`. `core.hooksPath`
#: points at a path that is not a directory, so git finds no hook there.
_NO_PROGRAMS = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    # `ext::<command>` is a remote URL that runs a command; nothing the harness fetches from is one.
    ("protocol.ext.allow", "never"),
    # A directory git finds by walking up that is not a `.git` is a bare repository whose config the role
    # wrote; only a git directory the harness names (`GIT_DIR`, `--git-dir`) is used.
    ("safe.bareRepository", "explicit"),
    # A nested repository the role adds has a config `sanitize` never sees: `status`, `diff`, `add` and
    # `fetch` must not recurse into it (its `filter.*.clean` would run). `--ignore-submodules=all` on the
    # command line (`SUBMODULES_OFF`) outranks a `submodule.<name>.ignore` in the role's `.gitmodules`.
    ("diff.ignoreSubmodules", "all"),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("status.submoduleSummary", "false"),
)
#: Added to `status` and `diff` while a sealed project is armed (`submodules_off`): the config above does not
#: outrank a `submodule.<name>.ignore = none` in the role's `.gitmodules`, only this option does.
SUBMODULES_OFF = "--ignore-submodules=all"
_NEEDS_SUBMODULES_OFF = ("status", "diff")
#: Files in `objects/info` that make git read objects from, or fetch them from, somewhere else. A clone the
#: harness made has none.
_ALTERNATES = ("alternates", "http-alternates")

#: Keys of `[core]` that are kept, and the values they may have. `repositoryformatversion` and `bare` are
#: written by `sanitize` as the only values a clone has: version 1 would let `extensions.*` change git.
_CORE_KEPT = {
    "repositoryformatversion": "0",
    "bare": "false",
    "filemode": None,
    "logallrefupdates": None,
    "ignorecase": None,
    "precomposeunicode": None,
    "symlinks": None,
}
_BOOLEANS = {"true", "false"}
_PUSH_KEPT = {"default", "autosetupremote", "followtags"}
#: `remote.origin.fetch` as `_seal_session.limit_fetch` writes it, for a repo whose refspecs the harness has
#: not yet set: a plain branch name, never a glob, so a role's `+refs/*:refs/remotes/origin/*` does not stay.
_PLAIN_REFSPEC = re.compile(r"\+refs/heads/([^:*?\[\\\s]+):refs/remotes/origin/\1")


class UnsafeRepo(RuntimeError):
    """An armed repository's git directory is missing or not a plain one: git would read config elsewhere."""


class _Armed:
    def __init__(self, remote_url: str | None, push_route: str | None, store: Path | None) -> None:
        self.remote_url = remote_url
        self.push_route = push_route
        self.store = store
        self.fetch: list[str] | None = None  # None: not set by the harness yet
        # Of the config this module last left. `st_ctime_ns` is the kernel's and the role cannot set it, as it
        # can the mtime; the digest covers a rewrite that keeps size, inode and (somehow) the ctime.
        self.signature: tuple[int, int, int, int] | None = None
        self.digest: str | None = None


_lock = threading.RLock()
_armed: dict[Path, _Armed] = {}


def arm(
    repo: Path, remote_url: str | None = None, *, push_route: str | None = None, store: Path | None = None
) -> None:
    """Register `repo` as writable by a sealed role. Harness git on it is now hardened and sanitised.

    `remote_url` is the harness's own URL for `origin` (`None`: the repo has no remote, as the task repo has
    none); `push_route` the one `url.<route>.pushInsteadOf` that `_seal_session.route_pushes` sets, and
    `store` the credential file for the harness's own network git.
    """
    with _lock:
        _armed[_key(repo)] = _Armed(remote_url, push_route, store)


def disarm_all() -> None:
    """Forget every armed repository (the project ended); an ordinary project's git is never touched."""
    with _lock:
        _armed.clear()


def is_armed(repo: Path | None = None) -> bool:
    """Whether `repo` (or, without one, any repository) is armed."""
    with _lock:
        return bool(_armed) if repo is None else _key(repo) in _armed


def remote_url(repo: Path) -> str | None:
    """Return the harness's URL for `repo`'s `origin`, or None when `repo` is not armed or has no remote."""
    with _lock:
        state = _armed.get(_key(repo))
        return state.remote_url if state else None


def set_fetch(repo: Path, refspecs: Sequence[str]) -> None:
    """Record the refspecs the harness set for `origin`: `sanitize` writes exactly these from then on."""
    with _lock:
        if (state := _armed.get(_key(repo))) is not None:
            state.fetch = list(refspecs)
            state.signature = None
            state.digest = None


def config_args() -> tuple[str, ...]:
    """`-c` options for a harness git call while a sealed project is armed; empty otherwise."""
    if not is_armed():
        return ()
    return tuple(arg for key, value in _NO_PROGRAMS for arg in ("-c", f"{key}={value}"))


def submodules_off(args: Sequence[str]) -> tuple[str, ...]:
    """Return git's `args` with `SUBMODULES_OFF` after a `status` or `diff`; as given if none is armed."""
    if not is_armed():
        return tuple(args)
    position = 0
    while position < len(args) and args[position].startswith("-"):
        position += 2 if args[position] in {"-C", "-c"} else 1
    if position < len(args) and args[position] in _NEEDS_SUBMODULES_OFF:
        return (*args[: position + 1], SUBMODULES_OFF, *args[position + 1 :])
    return tuple(args)


def git_argv(*args: str) -> list[str]:
    """Return `git` with hooks and fsmonitor off, after `prepare()`, for a caller without `_secret_env`."""
    prepare()
    return [
        "git",
        *(arg for key, value in _NO_PROGRAMS for arg in ("-c", f"{key}={value}")),
        *submodules_off(args),
    ]


def harness_env(
    repo: Path, env: Mapping[str, str] | None = None, *, root: Path | None = None
) -> dict[str, str] | None:
    """Return the environment for a network or object-moving git call in `repo`; None when it is not armed.

    `GIT_DIR` and `GIT_WORK_TREE` name `root` (default `repo`): git then never discovers a repository by
    walking up from where it runs, so a bare repository the role made at the worktree root, or a `.git`
    it emptied, is not what git uses. `root` is for a copy of `repo` the harness made (the delegation
    snapshot), which is not armed itself.

    No system or global config (the role may have written `$HOME/.gitconfig`), no prompt, and the config of
    `_NO_PROGRAMS` plus the harness's credential helper as `GIT_CONFIG_*`, which outranks the repo's file.
    The helper is here, not in `.git/config`, because `sanitize` drops every `credential.*` key.
    """
    with _lock:
        state = _armed.get(_key(repo))
    if state is None:
        return None
    out = dict(os.environ if env is None else env)
    pairs = list(_NO_PROGRAMS)
    if state.store is not None:
        # An empty value first resets any helper list from a lower-priority file.
        pairs += [
            ("credential.helper", ""),
            ("credential.helper", f"store --file={state.store}"),
            ("credential.useHttpPath", "false"),
        ]
    try:
        start = int(out.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        start = 0
    out["GIT_CONFIG_COUNT"] = str(start + len(pairs))
    for offset, (key, value) in enumerate(pairs):
        out[f"GIT_CONFIG_KEY_{start + offset}"] = key
        out[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    out.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    top = _key(repo if root is None else root)
    out.update(GIT_DIR=str(top / ".git"), GIT_WORK_TREE=str(top))
    return out


def prepare(repo: Path | None = None) -> None:
    """Sanitise `repo`'s config if armed, or every armed repository without one; call it right before git.

    Cheap when nothing changed: one `stat` per repository. Raises `UnsafeRepo` for a git directory git
    should not be pointed at.
    """
    with _lock:
        if repo is None:
            targets = list(_armed.items())
        else:
            targets = [(k, s) for k, s in _armed.items() if k == _key(repo)]
        for path, state in targets:
            _sanitize_armed(path, state)


def sanitize(repo: Path, remote_url: str | None, *, push_route: str | None = None) -> None:
    """Rewrite `repo/.git/config` to the allow-list, with `origin` at `remote_url` (None: no remote)."""
    _sanitize_armed(_key(repo), _Armed(remote_url, push_route, None))


def sanitize_copy(source: Path, copy: Path) -> None:
    """Sanitise `copy`, a copy of the armed repository `source`, as `source` is (the delegation snapshot)."""
    with _lock:
        state = _armed.get(_key(source))
    if state is None:
        return
    fresh = _Armed(state.remote_url, state.push_route, None)
    fresh.fetch = state.fetch
    _sanitize_armed(_key(copy), fresh, remember=False)


def _key(repo: Path) -> Path:
    return Path(os.path.abspath(repo))


def replace_file(path: Path, data: bytes, mode: int, *, strict_parent: bool = False) -> None:
    """Write `data` to `path` through a new file and a rename: a symlink at `path` is replaced, not followed.

    `mkstemp` creates with `O_EXCL`, which does not follow a link either. `strict_parent` refuses a `path`
    whose directory is a symlink (the role could point it anywhere); an ordinary project's own hooks
    directory may be one, so only armed repositories ask for it.
    """
    if strict_parent and path.parent.is_symlink():
        raise UnsafeRepo(f"{path.parent} is a symlink; refusing to write {path.name} through it")
    fd, partial = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(partial, mode)
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise


def _discard(path: Path) -> None:
    """Remove what is at `path`: a directory with its contents, anything else (a link, a FIFO) as a name."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _read_regular(path: Path) -> bytes | None:
    """Return the bytes of the regular file at `path`; None for a missing file or anything else.

    Opened without following a link and without blocking, then checked on the descriptor, so a FIFO swapped
    in at any moment is not waited on.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None  # missing, a symlink (ELOOP) or unreadable
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read()


def _require_git_dir(repo: Path, git_dir: Path) -> None:
    """Raise `UnsafeRepo` unless `git_dir` is the plain git directory of a clone the harness made."""
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise UnsafeRepo(f"{repo}: .git is missing or not a plain directory; refusing to run git in it")
    # `HEAD`, `objects`, `refs`: what git needs to accept it. Without them git would walk up and find the
    # worktree root as a bare repository, with the role's config.
    for name in ("HEAD", "objects", "refs"):
        if not os.path.lexists(git_dir / name):
            raise UnsafeRepo(f"{repo}: .git has no {name}, so it is not a git directory; refusing to run git")
    if (git_dir / "objects").is_symlink() or (git_dir / "refs").is_symlink():
        raise UnsafeRepo(f"{repo}: .git/objects or .git/refs is a symlink; refusing to run git in it")
    if (git_dir / "commondir").exists():
        raise UnsafeRepo(f"{repo}: .git/commondir points git at another repository; refusing to run git")


def _sanitize_armed(repo: Path, state: _Armed, *, remember: bool = True) -> None:
    git_dir = repo / ".git"
    _require_git_dir(repo, git_dir)
    for name in _ALTERNATES:
        _discard(git_dir / "objects" / "info" / name)
    config = git_dir / "config"
    try:
        seen = config.lstat()
    except FileNotFoundError:
        seen = None
    current = _read_regular(config)
    signature = (seen.st_mtime_ns, seen.st_size, seen.st_ino, seen.st_ctime_ns) if seen else None
    digest = hashlib.sha256(current).hexdigest() if current is not None else None
    if remember and signature is not None and signature == state.signature and digest == state.digest:
        return
    # Only read with `extensions.worktreeConfig`, which is dropped below; removed so it stays inert.
    _discard(git_dir / "config.worktree")
    wanted = _render(_kept_entries(_read(current) if current is not None else [], state)).encode()
    if current != wanted:
        if seen is not None and current is None:
            _discard(config)  # a symlink, FIFO or directory: not a config of the harness's
        replace_file(config, wanted, stat.S_IMODE(seen.st_mode) if seen and current is not None else 0o644)
    if remember:
        written = config.lstat()
        state.signature = (written.st_mtime_ns, written.st_size, written.st_ino, written.st_ctime_ns)
        state.digest = hashlib.sha256(wanted).hexdigest()


def _read(content: bytes) -> list[tuple[str, str]]:
    """Return the `(key, value)` pairs of config text, parsed by git itself; `include` is not followed.

    Keys have a lower-case section and variable name and a subsection as written. Read from stdin and run
    from `/` with no system or global config, so no repository's settings take part, and the file is not
    opened a second time by git (it could have been swapped for a FIFO since `_read_regular`).
    """
    try:
        done = subprocess.run(
            ["git", "config", "--file", "-", "--no-includes", "--list", "--null"],
            input=content,
            cwd="/",
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    if done.returncode != 0:
        return []  # unparseable: nothing of it is kept, and the config is rewritten from the harness's own
    pairs = []
    for record in done.stdout.decode(errors="replace").split("\0"):
        if record:
            key, _, value = record.partition("\n")
            pairs.append((key, value if "\n" in record else "true"))
    return pairs


def _kept_entries(pairs: list[tuple[str, str]], state: _Armed) -> list[tuple[str, str]]:
    kept: dict[str, list[str]] = {}

    def keep(key: str, value: str) -> None:
        kept.setdefault(key, []).append(value)

    keep("core.repositoryformatversion", "0")
    keep("core.bare", "false")
    fetch = []
    for key, value in pairs:
        section, _, rest = key.partition(".")
        subsection, _, name = rest.rpartition(".") if "." in rest else ("", "", rest)
        if section == "core" and name in _CORE_KEPT and not subsection:
            if _CORE_KEPT[name] is None and value in _BOOLEANS:
                keep(key, value)
        elif section == "user" and not subsection and name in {"name", "email", "useconfigonly"}:
            keep(key, value)
        elif section == "push" and not subsection and name in _PUSH_KEPT:
            keep(key, value)
        elif section == "branch" and subsection:
            if (name == "remote" and value in {"origin", "."}) or name == "merge":
                keep(key, value)
            elif name == "rebase" and value in _BOOLEANS:
                keep(key, value)
        elif section == "url" and name == "pushinsteadof" and state.push_route:
            if subsection == state.push_route and value == state.remote_url:
                keep(key, value)
        elif key == "remote.origin.fetch" and state.fetch is None and _PLAIN_REFSPEC.fullmatch(value):
            fetch.append(value)
        # Everything else is dropped: include.*, filter.*, diff.*, merge.*, credential.*, other url.*,
        # other remotes, core.* not above, alias.*, extensions.*, http.*, ...
    if state.remote_url is not None:
        keep("remote.origin.url", state.remote_url)
        for refspec in state.fetch if state.fetch is not None else fetch:
            keep("remote.origin.fetch", refspec)
    return [(key, value) for key, values in kept.items() for value in values]


def _render(entries: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    header = None
    for key, value in entries:
        section, _, rest = key.partition(".")
        subsection, _, name = rest.rpartition(".") if "." in rest else ("", "", rest)
        if (section, subsection) != header:
            header = (section, subsection)
            lines.append(f'[{section} "{_escape(subsection)}"]' if subsection else f"[{section}]")
        lines.append(f'\t{name} = "{_escape(value)}"')
    return "\n".join(lines) + "\n"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
