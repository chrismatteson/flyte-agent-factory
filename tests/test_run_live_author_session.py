"""The live-run task fails when the probe fails, masks the token, and insists on its secret when run."""

from __future__ import annotations

import asyncio
import base64
import io
import runpy
import sys
import tarfile
import textwrap
from pathlib import Path

import flyte
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "probes" / "run_live_author_session.py"
TOKEN = "sekrit"


@pytest.fixture
def live():
    module = runpy.run_path(str(SCRIPT), run_name="run_live_author_session")
    return module["live"]


def _payload(exit_code: int) -> str:
    data = textwrap.dedent(
        f"""\
        import sys
        print('RESULT {{"failed": ["x"], "checks": {{}}}}')
        print('token is sekrit', file=sys.stderr)
        sys.exit({exit_code})
        """
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("live_author_session.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _call(live, payload: str) -> str:
    out: str = asyncio.run(live.func(payload))
    return out


def test_failing_probe_fails_the_task_with_masked_output(live, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    with pytest.raises(flyte.errors.NonRecoverableError) as caught:
        _call(live, _payload(1))
    assert "RESULT" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_passing_probe_returns_masked_output(live, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    out = _call(live, _payload(0))
    assert out.startswith("exit 0")
    assert "RESULT" in out
    assert TOKEN not in out
    assert "<token>" in out


def test_missing_secret_env_exits_only_when_run_as_main(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "some-image"])
    with pytest.raises(SystemExit, match="AGENT_FACTORY_CLAUDE_TOKEN_SECRET is not set"):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    # Imported (as the child does) it builds its environment without the variable.
    runpy.run_path(str(SCRIPT), run_name="run_live_author_session")
