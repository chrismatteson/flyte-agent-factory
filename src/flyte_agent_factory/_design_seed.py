"""The harness's seeded root `DESIGN.md`, and how the gates recognise it as the harness's own.

The harness re-seeds a missing `DESIGN.md` after a pod restart, so an untracked, unedited copy is
harness state, not project work. It is not git-excluded (a project may commit its own), so the gates
recognise it by content: the current seed, or a hash recorded when an earlier seed was written.
"""

import hashlib
from pathlib import Path

DESIGN_FILE = "DESIGN.md"

#: One sha256 per line, so a seed from an older harness version stays recognised after the text changes.
HASHES_FILE = ".claude-flyte/design-seed.sha256"

#: Seeds shipped before hashes were recorded (2026-10-13 to 2026-11-04): a project seeded then has no
#: hashes file, and its DESIGN.md is never re-seeded, so these are known here instead.
EARLIER_SEEDS = frozenset(
    {
        "0ba879824d60e6c7419204d66e2632e4ecd140f2c4026ed439e5aa06579315f1",
        "df7817c86036bdf30235ff1f99b92585b8d3ba3d8675d7d5cfbf5cb4f34054ae",
        "77cca3952d2b6d3ec99f263a749c8ee4d9559a7c126e3c7977d1ceaafc3197eb",
        "92ad82f62f9d5e35376d0568bb22954aca7c8e4a2937169a4726d9f07edd1dc1",
        "24e65fe23d9b67381058853b9372f464812c0403d96e1be6824a9b0434015cab",
    }
)

# Seeded once and then left alone: this is where a human writes the house rules
# the agents are told to follow, so re-seeding would overwrite their edits.
SEED = """\
# Design guidance

House rules for this project. Every agent reads this; it overrides their own
instructions where they conflict. Edit it freely -- it is not regenerated.

General engineering and security practice lives in `.claude-flyte/standards/`,
including "Don't reinvent the wheel". Read those standards alongside
this project guidance.

## Things about this environment that have already cost time

- **Containers run as an unprivileged user.** Nothing may assume it can write
  outside `$HOME` (or `/tmp`). This broke three separate things before it was
  understood: the agent's own state paths, the `claude` binary's install
  location, and the browser app's profile directory. Derive writable paths from
  `$HOME`; never hardcode `/var/lib`, `/state` or `/usr/local/bin`.
- **The code bundle carries Python modules, not package data.** Anything that
  is not a `.py` has to be put into the image (`Image.with_source_folder`), or
  it will be present locally and silently absent in the container.
- **Building an image is not running it.** An image that builds can still exit
  on its first line. Prove a component starts, not that it compiles.

## Running work on the cluster

- Attach once with `flyte_agent_factory.child.attach()`, then write ordinary Flyte.
  Anything you submit becomes a child action of this run.
- **Images must already exist.** Reuse `$FLYTE_INTERNAL_IMAGE` or a public
  image; use `flyte.sandbox.create(packages=[...])` when you genuinely need a
  new one built.
- **Always bound the queue** with `flyte.Timeout(max_queued_time=...)`, and on
  `MaxQueuedTimeExceededError` fall back -- spot first, then on-demand with the
  timeout cleared. A job with no queue bound waits forever and that is not a
  failure.
- **Keep the workspace small.** It is checkpointed whole on a timer, so cost
  scales with its size. Datasets and model artifacts belong in a child action's
  outputs, not in the working tree.

## Working as a team

Use the subagents in `.claude/agents/`, named by what they do: `scope` settles scope and
acceptance criteria, `discover` finds out what the problem really is (DISCOVERY.md),
`prototype` tries two or more shapes of anything a person will see or use, `shape` settles the
design (DESIGN-RATIONALE.md), `build` builds it, and `verify` -- several in parallel, on
different aspects -- finds out whether it actually works. Don't let one agent both build and
bless its own work. The old names `product`, `designer`, `dev` and `qa` still work as aliases
this release.
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record(workspace: Path) -> None:
    """Remember the current seed's hash in the harness directory of `workspace`."""
    path = workspace / HASHES_FILE
    known = path.read_text().split() if path.is_file() else []
    digest = _digest(SEED.encode())
    if digest not in known:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{h}\n" for h in [*known, digest]))


def is_untouched_seed(workspace: Path, path: str, current: bytes | None, original: bytes | None) -> bool:
    """Return True for the root `DESIGN.md` that is untracked at base and still exactly a seed.

    An edited copy, or one the project tracks (`original` is set), is the project's work.
    """
    if path != DESIGN_FILE or current is None or original is not None:
        return False
    if current == SEED.encode() or _digest(current) in EARLIER_SEEDS:
        return True
    hashes = workspace / HASHES_FILE
    return hashes.is_file() and _digest(current) in hashes.read_text().split()
