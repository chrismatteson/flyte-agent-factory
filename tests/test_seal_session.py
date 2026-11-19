"""A sealed author session: the workspace clone, the task snapshot, the push route and the author's role."""

import asyncio
import base64
import dataclasses
import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import flyte.errors
import pytest

from flyte_agent_factory import (
    _author,
    _delegation,
    _git,
    _git_https,
    _github,
    _seal_guard,
    _seal_net,
    _seal_proxy,
    _seal_record,
    _seal_role,
    _seal_session,
    _session_config,
    _session_setup,
    _supervise,
    _timeline,
)
from tests._seal_bench import git
from tests._seal_fakes import SPEC
from tests.test_seal_proxy import _GitCGI, _serve

pytest_plugins = ["tests._seal_bench"]

needs_netns = pytest.mark.usefixtures("seal_namespaces")


def _clone(bench_repo, tmp_path, store, project="x"):
    dest = tmp_path / "workspace"
    asyncio.run(_git.clone(bench_repo.as_uri(), "", dest, store, single_branch=True))
    _git_https.install(
        dest, "unused/slug", store
    )  # the harness's order: origin and credentials, then the limit
    git(dest, "remote", "set-url", "origin", bench_repo.as_uri())
    _seal_session.limit_fetch(dest, project)
    return dest


def test_push_auth_reads_the_current_token_on_every_call(store):
    header = _git_https.push_auth(store)
    assert header() == "Basic " + base64.b64encode(b"x-access-token:ghs_firsttoken").decode()
    _git_https.write_token(store, "ghs_second")
    assert header() == "Basic " + base64.b64encode(b"x-access-token:ghs_second").decode()


def test_the_sealed_clone_never_sees_another_projects_branch(bench, tmp_path, store):
    bare, foreign = bench
    workspace = _clone(bare, tmp_path, store)
    # No project branch on the remote yet: the fetch must still work.
    assert git(workspace, "config", "--get-all", "remote.origin.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/origin/main"
    ]
    git(workspace, "fetch", "--prune", "origin")
    git(bare, "branch", "factory/x", "main")
    _seal_session.limit_fetch(workspace, "x")
    assert git(workspace, "config", "--get-all", "remote.origin.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/origin/main",
        "+refs/heads/factory/x:refs/remotes/origin/factory/x",
    ]
    # What the harness does in `_checkout_project_branch`.
    git(workspace, "fetch", "--prune", "origin")
    refs = git(workspace, "for-each-ref", "--format=%(refname)").splitlines()
    assert not any("other" in ref for ref in refs)
    assert "refs/remotes/origin/factory/x" in refs
    assert git(workspace, "cat-file", "-e", foreign, check=False).returncode != 0


def test_a_delegate_workers_tarball_of_a_sealed_clone_has_no_foreign_refs_or_objects(bench, tmp_path, store):
    bare, foreign = bench
    workspace = _clone(bare, tmp_path, store)
    git(workspace, "fetch", "--prune", "origin")
    target = tmp_path / "worker"
    target.mkdir()
    _delegation.snapshot(workspace, target)
    with tarfile.open(target / "repo.tar.gz") as archive:
        archive.extractall(tmp_path / "unpacked", filter="data")
    copied = tmp_path / "unpacked" / "repo"
    assert not any("other" in ref for ref in git(copied, "for-each-ref", "--format=%(refname)").splitlines())
    assert git(copied, "cat-file", "-e", foreign, check=False).returncode != 0
    assert not any(
        "OTHER-ARMS-WORK" in p.read_text(errors="ignore") for p in (copied / ".git").rglob("*") if p.is_file()
    )


def test_pushes_are_rewritten_to_the_proxy_route_and_fetches_are_not(bench, tmp_path, store):
    bare, _ = bench
    workspace = _clone(bare, tmp_path, store)
    _seal_session.route_pushes(workspace, bare.as_uri())
    assert git(workspace, "remote", "get-url", "--push", "origin") == _seal_role.PUSH_ROUTE
    assert git(workspace, "remote", "get-url", "origin") == bare.as_uri()


# Shaped like an AWS secret key assignment (`_bridge.find_credential_shape`); not a real credential.
_AWS_SHAPED = "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _commit_on_branch(workspace, name, text):
    git(workspace, "checkout", "-qb", "factory/x")
    (workspace / name).write_text(text)
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "work")


def _run_hook(workspace, url):
    """Run the pre-push hook as git does (git runs it only once connected to the remote, as these cannot)."""
    sha = git(workspace, "rev-parse", "HEAD")
    line = f"refs/heads/factory/x {sha} refs/heads/factory/x {'0' * 40}\n"
    return subprocess.run(
        [str(workspace / ".git" / "hooks" / "pre-push"), "origin", url],
        input=line, cwd=workspace, capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_a_sealed_clone_refuses_to_push_a_commit_holding_a_credential(bench, tmp_path, store):
    workspace = _clone(bench[0], tmp_path, store)
    _seal_session.route_pushes(workspace, bench[0].as_uri())
    _commit_on_branch(workspace, "creds.txt", _AWS_SHAPED + "\n")
    done = _run_hook(workspace, _seal_role.PUSH_ROUTE)
    assert done.returncode != 0
    assert "push refused" in done.stderr and "credential" in done.stderr
    assert "wJalrXUtnFEMI" not in done.stderr


def test_a_clean_push_to_the_route_passes_the_hook_and_another_url_does_not(bench, tmp_path, store):
    workspace = _clone(bench[0], tmp_path, store)
    _seal_session.route_pushes(workspace, bench[0].as_uri())
    _commit_on_branch(workspace, "work.txt", "done\n")
    assert _run_hook(workspace, _seal_role.PUSH_ROUTE).returncode == 0
    other = _run_hook(workspace, "https://github.com/example/elsewhere.git")
    assert other.returncode != 0 and "not the workspace repository" in other.stderr


def test_the_sealed_hook_keeps_the_proxy_route_and_the_real_fetch_url(bench, tmp_path, store):
    workspace = _clone(bench[0], tmp_path, store)
    _seal_session.route_pushes(workspace, bench[0].as_uri())
    # `_push_guard`'s own rewrites would send the route's http:// URL to a directory that does not exist.
    assert git(workspace, "remote", "get-url", "--push", "origin") == _seal_role.PUSH_ROUTE
    assert git(workspace, "remote", "get-url", "origin") == bench[0].as_uri()
    assert git(workspace, "config", "--get-regexp", r"pushinsteadof").count("\n") == 0
    hook = workspace / ".git" / "hooks" / "pre-push"
    assert hook.is_file() and _seal_role.PUSH_ROUTE in hook.read_text()


def test_the_task_repo_is_placed_in_the_project_folder_and_kept_out_of_git(
    tmp_path, bench, store, monkeypatch
):
    task = tmp_path / "task"
    task.mkdir()
    git(task, "init", "-q", "-b", "main")
    (task / "mod.py").write_text("x = 1\n")
    git(task, "add", ".")
    git(task, "commit", "-qm", "base")
    spec = dataclasses.replace(SPEC, task_repo=str(task), base_sha=git(task, "rev-parse", "HEAD"))
    workspace = _clone(bench[0], tmp_path, store)
    placed = _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")
    assert placed == workspace / "projects" / "x" / "repo"
    assert (placed / "mod.py").read_text() == "x = 1\n"
    assert git(workspace, "status", "--porcelain") == ""
    # Existing: left alone (a resume), and the exclude entry is not duplicated.
    (placed / "mod.py").write_text("x = 2\n")
    _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")
    assert (placed / "mod.py").read_text() == "x = 2\n"
    exclude = (workspace / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/projects/x/repo/") == 1
    # Gone (the checkpoint leaves ignored files out): placed again.
    subprocess.run(["rm", "-rf", str(placed)], check=True)
    _seal_session.place_task_repo(workspace, spec, "x", "workspace", "")
    assert (placed / "mod.py").read_text() == "x = 1\n"


def test_a_workspace_repo_without_projects_is_refused(tmp_path, bench, store):
    workspace = _clone(bench[0], tmp_path, store)
    with pytest.raises(flyte.errors.NonRecoverableError, match="must be a workspace"):
        _seal_session.place_task_repo(workspace, SPEC, "x", "root", "")


def test_a_sealed_project_without_an_app_push_token_is_refused():
    with pytest.raises(flyte.errors.NonRecoverableError, match="GitHub App token"):
        _seal_session.require_https_push("")
    _seal_session.require_https_push("example/bench-ws")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "SEAL_RECORD", tmp_path / "record")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "workspace")
    yield
    _seal_session.end()


def _current_seal():
    # Read through a call: mypy would otherwise keep the narrowing from an earlier assert across `end()`.
    return _session_config._seal


@needs_netns
def test_begin_registers_the_author_and_end_finishes_it(state, store):
    role = _seal_session.begin(SPEC.to_json(), "claude", store)
    assert _seal_session.role() is role
    assert _current_seal() == SPEC
    settings = _seal_session.settings_extra({"permissions": {}})
    assert settings is not None and "hooks" in settings
    paths = _seal_record.role_paths(_session_config.SEAL_RECORD, "author")
    assert (paths.dir / "role.json").is_file()
    _seal_session.close_record()  # the DONE hook: end.json exists while the session still runs
    assert paths.end.is_file()
    _seal_session.end()
    assert _seal_session.role() is None and _current_seal() is None
    _seal_session.end()  # idempotent


def test_begin_refuses_when_the_pod_cannot_seal(state, store, monkeypatch):
    monkeypatch.setattr(_seal_net, "unavailable_reason", lambda: "unshare: [Errno 1] Operation not permitted")
    with pytest.raises(flyte.errors.NonRecoverableError, match="namespaces"):
        _seal_session.begin(SPEC.to_json(), "claude", store)
    assert _session_config._seal is None


def test_unsealed_settings_pass_through_unchanged():
    assert _seal_session.settings_extra(None) is None
    base = {"a": 1}
    assert _seal_session.settings_extra(base) is base


@needs_netns
@pytest.mark.usefixtures("git_http_backend")
def test_a_push_from_the_sealed_clone_reaches_the_push_route_with_no_credential_in_the_clone(
    bench, tmp_path, store
):
    bare, _ = bench
    workspace = _clone(bare, tmp_path, store)
    _seal_session.route_pushes(workspace, bare.as_uri())
    # A clone that holds no credential: the proxy adds the token.
    git(workspace, "config", "--unset-all", "credential.helper", check=False)
    git(workspace, "checkout", "-qb", "factory/x")
    (workspace / "work.txt").write_text("done")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "work")
    host = tmp_path / "host"
    host.mkdir()
    upstream = host / "ws.git"
    git(tmp_path, "clone", "-q", "--bare", str(bare), str(upstream))
    git(upstream, "config", "http.receivepack", "true")
    server = _serve(_GitCGI)
    server.root = str(host)
    sock = Path("/tmp") / f"seal-test-{os.getpid()}.sock"
    log = tmp_path / "proxy.jsonl"
    try:
        with _seal_proxy.run_in_thread(
            SPEC, sock, log, push_remote=f"{server.url}/ws.git", port=_seal_role.PROXY_PORT,
            push_auth=_git_https.push_auth(store), allow_http_upstreams_for_tests=True,
        ):  # fmt: skip
            argv = _seal_net.launcher_argv(str(sock), _seal_role.PROXY_PORT, None, None, ())
            done = subprocess.run(
                [*argv, "git", "-C", str(workspace), "push", "-u", "origin", "HEAD:refs/heads/factory/x"],
                env={**os.environ, "PYTHONPATH": str(Path(_seal_net.__file__).parent.parent),
                     "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
    finally:
        server.shutdown()
        server.server_close()
        sock.unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    assert git(upstream, "rev-parse", "factory/x") == git(workspace, "rev-parse", "HEAD")
    assert "ghs_firsttoken" not in log.read_text()
    assert "ghs_firsttoken" not in (workspace / ".git" / "config").read_text()


# --- the author's launch ------------------------------------------------------------------------ #


def _role(prefix=("launcher", "--sock")):
    return _seal_role.SealedRole(
        role_id="author", argv_prefix=list(prefix), env={"PYTHONPATH": "/pkg"}, settings_extra={}
    )


def test_every_claude_candidate_starts_through_the_launcher(monkeypatch, tmp_path):
    started = []

    class Recorder:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs["env"]))

        async def start(self):
            pass

    async def no_check(proc):
        pass

    monkeypatch.setattr(_author, "ClaudeProcess", Recorder)
    monkeypatch.setattr(_author, "_check_headless_start", no_check)
    monkeypatch.setattr(_seal_session, "_role", _role())
    candidates = [["claude", "--continue"], ["claude"]]
    asyncio.run(_author._start_session(candidates, env={"A": "1"}, timeline=_timeline._Timeline()))
    deny = _seal_guard.cli_deny_args()  # every sealed claude also loses the web tools on its command line
    assert [argv for argv, _ in started] == [["launcher", "--sock", *c, *deny] for c in candidates][
        : len(started)
    ]
    assert all(env == {"A": "1", "PYTHONPATH": "/pkg"} for _, env in started)


def test_an_unsealed_claude_start_is_unchanged(monkeypatch):
    started = []

    class Recorder:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs["env"]))

        async def start(self):
            pass

    monkeypatch.setattr(_author, "ClaudeProcess", Recorder)
    monkeypatch.setattr(_seal_session, "_role", None)
    asyncio.run(_author._start_session([["claude"]], env={"A": "1"}, timeline=_timeline._Timeline()))
    assert started == [(["claude"], {"A": "1"})]


@pytest.mark.parametrize("sealed", [True, False])
def test_the_codex_author_is_launched_sealed_with_web_search_off(monkeypatch, tmp_path, sealed):
    seen = {}

    class Recorder:
        thread_id = ""

        def __init__(self, **kwargs):
            seen.update(kwargs)

        @staticmethod
        def saved_thread(path):
            return None

    monkeypatch.setattr(_author, "CodexAuthor", Recorder)
    monkeypatch.setattr(_author, "_prepare_codex_home", lambda: tmp_path)
    monkeypatch.setattr(_session_setup, "_session_env", lambda env: {})
    monkeypatch.setattr(_session_setup, "_export_runtime_env", lambda: {})
    monkeypatch.setattr(_seal_session, "_role", _role() if sealed else None)
    _author._start_codex_author(_timeline._Timeline(), model="", effort="")
    assert (list(seen["command"])[:2] == ["launcher", "--sock"]) is sealed
    assert (list(_seal_role.CODEX_TOOLS_OFF_ARGS) == seen["args"][-2:]) is sealed
    assert ("PYTHONPATH" in seen["env"]) is sealed


# --- `_prepare_workspace` for a sealed project -------------------------------------------------- #


class _StubApp:
    def __init__(self, app_id, key):
        pass

    def installation_token(self, repo, permissions=None, *, fresh=False):
        return "ghs_pushtoken"

    def redact(self, text):
        return text


@pytest.fixture
def armed(monkeypatch, tmp_path, bench):
    github = SimpleNamespace(configured=True, app_id="1", private_key_secret="k", push="app", workflows=False)
    monkeypatch.setattr(
        _session_config, "_PROFILE", SimpleNamespace(github=github, repo="git@github.com:acme/ws.git")
    )
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(_session_config, "_project_name", "x")
    monkeypatch.setattr(_session_config, "_project_folder", "")
    monkeypatch.setattr(_session_config, "_project_repo", "https://github.com/acme/bench.git")
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(_github, "GithubApp", _StubApp)
    monkeypatch.setattr(_session_setup, "_token_refresh", None)
    monkeypatch.setenv("AGENT_FACTORY_GITHUB_PUSH", "app")
    # The real URL is github.com's; the clone and the fetches here go to the local bench repo.
    monkeypatch.setattr(_git_https, "remote_url", lambda slug: bench[0].as_uri())
    task = tmp_path / "task"
    task.mkdir()
    git(task, "init", "-q", "-b", "main")
    (task / "mod.py").write_text("x = 1\n")
    git(task, "add", ".")
    git(task, "commit", "-qm", "base")
    spec = dataclasses.replace(SPEC, task_repo=str(task), base_sha=git(task, "rev-parse", "HEAD"))
    monkeypatch.setattr(_session_config, "_seal", spec)
    yield spec
    if _session_setup._token_refresh is not None:
        _session_setup._token_refresh.cancel()


async def _prepare(app_key="pem", resuming=False):
    await _session_setup._prepare_workspace(
        brief="b", repo="https://github.com/acme/bench.git", branch="", resuming=resuming,
        timeline=_timeline._Timeline(), app_key=app_key,
    )  # fmt: skip


async def test_a_sealed_workspace_has_one_branch_a_push_route_and_the_task_repo(armed, bench):
    await _prepare()
    workspace = _session_config.WORKSPACE
    git(workspace, "fetch", "--prune", "origin")
    refs = git(workspace, "for-each-ref", "--format=%(refname)").splitlines()
    assert not any("other" in ref for ref in refs)
    assert git(workspace, "remote", "get-url", "--push", "origin") == _seal_role.PUSH_ROUTE
    assert (workspace / "projects" / "x" / "repo" / "mod.py").is_file()
    assert "projects/x" not in git(workspace, "status", "--porcelain")  # the task repo is not the clone's
    assert git(workspace, "cat-file", "-e", bench[1], check=False).returncode != 0


async def test_a_sealed_workspace_is_refused_without_a_push_token(armed, monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_GITHUB_PUSH")
    with pytest.raises(flyte.errors.NonRecoverableError, match="GitHub App token"):
        await _prepare()
    assert not (_session_config.WORKSPACE / ".git").exists()


# --- the DONE path ------------------------------------------------------------------------------ #


def test_the_done_preflight_tells_a_sealed_session_to_just_push(tmp_path, bench, store, monkeypatch):
    from typing import Any, cast

    from flyte_agent_factory import _pull_requests, _supervise_support

    workspace = _clone(bench[0], tmp_path, store)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "_project_name", "x")
    monkeypatch.setattr(_session_config, "_seal", SPEC)
    asyncio.run(_session_setup._checkout_project_branch("factory/x", _timeline._Timeline()))
    pull = _pull_requests.PullRequests(cast(Any, None), "acme/bench", "x", "", lambda *_: None)
    monkeypatch.setattr(_session_setup, "_pr", pull)
    problem = asyncio.run(_supervise_support._done_preflight(workspace))
    assert "git push -u origin HEAD:refs/heads/factory/x" in problem
    assert "fetch" not in problem and "merge" not in problem


def test_the_done_path_closes_the_author_record_before_review():
    source = (Path(_supervise.__file__)).read_text()
    assert source.index("_seal_session.close_record") < source.index("await _pre_review.begin(")


def _corpus_case(name):
    from tests import seal_corpus

    return next(c for c in seal_corpus.CASES if c.name == name), seal_corpus


@pytest.mark.parametrize(
    ("name", "verdict"),
    [("clean-subagent-and-engine-subagent", "clean"), ("void-route-git-fetch-upstream", "void")],
)
def test_the_done_path_audits_the_record_and_keeps_the_verdict_beside_it(
    tmp_path, monkeypatch, name, verdict
):
    case_, corpus = _corpus_case(name)
    record = tmp_path / "state" / "seal-record"
    corpus.build_case(case_, record)
    monkeypatch.setattr(_session_config, "SEAL_RECORD", record)
    spec = dataclasses.replace(SPEC, task_project=corpus.TASK_PROJECT, forbidden_paths=corpus.FORBIDDEN)
    monkeypatch.setattr(_session_config, "_seal", spec)

    line = _seal_session.audit_record()

    kept = json.loads((record.parent / _seal_session.AUDIT_AT_DONE).read_text())
    assert kept["verdict"] == verdict
    assert kept["gold"] is False  # a run never holds the gold: `reached` is the operator's audit
    if verdict == "clean":
        assert line.startswith("seal audit at DONE (no gold): clean")
    else:
        first = kept["reasons"][0]
        assert line.startswith("seal audit at DONE (no gold): VOID")
        assert f"{first['kind']} {first['role_id']} {first['file']}:{first['line']}" in line


def test_an_unsealed_project_is_not_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "SEAL_RECORD", tmp_path / "seal-record")
    monkeypatch.setattr(_session_config, "_seal", None)
    assert _seal_session.audit_record() == ""
    assert not (tmp_path / _seal_session.AUDIT_AT_DONE).exists()
