"""Show that a child cannot read its non-dumpable parent's /proc/<pid>/environ (Linux, same uid).

    uv run python ops/probes/non_dumpable.py            # after set_non_dumpable(): the child is denied
    uv run python ops/probes/non_dumpable.py --control  # without the call: the child can read it

Prints `denied` or `readable` and exits 0 when the result is the expected one for the mode. The
environ is opened and counted, never printed. Run as root it proves nothing (root reads anyway), so
it reports `skipped` and exits 0.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Run under `uv run`, which does not install this project (`package = false`): without this the import in
# main fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# The child opens the parent's environ by pid and reports only whether that worked.
_CHILD = """
import sys
try:
    with open(f"/proc/{sys.argv[1]}/environ", "rb") as f:
        f.read(1)
except PermissionError:
    print("denied")
else:
    print("readable")
"""


def main(argv: list[str]) -> int:
    """Report whether a child can read this process's environ; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--control", action="store_true", help="skip set_non_dumpable(): the child can read it"
    )
    control = parser.parse_args(argv).control
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        print("skipped")
        return 0
    if not control:
        from flyte_agent_factory import _auth

        if not _auth.set_non_dumpable():
            print("prctl failed")
            return 1
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(os.getpid())], capture_output=True, text=True, check=True
    ).stdout.strip()
    print(out)
    return 0 if out == ("readable" if control else "denied") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
