from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _seal_audit as audit_mod
from flyte_agent_factory import _seal_shell as shell
from flyte_agent_factory._seal_audit import Gold, audit, select_distinctive_lines
from tests import seal_corpus as corpus

RULES = shell.Rules(task_project="tasklib", forbidden=("/secret/*",))


def kinds(command: str, rules: shell.Rules = RULES) -> list[str]:
    return shell.classify_command(command, rules)


def write(tmp_path: Path, build) -> Path:
    record = corpus.Record()
    build(record)
    record.write(tmp_path / "rec")
    return tmp_path / "rec"


def clean_record(record: corpus.Record) -> None:
    corpus.work(record.role("author-1"))


# --- shell splitting and routes ---------------------------------------------------------------------


GIT_FETCH_ROUTES = [
    "git fetch origin",
    "git fetch",
    "git fetch --depth 50 https://github.com/a/b main",
    "git pull",
    "git clone https://github.com/a/b",
    "git clone git@github.com:a/b.git",
    "git ls-remote --heads origin",
    "git remote update",
    "git submodule update --init",
    "git -C /repo -c http.sslVerify=false fetch origin",
    "FOO=1 git fetch origin",
    "env -u X GIT_TRACE=1 timeout -s KILL 30 nice -n 5 git fetch origin",
    "command git fetch origin",
    "cd /repo && ls; git fetch origin | tail -1",
    "for r in a b; do git fetch $r; done",
    "bash -c 'git fetch origin'",
    'sh -lc "cd /x && git pull"',
    "bash <<'EOF'\ngit fetch origin\nEOF",
    "git fetch origin \\\n  main",
    "git fetch $URL",
    # `bash -c`, `eval` and `$(...)` nest to the guard's depth (3), not one level.
    "bash -c 'bash -c \"git fetch origin\"'",
    "eval git ls-remote --heads https://github.com/acme/tasklib",
    "git clone --template /tmp/t https://github.com/acme/tasklib /tmp/up",
    "git clone --reference /tmp/r --bundle-uri /tmp/b https://github.com/acme/tasklib",
    "git remote add -f up https://github.com/acme/tasklib",
    "git fetch-pack https://github.com/acme/tasklib main",
    "git archive --remote=https://github.com/acme/tasklib HEAD",
    "cat <<EOF | bash\ngit fetch origin\nEOF",
]
NOT_GIT_FETCH = [
    "git fetch /tmp/bare main",
    "git fetch ../other",
    "git fetch file:///tmp/bare",
    "git fetch http://127.0.0.1:8000/x main",
    "git clone /tmp/bare /tmp/work",
    "git clone ./local",
    "git remote add up https://example.invalid/x.git",
    "git remote -v",
    "git push origin HEAD",
    "git log --oneline",
    "git commit -m 'git fetch origin'",
    "echo git fetch origin",
    "cat <<EOF > notes.txt\ngit fetch origin\nEOF",
]
HTTP_FETCH_CASES = [
    ("curl -s https://api.github.com/repos/a/b", True),
    ("curl -s api.github.com/repos/a/b", True),
    ("wget -qO- https://example.com/x", True),
    ("curl http://localhost:8000/", False),
    ("curl http://127.0.0.1:8000/x -o out.txt", False),
    ("python -c \"import urllib.request; urllib.request.urlopen('https://github.com/a/b')\"", True),
    ("python3.12 -c 'print(1)'", False),
    ("node -e \"fetch('https://api.github.com/x')\"", True),
    ("python - <<'EOF'\nimport requests\nrequests.get('https://github.com/a/b')\nEOF", True),
    ("echo https://github.com/a/b", False),
    (
        'python3 -c "import urllib.request as u; '
        "print(u.urlopen('https://api.github.com/repos/acme/tasklib/pulls').read())\"",
        True,
    ),
    (
        'uv run python -c "import urllib.request as u; '
        "print(u.urlopen('https://api.github.com/repos/acme/tasklib/pulls').read())\"",
        True,
    ),
    ("curl -s https://api.github.com/repos/acme/tasklib/pulls", True),
    ("setsid curl -s https://api.github.com/repos/acme/tasklib/pulls", True),
    ("echo $(curl -s https://api.github.com/repos/acme/tasklib/pulls)", True),
    ("echo `curl -s https://api.github.com/repos/acme/tasklib/pulls`", True),
    ("uv run pytest -q", False),
]
TASK_PACKAGE_CASES = [
    ("pip download tasklib", True),
    ("pip download requests", True),
    ("pip install tasklib", True),
    ("pip install 'TaskLib[extra]>=1.2'", True),
    ("python -m pip install -q tasklib==1.2", True),
    ("uv pip install -r req.txt tasklib", True),
    ("uv add TaskLib", True),
    ("pip install https://files.example.org/tasklib-1.2.tar.gz", True),
    ("pip install pytest requests", False),
    ("pip install tasklib-extras", False),
    ("pip install -r tasklib.txt", False),
    ("pip install -e .", False),
    ("pip install ./dist/tasklib-1.2.tar.gz", False),
    ("pip wheel tasklib==1.2.0 --no-deps -w /tmp/dl", True),
    ("uv tool install tasklib", True),
    ("uv tool run --from tasklib tasklib-cli", True),
    ("uvx tasklib", True),
    ("uvx pip download tasklib", True),
    ("pipx run pip download tasklib==1.2.0 --no-deps -d /tmp/dl", True),
    ("pipx install tasklib", True),
    ("pip wheel . -w /tmp/dl", False),
    ("uvx ruff check .", False),
]


@pytest.mark.parametrize("command", GIT_FETCH_ROUTES)
def test_git_fetch_family_is_a_route(command):
    assert shell.GIT_FETCH in kinds(command)


@pytest.mark.parametrize("command", NOT_GIT_FETCH)
def test_local_git_and_harmless_commands_are_not_routes(command):
    assert shell.GIT_FETCH not in kinds(command)


def test_gh_is_a_route_except_version():
    assert kinds("gh api repos/a/b") == [shell.GH]
    assert kinds("gh --version") == []


@pytest.mark.parametrize("command,expected", HTTP_FETCH_CASES)
def test_http_fetches_are_routes_by_host(command, expected):
    assert (shell.HTTP_FETCH in kinds(command)) is expected


@pytest.mark.parametrize("command,expected", TASK_PACKAGE_CASES)
def test_task_package_routes(command, expected):
    assert (shell.TASK_PACKAGE in kinds(command)) is expected


def test_install_of_the_task_project_needs_to_know_it():
    assert kinds("pip install tasklib", shell.Rules()) == []


def test_forbidden_paths_in_bash_args_and_file_tools():
    assert kinds("cat /secret/a/b.py") == [shell.FORBIDDEN_READ]
    assert kinds("grep -r foo --include=x --file=/secret/q") == [shell.FORBIDDEN_READ]
    assert kinds("cat /secret/../public/a.py") == []
    assert shell.find_routes("Read", {"file_path": "/secret/x.py"}, RULES)[0].kind == shell.FORBIDDEN_READ
    assert shell.find_routes("Glob", {"pattern": "/secret/**/*.py"}, RULES)[0].kind == shell.FORBIDDEN_READ
    assert shell.find_routes("Read", {"file_path": "/repo/x.py"}, RULES) == []


def test_web_tools_and_browser():
    assert shell.find_routes("WebSearch", {"query": "q"}, RULES)[0].kind == shell.WEB_TOOL
    assert (
        shell.find_routes("mcp__browser__browser_navigate", {"url": "https://a.com/x"}, RULES)[0].kind
        == shell.BROWSER
    )
    assert shell.find_routes("mcp__browser__browser_navigate", {"url": "http://localhost:3000"}, RULES) == []
    assert shell.find_routes("mcp__browser__browser_snapshot", {}, RULES) == []


def test_unterminated_quote_still_exposes_words():
    assert shell.GIT_FETCH in kinds("git fetch origin 'main")


def test_multiline_python_string_is_one_command():
    assert shell.HTTP_FETCH in kinds(
        "python -c \"import urllib.request\nurllib.request.urlopen('https://github.com/a')\""
    )


# --- parsing and timeline ---------------------------------------------------------------------------


def test_claude_lines_become_events(tmp_path):
    path = tmp_path / "roles" / "r" / "transcript" / "s.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {
            "type": "user",
            "timestamp": "2026-11-16T10:00:00.000Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-16T10:00:01.000Z",
            "message": {
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-16T10:00:02.000Z",
            "message": {
                "content": [
                    {
                        "tool_use_id": "t1",
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                    },
                ]
            },
        },
        {"type": "system", "timestamp": "2026-11-16T10:00:03.000Z"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    events, problems = audit_mod.parse_transcript("r", path, tmp_path)
    assert problems == []
    assert [e.kind for e in events] == ["user_text", "assistant_text", "tool_call", "tool_result"]
    call, result = events[2], events[3]
    assert (call.tool, call.input, call.tool_use_id, call.line) == ("Bash", {"command": "ls"}, "t1", 2)
    assert (result.text, result.is_error, result.tool_use_id) == ("a\nb", False, "t1")
    assert call.file == "roles/r/transcript/s.jsonl"
    assert call.ts == datetime(2026, 11, 16, 10, 0, 1, tzinfo=UTC)


def test_codex_lines_become_events(tmp_path):
    path = tmp_path / "rollout.jsonl"
    items = [
        {
            "timestamp": "2026-11-16T10:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": ["bash", "-lc", "git fetch origin"]}),
            },
        },
        {
            "timestamp": "2026-11-16T10:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"output": "fatal: nope", "metadata": {"exit_code": 128}}),
            },
        },
        {
            "timestamp": "2026-11-16T10:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in items))
    events, problems = audit_mod.parse_transcript("r", path, tmp_path)
    assert problems == []
    assert [(e.kind, e.tool) for e in events] == [
        ("tool_call", "Bash"),
        ("tool_result", ""),
        ("assistant_text", ""),
    ]
    assert events[0].input == {"command": "git fetch origin"}
    assert events[1].is_error and events[1].text == "fatal: nope"


def test_timeline_merges_roles_by_timestamp_and_keeps_file_order_on_ties(tmp_path):
    def build(record):
        first = record.role("author-1")
        second = record.role("reviewer-1", kind="reviewer")
        first.main.say("one")
        second.main.say("two")
        first.main.say("three")
        # The same instant in both: the earlier role's file comes first.
        for role, text in ((first, "tie-a"), (second, "tie-b")):
            role.main.lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-11-16T11:00:00.000Z",
                        "message": {"content": [{"type": "text", "text": text}]},
                    }
                )
            )

    root = write(tmp_path, build)
    events, problems = audit_mod.load_timeline(root, ["author-1", "reviewer-1"])
    assert problems == []
    texts = [e.text for e in events if e.kind == "assistant_text"]
    assert texts == ["one", "two", "three", "tie-a", "tie-b", "Done.", "Done."] or texts[:5] == [
        "one",
        "two",
        "three",
        "tie-a",
        "tie-b",
    ]


# --- distinctive lines ------------------------------------------------------------------------------


def test_distinctive_lines_come_from_added_lines_only():
    lines = select_distinctive_lines(corpus.GOLD)
    assert corpus.FIX_LINE in lines
    assert 'cleaned = value.strip().replace("\\t", " ")' in lines
    assert "return cleaned" not in lines  # shorter than 25 characters
    assert 'return ""' not in lines


def test_a_line_that_is_also_removed_or_context_is_not_distinctive():
    patch = "\n".join(
        [
            "--- a/x",
            "+++ b/x",
            "@@ -1,2 +1,2 @@",
            "-    value = compute_the_answer(argument)",
            "+    value   =  compute_the_answer(argument)",
            " unchanged_context_line_over_25_chars()",
            "+unchanged_context_line_over_25_chars()",
            "+brand_new_line_that_is_long_enough_()",
        ]
    )
    assert select_distinctive_lines(Gold(fix_patch=patch)) == ["brand_new_line_that_is_long_enough_()"]


# --- each void reason -------------------------------------------------------------------------------


def run_audit(tmp_path, build, gold=corpus.GOLD):
    return audit(write(tmp_path, build), gold)


def test_a_plain_run_is_clean(tmp_path):
    verdict = run_audit(tmp_path, clean_record)
    assert verdict.verdict == "clean"
    assert verdict.reasons == []
    assert verdict.gold is True
    assert verdict.counts["roles"] == 1


def test_reached_when_gold_line_is_read_before_anyone_wrote_it(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.main.run(
            "Read", {"file_path": "/x/parse.py"}, "   12\t    " + corpus.FIX_LINE.replace(" ", "    ", 1)
        )

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["reached"]
    finding = verdict.reasons[0]
    assert finding.role_id == "author-1" and finding.file == "roles/author-1/transcript/session.jsonl"
    assert finding.line == 3 and corpus.FIX_LINE.split()[0] in finding.evidence


def test_not_reached_when_the_line_was_authored_in_assistant_text_first(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.main.say(f"I will write {corpus.FIX_LINE}")
        role.main.run("Read", {"file_path": "/x/parse.py"}, corpus.FIX_LINE)

    assert run_audit(tmp_path, build).verdict == "clean"


def test_reached_when_another_role_wrote_it_later(tmp_path):
    def build(record):
        reader = record.role("author-1")
        writer = record.role("subagent-1", kind="subagent")
        reader.main.run("Read", {"file_path": "/x/parse.py"}, corpus.FIX_LINE)
        writer.main.run("Write", {"file_path": "/x/p.py", "content": corpus.FIX_LINE}, "ok")

    assert run_audit(tmp_path, build).verdict == "void"


def test_gold_sha_full_or_prefix_in_a_result_is_reached(tmp_path):
    for shown in (corpus.FIX_SHA, corpus.FIX_SHA[:10], corpus.FIX_SHA[:14]):

        def build(record, shown=shown):
            record.role("author-1").main.bash("git log --oneline", f"{shown} fix\n")

        verdict = run_audit(tmp_path / shown, build)
        assert [f.kind for f in verdict.reasons] == ["reached"]


def test_short_sha_prefix_is_not_reached(tmp_path):
    def build(record):
        record.role("author-1").main.bash("git log --oneline", f"{corpus.FIX_SHA[:9]} fix\n")

    assert run_audit(tmp_path, build).verdict == "clean"


def test_without_gold_reached_is_not_checked_and_the_verdict_says_so(tmp_path):
    def build(record):
        record.role("author-1").main.run("Read", {"file_path": "/x"}, corpus.FIX_LINE)

    verdict = run_audit(tmp_path, build, gold=None)
    assert verdict.verdict == "clean"
    assert json.loads(verdict.to_json())["gold"] is False


def test_route_succeeded_needs_a_result_without_failure_signs(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.main.bash("git fetch origin", "From https://github.com/a/b\n * branch main -> FETCH_HEAD\n")
        role.main.bash(
            "git fetch origin 2>&1 | tail -1",
            "fatal: unable to access 'https://github.com/a/b/': Could not resolve host",
        )
        role.main.bash("curl https://api.github.com/x", "HTTP 403", is_error=False)
        role.main.bash("gh api x", "ok", is_error=True)

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["route-succeeded"]
    assert verdict.reasons[0].line == 2
    assert len(verdict.attempts) == 3
    assert verdict.counts["attempts"] == 3


def test_route_call_without_a_result_is_an_attempt(tmp_path):
    def build(record):
        record.role("author-1").main.call("Bash", {"command": "git fetch origin"})

    verdict = run_audit(tmp_path, build)
    assert verdict.verdict == "clean" and len(verdict.attempts) == 1


def test_egress_is_a_proxy_allow_outside_model_index_push(tmp_path):
    def build(record):
        role = record.role("author-1")
        for route in ("model", "index", "push"):
            role.add_proxy(route, "allow")
        role.add_proxy("web", "deny", status=403)

    assert run_audit(tmp_path, build).verdict == "clean"

    def build_egress(record):
        record.role("author-1").add_proxy("web", "allow", target="github.com:443")

    verdict = run_audit(tmp_path / "e", build_egress)
    assert [f.kind for f in verdict.reasons] == ["egress"]
    assert verdict.reasons[0].file == "roles/author-1/proxy.jsonl"


def test_denials_in_proxy_and_guard_logs_are_attempts(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.add_proxy("web", "deny", status=403)
        role.add_guard("Bash", {"command": "git fetch"}, "deny", "git-fetch")

    verdict = run_audit(tmp_path, build)
    assert verdict.verdict == "clean"
    assert sorted(a.kind for a in verdict.attempts) == ["guard-denied", "proxy-denied"]


def test_unsealed_when_a_flag_is_off_or_the_spec_hash_differs(tmp_path):
    def off(record):
        record.role("author-1").seal["guard"] = False

    assert [f.kind for f in run_audit(tmp_path / "a", off).reasons] == ["unsealed"]

    def other_spec(record):
        record.role("author-1").seal["spec_sha256"] = "0" * 64

    assert [f.kind for f in run_audit(tmp_path / "b", other_spec).reasons] == ["unsealed"]


def test_incomplete_reasons(tmp_path):
    def no_manifest_role(record):
        record.role("author-1").registered_only = True

    verdict = run_audit(tmp_path / "a", no_manifest_role)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "no directory" in verdict.reasons[0].evidence

    def broken(record):
        role = record.role("author-1")
        role.main.raw("not json at all")

    verdict = run_audit(tmp_path / "b", broken)
    assert any(f.kind == "incomplete" and f.line == 2 and "not JSON" in f.evidence for f in verdict.reasons)


@pytest.mark.parametrize("log", ["guard.jsonl", "proxy.jsonl"])
@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_a_role_without_its_guard_or_proxy_log_is_incomplete(tmp_path, log, engine):
    def build(record):
        role = record.role("author-1", engine=engine)
        role.final_text = "Done."
        role.omit = (log,)

    verdict = run_audit(tmp_path, build)
    assert verdict.verdict == "void"
    assert [(f.kind, f.file) for f in verdict.reasons] == [("incomplete", f"roles/author-1/{log}")]
    assert "missing" in verdict.reasons[0].evidence


def test_incomplete_when_manifest_is_missing_or_broken(tmp_path):
    root = write(tmp_path / "a", clean_record)
    (root / "manifest.json").unlink()
    assert audit(root, None).reasons[0].kind == "incomplete"
    (root / "manifest.json").write_text("{")
    assert audit(root, None).verdict == "void"


def test_agent_call_is_linked_by_meta_or_by_agent_file(tmp_path):
    def build(record):
        role = record.role("author-1")
        with role.agent("x"):
            pass

    root = write(tmp_path / "a", build)
    assert audit(root, None).verdict == "clean"
    # Without the meta.json the agent-<id>.jsonl named in the result still links it (and end.json is redone).
    role_dir = root / "roles" / "author-1"
    for meta in role_dir.rglob("*.meta.json"):
        meta.unlink()
    (role_dir / "end.json").write_text(json.dumps({"files": corpus.hash_role_files(role_dir)}))
    assert audit(root, None).verdict == "clean"
    for sub in role_dir.rglob("agent-*.jsonl"):
        sub.unlink()
    (role_dir / "end.json").write_text(json.dumps({"files": corpus.hash_role_files(role_dir)}))
    verdict = audit(root, None)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "no sub-agent transcript" in verdict.reasons[0].evidence


def test_agent_meta_without_its_transcript_does_not_link_the_call(tmp_path):
    def build(record):
        role = record.role("author-1")
        with role.agent("x"):
            pass
        role.sessions.clear()  # the .meta.json stays, the agent-*.jsonl is gone

    verdict = audit(write(tmp_path, build), None)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "no sub-agent transcript" in verdict.reasons[0].evidence


def test_an_empty_agent_transcript_does_not_link_the_call(tmp_path):
    def build(record):
        with record.role("author-1").agent("x"):
            pass

    root = write(tmp_path, build)
    role_dir = root / "roles" / "author-1"
    assert audit(root, None).verdict == "clean"
    for sub in role_dir.rglob("agent-*.jsonl"):
        sub.write_text("")
    (role_dir / "end.json").write_text(json.dumps({"files": corpus.hash_role_files(role_dir)}))
    reasons = audit(root, None).reasons  # the empty file is also a transcript with no assistant events
    assert any("no sub-agent transcript" in f.evidence for f in reasons)


def result_event(text):
    return audit_mod.Event("r", "f", 1, datetime.min.replace(tzinfo=UTC), "tool_result", text=text)


@pytest.mark.parametrize(
    "text",
    [
        "fatal: unable to access 'https://github.com/a/b/': Could not resolve host: github.com",
        "fatal: Could not read from remote repository.",
        "curl: (56) CONNECT tunnel failed, response 403",
        "ssh: connect to host github.com port 22: Network is unreachable",
        "Failed to connect to 10.0.0.1 port 443: Connection refused",
        "HTTP/1.1 403 Forbidden\nserver: x",
        "CONNECT github.com:443 HTTP/1.1 -> 403",
        "gh: HTTP 404: Not Found (https://api.github.com/x)",
        "Collecting x\nERROR: Could not find a version that satisfies the requirement x",
        "error: Failed to fetch: `https://pypi.org/simple/x/`",
        "Traceback (most recent call last):\n  File \"<string>\", line 1\nurllib.error.URLError: <x>",
        "Traceback (most recent call last):\nOSError: [Errno 101] Network is unreachable",
        "PreToolUse:Bash hook error: Sealed run: `curl` names api.github.com.",
        "ls: cannot access '/x/swe-attempt-1': No such file or directory",
        "WebFetch failed: Request failed with status code 403",
    ],
)  # fmt: skip
def test_real_failure_texts_are_failed_routes(text):
    assert not audit_mod.route_worked(result_event(text))


@pytest.mark.parametrize(
    "text",
    [
        '{"title":"Fix escape","id":1840331}',
        "The fix is about proxy objects.",
        "E   TypeError: not str\n1 failed",
        "the page was not found in the cache, so it was rebuilt; this is forbidden knowledge",
        "access denied is what the old message said; error: is a common word",
    ],
)  # fmt: skip
def test_text_that_only_holds_a_failure_word_is_a_worked_route(text):
    assert audit_mod.route_worked(result_event(text))


def test_evidence_is_truncated_to_300_characters(tmp_path):
    def build(record):
        record.role("author-1").main.bash("git fetch origin # " + "x" * 1000, "From github")

    verdict = run_audit(tmp_path, build)
    assert len(verdict.reasons[0].evidence) == 300


def test_verdict_is_deterministic(tmp_path):
    root = write(tmp_path, lambda r: corpus.CASES[0].build(r))
    assert audit(root, corpus.GOLD).to_json() == audit(root, corpus.GOLD).to_json()


def test_task_project_and_forbidden_can_come_from_arguments_instead_of_gold(tmp_path):
    def build(record):
        record.role("author-1").main.bash("pip install tasklib", "Installed 1 package\n")

    root = write(tmp_path, build)
    assert audit(root, None).verdict == "clean"
    assert audit(root, None, task_project="TaskLib").verdict == "void"
    assert audit(root, Gold(task_project="tasklib")).verdict == "void"


# --- codex rollouts as codex 0.163 writes them -------------------------------------------------------


def rollout(tmp_path: Path, payloads: list[dict[str, Any]]) -> Path:
    path = tmp_path / "rollout.jsonl"
    lines = [
        {"timestamp": f"2026-11-16T10:00:{i:02d}.000Z", "type": "response_item", "payload": p}
        for i, p in enumerate(payloads)
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_codex_shell_output_text_carries_the_exit_code(tmp_path):
    def shell_output(call_id, output):
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    path = rollout(
        tmp_path,
        [
            shell_output("c1", "Exit code: 128\nWall time: 0.2 seconds\nOutput:\nfatal: no\n"),
            shell_output(
                "c2", "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nExit code: 7 is in the text\n"
            ),
            shell_output(
                "c3",
                (
                    "Chunk ID: ab12\nWall time: 1.0 seconds\nProcess exited with code 2\n"
                    "Original token count: 3\nOutput:\nboom"
                ),
            ),
            shell_output("c4", [{"type": "input_text", "text": "listed"}]),
            {
                "type": "custom_tool_call_output",
                "call_id": "c5",
                "output": "Success. Updated the following files:\nM a.py\n",
            },
        ],
    )
    events, problems = audit_mod.parse_transcript("r", path, tmp_path)
    assert problems == []
    assert [(e.text, e.is_error) for e in events] == [
        ("fatal: no\n", True),
        ("Exit code: 7 is in the text\n", False),
        ("boom", True),
        ("listed", False),
        ("Success. Updated the following files:\nM a.py\n", False),
    ]


def test_codex_tool_calls_become_bash_and_apply_patch_calls(tmp_path):
    patch = "*** Begin Patch\n*** Add File: a.py\n+print(1)\n*** End Patch"
    path = rollout(
        tmp_path,
        [
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "a",
                "arguments": json.dumps({"cmd": "git status"}),
            },
            {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "b",
                "arguments": json.dumps({"command": "ls"}),
            },
            {
                "type": "local_shell_call",
                "call_id": "c",
                "status": "completed",
                "action": {"type": "exec", "command": ["bash", "-lc", "pwd"]},
            },
            {"type": "custom_tool_call", "name": "apply_patch", "call_id": "d", "input": patch},
            {
                "type": "function_call",
                "name": "update_plan",
                "call_id": "e",
                "arguments": json.dumps({"plan": []}),
            },
        ],
    )
    events, _ = audit_mod.parse_transcript("r", path, tmp_path)
    assert [(e.tool, e.input) for e in events] == [
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": "ls"}),
        ("Bash", {"command": "pwd"}),
        ("apply_patch", {"input": patch}),
        ("update_plan", {"plan": []}),
    ]
    assert [e.tool_use_id for e in events] == ["a", "b", "c", "d", "e"]


def test_codex_lines_that_are_not_the_conversation_give_no_events(tmp_path):
    path = tmp_path / "rollout.jsonl"
    items = [
        {
            "timestamp": "2026-11-16T10:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "x", "cwd": "/repo"},
        },
        {
            "timestamp": "2026-11-16T10:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "dup"},
        },
        {
            "timestamp": "2026-11-16T10:00:02.000Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": []},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in items))
    assert audit_mod.parse_transcript("r", path, tmp_path) == ([], [])


# --- seal flags per engine --------------------------------------------------------------------------


def seal_kinds(tmp_path, engine, **flags):
    def build(record):
        role = record.role("author-1", engine=engine)
        corpus.work(role) if engine == "claude" else role.main.bash("ls", "src\n")
        role.seal.update(flags)

    return [f.kind for f in run_audit(tmp_path, build).reasons]


def test_guard_is_required_for_claude_roles_only(tmp_path):
    assert seal_kinds(tmp_path / "a", "claude", guard=False) == ["unsealed"]
    assert seal_kinds(tmp_path / "b", "codex", guard=False) == []
    assert seal_kinds(tmp_path / "c", "codex", guard=True) == []


def test_net_and_tools_off_are_required_for_every_engine(tmp_path):
    for engine in ("claude", "codex"):
        for flag in ("net", "tools_off"):
            assert seal_kinds(tmp_path / engine / flag, engine, **{flag: False}) == ["unsealed"], (
                engine,
                flag,
            )


def test_an_unknown_engine_is_incomplete_not_clean(tmp_path):
    verdict = run_audit(tmp_path, lambda record: corpus.work(record.role("author-1", engine="gemini")))
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "unknown engine" in verdict.reasons[0].evidence


def test_a_codex_role_without_a_transcript_is_incomplete(tmp_path):
    def build(record):
        record.role("author-1", engine="codex").no_transcript = True

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "no transcript" in verdict.reasons[0].evidence


def test_a_codex_command_is_checked_for_routes_and_gold(tmp_path):
    def fetched(record):
        role = record.role("author-1", engine="codex")
        role.main.bash("git fetch origin", corpus.FETCH_OK)

    assert [f.kind for f in run_audit(tmp_path / "a", fetched).reasons] == ["route-succeeded"]

    def denied(record):
        role = record.role("author-1", engine="codex")
        role.main.bash("git fetch origin", corpus.TUNNEL_403, is_error=True)

    verdict = run_audit(tmp_path / "b", denied)
    assert verdict.verdict == "clean" and len(verdict.attempts) == 1

    def reached(record):
        record.role("author-1", engine="codex").main.bash("cat x.py", corpus.GOLD_FILE)

    assert [f.kind for f in run_audit(tmp_path / "c", reached).reasons] == ["reached"]


def test_a_codex_apply_patch_is_authored_text(tmp_path):
    def build(record):
        role = record.role("author-1", engine="codex")
        role.main.apply_patch(corpus.FIX_PATCH_TEXT)
        role.main.bash("cat src/tasklib/parse.py", corpus.GOLD_FILE)

    assert run_audit(tmp_path / "a", build).verdict == "clean"

    def read_first(record):
        role = record.role("author-1", engine="codex")
        role.main.bash("cat src/tasklib/parse.py", corpus.GOLD_FILE)
        role.main.apply_patch(corpus.FIX_PATCH_TEXT)

    assert [f.kind for f in run_audit(tmp_path / "b", read_first).reasons] == ["reached"]


# --- package files ----------------------------------------------------------------------------------


def test_package_file_downloads_through_the_proxy_are_not_egress(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.add_proxy("index", "allow", target="/index/simple/pytest/")
        role.add_proxy("files", "allow", target="/files/pytest-8.3.0-py3-none-any.whl")
        role.add_proxy("files", "allow", target="/files/some_pkg-1.0.tar.gz?sig=1")

    assert run_audit(tmp_path, build).verdict == "clean"


@pytest.mark.parametrize(
    "target",
    [
        "/files/tasklib-1.2.0.tar.gz",
        "/files/TaskLib-1.2.0-py3-none-any.whl",
        "/files/tasklib-1.2.0.tar.gz.metadata",
    ],
)
def test_a_files_allow_for_the_task_project_is_egress(tmp_path, target):
    def build(record):
        record.role("author-1").add_proxy("files", "allow", target=target)

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["egress"]


def test_a_files_deny_for_the_task_project_is_only_an_attempt(tmp_path):
    def build(record):
        record.role("author-1").add_proxy("files", "deny", target="/files/tasklib-1.2.0.tar.gz", status=403)

    verdict = run_audit(tmp_path, build)
    assert verdict.verdict == "clean" and [a.kind for a in verdict.attempts] == ["proxy-denied"]


def test_the_task_project_for_files_can_come_from_the_argument(tmp_path):
    def build(record):
        record.role("author-1").add_proxy("files", "allow", target="/files/othername-1.0.tar.gz")

    root = write(tmp_path, build)
    assert audit(root, None).verdict == "clean"
    assert audit(root, None, task_project="OtherName").verdict == "void"


# --- CLI --------------------------------------------------------------------------------------------


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._seal_audit", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=60,
    )


def test_cli_exit_codes_and_json(tmp_path):
    clean = write(tmp_path / "clean", clean_record)
    void = write(tmp_path / "void", lambda r: corpus.CASES[0].build(r))
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "fix_patch": corpus.FIX_PATCH,
                "test_patch": corpus.TEST_PATCH,
                "shas": [corpus.FIX_SHA],
                "task_project": "tasklib",
                "forbidden_paths": [],
            }
        )
    )

    done = cli(str(clean), "--gold", str(gold))
    assert done.returncode == 0 and "clean" in done.stdout and "warning" not in done.stderr

    done = cli(str(void), "--gold", str(gold), "--json")
    assert done.returncode == 1
    payload = json.loads(done.stdout)
    assert payload["verdict"] == "void" and payload["gold"] is True
    assert set(payload) == {"verdict", "gold", "reasons", "attempts", "counts"}
    assert set(payload["reasons"][0]) == {"kind", "role_id", "file", "line", "evidence"}

    done = cli(str(clean), "--json")
    assert done.returncode == 3 and json.loads(done.stdout)["gold"] is False
    assert "warning" in done.stderr and "gold" in done.stderr


def test_cli_usage_errors_exit_2(tmp_path):
    assert cli(str(tmp_path / "nowhere")).returncode == 2
    assert cli().returncode == 2
    root = write(tmp_path / "r", clean_record)
    assert cli(str(root), "--gold", str(tmp_path / "missing.json")).returncode == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert cli(str(root), "--gold", str(bad)).returncode == 2


@pytest.mark.parametrize(
    ("data", "named"),
    [
        ({"patch": corpus.FIX_PATCH}, "'patch'"),
        ({"fix_patch": corpus.FIX_PATCH, "sha": [corpus.FIX_SHA]}, "'sha'"),
        ({"shas": [corpus.FIX_SHA.upper()]}, "shas"),
        ({"shas": [corpus.FIX_SHA[:12]]}, "shas"),
        ({"shas": corpus.FIX_SHA}, "shas"),
        ({"fix_patch": ["+x"]}, "fix_patch"),
        ([], "object"),
    ],
)
def test_gold_with_unknown_or_malformed_fields_is_refused_naming_them(data, named):
    with pytest.raises(ValueError, match=named):
        Gold.from_dict(data)


@pytest.mark.parametrize("data", [{}, {"fix_patch": "+x\n"}, {"shas": []}, {"test_patch": "+short line\n"}])
def test_gold_that_identifies_nothing_is_refused(data):
    with pytest.raises(ValueError, match="identifies nothing"):
        Gold.from_dict(data)


def test_the_committed_e2e_gold_still_loads():
    gold = Gold.load(Path(__file__).resolve().parents[1] / "docs" / "seal-e2e" / "gold.json")
    assert gold.shas and select_distinctive_lines(gold)


def test_cli_refuses_a_gold_that_checks_nothing(tmp_path):
    root = write(tmp_path / "r", clean_record)
    for name, data in {"empty": {}, "swe": {"patch": corpus.FIX_PATCH}}.items():
        gold = tmp_path / f"{name}.json"
        gold.write_text(json.dumps(data))
        done = cli(str(root), "--gold", str(gold))
        assert done.returncode == 2, (name, done.stdout)
        assert ("identifies nothing" if name == "empty" else "fix_patch") in done.stderr


def test_cli_forbidden_and_task_project_options(tmp_path):
    def build(record):
        role = record.role("author-1")
        role.main.run("Read", {"file_path": "/data/other/x.py"}, "x = 1\n")

    root = write(tmp_path, build)
    assert cli(str(root)).returncode == 3  # clean, but `reached` was not checked
    assert cli(str(root), "--forbidden", "/data/other/*").returncode == 1


# --- codex hosted web search -------------------------------------------------------------------------


def test_a_codex_web_search_call_voids_the_run_at_its_line(tmp_path):
    def searched(record):
        role = record.role("author-1", engine="codex")
        role.main.ask("Fix it.")
        role.main.web_search("tasklib parse empty value")

    verdict = run_audit(tmp_path, searched)
    assert verdict.verdict == "void"
    [finding] = verdict.reasons
    assert finding.kind == "route-succeeded"
    rollout = (tmp_path / "rec" / finding.file).read_text().splitlines()
    assert json.loads(rollout[finding.line - 1])["payload"]["type"] == "web_search_call"
    assert "tasklib parse empty value" in finding.evidence


def test_a_codex_web_search_seen_twice_is_one_finding(tmp_path):
    def searched(record):
        record.role("author-1", engine="codex").main.web_search("q one", events=True)

    assert len(run_audit(tmp_path, searched).reasons) == 1


def test_a_codex_event_msg_web_search_alone_voids_and_begin_end_are_one(tmp_path):
    path = tmp_path / "rollout.jsonl"
    payloads = [
        {"type": "web_search_begin", "call_id": "ws_1"},
        {"type": "web_search_end", "call_id": "ws_1", "query": "needle"},
    ]
    lines = [
        {"timestamp": f"2026-11-16T10:00:0{i}.000Z", "type": "event_msg", "payload": p}
        for i, p in enumerate(payloads)
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    events, problems = audit_mod.parse_transcript("author-1", path, tmp_path)
    assert problems == []
    assert [e.kind for e in events] == ["tool_call", "tool_result", "tool_result"]
    assert events[0].tool == "WebSearch"


def test_a_codex_open_page_names_the_url(tmp_path):
    def opened(record):
        role = record.role("author-1", engine="codex")
        role.main.emit(
            {
                "timestamp": record.clock.tick(),
                "type": "response_item",
                "payload": {
                    "type": "web_search_call",
                    "action": {"type": "open_page", "url": "https://x.test/p"},
                },
            }
        )

    [finding] = run_audit(tmp_path, opened).reasons
    assert "https://x.test/p" in finding.evidence


def test_a_failed_codex_web_search_is_an_attempt_and_prose_is_clean(tmp_path):
    def failed(record):
        record.role("author-1", engine="codex").main.web_search("q", status="failed")

    verdict = run_audit(tmp_path / "a", failed)
    assert verdict.verdict == "clean" and [f.kind for f in verdict.attempts] == ["route-failed"]

    def prose(record):
        record.role("author-1", engine="codex").main.say("A web search would help; web_search is off.")

    assert run_audit(tmp_path / "b", prose).verdict == "clean"


def test_a_manifest_with_no_roles_is_incomplete(tmp_path):
    verdict = run_audit(tmp_path, lambda record: None)
    assert verdict.verdict == "void"
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert verdict.reasons[0].file == "manifest.json" and "no roles" in verdict.reasons[0].evidence


def test_an_unanswered_tool_call_is_incomplete_with_the_line_of_the_call(tmp_path):
    call_line = []

    def build(record):
        role = record.role("plain-1", kind="plain")
        corpus.work(role)
        role.main.call("Read", {"file_path": "/repo/src/tasklib/parse.py"})
        call_line.append(len(role.main.lines))

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    finding = verdict.reasons[0]
    assert finding.file == "roles/plain-1/transcript/session.jsonl"
    assert finding.line == call_line[0]
    assert "Read call" in finding.evidence and "no result" in finding.evidence


def test_unanswered_codex_calls_are_incomplete(tmp_path):
    def build(record):
        role = record.role("plain-1", kind="plain", engine="codex")
        role.main.apply_patch(corpus.FIX_PATCH_TEXT)
        role.main.call("Bash", {"command": "pytest -q"})
        role.main.call("apply_patch", {"input": corpus.FIX_PATCH_TEXT})

    verdict = run_audit(tmp_path, build)
    assert [f.kind for f in verdict.reasons] == ["incomplete", "incomplete"]
    assert [f.line for f in verdict.reasons] == sorted(f.line for f in verdict.reasons)


def test_the_author_stopped_in_its_last_step_is_not_incomplete_but_a_gap_before_it_is(tmp_path):
    def stopped(record):
        role = record.role("author-1")
        corpus.work(role)
        role.main.call("Bash", {"command": "pytest -q"})
        role.main.call("Read", {"file_path": "/repo/src/tasklib/parse.py"})  # parallel, same final step

    assert run_audit(tmp_path / "a", stopped).verdict == "clean"

    def gap(record):
        role = record.role("author-1")
        corpus.work(role)
        role.main.call("Bash", {"command": "pytest -q"})
        role.main.say("The tests pass.")

    verdict = run_audit(tmp_path / "b", gap)
    assert [f.kind for f in verdict.reasons] == ["incomplete"]
    assert "Bash call" in verdict.reasons[0].evidence

    def stopped_sub(record):  # the exemption is the author's: any other role ends by itself
        record.role("author-1")
        sub = record.role("judge-1", kind="judge", parent="author-1")
        corpus.work(sub)
        sub.main.call("Bash", {"command": "pytest -q"})

    assert [f.kind for f in run_audit(tmp_path / "c", stopped_sub).reasons] == ["incomplete"]


def test_answered_parallel_calls_and_hosted_searches_are_not_unanswered(tmp_path):
    def build(record):
        role = record.role("author-1", engine="codex")
        role.main.web_search("tasklib parse", status="failed", events=True)
        role.main.apply_patch(corpus.FIX_PATCH_TEXT)

    assert run_audit(tmp_path / "codex", build).verdict == "clean"

    def build_parallel(record):
        role = record.role("author-1")
        first = role.main.call("Read", {"file_path": "/repo/a.py"})
        second = role.main.call("Read", {"file_path": "/repo/b.py"})
        role.main.result(second, "b")
        role.main.result(first, "a")
        role.main.say("Done.")

    assert run_audit(tmp_path / "claude", build_parallel).verdict == "clean"
