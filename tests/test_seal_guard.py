import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _context_guard, _seal_guard

CWD = "/work/repo"
LOCAL = "/work/local"
FORBIDDEN = "/work/other-attempt/**"


@pytest.fixture
def config():
    return {
        "task_project": "django",
        "forbidden_paths": [FORBIDDEN, "/secret/*.diff"],
        "local_roots": [LOCAL],
        "guard_log": "/dev/null",
        "role_id": "author-1",
    }


ToolCall = tuple[str, dict[str, Any]]


def bash(command: str) -> ToolCall:
    return ("Bash", {"command": command})


DENIED: list[tuple[str, ToolCall]] = [
    # git routes
    ("git-fetch", bash("git fetch upstream")),
    ("git-fetch", bash("git fetch")),
    ("git-fetch", bash("git -C repo fetch origin")),
    ("git-fetch", bash("git fetch --unshallow")),
    ("git-fetch", bash("git fetch --deepen 50 origin")),
    ("git-fetch", bash("git fetch --depth 1 https://github.com/django/django abc123")),
    ("git-fetch", bash("git ls-remote https://github.com/django/django")),
    ("git-fetch", bash("git clone https://github.com/django/django.git")),
    ("git-fetch", bash("git clone git@github.com:django/django.git")),
    ("git-fetch", bash("git pull")),
    ("git-fetch", bash("git pull origin main")),
    ("git-fetch", bash("git remote update")),
    ("git-fetch", bash("git submodule update --init")),
    ("git-fetch", bash('bash -c "git fetch https://github.com/django/django"')),
    ("git-fetch", bash("cd x && git pull")),
    ("git-fetch", bash("echo start; git -c http.sslVerify=false fetch origin")),
    ("git-fetch", bash("env GIT_TERMINAL_PROMPT=0 timeout 30 git fetch origin")),
    ("git-fetch", bash("GIT_TRACE=1 sudo -u nobody nice -n 5 command git fetch origin")),
    ("git-fetch", bash("echo $(git fetch origin)")),
    ("git-fetch", bash("echo `git ls-remote origin`")),
    ("git-fetch", bash('sh -lc "cd x\ngit pull"')),
    ("git-fetch", bash("git fetch /elsewhere/repo")),
    ("git-fetch", bash("git fetch file:///elsewhere/repo")),
    ("git-fetch", bash("git fetch /work/local/../elsewhere")),
    ("git-fetch", bash("true || git fetch origin")),
    ("git-fetch", bash("ls | xargs echo; if git fetch origin; then echo ok; fi")),
    ("git-remote-add", bash("git remote add x https://example.invalid/x.git")),
    ("git-remote-add", bash("git remote set-url origin https://github.com/acme/tasklib")),
    # hosting API and HTTP clients
    ("gh", bash("gh api repos/x/y/pulls")),
    ("gh", bash("gh pr view 12")),
    ("http-client", bash("curl -s https://api.github.com/repos/django/django/pulls")),
    ("http-client", bash("curl example.com/patch.diff")),
    ("http-client", bash("wget https://codeload.github.com/django/django/zip/main")),
    ("http-client", bash("wget -O out.zip https://example.org/x.zip")),
    ("http-client", bash("aria2c https://example.org/x.zip")),
    ("http-client", bash("https api.github.com/user")),
    ("http-client", bash("http GET https://example.org/")),
    ("http-client", bash("curl -o out.txt http://127.0.0.1:8000/ https://example.org/")),
    (
        "script-url",
        bash("python3 -c \"import urllib.request;urllib.request.urlopen('https://github.com/x')\""),
    ),
    ("script-url", bash("python -c 'import requests; requests.get(\"http://example.org/\")'")),
    ("script-url", bash("node -e \"fetch('https://github.com/x')\"")),
    # packages
    ("pip-download", bash("pip download django==4.2")),
    ("pip-download", bash("python -m pip download requests")),
    ("pip-task-project", bash("pip install Django==4.2")),
    ("pip-task-project", bash("pip install django")),
    ("pip-task-project", bash("pip3 install -q 'Django>=4,<5'")),
    ("pip-task-project", bash("pip install pytest DJANGO")),
    ("pip-task-project", bash("python -m pip install django[argon2]")),
    ("pip-task-project", bash("uv pip install 'django[argon2]'")),
    ("pip-task-project", bash("uv add django")),
    ("pip-task-project", bash("uv run python -m pip install Django")),
    ("pip-task-project", bash("pip install https://files.pythonhosted.org/packages/ab/cd/Django-4.2.tar.gz")),
    ("pip-task-project", bash("pip install --index-url http://127.0.0.1:1/ Django-4.2.tar.gz")),
    ("pip-task-project", bash("pip install git+https://github.com/django/django.git@main")),
    ("pip-task-project", bash("pip install -e git+https://github.com/django/django#egg=django")),
    ("pip-task-project", bash("pip install 'django @ https://example.org/d.zip'")),
    ("pip-task-project", bash("pip install /tmp/Django-4.2-py3-none-any.whl")),
    # forbidden paths
    ("forbidden-path", ("Read", {"file_path": "/work/other-attempt/patch.diff"})),
    ("forbidden-path", ("Read", {"file_path": "../other-attempt/patch.diff"})),
    ("forbidden-path", ("Read", {"file_path": "/secret/gold.diff"})),
    ("forbidden-path", ("Grep", {"pattern": "def fix", "path": "/work/other-attempt/src"})),
    ("forbidden-path", ("Grep", {"pattern": "def fix", "path": "/work"})),
    ("forbidden-path", ("Glob", {"pattern": "/work/other-attempt/**/*.py"})),
    ("forbidden-path", ("NotebookRead", {"notebook_path": "/work/other-attempt/n.ipynb"})),
    ("forbidden-path", bash("cat ../other-attempt/patch.diff")),
    ("forbidden-path", bash("cd .. && cat other-attempt/patch.diff")),
    ("forbidden-path", bash("grep -r fix /work/other-attempt")),
    ("forbidden-path", bash("git -C /work/other-attempt log -p")),
    ("forbidden-path", bash("sort --files0-from=/work/other-attempt/list")),
    ("forbidden-path", bash("bash -c 'cat /work/other-attempt/x'")),
    # tools
    ("web-tool", ("WebSearch", {"query": "django ticket 1234 fix"})),
    ("web-tool", ("WebFetch", {"url": "https://example.org", "prompt": "x"})),
    ("browser-mcp", ("mcp__browser__browser_navigate", {"url": "http://localhost"})),
    ("malformed", ("Bash", {"command": ["git", "fetch"]})),
]

ALLOWED: list[ToolCall] = [
    bash("pip install -e ."),
    bash("pip install ."),
    bash("pip install -e '.[test]'"),
    bash("pip install ../sibling"),
    bash("pip install pytest requests"),
    bash("pip install requests"),
    bash("pip install django-extensions"),
    bash("pip install --index-url http://127.0.0.1:8000/index/ pytest"),
    bash("uv pip install -r requirements.txt"),
    bash("uv add requests"),
    bash("uv run pytest -q"),
    bash("python -m pytest -q"),
    bash("python -c 'print(1)'"),
    bash("python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/x')\""),
    bash("git log --oneline"),
    bash("git diff"),
    bash("git show HEAD~1"),
    bash("git status -sb"),
    bash("git commit -am x"),
    bash("git add -A && git commit -m 'fix the thing'"),
    bash("git push"),
    bash("git push origin HEAD:refs/heads/work"),
    bash("git remote -v"),
    bash("git remote add x /work/local/bare"),
    bash("git fetch /work/local/repo"),
    bash("git fetch file:///work/local/repo main"),
    bash("git -C /work/local/repo fetch ../repo2"),
    bash("git clone /work/local/repo copy"),
    bash("git pull --ff-only /work/local/repo main"),
    bash("curl http://127.0.0.1:8000/"),
    bash("curl -s -o out.txt http://localhost:8000/health"),
    bash("curl -H 'Accept: application/json' http://127.0.0.1:8000/x"),
    bash("wget -O out.txt http://127.0.0.1:9/x"),
    bash("gh --version"),
    bash('echo "see https://github.com/x"'),
    bash("grep -rn 'http://example.org' src"),
    bash("ls -la && cat setup.py | head -20"),
    bash("sed -n '1,20p' src/mod.py"),
    bash("cat /work/repo/README.md"),
    bash("echo it's unbalanced"),
    bash("timeout 60 pytest -x -q tests/"),
    bash("find . -name '*.py' | xargs grep -n fix"),
    ("Read", {"file_path": "/work/repo/src/mod.py"}),
    ("Read", {"file_path": "src/mod.py"}),
    ("Grep", {"pattern": "def fix", "path": "/work/repo/src"}),
    ("Grep", {"pattern": "def fix"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("Edit", {"file_path": "/work/repo/a.py", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "/work/repo/a.py", "content": "x"}),
    ("mcp__other__tool", {}),
]


def test_the_table_has_at_least_forty_cases():
    assert len(DENIED) + len(ALLOWED) >= 40


@pytest.mark.parametrize(("rule", "call"), DENIED, ids=[f"{r}:{str(c[1])[:60]}" for r, c in DENIED])
def test_each_route_is_denied(config, rule: str, call: ToolCall):
    decision = _seal_guard.decide(*call, config, cwd=CWD)

    assert not decision.allow
    assert decision.rule == rule
    assert decision.reason.startswith("Sealed run: ")


@pytest.mark.parametrize("call", ALLOWED, ids=[str(c[1])[:70] for c in ALLOWED])
def test_each_legitimate_call_is_allowed(config, call: ToolCall):
    assert _seal_guard.decide(*call, config, cwd=CWD).allow


def test_a_cwd_in_the_tool_input_wins_over_the_session_cwd(config):
    call = ("Bash", {"command": "cat patch.diff", "cwd": "/work/other-attempt"})

    assert not _seal_guard.decide(*call, config, cwd=CWD).allow


def test_with_no_task_project_a_pip_install_is_allowed(config):
    config["task_project"] = ""

    assert _seal_guard.decide(*bash("pip install django"), config, cwd=CWD).allow


def test_with_no_local_roots_every_git_source_is_denied(config):
    config["local_roots"] = []

    assert not _seal_guard.decide(*bash("git fetch /work/local/repo"), config, cwd=CWD).allow


def test_a_requirements_file_naming_the_task_project_is_denied(config, tmp_path):
    (tmp_path / "requirements.txt").write_text("# deps\npytest\nDjango==4.2\n")

    decision = _seal_guard.decide(*bash("pip install -r requirements.txt"), config, cwd=str(tmp_path))

    assert decision.rule == "pip-task-project"


def test_a_relative_local_git_source_is_judged_against_the_cwd(config, tmp_path):
    config["local_roots"] = [str(tmp_path)]
    (tmp_path / "upstream").mkdir()

    assert _seal_guard.decide(*bash("git fetch upstream"), config, cwd=str(tmp_path)).allow
    assert not _seal_guard.decide(*bash("git fetch upstream"), config, cwd="/work/repo").allow


# --- the hook process ------------------------------------------------------------------------ #


def run_hook(config_path, payload):
    return subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._seal_guard", "--config", str(config_path)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def hook_config(tmp_path):
    return _seal_guard.write_config(
        tmp_path / "guard-config.json",
        task_project="django",
        forbidden_paths=["/work/other-attempt/**"],
        local_roots=[],
        guard_log=tmp_path / "guard.jsonl",
        role_id="author-1",
    )


def logged(tmp_path):
    return [json.loads(line) for line in (tmp_path / "guard.jsonl").read_text().splitlines()]


def test_main_denies_with_the_hook_output_shape_and_logs(tmp_path, hook_config):
    tool_input = {"command": "git fetch origin"}

    result = run_hook(hook_config, {"tool_name": "Bash", "tool_input": tool_input, "cwd": "/work/repo"})

    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"].startswith("Sealed run: ")
    [line] = logged(tmp_path)
    assert line["decision"] == "deny"
    assert line["rule"] == "git-fetch"
    assert line["tool"] == "Bash"
    assert line["input"] == tool_input
    assert line["role_id"] == "author-1"
    assert line["ts"].endswith("Z")


def test_main_allows_silently_and_logs(tmp_path, hook_config):
    tool_input = {"command": "git log --oneline"}

    result = run_hook(hook_config, {"tool_name": "Bash", "tool_input": tool_input, "cwd": "/work/repo"})

    assert (result.returncode, result.stdout) == (0, "")
    [line] = logged(tmp_path)
    assert (line["decision"], line["rule"], line["input"]) == ("allow", "allow", tool_input)


def test_main_appends_one_line_per_call(tmp_path, hook_config):
    for command in ("git log", "git fetch origin", "pytest"):
        run_hook(hook_config, {"tool_name": "Bash", "tool_input": {"command": command}})

    assert [line["decision"] for line in logged(tmp_path)] == ["allow", "deny", "allow"]


def test_main_fails_closed_on_a_missing_config(tmp_path):
    result = run_hook(tmp_path / "missing.json", {"tool_name": "Bash", "tool_input": {"command": "ls"}})

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.stdout and "Sealed run: " in result.stdout


@pytest.mark.parametrize(
    "payload", ["not json", "[]", '{"tool_input": {}}', '{"tool_name": "Bash", "tool_input": 3}']
)
def test_main_fails_closed_on_malformed_input_and_logs_it(tmp_path, hook_config, payload):
    result = run_hook(hook_config, payload)

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    [line] = logged(tmp_path)
    assert line["decision"] == "deny"
    assert line["rule"] in {"fail-closed", "malformed"}


def test_main_fails_closed_when_the_log_cannot_be_written(tmp_path):
    config = _seal_guard.write_config(
        tmp_path / "c.json",
        task_project="",
        forbidden_paths=[],
        local_roots=[],
        guard_log=tmp_path / "no-such-dir" / "guard.jsonl",
        role_id="r",
    )

    result = run_hook(config, {"tool_name": "Bash", "tool_input": {"command": "ls"}})

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- settings -------------------------------------------------------------------------------- #


def test_settings_extra_denies_the_web_tools_and_installs_the_hook(tmp_path):
    extra = _seal_guard.settings_extra(tmp_path / "guard config.json")

    assert extra["permissions"]["deny"] == ["WebSearch", "WebFetch", "mcp__browser"]
    [entry] = extra["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    assert "-m flyte_agent_factory._seal_guard --config" in hook["command"]
    assert "'" in hook["command"].split("--config")[1]  # the path with a space is quoted


def test_merge_settings_installs_both_hooks_and_the_deny_rules(tmp_path):
    base = {"permissions": {"deny": ["Bash(flyte run:*)"]}, "pluginConfigs": {"a": {"options": {"x": 1}}}}
    merged = _seal_guard.merge_settings(
        _seal_guard.merge_settings(base, _context_guard.settings_extra({})),
        _seal_guard.settings_extra(tmp_path / "c.json"),
    )

    commands = [h["command"] for e in merged["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert any("_context_guard" in c for c in commands)
    assert any("_seal_guard" in c for c in commands)
    assert merged["permissions"]["deny"] == ["Bash(flyte run:*)", "WebSearch", "WebFetch", "mcp__browser"]
    assert merged["pluginConfigs"] == base["pluginConfigs"]


def test_merge_settings_treats_none_as_nothing_and_does_not_mutate(tmp_path):
    extra = _seal_guard.settings_extra(tmp_path / "c.json")
    before = json.dumps(extra)

    assert _seal_guard.merge_settings(None, extra) == extra
    assert _seal_guard.merge_settings(extra, None) == extra
    assert _seal_guard.merge_settings(extra, extra) == extra
    assert json.dumps(extra) == before


ANCESTOR_DENIED: list[ToolCall] = [
    bash("find /work"),
    bash("find /work -name '*.diff'"),
    bash("find / -name '*.diff'"),
    bash("grep -r x /work"),
    bash("grep -rn x /work/"),
    bash("grep -R x /work"),
    bash("grep --recursive x /work"),
    bash("rg x /work"),
    bash("tar cf - /work"),
    bash("cp -r /work /tmp/copy"),
    bash("rsync -a /work/ /tmp/copy/"),
    bash("du -a /work"),
    bash("ls -R /work"),
    bash("ls -laR /work"),
    bash("cd /work && find ."),
    bash("cd /work && grep -r x"),
    bash("cd /work && rg x"),
    bash("find /work/*"),
    bash("bash -c 'find /work'"),
    bash("cat /work/other-attempt/a.py"),
    bash("cp /secret/fix.diff /tmp/x"),
]
ANCESTOR_ALLOWED: list[ToolCall] = [
    bash("ls /work"),
    bash("ls /tmp"),
    bash("ls -la /"),
    bash("cat /tmp/notes.txt"),
    bash("find . -name x"),
    bash("find /work/local -name '*.py'"),
    bash("grep -r foo src"),
    bash("grep -rn foo /work/local/repo"),
    bash("grep foo /work/local/repo/a.py"),
    bash("tar czf /tmp/out.tgz src"),
    bash("cp -r src /tmp/copy"),
    bash("du -sh /tmp"),
    bash("rg foo"),
    bash("git -C /work/local/repo log"),
    bash("pytest -q"),
    ("Glob", {"pattern": "/work/local/**/*.py"}),
    ("Glob", {"pattern": "**/*.py", "path": "/work/local"}),
    ("Grep", {"pattern": "/work/other", "path": "/work/local", "glob": "*.py"}),
    ("Write", {"file_path": "/work/local/a.py", "content": "x"}),
    ("Edit", {"file_path": "/tmp/a.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": "/work/local/a.py", "edits": []}),
]
FORBIDDEN_TOOL_CALLS: list[ToolCall] = [
    ("Glob", {"pattern": "/work/**"}),
    ("Glob", {"pattern": "/work/**/*.py"}),
    ("Glob", {"pattern": "/work/other-attempt/*.py"}),
    ("Glob", {"pattern": "/work/{other-attempt,x}/*"}),
    ("Glob", {"pattern": "/secret/*.diff"}),
    ("Grep", {"pattern": "x", "glob": "/work/**"}),
    ("Write", {"file_path": "/work/other-attempt/a.py", "content": "x"}),
    ("Edit", {"file_path": "/work/other-attempt/a.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": "/secret/fix.diff", "edits": []}),
    ("NotebookEdit", {"notebook_path": "/work/other-attempt/n.ipynb", "new_source": "x"}),
]


@pytest.mark.parametrize("call", ANCESTOR_DENIED, ids=[str(c[1])[:60] for c in ANCESTOR_DENIED])
def test_a_traversal_of_an_ancestor_of_a_forbidden_path_is_denied(config, call: ToolCall):
    decision = _seal_guard.decide(*call, config, cwd="/")
    assert not decision.allow and decision.rule == "forbidden-path", call


@pytest.mark.parametrize("call", FORBIDDEN_TOOL_CALLS, ids=[str(c)[:60] for c in FORBIDDEN_TOOL_CALLS])
def test_absolute_globs_and_writes_into_forbidden_paths_are_denied(config, call: ToolCall):
    decision = _seal_guard.decide(*call, config, cwd=CWD)
    assert not decision.allow and decision.rule == "forbidden-path", call


@pytest.mark.parametrize("call", ANCESTOR_ALLOWED, ids=[str(c[1])[:60] for c in ANCESTOR_ALLOWED])
def test_ordinary_use_of_an_ancestor_directory_is_allowed(config, call: ToolCall):
    decision = _seal_guard.decide(*call, config, cwd=CWD)
    assert decision.allow, (call, decision)


def test_a_denied_traversal_is_recorded(tmp_path, hook_config):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "find /work"}, "cwd": CWD})
    done = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._seal_guard", "--config", str(hook_config)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    line = json.loads(Path(json.loads(hook_config.read_text())["guard_log"]).read_text().splitlines()[-1])
    assert (line["decision"], line["rule"]) == ("deny", "forbidden-path")
