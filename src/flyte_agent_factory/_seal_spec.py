"""The seal spec: what a sealed benchmark run may reach, validated once at parse time."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ._seal_shell import normalize_project

DEFAULT_MODEL_HOSTS = ("api.anthropic.com",)
#: Hosts the codex CLI needs besides the Anthropic API (the design's layer 3).
_CODEX_HOSTS = ("api.openai.com", "chatgpt.com")

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def model_hosts_for(engine: str) -> tuple[str, ...]:
    """Return the model API hosts a role running `engine` may CONNECT to."""
    if engine == "codex":
        return DEFAULT_MODEL_HOSTS + _CODEX_HOSTS
    return DEFAULT_MODEL_HOSTS


def _https_url(name: str, value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"seal spec {name}: expected a string, got {value!r}")
    if allow_empty and value == "":
        return ""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"seal spec {name}: expected an https URL, got {value!r}")
    return value


def _string_list(name: str, value: Any, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"seal spec {name}: expected a list of strings, got {value!r}")
    if not value and not allow_empty:
        raise ValueError(f"seal spec {name}: must not be empty")
    return tuple(value)


def _utc_instant(name: str, value: Any) -> str:
    """Normalise an ISO date or datetime to `YYYY-MM-DDTHH:MM:SSZ`; a date is midnight, naive means UTC."""
    if isinstance(value, datetime):  # TOML reads an unquoted datetime as one
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"seal spec {name}: not an ISO-8601 date or datetime: {value!r}") from None
    else:
        raise ValueError(f"seal spec {name}: expected an ISO-8601 date or datetime, got {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SealSpec:
    """Operator-set limits of a sealed run; `sha256()` of it goes in every role's record."""

    task_repo: str
    base_sha: str
    index_cutoff: str
    task_project: str = ""
    model_hosts: tuple[str, ...] = field(default=DEFAULT_MODEL_HOSTS)
    push_remote: str = ""
    forbidden_paths: tuple[str, ...] = ()
    allow_index: bool = True
    #: Where the task snapshot goes, relative to the project's folder in the workspace (or the work dir).
    task_dir: str = "repo"

    @classmethod
    def from_mapping(cls, data: Any) -> SealSpec:
        """Validate `data` and build a spec; raise `ValueError` naming the first bad field."""
        if not isinstance(data, dict):
            raise ValueError(f"seal spec: expected an object, got {type(data).__name__}")
        known = {f.name for f in fields(cls)}
        for key in data:
            if key not in known:
                raise ValueError(f"seal spec {key}: unknown key")
        for name in ("task_repo", "base_sha", "index_cutoff"):
            if name not in data:
                raise ValueError(f"seal spec {name}: required")
        base_sha = data["base_sha"]
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"seal spec base_sha: expected 40 lowercase hex characters, got {base_sha!r}")
        task_project = data.get("task_project", "")
        if not isinstance(task_project, str):
            raise ValueError(f"seal spec task_project: expected a string, got {task_project!r}")
        model_hosts = _string_list(
            "model_hosts", data.get("model_hosts", DEFAULT_MODEL_HOSTS), allow_empty=False
        )
        for host in model_hosts:
            if not _HOST_RE.fullmatch(host):
                raise ValueError(f"seal spec model_hosts: not a lowercase host name: {host!r}")
        allow_index = data.get("allow_index", True)
        if not isinstance(allow_index, bool):
            raise ValueError(f"seal spec allow_index: expected true or false, got {allow_index!r}")
        task_dir = data.get("task_dir", "repo")
        parts = PurePosixPath(task_dir).parts if isinstance(task_dir, str) else ()
        if not parts or PurePosixPath(task_dir).is_absolute() or ".." in parts:
            raise ValueError(f"seal spec task_dir: expected a relative path without '..', got {task_dir!r}")
        return cls(
            task_repo=_https_url("task_repo", data["task_repo"], allow_empty=False),
            base_sha=base_sha,
            index_cutoff=_utc_instant("index_cutoff", data["index_cutoff"]),
            task_project=task_project,
            model_hosts=model_hosts,
            push_remote=_https_url("push_remote", data.get("push_remote", ""), allow_empty=True),
            forbidden_paths=_string_list(
                "forbidden_paths", data.get("forbidden_paths", ()), allow_empty=True
            ),
            allow_index=allow_index,
            task_dir=task_dir,
        )

    @classmethod
    def from_json(cls, text: str) -> SealSpec:
        """Parse a JSON object (the `seal` launch input) into a spec."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"seal spec: not valid JSON: {error}") from None
        if not isinstance(data, dict):
            raise ValueError("seal spec: JSON must be an object")
        return cls.from_mapping(data)

    @classmethod
    def from_toml(cls, path: Path) -> SealSpec:
        """Read a spec from a flat TOML file."""
        with Path(path).open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no spaces. The form that is hashed."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        """Hex SHA-256 of `to_json()`."""
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def normalized_project(self) -> str:
        """`task_project` as PEP 503 normalises it ('' stays '')."""
        return normalize_project(self.task_project)
