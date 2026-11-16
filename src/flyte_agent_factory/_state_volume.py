"""The state on a directory of a persistent volume: an `Adapter` for `_state_bucket.BucketStore`.

For an open-source, single-node factory with no bucket. The compare-and-swap is a file lock: every write takes
an exclusive `flock` on `<root>/.lock`, checks the object's tag, and writes a temporary file renamed over the
target (`os.replace`), so a reader without the lock sees the old file or the new one, never half of one. A
tag is the SHA-256 of the content: "unchanged since I read it" is then exactly "the content is the same".
`flock` works across the processes and threads of one machine (each `open` has its own lock); on NFS it is
only as good as the server's lock manager, so a shared network volume is not supported, use the bucket.

obstore's `LocalStore` has no update mode, which is why this is not that.
"""

from __future__ import annotations

import builtins
import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flyte_agent_factory._state_bucket import Adapter, BucketStore, Tag
from flyte_agent_factory._state_store import _RETRIES

_LOCK = ".lock"
_TMP = ".tmp-"


def _tag(body: bytes) -> Tag:
    return Tag(e_tag=hashlib.sha256(body).hexdigest())


def _check_not_reserved(path: str) -> None:
    """Refuse a key that `list` would hide: the lock file and temporary files share the keys' directories."""
    for name in path.split("/"):
        if name == _LOCK or name.startswith(_TMP):
            raise ValueError(
                f"a volume state key may not have a path part named {_LOCK!r} or starting {_TMP!r}"
            )


class VolumeAdapter:
    """Objects as files under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _file(self, path: str) -> Path:
        return self.root / path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self.root / _LOCK, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def list(self, prefix: str) -> dict[str, Tag]:
        base = self._file(prefix.rstrip("/"))
        found = {}
        for dirpath, _, names in os.walk(base):
            for name in names:
                if name == _LOCK or name.startswith(_TMP):
                    continue
                file = Path(dirpath, name)
                try:
                    found[file.relative_to(self.root).as_posix()] = _tag(file.read_bytes())
                except FileNotFoundError:  # deleted since the walk began
                    continue
        return found

    def list_dirs(self, prefix: str) -> builtins.list[str]:
        base = self._file(prefix.rstrip("/"))
        if not base.is_dir():
            return []
        return sorted(f"{prefix.rstrip('/')}/{d.name}/" for d in base.iterdir() if d.is_dir())

    def head(self, path: str) -> Tag | None:
        found = self.get(path)
        return None if found is None else found[1]

    def get(self, path: str) -> tuple[bytes, Tag] | None:
        try:
            body = self._file(path).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None
        return body, _tag(body)

    def create(self, path: str, body: bytes) -> bool:
        _check_not_reserved(path)
        with self._locked():
            if self._file(path).is_file():
                return False
            self._write(path, body)
            return True

    def replace(self, path: str, body: bytes, tag: Tag) -> bool:
        _check_not_reserved(path)
        with self._locked():
            found = self.get(path)
            if found is None or found[1] != tag:
                return False
            self._write(path, body)
            return True

    def put(self, path: str, body: bytes) -> None:
        _check_not_reserved(path)
        with self._locked():
            self._write(path, body)

    def delete(self, path: str) -> None:
        with self._locked():
            file = self._file(path)
            file.unlink(missing_ok=True)
            # Directories only hold files: drop the ones this left empty, as an object store has none.
            for parent in file.parents:
                if parent == self.root:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break

    def _write(self, path: str, body: bytes) -> None:
        """Write to a temporary file in the target's directory and rename it over the target. Lock held."""
        file = self._file(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=_TMP)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())  # a crash must leave the old file or the new one, not an empty one
            os.replace(tmp, file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def open_store(path: str, *, retention_days: int = 30, retries: int = _RETRIES) -> BucketStore:
    adapter: Adapter = VolumeAdapter(Path(path))
    return BucketStore(adapter, retention_days=retention_days, retries=retries)
