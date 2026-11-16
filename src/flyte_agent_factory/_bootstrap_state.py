"""Where the bootstrap puts the state: the factory's own bucket, and Flyte's bucket to keep clear of.

The state bucket comes from, in order, `--state-bucket`, `AGENT_FACTORY_STATE_BUCKET`, and (only when
`--terraform-dir` is given) the `factory_bucket` output of `terraform output -json` in that directory
(`factory_bucket_region`, `factory_bucket_endpoint` and the service account names in
`factory_service_accounts` go with it; `--factory-service-account` and `--browser-service-account` win
over them). Terraform is only ever asked for its
outputs, never to plan or apply. With none of them the profile keeps the git store.

Flyte's data bucket is not in the local Flyte config (its `storage` section holds credentials and an endpoint,
and a run's raw-data path is chosen at run time), so what is found here is best effort: `--flyte-bucket`, a
`raw_data_path` the config does carry, and the raw-data path of the task this runs in. It goes into the
profile as `[state] flyte_bucket`, which is what the profile load and the store's first use check.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _profile_state
from ._bootstrap_base import BootstrapError

ENV_BUCKET = "AGENT_FACTORY_STATE_BUCKET"
_TERRAFORM_SECONDS = 60
# The scheme is required: the operator did not type this value, so one that could never match is dropped
# rather than refused (an explicit `--flyte-bucket` is refused instead, see `find_flyte_buckets`).
_RAW_DATA_KEY = re.compile(
    r"^\s*(?:raw[_-]?data[_-]?path|rawDataPath)\s*:\s*[\"']?([a-z0-9]+://\S+)", re.IGNORECASE | re.MULTILINE
)
VERSIONING = "turn object versioning on for the bucket (the Terraform module does); it undoes a bad write"
KEEP_GIT = (
    "kept on a git branch. A bucket is recommended for production: run again with --state-bucket "
    "s3://BUCKET (or gs://), or set AGENT_FACTORY_STATE_BUCKET, or give --terraform-dir DIR "
    "(its `factory_bucket` output); see docs/state-store.md"
)


@dataclass(frozen=True)
class StateBucket:
    uri: str
    region: str = ""
    endpoint: str = ""
    factory_service_account: str = ""  # the Kubernetes service account of every pod that touches state
    browser_service_account: str = ""  # that of the personal browser apps
    source: str = ""  # what it was found in, for the line the bootstrap says


def find_flyte_buckets(config_path: Path, explicit: Sequence[str] = ()) -> tuple[str, ...]:
    """Return Flyte's data bucket(s) as far as they can be known here (see the module docstring)."""
    for item in explicit:
        try:
            _profile_state.check_flyte_prefix(item.strip("\"'"), "--flyte-bucket")
        except _profile_state.StateConfigError as exc:
            raise BootstrapError(f"--flyte-bucket is refused: {exc}") from exc
    found = [*explicit]
    try:
        found += _RAW_DATA_KEY.findall(config_path.read_text())
    except OSError:
        pass
    found += _profile_state.runtime_flyte_prefixes()
    return tuple(dict.fromkeys(item.strip("\"'") for item in found))


def _terraform_output(
    directory: Path, run: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[StateBucket | None, str]:
    """Return (the bucket the outputs name, "") or (None, why not)."""
    try:
        proc = run(
            ["terraform", f"-chdir={directory}", "output", "-json"],
            capture_output=True,
            text=True,
            timeout=_TERRAFORM_SECONDS,
        )
    except FileNotFoundError:
        return None, "terraform is not installed"
    except subprocess.TimeoutExpired:
        return None, "terraform output timed out"
    if proc.returncode != 0:
        return None, "terraform output failed (is it initialised and applied?)"
    try:
        outputs = json.loads(proc.stdout)
    except ValueError:
        return None, "terraform output is not JSON"

    def value(name: str) -> str:
        entry = outputs.get(name) if isinstance(outputs, dict) else None
        got = entry.get("value") if isinstance(entry, dict) else None
        return got.strip() if isinstance(got, str) else ""

    def account(role: str) -> str:
        # `factory_service_accounts` is `{factory = {name = ...}, browser = {name = ...}}`
        accounts = outputs.get("factory_service_accounts") if isinstance(outputs, dict) else None
        accounts = accounts.get("value") if isinstance(accounts, dict) else None
        entry = accounts.get(role) if isinstance(accounts, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        return name.strip() if isinstance(name, str) else ""

    uri = value("factory_bucket")
    if not uri:
        return None, "the Terraform outputs have no factory_bucket"
    return StateBucket(
        uri,
        value("factory_bucket_region"),
        value("factory_bucket_endpoint"),
        account("factory"),
        account("browser"),
        "terraform",
    ), ""


def find_state_bucket(
    *,
    explicit: str,
    region: str,
    endpoint: str,
    factory_service_account: str = "",
    browser_service_account: str = "",
    environ: Mapping[str, str],
    terraform_dir: Path | None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[StateBucket | None, str]:
    """Return the state bucket to use (or None) and, when a Terraform lookup found none, why."""
    if explicit:
        return StateBucket(
            explicit.strip(),
            region,
            endpoint,
            factory_service_account,
            browser_service_account,
            "--state-bucket",
        ), ""
    if environ.get(ENV_BUCKET, "").strip():
        return StateBucket(
            environ[ENV_BUCKET].strip(),
            region,
            endpoint,
            factory_service_account,
            browser_service_account,
            ENV_BUCKET,
        ), ""
    if terraform_dir is not None:
        found, why = _terraform_output(terraform_dir, run)
        if found is not None:
            return StateBucket(
                found.uri,
                region or found.region,
                endpoint or found.endpoint,
                factory_service_account or found.factory_service_account,
                browser_service_account or found.browser_service_account,
                found.source,
            ), ""
        return None, why
    return None, ""


def check_state_bucket(bucket: StateBucket, flyte_buckets: Sequence[str]) -> None:
    """Raise BootstrapError when `bucket` is not a usable state bucket, or is inside Flyte's own.

    Never echoes the value: a mistyped bucket could carry a credential.
    """
    table: dict[str, object] = {"bucket": bucket.uri, "flyte_bucket": list(flyte_buckets)}
    table.update(
        {
            k: v
            for k, v in (
                ("region", bucket.region),
                ("endpoint", bucket.endpoint),
                ("factory_service_account", bucket.factory_service_account),
                ("browser_service_account", bucket.browser_service_account),
            )
            if v
        }
    )
    try:
        state = _profile_state.parse(table)
        _profile_state.check_not_flyte_bucket(state, [])
    except _profile_state.StateConfigError as exc:
        raise BootstrapError(f"the state bucket from {bucket.source} is refused: {exc}") from exc
