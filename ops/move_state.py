r"""Move the factory's state from one backend to another, in one command, and back again.

    PYTHONPATH=src uv run python ops/move_state.py --from git --to s3://bucket/prefix \
        [--region R] [--endpoint URL] [--dry-run] [--overwrite] [--profile NAME]

A backend is named as:

  git                  the profile's `[repo] project` and state branch (or `--git-remote` / `--git-branch`)
  profile              whatever the profile's `[state]` selects now
  s3://b/p, gs://b/p   a bucket (`--endpoint` / `--region` for the destination, `--from-endpoint` /
                       `--from-region` for the source; credentials come from the environment, never a flag)
  file:///path         a directory on a volume
  memory://name        an in-process store, for tests

`--from-legacy <old [state] uri>` replaces `--from` and reads the old Flyte-bucket layout through
`migrate_state`, which keeps its three-way merge and its renumbering of colliding requests.

Every state key is copied (the bucket's `audit/` records are not state and stay). A key the destination
already has with other content is a conflict: reported and kept, unless `--overwrite`. Running it again copies
nothing new. A key only the destination has is reported ("only at destination", exit 1) and nothing is
written; `--mirror` deletes it, so the destination ends with the source's keys and no others. That is what
makes it reversible: `--from git --to bucket`, then `--from bucket --to git --mirror` gives back
byte-identical keys, without bringing back what the factory deleted in between. Nothing is deleted at the
source. The last line says which `[state]` lines to set to switch over.

`--browser IDENTITY --from-profile-uri OLD --to <bucket or file:///dir>` moves a personal browser's archive
to `<to>/browser/<identity>/profile.tar.gz` instead (see `docs/state-store.md`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from obstore.exceptions import BaseError as ObstoreError
from obstore.exceptions import NotFoundError

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # migrate_state, the reader of the legacy layout
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src/flyte_agent_factory/_assets/browser"))

from flyte_agent_factory import _profile

_profile.select_from_argv()  # before anything reads the profile

import browser_archive  # noqa: E402 -- the app's own archive helpers (is_sealed, open_fs)

# Beside this file, so not on mypy's path (adding ops/ to it finds every script twice).
import migrate_state  # type: ignore[import-not-found]  # noqa: E402 -- its import also honours `--profile`

from flyte_agent_factory import _profile_state, _state_bucket, _state_store, _state_volume  # noqa: E402 -- after select_from_argv(), like the imports above

_memory: dict[str, _state_bucket.ObstoreAdapter] = {}


class MoveError(Exception):
    """A request this tool cannot carry out; the message is safe to print."""


def _profile_or_none() -> _profile.Profile | None:
    try:
        return _profile.current()
    except _profile.ProfileError as exc:
        raise MoveError(f"the profile does not load: {exc}") from exc


def _flyte_prefixes() -> tuple[str, ...]:
    profile = _profile_or_none()
    return (*(profile.state.flyte_bucket if profile else ()), *_profile_state.runtime_flyte_prefixes())


def _open_git_store(remote: str, branch: str) -> _state_store.Store:
    profile = None if remote and branch else _profile_or_none()
    if not remote:
        if profile is None:
            raise MoveError("git needs a profile (--profile) or --git-remote")
        remote = profile.repo
    if not branch:
        if profile is None:
            raise MoveError("git needs a profile (--profile) or --git-branch")
        branch = profile.state.branch or _state_store.default_branch(
            profile.backend.project, profile.backend.domain
        )
    if not _state_store.valid_branch(branch):
        raise MoveError("--git-branch is not a valid git branch name")
    return _state_store.git_store(remote, branch)


def open_backend(
    spec: str, *, endpoint: str = "", region: str = "", git_remote: str = "", git_branch: str = ""
) -> _state_store.Store:
    """Return the store `spec` names (see the module docstring)."""
    if spec == "profile":
        profile = _profile_or_none()
        if profile is None:
            raise MoveError("`profile` needs a profile: pass --profile NAME")
        if profile.state.backend == "git" or git_remote or git_branch:
            return _open_git_store(git_remote, git_branch)
        # The engine's own choice, so a move to `profile` has the retention and the runtime Flyte-bucket
        # check the factory itself has.
        return _state_store.current()
    if spec == "git":
        return _open_git_store(git_remote, git_branch)
    scheme, _, rest = spec.partition("://")
    if scheme == "memory":
        adapter = _memory.setdefault(rest, _state_bucket.open_adapter("memory://"))
        return _state_bucket.BucketStore(adapter)
    if scheme == "file":
        path = spec[len("file://") :]
        if not path.startswith("/"):
            raise MoveError("file:// needs an absolute path: file:///path")
        return _state_volume.open_store(path)
    if scheme in _profile_state.BUCKET_SCHEMES:
        try:
            return _state_bucket.open_store(spec, endpoint=endpoint, region=region)
        except ValueError as exc:
            raise MoveError(str(exc)) from exc
    raise MoveError("a backend is git, profile, s3://, gs://, file:///path or memory://name")


def _refuse_flyte_bucket(spec: str, field: str) -> None:
    try:  # any scheme: `gcs://` and `s3a://` are aliases it knows, and `git` or a path matches nothing
        _profile_state.refuse_flyte_bucket(spec, _flyte_prefixes(), field)
    except _profile_state.StateConfigError as exc:
        raise MoveError(str(exc)) from exc


def copy_state(
    source: _state_store.Store,
    dest: _state_store.Store,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    mirror: bool = False,
    allow_empty: bool = False,
    message: str = "move state",
) -> dict[str, list[str]]:
    """Copy every key of `source` that `dest` lacks (or, with `overwrite`, has with other content).

    Returns the keys by outcome: `copied` (would be, on a dry run), `same`, `conflict` and
    `only_at_destination` (kept, or with `mirror` deleted in the same write, so the destination's keys become
    the source's). Without `mirror`, a destination with keys the source lacks gets no write at all: the keys
    that would have been copied are returned as `held`. The writes are one `transact` on `dest`, decided
    against the destination as it is when they are made.
    """
    snap = source.snapshot(fresh=True)
    wanted = snap.read_many(snap.list())
    if not wanted:
        # Before any write: an empty or mistyped prefix or branch looks exactly like a finished move, and with
        # `mirror` it would delete every key at the destination.
        if mirror:
            raise MoveError(
                "the source has no state keys and --mirror would delete every key at the destination: check "
                "--from (an empty or mistyped prefix or branch looks exactly like a finished move)"
            )
        if not allow_empty:
            raise MoveError(
                "the source has no state keys: check --from (an empty or mistyped prefix or branch looks "
                "exactly like a finished move); --allow-empty-source copies nothing and switches over"
            )

    def decide(have: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], dict[str, list[str]]]:
        changes: dict[str, bytes | None] = {}
        result: dict[str, list[str]] = {"copied": [], "same": [], "conflict": [], "only_at_destination": []}
        current = have.read_many(have.list())
        for key, body in sorted(wanted.items()):
            if key not in current:
                outcome = "copied"
            elif current[key] == body:
                outcome = "same"
            else:
                outcome = "copied" if overwrite else "conflict"
            if outcome == "copied":
                changes[key] = body
            result[outcome].append(key)
        result["only_at_destination"] = sorted(set(current) - set(wanted))
        if mirror:
            changes.update(dict.fromkeys(result["only_at_destination"]))
        elif result["only_at_destination"]:
            # The destination has moved on since the source was its copy: merging the rest in would leave a
            # store that is neither. Write nothing; what would have been copied is held.
            result["held"], result["copied"], changes = result["copied"], [], {}
        return changes, result

    if dry_run:
        return decide(dest.snapshot(fresh=True))[1]
    return dest.transact(decide, message)


def _switch_over_line(spec: str, *, endpoint: str, region: str, git_branch: str) -> str:
    """Return the last line: the `[state]` lines that make the profile use the destination."""
    scheme = spec.partition("://")[0]
    if scheme in _profile_state.BUCKET_SCHEMES:
        extra = "".join(f' {k} = "{v}"' for k, v in (("region", region), ("endpoint", endpoint)) if v)
        return (
            f'switch over: in the profile\'s [state] set bucket = "{spec}"{extra} (and unset volume, branch)'
        )
    if scheme == "file":
        return (
            f'switch over: in the profile\'s [state] set volume = "{spec[len("file://") :]}" '
            'and volume_claim = "<the PVC mounted there>" (unset bucket)'
        )
    if spec == "git":
        branch = f' and branch = "{git_branch}"' if git_branch else ""
        unset = "bucket, endpoint, region, volume and volume_claim"
        return f"switch over: in the profile's [state] unset {unset}{branch}"
    return "no switch-over line: the destination is not a profile backend"


def _print_result(result: Mapping[str, list[str]], dry_run: bool, mirror: bool) -> None:
    for key in result["only_at_destination"]:
        if mirror:
            print(f"{'would delete' if dry_run else 'deleted'} at the destination: {key}")
        else:
            print(f"only at destination (kept; --mirror deletes it): {key}")
    for key in result["conflict"]:
        print(f"conflict (the destination's is kept; --overwrite takes the source's): {key}")
    for key in result.get("held", ()):
        print(f"held (nothing is written while only the destination has keys): {key}")
    for key in result["copied"]:
        print(f"{'would copy' if dry_run else 'copied'}: {key}")
    print(
        f"{'would copy' if dry_run else 'copied'} {len(result['copied'])}, same {len(result['same'])}, "
        f"conflict {len(result['conflict'])}, only at destination {len(result['only_at_destination'])}"
    )


def move_legacy(source_uri: str, dest: _state_store.Store, *, dry_run: bool, overwrite: bool) -> int:
    """Carry the old Flyte-bucket layout onto `dest` with `migrate_state`'s merge; return the exit code."""
    bucket = migrate_state.read_bucket(source_uri)
    if not any(migrate_state.in_layout(key) for key in bucket):
        # An empty or mistyped prefix looks exactly like a finished move; refuse as migrate_state does.
        print(
            f"no factory state under {_state_store._redact(source_uri)}: check --from-legacy", file=sys.stderr
        )
        return 2
    outcome = migrate_state.migrate(bucket, dest, dry_run=dry_run, overwrite=overwrite)
    for key in outcome["conflicts"]:
        print(f"conflict (the destination's is newer and kept; --overwrite takes the old bucket's): {key}")
    for group, gen in outcome["renumbered"].items():
        print(f"renumbered: {group} -> generation {gen} (the destination has another request at that one)")
    counts = outcome["counts"]
    print(
        f"{'would copy' if dry_run else 'copied'} {counts.get('copied', 0)}, same {counts.get('same', 0)}, "
        f"conflict {counts.get('conflict', 0)}, skipped {counts.get('skipped', 0)} (not state)"
    )
    return 0


def _fetch(store: Any, key: str) -> tuple[bytes, Any] | None:
    """Return the object's body and metadata, or None when there is none."""
    try:
        got = browser_archive.lease_get(store, key)
    except (NotFoundError, FileNotFoundError):
        return None
    return bytes(got.bytes()), got.meta


def _object_key(uri: str) -> str:
    """Return the key `personal.Lease` uses for `uri` (its path, without the leading slash)."""
    return urlsplit(uri).path.lstrip("/")


def _listed_key(entry: str, uri: str) -> str | None:
    """Return the object key of an archive the old `.lease` lists (`bucket/path`, or a path for file)."""
    parts = urlsplit(uri)
    if parts.scheme == "file":
        return entry.lstrip("/")
    prefix = f"{parts.netloc}/"
    return entry[len(prefix) :] if entry.startswith(prefix) else None


def move_browser(
    old_uri: str,
    new_uri: str,
    *,
    from_endpoint: str = "",
    from_region: str = "",
    endpoint: str = "",
    region: str = "",
    dry_run: bool,
    overwrite: bool,
    say: Any = print,
) -> dict[str, list[str]]:
    """Copy a browser identity's archive objects from `old_uri` to `new_uri`; the source is never touched.

    The newest archive is the first one the old `.lease` object lists (a `.pending-*` generation), else
    `old_uri` itself; the one behind it becomes `.prev`. The lease itself is not copied: it names objects
    at the old place, and the app makes a new one and restores from `profile.tar.gz`, then `.prev`.
    The bytes are copied as they are: a sealed archive stays sealed, a plaintext one (from before
    encryption) is re-sealed by the app on its next snapshot.

    Objects are read and written through obstore (the app's lease code), not fsspec: the engine's
    environment has no fsspec bucket drivers. A write to a place with an archive already (`overwrite`) is
    conditional on the e_tag read, as the app's lease renewal is. A `file://` destination leaves a `.flock`
    sidecar beside each file, as the app does.
    """
    old_store = browser_archive.lease_store(old_uri, endpoint=from_endpoint, region=from_region)
    new_store = browser_archive.lease_store(new_uri, endpoint=endpoint, region=region)
    old_key, new_key = _object_key(old_uri), _object_key(new_uri)
    listed: list[str] = []
    lease = _fetch(old_store, old_key + ".lease")
    if lease:
        try:
            entries = json.loads(lease[0]).get("archives", [])
            listed = [k for k in (_listed_key(e, old_uri) for e in entries) if k]
        except (ValueError, AttributeError, TypeError):
            listed = []
    found = [
        (key, got[0])
        for key in (listed or [old_key, old_key + ".prev"])
        if (got := _fetch(old_store, key)) is not None
    ]
    if not found:
        raise MoveError(f"no archive at {_state_store._redact(old_uri)}: check --from-profile-uri")
    result: dict[str, list[str]] = {"copied": [], "same": [], "conflict": []}
    for (_, body), target in zip(found, (new_key, new_key + ".prev"), strict=False):
        existing = _fetch(new_store, target)
        if existing is not None and existing[0] == body:
            outcome = "same"
        elif existing is None or overwrite:
            outcome = "copied"
            if not dry_run:
                mode: Any = "create"
                if existing is not None:
                    mode = {k: v for k, v in existing[1].items() if k in ("e_tag", "version") and v}
                browser_archive.lease_put(new_store, target, body, mode)
        else:
            outcome = "conflict"
        result[outcome].append(target)
        kind = "sealed" if browser_archive.is_sealed(body) else "plaintext legacy archive"
        say(f"{outcome}: {target.rsplit('/', 1)[-1]} ({kind}, {len(body)} bytes)")
        if outcome != "conflict" and not browser_archive.is_sealed(body):
            say("  the app seals it on its next snapshot; until then it is as readable as it was")
    return result


def _browser_uri(to: str, identity: str) -> str:
    base = to.rstrip("/")
    if to.partition("://")[0] not in (*_profile_state.BUCKET_SCHEMES, "file"):
        raise MoveError(
            "--browser needs --to s3://..., gs://... or file:///dir (where the identity's archive goes)"
        )
    return f"{base}/browser/{identity}/profile.tar.gz"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ops/move_state.py", description=__doc__.split("\n\n")[0])
    where = ap.add_mutually_exclusive_group()
    where.add_argument(
        "--from", dest="source", metavar="BACKEND", help="git, profile, s3://, gs://, file:///, memory://"
    )
    where.add_argument(
        "--from-legacy", metavar="URI", help="the old Flyte-bucket layout (the old [state] uri)"
    )
    ap.add_argument("--to", required=True, metavar="BACKEND", help="the same names as --from")
    ap.add_argument("--endpoint", default="", help="S3-compatible endpoint of the destination bucket")
    ap.add_argument("--region", default="", help="region of the destination bucket")
    ap.add_argument("--from-endpoint", default="", help="S3-compatible endpoint of the source bucket")
    ap.add_argument("--from-region", default="", help="region of the source bucket")
    ap.add_argument(
        "--git-remote", default="", help="git remote for `git` (default: the profile's [repo] project)"
    )
    ap.add_argument("--git-branch", default="", help="state branch for `git` (default: the profile's)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be copied; write nothing")
    ap.add_argument(
        "--overwrite", action="store_true", help="replace a key whose content differs at the destination"
    )
    ap.add_argument(
        "--mirror",
        action="store_true",
        help="delete the keys only the destination has, so it ends with the source's keys and no others",
    )
    ap.add_argument(
        "--allow-empty-source",
        action="store_true",
        help="proceed when the source has no state keys, e.g. a fresh install switching to an empty bucket; "
        "never with --mirror",
    )
    ap.add_argument(
        "--browser", metavar="IDENTITY", help="move this browser identity's archive instead of the state"
    )
    ap.add_argument("--from-profile-uri", default="", help="with --browser: the identity's old profile_uri")
    ap.add_argument("--profile", help="the deployment profile (as for the other ops tools)")
    return ap


def main(argv: list[str] | None = None) -> int:
    """Run the move from the command line; return the exit code."""
    ap = _build_parser()
    args = ap.parse_args(argv)
    try:
        return _run(ap, args)
    except (
        MoveError,
        _state_store.StateError,
        OSError,
        ValueError,
        ObstoreError,
        _profile.ProfileError,
    ) as exc:
        # An object store's error message carries the request URL, so it is caught here rather than printed
        # as a traceback. The message passes through the redaction of `scheme://user:pass@` (a git remote
        # with a token, an endpoint with a key).
        print(f"error: {type(exc).__name__}: {_state_store._redact(str(exc))}", file=sys.stderr)
        return 1


def _refuse_credentials(value: str, flag: str) -> None:
    if value and _profile_state.has_credentials_or_query(urlsplit(value)):
        raise MoveError(f"{flag} must carry no credentials, query or fragment: the environment's are used")


def _run(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    # One boundary for every argument that names a place, before anything is opened or parsed by a library
    # that would echo it. `--git-remote` is left out: a token there is a legitimate form, redacted on print.
    for value, flag in (
        (args.source or "", "--from"),
        (args.to, "--to"),
        (args.from_legacy or "", "--from-legacy"),
        (args.from_profile_uri, "--from-profile-uri"),
        (args.endpoint, "--endpoint"),
        (args.from_endpoint, "--from-endpoint"),
    ):
        _refuse_credentials(value, flag)
    if args.browser:
        if not args.from_profile_uri:
            ap.error("--browser needs --from-profile-uri")
        _refuse_flyte_bucket(args.to, "--to")
        new_uri = _browser_uri(args.to, args.browser)
        _refuse_flyte_bucket(new_uri, "the new profile_uri")
        result = move_browser(
            args.from_profile_uri,
            new_uri,
            from_endpoint=args.from_endpoint,
            from_region=args.from_region,
            endpoint=args.endpoint,
            region=args.region,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        if not args.dry_run and not result["conflict"]:
            print(f'switch over: set profile_uri = "{new_uri}" for {args.browser}, or leave it out (default)')
            print(
                "the old archive is untouched; when the identity runs from the new place, delete "
                f"{_state_store._redact(args.from_profile_uri)} and its .prev, .lease and .pending-* objects"
            )
        return 1 if result["conflict"] else 0
    if not (args.source or args.from_legacy):
        ap.error("one of --from or --from-legacy is required")
    _refuse_flyte_bucket(args.to, "--to")
    dest = open_backend(
        args.to,
        endpoint=args.endpoint,
        region=args.region,
        git_remote=args.git_remote,
        git_branch=args.git_branch,
    )
    if args.from_legacy:
        code = move_legacy(args.from_legacy, dest, dry_run=args.dry_run, overwrite=args.overwrite)
    else:
        source = open_backend(
            args.source,
            endpoint=args.from_endpoint,
            region=args.from_region,
            git_remote=args.git_remote,
            git_branch=args.git_branch,
        )
        message = f"move state from {_state_store._redact(args.source)}"
        result = copy_state(
            source,
            dest,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            mirror=args.mirror,
            allow_empty=args.allow_empty_source,
            message=message,
        )
        _print_result(result, args.dry_run, args.mirror)
        kept_extra = bool(result["only_at_destination"]) and not args.mirror
        code = 1 if result["conflict"] or kept_extra else 0
        if code and not args.dry_run:
            print(
                "the stores differ: the destination was left as it is for the keys above; "
                "check them, then run again with --overwrite to take the source's for a conflict, "
                "and --mirror to delete a key only the destination has",
                file=sys.stderr,
            )
    if code == 0 and not args.dry_run:
        print(
            _switch_over_line(
                _state_store._redact(args.to),
                endpoint=_state_store._redact(args.endpoint),
                region=args.region,
                git_branch=args.git_branch,
            )
        )
        print("the source is untouched; delete it yourself once the factory runs from the new place")
    return code


if __name__ == "__main__":
    sys.exit(main())
