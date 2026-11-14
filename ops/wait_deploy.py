"""Block until the first slot's project task has a version other than --current, then print it."""

import argparse
import sys
import time
from pathlib import Path

from flyte import remote

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory_base, _union_identity  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first


def main(argv: list[str] | None = None) -> None:
    """Poll the first slot's task every `--every` seconds, for at most `--max` seconds."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    p.add_argument("--current", required=True)
    p.add_argument("--every", type=int, default=600)
    p.add_argument("--max", type=int, default=7000)
    a = p.parse_args(argv)
    _union_identity.init_for_script(_factory_base.PROFILE)
    end = time.time() + a.max
    while time.time() < end:
        v = remote.Task.get(_factory_base.SLOTS[0], auto_version="latest").fetch().version
        if v != a.current:
            print("NEW VERSION", v)
            break
        time.sleep(a.every)
    else:
        print("no new deploy yet")


if __name__ == "__main__":
    main()
