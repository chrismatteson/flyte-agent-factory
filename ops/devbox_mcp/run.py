r"""Run the devbox MCP harness once and write its evidence file.

One command, from the agent session (system Python, which has flyte and the session's
`flyte_agent_factory.child`; `kubernetes` only for building the pod template):

    cd projects/mcp-apps-reliability && uv pip install -q --target ~/.local/kube-pkgs kubernetes==37.0.2 \
        && PYTHONPATH=ops:$HOME/.local/kube-pkgs:$PYTHONPATH \
        python -m devbox_mcp.run --out evidence/devbox-<label>.json

(`$PYTHONPATH` kept: it is how the session's `flyte_agent_factory.child` is found.)

It packs this working tree (tracked and untracked files git does not ignore, uncommitted changes included),
submits one privileged child action of this run on a spot c5.4xlarge (on demand if none is placed in 20
minutes), and waits for it: a fresh `flyte start devbox`, this engine installed and its factory deployed,
a stand-in project run that logs and asks a question, the factory MCP tools called about it, and the devbox
torn down. About 12 minutes once a node is placed (two runs: 12 and 18 minutes end to end, image build and
spot queue included). `--out` is never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path
from types import FrameType, TracebackType

ROOT = Path(__file__).resolve().parents[2]  # projects/mcp-apps-reliability: the engine tree
WORKSPACE = ROOT.parent.parent


def pack(root: Path, dest: Path) -> int:
    """Write `root`'s files that git tracks or would track to the tarball `dest`; return how many.

    A tarball of the working tree rather than `git bundle`: a bundle carries commits, and the point is to
    test uncommitted changes too.
    """
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout.decode()
    # A path deleted in the working tree is still listed as cached until the deletion is staged.
    names = sorted(n for n in set(listed.split("\0")) if n and (root / n).is_file())
    with tarfile.open(dest, "w:gz") as t:
        for name in names:
            t.add(root / name, arcname=name, recursive=False)
    return len(names)


def main(argv: list[str] | None = None) -> int:
    """Pack, submit, wait, and write the evidence JSON; exit 0 only if every step passed."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="evidence JSON to write (must not exist)")
    args = ap.parse_args(argv)
    if args.out.exists():
        ap.error(f"{args.out} exists; pass a new --out")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    from .task import submit

    started = time.time()
    signal.signal(signal.SIGTERM, _raise_exit)  # so `submit` can abort its child
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "engine.tgz"
        files = pack(ROOT, tarball)
        print(f"packed {files} files ({tarball.stat().st_size // 1024} KiB) from {ROOT}", flush=True)
        actions, out = submit(tarball, WORKSPACE)
    data = json.loads(out)
    ended = time.time()
    data = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "seconds": round(ended - started),
        "child_actions": actions,
        "packed_files": files,
        **data,
    }
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    for step in data["steps"]:
        print(f"{step['name']:16} {step['result']:8} {step['seconds']:>7}s", flush=True)
    print(f"wrote {args.out} (child actions: {', '.join(actions)})", flush=True)
    return 0 if all(s["result"] == "pass" for s in data["steps"]) else 1


def _raise_exit(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _exit_process(code: int) -> None:
    # The Flyte controller leaves non-daemon threads behind, which kept a finished run alive (selftest).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _report_crash(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    traceback.print_exception(kind, exc, tb)
    _exit_process(1)


if __name__ == "__main__":
    sys.excepthook = _report_crash
    try:
        code = main()
    except SystemExit as e:  # argparse: --help, usage errors
        code = e.code if isinstance(e.code, int) else 1
    _exit_process(code)
