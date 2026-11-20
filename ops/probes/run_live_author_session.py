"""Submit `live_author_session.py` as a child action of this run, with the slot's setup-token.

Ships this project's `src/flyte_agent_factory` with it (the image's copy is the deployed engine, not this
one), runs it with the image's `claude`, and prints its output with the token masked. Usage, from the
project directory, inside a run: `python ops/probes/run_live_author_session.py <image>`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import sys
import tarfile
from pathlib import Path

import flyte

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails.
sys.path.insert(0, str(ROOT / "src"))

from flyte_agent_factory import _names  # noqa: E402 -- needs `src` on the path, put there just above
from flyte_agent_factory.child import attach  # noqa: E402 -- needs `src` on the path, put there just above


def bundle() -> str:
    """Return this project's engine package and the probe as a base64 tarball."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(ROOT / "src" / "flyte_agent_factory", "flyte_agent_factory", filter=_no_cache)
        tar.add(HERE / "live_author_session.py", "live_author_session.py")
    return base64.b64encode(buf.getvalue()).decode()


def _no_cache(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if "__pycache__" in info.name else info


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="an image with `claude` on its PATH (the run's own image does)")
    return parser.parse_args()


# Read before the task environment below is built from it; the task's own import (in the child) has no
# arguments and needs none.
IMAGE = _args().image if __name__ == "__main__" else ""

SECRET_KEY = _names.getenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "").strip()
if not SECRET_KEY and __name__ == "__main__":
    raise SystemExit(
        "AGENT_FACTORY_CLAUDE_TOKEN_SECRET is not set; name the secret that holds the slot's setup-token."
    )

env = flyte.TaskEnvironment(
    name="live-author-session",
    image=IMAGE or None,
    resources=flyte.Resources(cpu=2, memory="4Gi"),
    secrets=[
        # The child's own import (not `__main__`) has no env var to read; it only needs a key to build.
        flyte.Secret(key=SECRET_KEY or "unset", as_env_var="CLAUDE_CODE_OAUTH_TOKEN")
    ],
)


@env.task
async def live(payload: str) -> str:
    """Unpack the bundle, run the live check with the image's `claude`, and return its output, masked."""
    import os
    import subprocess
    import tempfile

    where = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload))) as tar:
        tar.extractall(where, filter="data")
    run = subprocess.run(
        [sys.executable, "live_author_session.py"],
        cwd=where,
        env={**os.environ, "PYTHONPATH": where},
        capture_output=True,
        text=True,
        timeout=2400,
    )
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    out = f"exit {run.returncode}\n{run.stdout}\nSTDERR:\n{run.stderr[-3000:]}"
    out = out.replace(token, "<token>") if token else out
    if run.returncode != 0:
        # Fail the action so a failed check is not a green run; the masked output is the message.
        raise flyte.errors.NonRecoverableError(out)
    return out


if __name__ == "__main__":
    attach()
    print(asyncio.run(live.override(interruptible=True).aio(bundle())))
