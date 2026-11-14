"""Print the newest rows of a run's report, as text: what `flyte get logs` should have shown."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from flyte import remote

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory_base, _union_identity  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first


def main(argv: list[str] | None = None) -> None:
    """Print the newest rows and the status of the report of the run named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="the run's name")
    parser.add_argument("rows", nargs="?", type=int, default=15, help="how many rows (default: 15)")
    parser.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    args = parser.parse_args(argv)
    _union_identity.init_for_script(_factory_base.PROFILE)
    html = str(remote.Run.get(args.run).get_report())
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    rows = re.findall(r"<tr><td><code>(.*?)</code></td><td>(.*?)</td><td>(.*?)</td></tr>", html, re.DOTALL)
    for at, icon, text in rows[: args.rows][::-1]:
        print(at, icon, re.sub(r"\s+", " ", text)[:600])
    status = re.search(r"<b>Status:</b>(.*?)</p>", html, re.DOTALL)
    print("STATUS:", status.group(1).strip() if status else "?")


if __name__ == "__main__":
    main()
