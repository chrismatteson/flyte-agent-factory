"""After the session's first push, `@{u}` must resolve in the clone (the DONE preflight needs it)."""

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from flyte_agent_factory import (
    _git_https,
    _pull_requests,
    _seal_net,
    _seal_proxy,
    _seal_role,
    _seal_session,
    _session_config,
    _session_setup,
    _supervise_support,
    _timeline,
)
from tests._seal_bench import git
from tests._seal_fakes import SPEC
from tests.test_seal_proxy import _GitCGI, _serve
from tests.test_seal_session import _clone

pytest_plugins = ["tests._seal_bench"]

needs_netns = pytest.mark.usefixtures("seal_namespaces")
needs_backend = pytest.mark.usefixtures("git_http_backend")


def _refs(workspace):
    return git(workspace, "for-each-ref", "--format=%(refname)").splitlines()


def test_claim_branch_creates_the_project_branch_at_the_default_tip_and_is_idempotent(bench, tmp_path, store):
    bare, _ = bench
    workspace = _clone(bare, tmp_path, store)
    assert git(bare, "rev-parse", "--verify", "--quiet", "factory/x", check=False).returncode != 0
    assert _seal_session.claim_branch(workspace, "x") is True
    assert git(bare, "rev-parse", "factory/x") == git(bare, "rev-parse", "main")
    assert _seal_session.claim_branch(workspace, "x") is True  # already there: nothing to do
    _seal_session.limit_fetch(workspace, "x")
    git(workspace, "fetch", "--prune", "origin")
    refs = _refs(workspace)
    assert "refs/remotes/origin/factory/x" in refs
    assert not any("other" in ref for ref in refs)  # the other project's branch is still not fetched


def test_claim_branch_reports_a_remote_it_cannot_reach(bench, tmp_path, store):
    workspace = _clone(bench[0], tmp_path, store)
    git(workspace, "remote", "set-url", "origin", (tmp_path / "gone.git").as_uri())
    assert _seal_session.claim_branch(workspace, "x") is False


def test_the_harness_fetch_works_before_and_after_the_branch_exists_and_brings_no_other_branch(
    bench, tmp_path, store
):
    bare, foreign = bench
    workspace = _clone(bare, tmp_path, store)
    git(workspace, "fetch", "--prune", "origin")  # before: no branch, no refspec for it
    git(bare, "branch", "factory/x", "main")
    _seal_session.refresh_upstream(workspace, "x")
    git(workspace, "fetch", "--prune", "origin")  # after
    refs = _refs(workspace)
    assert "refs/remotes/origin/factory/x" in refs
    assert not any("other" in ref for ref in refs)
    assert git(workspace, "cat-file", "-e", foreign, check=False).returncode != 0


def _push_through_the_route(workspace, bare, tmp_path, store):
    """Push `HEAD` as the session does (`git push -u origin ...`), through the real proxy and netns."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "ws.git").symlink_to(bare)  # the push lands in the repo that origin fetches from
    git(bare, "config", "http.receivepack", "true")
    server = _serve(_GitCGI)
    server.root = str(host)
    sock = Path("/tmp") / f"seal-upstream-{os.getpid()}.sock"
    try:
        with _seal_proxy.run_in_thread(
            SPEC, sock, tmp_path / "proxy.jsonl", push_remote=f"{server.url}/ws.git",
            port=_seal_role.PROXY_PORT, push_auth=_git_https.push_auth(store),
            allow_http_upstreams_for_tests=True,
        ):  # fmt: skip
            argv = _seal_net.launcher_argv(str(sock), _seal_role.PROXY_PORT, None, None, ())
            return subprocess.run(
                [*argv, "sh", "-c", "git push -u origin HEAD:refs/heads/factory/x "
                 "; git rev-list --count @{u}..HEAD"],
                cwd=workspace,
                env={**os.environ, "PYTHONPATH": str(Path(_seal_net.__file__).parent.parent),
                     "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
    finally:
        server.shutdown()
        server.server_close()
        sock.unlink(missing_ok=True)


def _session_in(workspace, monkeypatch):
    """The harness's setup for the project branch, as `_checkout_project_branch` does it."""
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "_project_name", "x")
    monkeypatch.setattr(_session_config, "_seal", SPEC)
    asyncio.run(_session_setup._checkout_project_branch("factory/x", _timeline._Timeline()))
    (workspace / "work.txt").write_text("done")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "work")


def _preflight(workspace, monkeypatch):
    pull = _pull_requests.PullRequests(cast(Any, None), "acme/bench", "x", "", lambda *_: None)
    monkeypatch.setattr(_session_setup, "_pr", pull)
    return asyncio.run(_supervise_support._done_preflight(workspace))


@needs_netns
@needs_backend
def test_a_claimed_branch_resolves_the_upstream_straight_after_the_first_push(
    bench, tmp_path, store, monkeypatch
):
    bare, _ = bench
    workspace = _clone(bare, tmp_path, store)
    assert _seal_session.claim_branch(workspace, "x")
    _seal_session.limit_fetch(workspace, "x")
    _seal_session.route_pushes(workspace, bare.as_uri())
    _session_in(workspace, monkeypatch)
    done = _push_through_the_route(workspace, bare, tmp_path, store)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "0"  # inside the session: nothing ahead of @{u}
    assert "no upstream" not in _preflight(workspace, monkeypatch)
    assert "not pushed" not in _preflight(workspace, monkeypatch)
    git(workspace, "fetch", "--prune", "origin")
    assert not any("other" in ref for ref in _refs(workspace))


@needs_netns
@needs_backend
def test_when_the_claim_failed_the_refresh_before_the_preflight_resolves_the_upstream(
    bench, tmp_path, store, monkeypatch
):
    bare, _ = bench
    workspace = _clone(bare, tmp_path, store)  # no claim: the branch is created by the session's push
    _seal_session.route_pushes(workspace, bare.as_uri())
    _session_in(workspace, monkeypatch)
    done = _push_through_the_route(workspace, bare, tmp_path, store)
    assert git(bare, "rev-parse", "factory/x") == git(workspace, "rev-parse", "HEAD"), done.stderr
    assert done.returncode != 0  # the push worked; `@{u}` does not resolve yet
    assert git(workspace, "rev-list", "--count", "@{u}..HEAD", check=False).returncode != 0
    problem = _preflight(workspace, monkeypatch)  # the harness refreshes before it judges
    assert "no upstream" not in problem and "not pushed" not in problem
    assert git(workspace, "rev-list", "--count", "@{u}..HEAD") == "0"
