"""What a personal browser's profile archive holds, how it is sealed, and where it can live.

Beside personal.py (not in it) because that file is near the 1,500-line module limit. It ships
with the rest of `_assets/browser/` (`Image.with_source_folder`, see `_images.py`), which personal.py
finds on its own `sys.path` entry. Never log a key, a cookie or any profile content: callers print
error classes only.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import os
import tarfile
import tempfile
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import fsspec  # type: ignore[import-untyped]  # fsspec does not ship type information.
import obstore
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap
from obstore.exceptions import AlreadyExistsError, PreconditionError
from obstore.store import from_url

if TYPE_CHECKING:
    from obstore.store import S3Config

# What the archive holds, relative to the Chromium profile directory. An allow-list: a file Chromium adds
# in a later release is left out until someone decides it is needed, instead of silently carrying history,
# caches or saved passwords into the bucket. The app runs Playwright's Chromium, which (from Playwright's
# default arguments, `--password-store=basic`) encrypts cookies with a fixed key rather than one from a
# keyring, so `Cookies` restores on a new pod with nothing else: measured with Chrome for Testing 155, a
# profile holding only `Default/Cookies` and `Default/LocalStorage` resumed both a server cookie and a
# localStorage token (docs/personal-browser.md). `Local State` is kept anyway as the place Chromium keeps
# the key on setups that do use one.
SESSION_STATE = (
    # Signed-in cookies. Chromium 96+ keeps them under Network/, older builds directly in Default/; the
    # journal is the SQLite write-ahead state, and restoring a database without it can lose recent cookies.
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Cookies",
    "Default/Cookies-journal",
    # Sites keep auth tokens and "remember me" flags here. Chrome for Testing 155 (the build measured, see
    # docs/personal-browser.md) names them without the space; older builds with it.
    "Default/LocalStorage",
    "Default/SessionStorage",
    "Default/Local Storage",
    "Default/Session Storage",
    # Preferences that the cookies depend on (content settings such as cookie exceptions).
    "Default/Preferences",
    # Browser-wide state: os_crypt key material where the platform uses one, and the profile list.
    "Local State",
    # Left out on purpose: Default/IndexedDB (large, and the sites this app signs in to keep their session
    # in cookies and Local Storage; add it if a site is found to sign out without it) and
    # Default/Login Data (saved passwords; the owner signs in by hand and nothing here relies on them).
)

MAGIC = b"FBAR"
VERSION = 1
KEY_ID_LEN = 8
NONCE_LEN = 12
WRAPPED_KEY_LEN = 40  # AES-KW adds one 8-byte block to the 32-byte data key.
HEADER_LEN = len(MAGIC) + 1 + KEY_ID_LEN + 1 + WRAPPED_KEY_LEN + NONCE_LEN
KEY_ENV = "BROWSER_ARCHIVE_KEY"
GZIP_MAGIC = b"\x1f\x8b"


class ArchiveKeyError(RuntimeError):
    """The archive is encrypted and this key is missing or cannot open it.

    Unlike corruption this says nothing about the stored bytes: the caller must neither fall back to an
    older generation nor start on an empty profile, which the next snapshot would publish over the archive.
    """


class ArchiveCorrupt(ValueError):
    """The stored object is damaged (truncated, altered, or not an archive at all)."""


def load_key(encoded: str) -> bytes:
    """Return the 32-byte key-encryption key from its base64 secret; raise ArchiveKeyError if unusable.

    The message never contains the value.
    """
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ArchiveKeyError("the archive key is not base64") from None
    if len(key) != 32:
        raise ArchiveKeyError("the archive key must be 32 bytes")
    return key


def key_id(kek: bytes) -> bytes:
    """Return a fingerprint that names the key without revealing it (an HMAC of a label under the key)."""
    return hmac.new(kek, b"flyte-agent-factory browser archive key id", hashlib.sha256).digest()[:KEY_ID_LEN]


def seal(plaintext: bytes, kek: bytes) -> bytes:
    """Encrypt `plaintext` under a fresh data key, which `kek` wraps; the header is authenticated.

    The whole archive is held in memory: the trimmed profile is megabytes, and AES-GCM in `cryptography`
    is one-shot.
    """
    data_key = AESGCM.generate_key(bit_length=256)
    wrapped = aes_key_wrap(kek, data_key)
    nonce = os.urandom(NONCE_LEN)
    header = MAGIC + bytes([VERSION]) + key_id(kek) + bytes([len(wrapped)]) + wrapped + nonce
    return header + AESGCM(data_key).encrypt(nonce, plaintext, header)


def is_sealed(blob: bytes) -> bool:
    """Return whether `blob` is a sealed archive rather than a legacy plaintext tar.gz."""
    return blob.startswith(MAGIC)


def unseal(blob: bytes, kek: bytes | None) -> bytes:
    """Decrypt a sealed archive. Raises ArchiveKeyError for no or a wrong key, ArchiveCorrupt for damage."""
    if len(blob) < HEADER_LEN + 16 or blob[len(MAGIC)] != VERSION:
        raise ArchiveCorrupt("unknown archive header")
    if kek is None:
        raise ArchiveKeyError("the archive is encrypted and no archive key is set")
    start = len(MAGIC) + 1
    if not hmac.compare_digest(blob[start : start + KEY_ID_LEN], key_id(kek)):
        raise ArchiveKeyError("the archive was encrypted with a different key")
    start += KEY_ID_LEN
    if blob[start] != WRAPPED_KEY_LEN:
        raise ArchiveCorrupt("unknown archive header")
    wrapped = blob[start + 1 : start + 1 + WRAPPED_KEY_LEN]
    header = blob[:HEADER_LEN]
    nonce = header[-NONCE_LEN:]
    # The key id matched, so a failure from here on is damage, not a wrong key.
    try:
        data_key = aes_key_unwrap(kek, wrapped)
        return AESGCM(data_key).decrypt(nonce, blob[HEADER_LEN:], header)
    except (InvalidUnwrap, InvalidTag):
        raise ArchiveCorrupt("archive failed authentication") from None


def add_session_state(tar: tarfile.TarFile, profile: Path) -> None:
    """Add the `SESSION_STATE` files of `profile` to `tar`, leaving everything else out."""
    for entry in SESSION_STATE:
        root = profile / entry
        if root.is_symlink() or not root.exists():
            continue
        paths = [root] if root.is_file() else _files_below(root)
        for path in paths:
            try:
                tar.add(path, arcname=str(path.relative_to(profile)), recursive=False)
            except FileNotFoundError:
                # Chromium rewrites its databases while it runs.
                continue


def _files_below(directory: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith("Singleton")]
        for name in files:
            path = Path(current) / name
            if not path.is_symlink() and not name.startswith("Singleton"):
                yield path


def storage_options(uri: str) -> dict[str, Any]:
    """Return the fsspec options for `uri`: the S3-compatible endpoint and region from the environment.

    Set by browser.py from the profile's `[state]` table; empty means the cloud's own endpoint.
    """
    scheme = urllib.parse.urlsplit(uri).scheme
    if scheme == "file":
        return {"auto_mkdir": True}
    client_kwargs = {
        key: value
        for key, value in (
            ("endpoint_url", os.environ.get("BROWSER_PROFILE_ENDPOINT", "")),
            ("region_name", os.environ.get("BROWSER_PROFILE_REGION", "")),
        )
        if value
    }
    return {"client_kwargs": client_kwargs} if scheme == "s3" and client_kwargs else {}


def open_fs(uri: str) -> tuple[Any, str]:
    """Return the fsspec filesystem and the object key for `uri`."""
    fs, key = fsspec.core.url_to_fs(uri, **storage_options(uri))
    return fs, key


def lease_store(uri: str, *, endpoint: str | None = None, region: str | None = None) -> Any:
    """Return the store for the lease object next to `uri`: obstore for a bucket, a lock file for `file://`.

    `endpoint` and `region` default to the environment (the app's way); `ops/move_state.py` passes them, since
    it moves between two places that differ in them.
    """
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme == "file":
        return LocalLeaseStore(Path("/"))
    if endpoint is None:
        endpoint = os.environ.get("BROWSER_PROFILE_ENDPOINT", "")
    if region is None:
        region = os.environ.get("BROWSER_PROFILE_REGION", "")
    if parsed.scheme == "s3" and (endpoint or region):
        config: "S3Config" = {}
        if endpoint:
            # The engine's `_profile_state.plain_http_is_allowed`, repeated: this app image cannot import the
            # engine. The endpoint is not echoed: it may carry a key.
            host = urllib.parse.urlsplit(endpoint)
            if host.scheme == "http" and host.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("BROWSER_PROFILE_ENDPOINT may use http only for localhost")
            config["endpoint"] = endpoint
        if region:
            config["region"] = region
        # A plain-http endpoint (a lab MinIO) is refused unless allowed.
        return from_url(
            f"s3://{parsed.netloc}",
            config=config,
            client_options={"allow_http": endpoint.startswith("http://")},
        )
    return from_url(f"{parsed.scheme}://{parsed.netloc}")


class _LocalObject:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.meta: dict[str, str | None] = {"e_tag": hashlib.sha256(data).hexdigest(), "version": None}

    def bytes(self) -> bytes:
        return self._data


class LocalLeaseStore:
    """The slice of obstore the lease uses, over a directory, for `file://` profile locations.

    obstore's LocalStore rejects update-mode puts, so the compare-and-swap is done here under an
    flock on a sidecar file. Considered and rejected: `os.link` / `O_EXCL` alone, which arbitrate creation
    but not the replace of a live lease.
    """

    def __init__(self, root: Path) -> None:
        """Keep objects as files below `root`."""
        self.root = root

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / (path.name + ".flock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def get(self, key: str) -> _LocalObject:
        """Return the object at `key`; raise FileNotFoundError when there is none."""
        return _LocalObject((self.root / key).read_bytes())

    def put(
        self, key: str, payload: bytes, mode: Literal["create"] | dict[str, str]
    ) -> dict[str, str | None]:
        """Write `payload` if `mode` still holds ("create": no object; else the e_tag read earlier)."""
        path = self.root / key
        with self._locked(path):
            try:
                current: str | None = _LocalObject(path.read_bytes()).meta["e_tag"]
            except FileNotFoundError:
                current = None
            if mode == "create":
                if current is not None:
                    raise AlreadyExistsError("lease exists")
            elif current is None or current != mode.get("e_tag"):
                raise PreconditionError("lease changed")
            # As `_state_volume._write`: a crash leaves the old lease or the new one; a failure, no temp file.
            fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".lease-")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(payload)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(staged, path)
            except BaseException:
                Path(staged).unlink(missing_ok=True)
                raise
        return {"e_tag": hashlib.sha256(payload).hexdigest(), "version": None}


def lease_get(store: Any, key: str) -> Any:
    """`obstore.get`, or the local store's."""
    return store.get(key) if isinstance(store, LocalLeaseStore) else obstore.get(store, key)


def lease_put(store: Any, key: str, payload: bytes, mode: Any) -> Any:
    """`obstore.put` in a conditional `mode`, or the local store's."""
    if isinstance(store, LocalLeaseStore):
        return store.put(key, payload, mode)
    return obstore.put(store, key, payload, mode=mode)
