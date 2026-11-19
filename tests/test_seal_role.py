"""`sealed_role`: one launcher for every sealed role, proven with a fake `claude` and no model."""

import dataclasses
import errno
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from flyte_agent_factory import _seal_audit, _seal_net, _seal_record, _seal_role
from tests._seal_fakes import SPEC, install_fake_claude, read_jsonl

needs_netns = pytest.mark.usefixtures("seal_namespaces")


def _run_fake(role, tmp_path, config_dir):
    install_fake_claude(tmp_path / "bin")
    env = {
        **os.environ,
        **role.env,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    return subprocess.run(
        role.wrap(["claude", "-p", "go"]), env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path
    )


@needs_netns
def test_a_role_has_no_way_out_and_leaves_a_complete_record(tmp_path):
    root, config = tmp_path / "record", tmp_path / "claude"
    with _seal_role.sealed_role(
        SPEC, root, "author", "author", "claude", workdir_roots=[tmp_path], config_dir=config
    ) as role:
        done = _run_fake(role, tmp_path, config)
        role.exit_code = done.returncode
    assert done.returncode == 0, done.stderr
    assert "curl_rc=" in done.stdout and "curl_rc=0" not in done.stdout
    assert "ls_remote_rc=0" not in done.stdout

    paths = _seal_record.role_paths(root, "author")
    proxy = read_jsonl(paths.proxy_log)
    connect = [line for line in proxy if line["method"] == "CONNECT"]
    assert sorted((line["target"], line["decision"]) for line in connect) == [
        ("api.github.com:443", "deny"),
        ("github.com:443", "deny"),
    ]
    assert all(line["decision"] == "deny" for line in proxy)
    end = json.loads(paths.end.read_text())
    assert end["exit_code"] == 0
    assert {"role.json", "proxy.jsonl", "guard.jsonl", "transcript/projects/-work/session-1.jsonl"} <= set(
        end["files"]
    )
    role_json = json.loads((paths.dir / "role.json").read_text())
    assert role_json["seal"] == {
        "net": True,
        "guard": True,
        "tools_off": True,
        "fs_readonly": True,
        "spec_sha256": SPEC.sha256(),
    }
    assert json.loads((root / "manifest.json").read_text())["roles"][0]["role_id"] == "author"


@needs_netns
def test_the_guard_settings_come_with_the_role(tmp_path):
    with _seal_role.sealed_role(
        SPEC, tmp_path / "r", "author", "author", "claude", workdir_roots=[tmp_path]
    ) as role:
        assert "WebSearch" in role.settings_extra["permissions"]["deny"]
        assert role.settings_extra["hooks"]["PreToolUse"]
        assert role.argv_prefix[-1] == "--"
        assert "--exclude-newer" in role.argv_prefix and "2025-03-01T00:00:00Z" in role.argv_prefix


@needs_netns
def test_codex_has_no_guard_but_is_sealed_and_has_web_search_off(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n')
    with _seal_role.sealed_role(
        SPEC, tmp_path / "r", "d1", "delegate", "codex", workdir_roots=[tmp_path], config_dir=codex_home
    ) as role:
        assert role.settings_extra == {}
    paths = _seal_record.role_paths(tmp_path / "r", "d1")
    role_json = json.loads((paths.dir / "role.json").read_text())
    assert role_json["seal"]["guard"] is False and role_json["seal"]["net"] is True
    assert (paths.transcript_dir / "sessions" / "2026" / "rollout-1.jsonl").is_file()
    assert _seal_role.CODEX_TOOLS_OFF_ARGS == ("-c", 'web_search="disabled"')


@needs_netns
def test_a_failing_role_is_still_finished_with_a_nonzero_exit(tmp_path):
    with pytest.raises(ValueError, match="boom"):
        with _seal_role.sealed_role(
            SPEC, tmp_path / "r", "author", "author", "claude", workdir_roots=[tmp_path]
        ):
            raise ValueError("boom")
    end = json.loads(_seal_record.role_paths(tmp_path / "r", "author").end.read_text())
    assert end["exit_code"] == 1


def test_an_unavailable_seal_refuses_instead_of_running_unsealed(tmp_path, monkeypatch):
    monkeypatch.setattr(_seal_net, "unavailable_reason", lambda: "unshare: [Errno 1] Operation not permitted")
    with pytest.raises(RuntimeError, match=r"Operation not permitted.*unsealed"):
        with _seal_role.sealed_role(SPEC, tmp_path / "r", "author", "author", "claude", workdir_roots=[]):
            pytest.fail("the role must not start")
    assert not (tmp_path / "r").exists()


def test_a_registered_role_is_not_registered_twice_and_close_author_record_rewrites_end(tmp_path):
    root, config = tmp_path / "r", tmp_path / "claude"
    session = config / "projects" / "-w" / "s.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"type":"user"}\n')
    _seal_record.register(
        root, "author", "author", engine="claude", seal=_seal_role.seal_flags(SPEC, "claude")
    )
    _seal_role.close_author_record(root, config)
    first = _seal_record.role_paths(root, "author").end.read_text()
    session.write_text('{"type":"user"}\n{"type":"assistant"}\n')
    _seal_role.close_author_record(root, config)
    second = json.loads(_seal_record.role_paths(root, "author").end.read_text())
    assert json.loads(first)["files"]["transcript/projects/-w/s.jsonl"]["lines"] == 1
    assert second["files"]["transcript/projects/-w/s.jsonl"]["lines"] == 2
    assert len(json.loads((root / "manifest.json").read_text())["roles"]) == 1


@pytest.mark.parametrize(
    ("purpose", "kind"),
    [
        ("delegate", "delegate"),
        ("judgement", "judge"),
        ("correctness", "reviewer"),
        ("arbiter", "reviewer"),
        ("correctness-check", "reviewer"),
        ("planner", "subagent"),
        ("contract", "subagent"),
        ("", "subagent"),
    ],
)
def test_kind_for_purpose(purpose, kind):
    assert _seal_role.kind_for(purpose) == kind


def test_a_wrapped_claude_command_loses_the_web_tools_on_its_command_line():
    role = _seal_role.SealedRole(role_id="r", argv_prefix=["launch", "--"], env={}, settings_extra={})
    argv = role.wrap(["/usr/bin/claude", "-p", "go", "--permission-mode", "bypassPermissions"])
    assert argv[:2] == ["launch", "--"]
    # Last, so the variadic option cannot swallow the prompt or another argument.
    assert argv[-4:] == ["--disallowedTools", "WebSearch", "WebFetch", "mcp__browser"]
    assert argv[2:-4] == ["/usr/bin/claude", "-p", "go", "--permission-mode", "bypassPermissions"]
    assert role.wrap(["codex", "exec", "go"]) == ["launch", "--", "codex", "exec", "go"]


def test_expand_forbidden_globs_keeps_directories_and_files_and_drops_what_is_below_a_directory(tmp_path):
    (tmp_path / "data" / "sub").mkdir(parents=True)
    (tmp_path / "data" / "sub" / "deep.json").write_text("1")
    (tmp_path / "gold.diff").write_text("2")
    (tmp_path / "t1.json").write_text("3")
    (tmp_path / "t2.json").write_text("4")
    found = _seal_role._expand_forbidden(
        [
            str(tmp_path / "data" / "**"),
            str(tmp_path / "data"),
            str(tmp_path / "gold.diff"),
            str(tmp_path / "t*.json"),
            str(tmp_path / "absent" / "*"),
        ]
    )
    assert sorted(found) == sorted(
        [
            str(tmp_path / "data"),
            str(tmp_path / "gold.diff"),
            str(tmp_path / "t1.json"),
            str(tmp_path / "t2.json"),
        ]
    )


_TAMPER = """
import json, os, sys
config = sys.argv[1]
out = {}
try:
    open(config, "w").write("{}")
    out["config_written"] = True
except OSError as error:
    out["config_written"] = error.errno
try:
    os.unlink(config)
    out["config_removed"] = True
except OSError as error:
    out["config_removed"] = error.errno
for name in sys.argv[2:]:
    try:
        out[name] = open(name).read()
    except OSError as error:
        out[name] = error.errno
print(json.dumps(out))
"""


@needs_netns
def test_a_sealed_role_cannot_rewrite_its_guard_config_or_read_forbidden_files(tmp_path):
    task_data = tmp_path / "task-data"
    task_data.mkdir()
    (task_data / "t.json").write_text("secret")
    other = tmp_path / "other.diff"
    other.write_text("secret diff")
    attempts = tmp_path / "attempts"
    attempts.mkdir()
    (attempts / "a1.txt").write_text("secret attempt")
    spec = dataclasses.replace(
        SPEC,
        forbidden_paths=(str(task_data / "*"), str(other), str(attempts / "**")),
    )
    with _seal_role.sealed_role(
        spec, tmp_path / "r", "author", "author", "claude", workdir_roots=[tmp_path]
    ) as role:
        hook = role.settings_extra["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        config = shlex.split(hook)[-1]
        done = subprocess.run(
            role.wrap(
                [
                    sys.executable,
                    "-c",
                    _TAMPER,
                    config,
                    str(task_data / "t.json"),
                    str(other),
                    str(attempts / "a1.txt"),
                ]
            ),
            env={**os.environ, **role.env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        # EROFS: the guard's directory is a read-only mount in the role's namespace.
        assert result["config_written"] == errno.EROFS
        assert result["config_removed"] == errno.EROFS
        assert result[str(task_data / "t.json")] == ""
        assert result[str(other)] == ""
        assert result[str(attempts / "a1.txt")] in ("", errno.ENOENT)
        # Outside the seal the guard still reads its intact config, and the files are untouched.
        assert json.loads(Path(config).read_text())["forbidden_paths"]
        assert (task_data / "t.json").read_text() == "secret"
        assert other.read_text() == "secret diff"


_TAMPER_RECORD = """
import json, os, sys
root, other_dir, own_guard = sys.argv[1:4]
out = {}
def attempt(name, action):
    try:
        action()
        out[name] = True
    except OSError as error:
        out[name] = error.errno
attempt("manifest_write", lambda: open(os.path.join(root, "manifest.json"), "w").write("{}"))
attempt("manifest_replace", lambda: os.replace(os.path.join(root, "manifest.json"), os.path.join(root, "m2")))
attempt("new_file", lambda: open(os.path.join(root, "new"), "w").write("x"))
attempt("other_role_rmdir", lambda: os.rmdir(other_dir))
attempt("other_role_unlink", lambda: os.unlink(os.path.join(other_dir, "role.json")))
attempt("other_role_rename", lambda: os.rename(other_dir, other_dir + "-moved"))
attempt("own_guard_truncate", lambda: open(own_guard, "w").write(""))
attempt("own_guard_unlink", lambda: os.unlink(own_guard))
print(json.dumps(out))
"""


def _run_hook(role, tmp_path, tool_input):
    hook = role.settings_extra["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return subprocess.run(
        role.wrap(shlex.split(hook)),
        input=json.dumps({"tool_name": "Bash", "tool_input": tool_input, "hook_event_name": "PreToolUse"}),
        env={**os.environ, **role.env},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )


@needs_netns
def test_a_sealed_role_cannot_change_the_record_and_its_guard_log_still_lands_in_it(tmp_path):
    root = tmp_path / "record"
    _seal_record.register(
        root, "other", "subagent", engine="claude", seal=_seal_role.seal_flags(SPEC, "claude")
    )
    with _seal_role.sealed_role(SPEC, root, "author", "author", "claude", workdir_roots=[tmp_path]) as role:
        paths = _seal_record.role_paths(root, "author")
        done = subprocess.run(
            role.wrap(
                [
                    sys.executable,
                    "-c",
                    _TAMPER_RECORD,
                    str(root),
                    str(_seal_record.role_paths(root, "other").dir),
                    str(paths.guard_log),
                ]
            ),
            env={**os.environ, **role.env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        assert result and set(result.values()) == {errno.EROFS}, result
        # The hook runs inside the seal and still logs: to its own writable dir, not the record.
        hooked = _run_hook(role, tmp_path, {"command": "echo hi"})
        assert hooked.returncode == 0, hooked.stderr
        assert paths.guard_log.read_text() == ""
        role.exit_code = 0
    assert (root / "manifest.json").read_text() != "{}"
    assert _seal_record.role_paths(root, "other").dir.is_dir()

    lines = read_jsonl(paths.guard_log)
    assert [line["tool"] for line in lines] == ["Bash"]
    end = json.loads(paths.end.read_text())
    assert end["files"]["guard.jsonl"] == _seal_record._describe(paths.guard_log)
    assert end["files"]["guard.jsonl"]["lines"] == 1
    verdict = _seal_audit.audit(root, task_project=SPEC.task_project, forbidden=SPEC.forbidden_paths)
    # No engine ran, so "no transcript" is expected; nothing may be said about the guard log or a hash.
    assert [r.file for r in verdict.reasons if r.role_id == "author"] == ["roles/author"]


@needs_netns
def test_a_role_with_no_tool_calls_keeps_the_empty_registered_guard_log(tmp_path):
    root = tmp_path / "record"
    with _seal_role.sealed_role(SPEC, root, "author", "author", "claude", workdir_roots=[tmp_path]):
        pass
    paths = _seal_record.role_paths(root, "author")
    assert paths.guard_log.read_text() == ""
    assert json.loads(paths.end.read_text())["files"]["guard.jsonl"]["lines"] == 0


@needs_netns
def test_a_codex_role_also_has_a_read_only_record(tmp_path):
    root = tmp_path / "record"
    with _seal_role.sealed_role(SPEC, root, "d1", "delegate", "codex", workdir_roots=[tmp_path]) as role:
        done = subprocess.run(
            role.wrap(
                [sys.executable, "-c", "import sys; open(sys.argv[1], 'w')", str(root / "manifest.json")]
            ),
            env={**os.environ, **role.env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode != 0 and "Read-only file system" in done.stderr


def test_the_guard_log_is_synced_into_the_record_while_the_role_runs(tmp_path):
    root = tmp_path / "record"
    _seal_record.register(
        root, "author", "author", engine="claude", seal=_seal_role.seal_flags(SPEC, "claude")
    )
    paths = _seal_record.role_paths(root, "author")
    live = tmp_path / "live.jsonl"
    role = _seal_role.SealedRole("author", [], {}, {}, live_guard_log=live, record_guard_log=paths.guard_log)
    role.sync_guard_log()  # no live log yet: the registered empty one stays
    assert paths.guard_log.read_text() == ""
    live.write_text('{"decision":"deny"}\n')
    _seal_role.close_author_record(root, tmp_path / "no-config", role)
    assert json.loads(paths.end.read_text())["files"]["guard.jsonl"]["lines"] == 1


@pytest.mark.parametrize("plant", ["link", "directory"])
def test_a_guard_log_the_role_replaced_voids_the_record(tmp_path, plant):
    root = tmp_path / "record"
    _seal_record.register(
        root, "author", "author", engine="claude", seal=_seal_role.seal_flags(SPEC, "claude")
    )
    paths = _seal_record.role_paths(root, "author")
    secret, live = tmp_path / "secret", tmp_path / "guard" / "live.jsonl"
    secret.write_text('{"decision":"allow","note":"harness only"}\n')
    live.parent.mkdir()
    if plant == "link":
        live.symlink_to(secret)
    else:
        live.mkdir()
    role = _seal_role.SealedRole("author", [], {}, {}, live_guard_log=live, record_guard_log=paths.guard_log)
    role.sync_guard_log()
    text = paths.guard_log.read_text()
    assert "harness only" not in text and text.startswith("guard log refused")
    _seal_record.finish(root, "author", exit_code=0)
    verdict = _seal_audit.audit(root, task_project=SPEC.task_project, forbidden=SPEC.forbidden_paths)
    assert verdict.verdict == "void"
    assert any(r.kind == "incomplete" and "guard" in r.file for r in verdict.reasons), verdict.reasons


def test_a_workdir_inside_the_record_root_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="inside the record root"):
        _seal_role._check_record_apart(tmp_path / "r", [tmp_path / "r" / "work"])
    _seal_role._check_record_apart(tmp_path / "r", [tmp_path, tmp_path / "other"])


def _writable_of(argv):
    """Return the `--writable` paths and the `--private-tmp` of a sealed argv."""
    pairs = list(zip(argv, argv[1:], strict=False))
    return [v for f, v in pairs if f == "--writable"], [v for f, v in pairs if f == "--private-tmp"]


@pytest.fixture
def throwaway_home():
    """A directory below the real $HOME (so the seal locks it like $HOME) that a regressed lock may write."""
    home = Path.home() / f".seal-test-home-{os.getpid()}"
    (home / ".local").mkdir(parents=True)
    try:
        yield home
    finally:
        shutil.rmtree(home)


@needs_netns
def test_a_role_may_write_only_its_workdir_config_guard_log_tmp_and_caches(tmp_path, throwaway_home):
    root, config, work = tmp_path / "record", tmp_path / "claude", tmp_path / "work"
    work.mkdir()
    with _seal_role.sealed_role(
        SPEC, root, "author", "author", "claude", workdir_roots=[work], config_dir=config
    ) as role:
        writable, tmp = _writable_of(role.wrap(["true"]))
        cache = Path(role.env["UV_CACHE_DIR"]).parent
        assert tmp == ["/tmp"]
        assert len(writable) == 5
        assert writable[:2] == [str(work), str(config)]
        assert writable[3:] == ["/tmp", str(cache)]
        guard_dir = Path(writable[2])
        assert guard_dir.name.startswith("sealg") and guard_dir.parent == Path(tempfile.gettempdir())
        assert str(Path.home()) not in writable and not any(str(root) == w for w in writable)
        assert {role.env[v] for v in ("XDG_CACHE_HOME", "PIP_CACHE_DIR", "npm_config_cache")} <= {
            str(p) for p in cache.iterdir()
        }
        home = throwaway_home
        script = f"""
import errno, os, pathlib, sys
home = pathlib.Path({str(home)!r})
for path in [home / '.gitconfig', home / '.local' / 'seal-escape', home / '.bashrc-seal']:
    try:
        path.write_text('[core]\\n\\tfsmonitor = /tmp/evil\\n')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.ENOENT), (path, exc)
    else:
        sys.exit(f'write escaped: {{path}}')
pathlib.Path({str(work)!r}, 'ok').write_text('ok')
pathlib.Path({str(config)!r}, 'ok').write_text('ok')
pathlib.Path(os.environ['UV_CACHE_DIR'], 'ok').write_text('ok')
pathlib.Path(os.environ['TMPDIR'], 'seal-role-private').write_text('private')
print('contained')
"""
        gitconfig_before = (home / ".gitconfig").read_bytes() if (home / ".gitconfig").exists() else None
        done = subprocess.run(
            role.wrap([sys.executable, "-c", script]),
            env={**os.environ, **role.env},
            capture_output=True,
            text=True,
            timeout=120,
            cwd=work,
        )
        role.exit_code = done.returncode
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "contained"
    assert (work / "ok").read_text() == "ok" and (config / "ok").read_text() == "ok"
    assert not Path("/tmp/seal-role-private").exists()
    assert not (home / ".local" / "seal-escape").exists() and not (home / ".bashrc-seal").exists()
    gitconfig = home / ".gitconfig"
    assert (gitconfig.read_bytes() if gitconfig.exists() else None) == gitconfig_before
    assert not cache.exists()  # the caches go with the role's scratch
