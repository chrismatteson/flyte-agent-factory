"""A `claude` that behaves like a model looking for the answer, for sealed-run tests that run no model.

It writes a realistic session transcript, then tries two ways out of the seal: `curl` to the GitHub API
and `git ls-remote` of a public repository. Both must fail inside the seal; it prints what happened.
"""

import json
import stat
import sys
from pathlib import Path
from typing import Any

from flyte_agent_factory._seal_spec import SealSpec

FAKE_CLAUDE = r"""#!{python}
import json, os, pathlib, subprocess, sys

cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
session = cfg / "projects" / "-work" / "session-1.jsonl"
session.parent.mkdir(parents=True, exist_ok=True)
env = {{**os.environ, "GIT_TERMINAL_PROMPT": "0"}}
curl = subprocess.run(["curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{{http_code}}",
                       "https://api.github.com/repos/x/y"], capture_output=True, text=True, env=env)
ls = subprocess.run(["git", "ls-remote", "https://github.com/x/y"], capture_output=True, text=True, env=env)
print("fake-claude curl_rc=%d ls_remote_rc=%d" % (curl.returncode, ls.returncode), flush=True)
lines = [
    {{"type": "user", "message": {{"role": "user", "content": "read the brief"}}}},
    {{"type": "assistant", "message": {{"role": "assistant", "content": [
        {{"type": "tool_use", "id": "t1", "name": "Bash",
          "input": {{"command": "curl https://api.github.com/repos/x/y"}}}}]}}}},
    {{"type": "user", "message": {{"role": "user", "content": [
        {{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
          "content": curl.stderr[-200:] or "failed"}}]}}}},
    {{"type": "assistant", "message": {{"role": "assistant",
      "content": [{{"type": "text", "text": "no network; done"}}]}}}},
]
session.write_text("".join(json.dumps(line) + "\n" for line in lines))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "finished: no route out", "session_id": "session-1"}}))
"""

SPEC = SealSpec.from_mapping(
    {
        "task_repo": "https://github.com/example/task.git",
        "base_sha": "a" * 40,
        "task_project": "taskproj",
        "index_cutoff": "2025-03-01",
        "push_remote": "https://github.com/example/bench-ws.git",
    }
)


def install_fake_claude(bin_dir: Path) -> Path:
    """Write the fake as `claude` in `bin_dir`; put `bin_dir` on PATH to use it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


#: Set in a sealed start's environment by nothing but the role's jail (`_seal_role._cache_env`).
JAIL_CACHE_VARS = {"XDG_CACHE_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "npm_config_cache"}


def assert_inside_the_jail(start_env: dict[str, str], role_env: dict[str, str]) -> None:
    """Assert the child got every variable the role's jail set: its caches and the launcher's import path."""
    assert role_env.items() <= start_env.items(), sorted(role_env.items() - start_env.items())
    assert JAIL_CACHE_VARS <= start_env.keys()
