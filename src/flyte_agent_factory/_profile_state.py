"""The profile's `[state]` table: which backend holds the factory's state, and where.

Parsing lives here, not in `_profile`, which is at the size limit for a module. `_profile.parse` turns a
`StateConfigError` into a `ProfileError`. The table never holds a credential: a bucket is reached with the
pod's own identity (IRSA, workload identity, `AWS_*`), see `docs/state-store.md`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

BACKENDS = ("git", "bucket", "volume")
#: Schemes a state bucket may use. `memory://` exists for tests only and is not a profile value.
BUCKET_SCHEMES = ("s3", "gs")
RETENTION_DAYS = 30
#: Aliases that name one bucket: Hadoop's s3a/s3n, and gcs for gs.
_SCHEME_FAMILY = {"s3": "s3", "s3a": "s3", "s3n": "s3", "gs": "gs", "gcs": "gs"}
_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_STRING_KEYS = ("branch", "uri", "backend", "bucket", "endpoint", "region", "volume", "volume_claim")
_SERVICE_ACCOUNT_KEYS = ("factory_service_account", "browser_service_account")


class StateConfigError(ValueError):
    """The `[state]` table is invalid. `_profile.parse` turns it into a `ProfileError`."""


@dataclass(frozen=True)
class State:
    branch: str = ""  # the git backend's state branch; "" is `_state_store.default_branch`
    uri: str = ""  # retired: read only so that a profile that still sets it loads; nothing uses it
    backend: str = "git"  # "git", "bucket" or "volume"; `parse` resolves the default
    bucket: str = ""  # `s3://bucket/prefix` or `gs://bucket/prefix`
    endpoint: str = ""  # S3-compatible endpoint (MinIO); `s3://` buckets only
    region: str = ""
    volume: str = ""  # absolute directory on a persistent volume: where the claim is mounted in each pod
    volume_claim: str = ""  # the PersistentVolumeClaim mounted at `volume` (volume backend only)
    #: Kubernetes service accounts the pods run as: how a bucket is reached (IRSA / Workload Identity).
    factory_service_account: str = ""  # every pod that touches state
    browser_service_account: str = ""  # the personal browser apps
    retention_days: int = RETENTION_DAYS  # how long audit records are kept
    #: Flyte's own data bucket (or prefix): the state bucket and browser archives must not be inside it.
    flyte_bucket: tuple[str, ...] = ()


def parse(table: Mapping[str, Any]) -> State:
    """Return the validated `[state]` table; StateConfigError names the offending field."""
    unknown = sorted(set(table) - {*_STRING_KEYS, *_SERVICE_ACCOUNT_KEYS, "retention_days", "flyte_bucket"})
    if unknown:
        raise StateConfigError(f"unknown key(s) in [state]: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key in (*_STRING_KEYS, *_SERVICE_ACCOUNT_KEYS):
        if key in table:
            value = table[key]
            if not isinstance(value, str) or (key == "bucket" and not value.strip()):
                raise StateConfigError(f"[state] {key} must be a non-empty string")
            values[key] = value.strip()
    if "retention_days" in table:
        days = table["retention_days"]
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            raise StateConfigError("[state] retention_days must be a whole number of days, at least 1")
        values["retention_days"] = days
    if "flyte_bucket" in table:
        raw = table["flyte_bucket"]
        items = [raw] if isinstance(raw, str) else raw
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            raise StateConfigError("[state] flyte_bucket must be a URI or a list of URIs")
        values["flyte_bucket"] = tuple(i.strip() for i in items)
        for item in values["flyte_bucket"]:
            check_flyte_prefix(item)

    backend = values.get("backend", "")
    if not backend:
        backend = "bucket" if values.get("bucket") else "volume" if values.get("volume") else "git"
        if values.get("bucket") and values.get("volume"):
            raise StateConfigError("[state] bucket and volume are both set: choose one with backend")
    if backend not in BACKENDS:
        raise StateConfigError(f"[state] backend must be one of {', '.join(BACKENDS)}, not {backend!r}")
    values["backend"] = backend
    state = State(**values)
    _check_fields_match_backend(state)
    if state.bucket:
        _check_bucket(state)
    if state.volume:
        _check_volume(state.volume)
    for key in _SERVICE_ACCOUNT_KEYS:
        name = getattr(state, key)
        if name and not (len(name) <= 253 and _K8S_NAME.match(name)):
            # Not echoed, like every value in this table.
            raise StateConfigError(
                f"[state] {key} must be a Kubernetes name: lower case letters, digits, '-'"
            )
    return state


def _check_fields_match_backend(state: State) -> None:
    owner = {
        "bucket": "bucket",
        "endpoint": "bucket",
        "region": "bucket",
        "volume": "volume",
        "volume_claim": "volume",
    }
    for field, backend in owner.items():
        if getattr(state, field) and state.backend != backend:
            raise StateConfigError(
                f"[state] {field} is set but backend is {state.backend!r}, not {backend!r}"
            )
    if state.backend == "bucket" and not state.bucket:
        raise StateConfigError(
            "[state] backend = bucket needs bucket (s3://bucket/prefix or gs://bucket/prefix)"
        )
    if state.backend == "volume" and not state.volume:
        raise StateConfigError("[state] backend = volume needs volume (an absolute directory)")
    if state.backend == "volume" and not state.volume_claim:
        raise StateConfigError(
            "[state] backend = volume needs volume_claim (the PersistentVolumeClaim mounted at volume)"
        )
    if state.backend != "git" and state.branch:
        raise StateConfigError(f"[state] branch is the git backend's, but backend is {state.backend!r}")


def has_credentials_or_query(parts: SplitResult) -> bool:
    """Whether a split URL carries userinfo, a query or a fragment: none belongs in a bucket or endpoint.

    Shared with `_state_bucket`, so that a path which bypasses the profile parser refuses the same things.
    """
    return "@" in parts.netloc or bool(parts.query) or bool(parts.fragment)


def plain_http_is_allowed(hostname: str | None) -> bool:
    """Whether an endpoint on `hostname` may use http: only a MinIO on the same machine.

    `_assets/browser/browser_archive.lease_store` repeats this tuple (the app image cannot import the engine).
    """
    return hostname in _LOCAL_HOSTS


def _check_bucket(state: State) -> None:
    # No value is echoed back below: a mistyped bucket could be `s3://KEY:SECRET@bucket`.
    parts = urlsplit(state.bucket)
    if parts.scheme not in BUCKET_SCHEMES:
        raise StateConfigError(
            f"[state] bucket must start with {' or '.join(s + '://' for s in BUCKET_SCHEMES)}"
        )
    if not parts.hostname or has_credentials_or_query(parts):
        raise StateConfigError(
            "[state] bucket must be scheme://bucket/prefix, with no credentials, query or fragment: "
            "the pod's own identity is used"
        )
    if state.endpoint:
        _check_endpoint(state.endpoint, parts.scheme)


def _check_endpoint(endpoint: str, scheme: str) -> None:
    if scheme != "s3":
        raise StateConfigError("[state] endpoint is for s3:// buckets only")
    parts = urlsplit(endpoint)
    # The value is not echoed: it may carry a key or token.
    if parts.scheme not in ("http", "https") or not parts.hostname or has_credentials_or_query(parts):
        raise StateConfigError(
            "[state] endpoint must be an http(s) URL with no credentials, query or fragment"
        )
    # Credentials are signed into every request: plain http is for a MinIO next to the factory only.
    if parts.scheme == "http" and not plain_http_is_allowed(parts.hostname):
        raise StateConfigError("[state] endpoint may use http only for localhost; use https")


def _check_volume(volume: str) -> None:
    if not volume.startswith("/"):
        raise StateConfigError("[state] volume must be an absolute path")
    if ".." in volume.split("/"):
        raise StateConfigError("[state] volume must not contain '..'")


def default_profile_uri(state: State, identity: str) -> str:
    """Return where a browser identity's archive goes when it names none; "" for the git backend."""
    if state.backend == "bucket":
        return f"{state.bucket.rstrip('/')}/browser/{identity}/profile.tar.gz"
    if state.backend == "volume":
        return f"file://{posixpath.normpath(state.volume)}/browser/{identity}/profile.tar.gz"
    return ""


def _split(uri: str) -> tuple[str, str, str]:
    """Return (scheme family, bucket or host, path) of a storage URI; scheme aliases give one family."""
    parts = urlsplit(uri.strip())
    scheme = parts.scheme.lower()
    return (
        _SCHEME_FAMILY.get(scheme, scheme),
        (parts.hostname or "").lower(),  # not netloc: `u@bucket` and `bucket:443` are the bucket
        posixpath.normpath("/" + parts.path.strip("/")),
    )


def check_flyte_prefix(prefix: str, field: str = "[state] flyte_bucket") -> None:
    """Raise StateConfigError when `prefix` could never match a URI, so that the guard would skip it.

    A bare name (`flyte-data`) has no scheme family: `refuse_flyte_bucket` would compare it with nothing and
    let a state bucket inside Flyte's own through. The value is not echoed.
    """
    family, bucket, _ = _split(prefix)
    if not family or (family != "file" and not bucket):
        raise StateConfigError(
            f"{field} entries must be scheme://bucket (s3://, gs://), or file:///path; "
            "a bare bucket name would match nothing"
        )


def refuse_flyte_bucket(
    uri: str, flyte_prefixes: tuple[str, ...] | list[str], field: str = "[state] bucket"
) -> None:
    """Raise StateConfigError when `uri` is inside Flyte's data bucket.

    The factory's state and a browser's credential archive must not live where Flyte writes task data:
    anyone who can read a run's inputs could read them, and Flyte's lifecycle rules could delete them.
    Same bucket is refused whatever the prefix; `s3`/`s3a`/`s3n` and `gs`/`gcs` name the same bucket. For a
    path on a file system (no bucket) `uri` is refused when it and a Flyte path contain one another.
    """
    family, bucket, path = _split(uri)
    for prefix in flyte_prefixes:
        check_flyte_prefix(prefix)
        f_family, f_bucket, f_path = _split(prefix)
        if (family, bucket) != (f_family, f_bucket):
            continue
        if family in ("s3", "gs") or _contains(path, f_path) or _contains(f_path, path):
            raise StateConfigError(
                f"{field} is inside Flyte's own data bucket ({f_family}://{f_bucket}); "
                "use a separate bucket for the factory's state"
            )


def check_not_flyte_bucket(state: State, profile_uris: list[str]) -> None:
    """Apply `refuse_flyte_bucket` to the state bucket and each browser archive the profile names."""
    if state.bucket:
        refuse_flyte_bucket(state.bucket, state.flyte_bucket, "[state] bucket")
    for uri in profile_uris:
        refuse_flyte_bucket(uri, state.flyte_bucket, "[browser] profile_uri")


def _contains(outer: str, inner: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def runtime_flyte_prefixes() -> tuple[str, ...]:
    """Return the raw-data prefix of the Flyte task this process runs in, if any (best effort)."""
    import flyte

    # Outside a task `ctx()` is a null context with no raw-data path: nothing to compare.
    path = getattr(getattr(flyte.ctx(), "raw_data_path", None), "path", None)
    return (path,) if isinstance(path, str) and re.match(r"[a-z0-9]+://", path) else ()
