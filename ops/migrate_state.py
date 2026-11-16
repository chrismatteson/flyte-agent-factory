r"""Copy the factory's state out of the old `[state] uri` bucket prefix onto the git state branch.

The only module that still reads the old `[state] uri` prefix (read-only, through the filesystem Flyte
itself uses), and only for this one-time move; see "Cutover from the bucket" in `docs/state-store.md`
for when to run it.

    PYTHONPATH=src uv run python ops/migrate_state.py --from s3://bucket/prefix \
        [--dry-run] [--overwrite] [--profile NAME]

Every object under the prefix whose key is in the state layout is carried to the branch in ONE commit,
merged three ways against what the previous pass copied (recorded on the branch in `migration.json`):
what the new code has written since is never replaced (it is reported as a conflict and kept, unless
`--overwrite`), a bucket request that collides with a different one the new code queued under the same
generation moves, with its markers, to the next free generation, and a `started` marker the old code
removed (it reopened the request) is removed on the branch too while the branch still has the copy.
Nothing else is ever deleted. Run it again and it finds nothing new: no commit. It needs read access to
the bucket (the operator's own credentials) and push access to `[repo] project` (the operator's git).

`checkpoints/` is copied too: runs started before the cutover never put their checkpoint pointer in their
report, and recreating one after an abort reads it from there. Not copied: `waits.json` (each admission
pass rewrites it whole, so the new code's own is the current one), the retired `board/`, `github/outbox*`
and `deployer/`, anything else that is not state, and the dispatch leases (`.dispatch-lease.json`): a
lease the old code took means nothing to the new one, and copying it would only hold dispatch back
until it expires.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _profile

_profile.select_from_argv()  # before `_state_store.current()` reads the profile

from flyte_agent_factory import _state_store  # noqa: E402 -- must follow select_from_argv(): the profile is chosen first

#: Directories of the state layout (`docs/state-store.md`), and the file that sits at its top.
#: `checkpoints/` holds the pointers of runs started before the cutover, which recreate falls back to.
LAYOUT_DIRS = ("queue/", "gardening/", "github/intake/", "loop/backlog/", "checkpoints/")
LAYOUT_FILES = ("reserve.json",)
LEASE = ".dispatch-lease.json"
#: On the branch, beside the layout: what each pass carried over (`copied`: bucket key -> digest of the
#: body written for it) and which bucket requests took a new generation (`renumbered`: `queue/<name>/<gen>`
#: -> new gen). It is what makes a later pass a three-way merge rather than a blind copy. `gens` (name ->
#: the highest generation the bucket had) is what the new code numbers launches above
#: (`_queue.cutover_floor`), so a run the old code labels can never carry a generation the new code uses.
RECORD = "migration.json"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_QUEUE_KEY = re.compile(r"^queue/([^/]+)/(\d+)\.([^/]+)$")


def in_layout(key: str) -> bool:
    """Whether `key` (relative to the old prefix) is state the new store holds, and not a lease."""
    if key.rsplit("/", 1)[-1] == LEASE:
        return False
    return key in LAYOUT_FILES or key.startswith(LAYOUT_DIRS)


def read_bucket(uri: str) -> dict[str, bytes]:
    """Return every object under `uri` as key (relative to it) -> body. Reads only."""
    import flyte.storage  # here and nowhere else: this is the one reader of the old bucket

    fs = flyte.storage.get_underlying_filesystem(path=uri)
    # Object-store listings return `bucket/key` without the scheme and a local one an absolute path, so
    # both sides are compared with the scheme and leading slashes taken off.
    prefix = _SCHEME.sub("", uri).strip("/")
    found = {}
    for path in fs.find(uri):
        bare = _SCHEME.sub("", path).lstrip("/")
        if bare.startswith(prefix + "/"):
            found[bare[len(prefix) + 1 :]] = path
    return {key: fs.cat_file(path) for key, path in sorted(found.items())}


def _digest(body: bytes) -> str:
    return hashlib.sha1(body).hexdigest()


def _record(snap: _state_store.SnapshotView) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(snap.read(RECORD) or b"{}")
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    return {part: dict(raw.get(part) or {}) for part in ("copied", "renumbered", "gens")}


def _renumber(body: bytes, gen: int) -> bytes:
    """Return the request `body` with its `gen` field set to `gen` (file name and field agree)."""
    try:
        raw = json.loads(body)
    except ValueError:
        return body
    if not isinstance(raw, dict):
        return body
    return json.dumps({**raw, "gen": gen}, sort_keys=True).encode()


def _targets(
    wanted: list[str],
    source: Mapping[str, bytes],
    have: Mapping[str, bytes],
    record: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Map each bucket key to its branch key.

    A request the branch already holds a different one of at the same (name, gen) moves, with all its
    markers, to the next generation free on both sides.

    That happens when a launch reached the new code while the bucket still had (or was still being given)
    the same name's generation: the bucket's request then must not take the branch's place, nor its
    markers land on the branch's request (a copied `started` would make that request look dispatched).
    """
    groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    gens: dict[str, set[int]] = defaultdict(set)
    for key in wanted:
        if m := _QUEUE_KEY.match(key):
            groups[(m[1], int(m[2]))].append(key)
    for key in have:
        if m := _QUEUE_KEY.match(key):
            gens[m[1]].add(int(m[2]))
    for name, gen in groups:
        gens[name].add(gen)
    for moved, gen in record["renumbered"].items():
        gens[moved.split("/")[1]].add(int(gen))
    targets = {key: key for key in wanted}
    for (name, gen), keys in sorted(groups.items()):
        group = f"queue/{name}/{gen:04d}"
        new = record["renumbered"].get(group)
        if new is None:
            request = f"{group}.json"
            on_branch = [k for k in have if k.startswith(f"{group}.")]
            carried = request in record["copied"]
            if carried or not on_branch or (request in source and have.get(request) == source[request]):
                continue
            new = max(gens[name]) + 1
            gens[name].add(new)
            record["renumbered"][group] = new
        for key in keys:
            suffix = key.rsplit("/", 1)[1].split(".", 1)[1]  # a queue key: `<gen>.<suffix>`
            targets[key] = f"queue/{name}/{int(new):04d}.{suffix}"
    return targets


def migrate(
    source: Mapping[str, bytes],
    store: _state_store.Store,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Carry what `source` has in the state layout over to the branch, in one commit.

    Each bucket key is merged three ways against what the last pass copied (`RECORD`): a key never
    copied is written where the branch has none, counted `same` where it has the same, and a
    `conflict` (the branch's is newer and kept, unless `overwrite`) where it differs; a request's whole
    group is renumbered instead (`_targets`). A key the bucket changed since it was copied is updated
    if the branch still has what was copied, and a `started` marker the bucket dropped (the old code
    reopened the request) is dropped on the branch too, on the same condition. Nothing else is ever
    deleted on the branch, and nothing the new code wrote is replaced unless `overwrite`.

    Returns `counts` (copied, same, conflict, skipped; renumbered and deleted when nonzero), the
    `conflicts` (keys), the `skipped` keys, `renumbered` (bucket group -> new gen, this pass) and the
    `deleted` branch keys.
    """
    skipped = [key for key in source if not in_layout(key)]
    wanted = [key for key in source if in_layout(key)]
    invalid = []
    for key in wanted:
        try:
            _state_store._check_key(key)
        except ValueError:
            invalid.append(key)
    wanted = [key for key in wanted if key not in invalid]

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], dict[str, Any]]:
        # Decided on the tip the push is made against: a write the new code makes meanwhile makes the
        # push lose, and this runs again on top of it rather than over it.
        return _plan(source, wanted, skipped + invalid, snap, overwrite=overwrite)

    if dry_run:
        return decide(store.snapshot(fresh=True))[1]
    return store.transact(decide, "migrate state from the bucket")


def _plan(
    source: Mapping[str, bytes],
    wanted: list[str],
    skipped: list[str],
    snap: _state_store.SnapshotView,
    *,
    overwrite: bool,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Return the branch changes `migrate` makes on `snap`, and its result."""
    record = _record(snap)
    recorded = json.dumps(record, sort_keys=True)
    before = dict(record["renumbered"])
    have = snap.read_many(sorted({*snap.list(), *wanted}))
    targets = _targets(wanted, source, have, record)
    for key in wanted:  # what the old code has numbered, so the new code numbers above it (`cutover_floor`)
        if m := _QUEUE_KEY.match(key):
            record["gens"][m[1]] = max(int(record["gens"].get(m[1], 0)), int(m[2]))
    changes: dict[str, bytes | None] = {}
    counts: Counter[str] = Counter(skipped=len(skipped))
    conflicts, deleted = [], []
    for key in wanted:
        dest, body = targets[key], source[key]
        if dest != key and dest.endswith(".json"):
            body = _renumber(body, int(dest.rsplit("/", 1)[1].split(".")[0]))
        base, current, digest = record["copied"].get(key), have.get(dest), _digest(body)
        if base == digest:  # carried by an earlier pass; whatever the branch did since stands
            counts["same"] += 1
            continue
        if current == body:
            counts["same"] += 1
        elif current is None if base is None else (current is not None and _digest(current) == base):
            changes[dest] = body  # new to the branch, or changed in the bucket and untouched on the branch
        elif overwrite:
            changes[dest] = body
        else:  # the new code wrote its own (or dropped what was copied): it is newer, and kept
            conflicts.append(key)
            continue
        record["copied"][key] = digest
    for key, digest in list(record["copied"].items()):
        # The old code reopens a request by removing its `started` marker and nothing else, so a dropped
        # marker is mirrored only while the bucket still holds that request: a listing of the wrong prefix
        # (or an empty one) has no such request and reopens nothing.
        m = _QUEUE_KEY.match(key)
        if key in source or m is None or m[3] != "started" or f"queue/{m[1]}/{m[2]}.json" not in source:
            continue
        dest = _moved(key, record)
        if (current := have.get(dest)) is not None and _digest(current) == digest:
            changes[dest] = None
            deleted.append(dest)
        del record["copied"][key]
    counts["conflict"] = len(conflicts)
    counts["copied"] = sum(body is not None for body in changes.values())
    if deleted:
        counts["deleted"] = len(deleted)
    renumbered = {group: gen for group, gen in record["renumbered"].items() if group not in before}
    if renumbered:
        counts["renumbered"] = len(renumbered)
    if changes or json.dumps(record, sort_keys=True) != recorded:
        changes[RECORD] = json.dumps(record, sort_keys=True, indent=1).encode()
    result = {
        "counts": dict(counts),
        "conflicts": conflicts,
        "skipped": skipped,
        "renumbered": renumbered,
        "deleted": deleted,
    }
    return changes, result


def _moved(key: str, record: dict[str, dict[str, Any]]) -> str:
    """Where bucket `key` went on the branch: its own key, or its group's new generation."""
    m = _QUEUE_KEY.match(key)
    if m is None or (new := record["renumbered"].get(f"queue/{m[1]}/{m[2]}")) is None:
        return key
    return f"queue/{m[1]}/{int(new):04d}.{m[3]}"


def main(argv: list[str] | None = None) -> int:
    """Run the migration from the command line; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="source", required=True, metavar="URI", help="the old [state] uri")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied; write nothing")
    parser.add_argument(
        "--overwrite", action="store_true", help="replace a branch file whose content differs"
    )
    parser.add_argument("--profile", help="the deployment profile (as for the other ops tools)")
    args = parser.parse_args(argv)

    bucket = read_bucket(args.source)
    in_layout_keys = [key for key in bucket if in_layout(key)]
    if not in_layout_keys:
        # An empty, missing or too-deep prefix looks exactly like a finished migration ("nothing to
        # migrate"), and the cutover then deletes the prefix. Refuse before touching the branch.
        print(
            f"no factory state under {args.source}: check --from "
            f"({len(bucket)} keys outside the layout were found)",
            file=sys.stderr,
        )
        return 2

    result = migrate(bucket, _state_store.current(), dry_run=args.dry_run, overwrite=args.overwrite)
    names = ("copied", "same", "conflict", "skipped", "renumbered", "deleted")
    counts = {name: result["counts"].get(name, 0) for name in names}
    for key in result["conflicts"]:
        print(f"conflict (the branch's is newer and kept; --overwrite takes the bucket's): {key}")
    for group, gen in result["renumbered"].items():
        print(f"renumbered: {group} -> generation {gen} (the branch has another request at that one)")
    for key in result["deleted"]:
        print(f"deleted (the old code reopened the request): {key}")
    for key in result["skipped"]:
        print(f"skipped: {key}")
    verb = "would copy" if args.dry_run else "copied"
    print(
        f"{verb} {counts['copied']}, same {counts['same']}, conflict {counts['conflict']}, "
        f"skipped {counts['skipped']}, renumbered {counts['renumbered']}, deleted {counts['deleted']}"
    )
    if not (counts["copied"] or counts["deleted"]):
        print("nothing to migrate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
