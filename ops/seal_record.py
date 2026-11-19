"""Copy a sealed project's record out of its last checkpoint.

    PYTHONPATH=src uv run python ops/seal_record.py RUN DEST [--profile NAME]

RUN is the project's run name (the one `ops/operate.py launch` printed). The record root lives under the
project's state directory, which the project checkpoints whole (`_state.save_checkpoint`), and the run's
report names where: so this reads the same checkpoint a recreated run resumes from, with no new store and
no new write. DEST then holds `manifest.json` and `roles/<role id>/...`, which the audit reads
(`docs/sealed-runs.md`). A finished project's record is complete: the project closes the author's role
before it saves its last checkpoint. The record of a plain-arm run comes back in the task's output instead.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import flyte
from flyte import remote

# Run under `uv run`, which does not install this project (`package = false`; as in ops/operate.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _profile

_profile.select_from_argv()  # before anything reads the profile

from flyte_agent_factory import (  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first
    _factory_base,
    _factory_recreate,
    _union_identity,
)
from flyte_agent_factory._errors import REMOTE_ERRORS  # noqa: E402 -- must follow select_from_argv()

#: The record root's directory name in the state tree (`_session_config.SEAL_RECORD`).
RECORD_DIR = "seal-record"


def copy_record(tree: Path, dest: Path) -> Path:
    """Copy the record out of a restored state tree into `dest` (which must not exist); return `dest`."""
    source = Path(tree) / RECORD_DIR
    if not source.is_dir():
        raise FileNotFoundError(
            f"the checkpoint holds no {RECORD_DIR}/: this project was not sealed, or it never started"
        )
    if Path(dest).exists():
        raise FileExistsError(f"{dest} already exists; give a new directory")
    shutil.copytree(source, dest)
    return Path(dest)


def fetch(run_name: str, dest: Path) -> Path:
    """Read the checkpoint location from the run's report, load that checkpoint and copy its record."""
    location = _factory_recreate._report_note(remote.Run.get(name=run_name)).get("checkpoint", "")
    if not location:
        raise LookupError(f"run {run_name} has no checkpoint location in its report")
    tree = flyte.Checkpoint(checkpoint_dest=location, checkpoint_src=location).load_sync()
    if tree is None:
        raise LookupError(f"the checkpoint of {run_name} at {location} is empty or unreadable")
    return copy_record(tree, dest)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, fetch the record, print where it went; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run")
    parser.add_argument("dest", type=Path)
    parser.add_argument("--profile", help="deployment profile name (read before anything else)")
    args = parser.parse_args(argv)
    _union_identity.init_for_script(_factory_base.PROFILE)
    try:
        print(fetch(args.run, args.dest))
    except (OSError, LookupError, *REMOTE_ERRORS) as exc:
        print(f"seal_record: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
