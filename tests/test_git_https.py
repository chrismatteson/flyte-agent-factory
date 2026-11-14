"""A session's HTTPS push: credential file, refresh supervisor, clone through the store, the push guard."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _git, _git_https, _github, _push_guard
from tests.test_push_guard import git

URL = "https://github.com/acme/widgets.git"


def test_write_token_is_0600_and_replaces_atomically(tmp_path):
    store = tmp_path / "creds"
    _git_https.write_token(store, "ghs_one")
    _git_https.write_token(store, "ghs_two")
    assert store.read_text() == "https://x-access-token:ghs_two@github.com\n"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["creds"]  # no partial file left behind


def test_install_points_origin_at_https_and_at_the_store(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    store = tmp_path / "creds"
    _git_https.install(tmp_path, "acme/widgets", store)
    assert git(tmp_path, "remote", "get-url", "origin").stdout.strip() == URL
    assert git(tmp_path, "config", "credential.helper").stdout.strip() == f"store --file={store}"
    assert git(tmp_path, "config", "credential.useHttpPath").stdout.strip() == "false"
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"


def test_git_finds_the_token_in_the_store(tmp_path):
    store = tmp_path / "creds"
    _git_https.write_token(store, "ghs_abc")
    git(tmp_path, "init", "-q")
    _git_https.install(tmp_path, "acme/widgets", store)
    out = subprocess.run(
        ["git", "credential", "fill"],
        cwd=tmp_path,
        input="protocol=https\nhost=github.com\npath=acme/widgets.git\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "username=x-access-token" in out and "password=ghs_abc" in out


class StubApp:
    """Mints `tok1`, `tok2`, ...; the first `fail` mints raise."""

    def __init__(self, fail=0):
        self.fail, self.calls = fail, []

    def installation_token(self, repo, permissions=None, *, fresh=False):
        self.calls.append((repo, dict(permissions), fresh))
        if self.fail:
            self.fail -= 1
            raise _github.GithubError("github POST /x: HTTP 500 tok_secret")
        return f"tok{len(self.calls)}"

    def redact(self, text):
        return text.replace("tok_secret", "***")


async def run_loop(app, store, ticks):
    rows, slept = [], []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) > ticks:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _git_https.refresh_loop(
            app, "acme/widgets", store, _github.CONTENTS_READ, lambda *row: rows.append(row), sleep=sleep
        )
    return rows, slept


async def test_the_loop_remints_every_45_minutes_forcing_a_new_token(tmp_path):
    store, app = tmp_path / "creds", StubApp()
    rows, slept = await run_loop(app, store, 2)
    assert slept[:2] == [45 * 60, 45 * 60]
    assert [fresh for *_, fresh in app.calls] == [True, True]
    assert "tok2" in store.read_text() and rows == []


async def test_a_failed_mint_keeps_the_old_token_and_says_so_without_the_secret(tmp_path):
    store, app = tmp_path / "creds", StubApp(fail=1)
    _git_https.write_token(store, "old")
    rows, slept = await run_loop(app, store, 1)
    assert "old" in store.read_text()
    assert len(rows) == 1 and "keeping the old one" in rows[0][1] and "tok_secret" not in rows[0][1]
    assert slept[0] == 45 * 60


async def test_clone_through_the_store_needs_no_deploy_key(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_exec(*cmd, **kwargs):
        seen.update(cmd=cmd, env=kwargs["env"])

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", None

        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(_git, "install_deploy_key", lambda repo="": pytest.fail("deploy key installed"))
    store = tmp_path / "creds"
    await _git.clone(URL, "main", tmp_path / "dest", store)
    assert seen["env"]["GIT_CONFIG_VALUE_0"] == f"store --file={store}"
    assert URL in seen["cmd"] and not any("x-access-token" in str(a) for a in seen["cmd"])


@pytest.fixture
def https_checkout(tmp_path):
    """A checkout whose `origin` is a local bare repo, guarded as an App-token push would be."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(bare), str(checkout))
    git(checkout, "checkout", "-q", "-b", "main")
    (checkout / "a.txt").write_text("a\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "a")
    return checkout, bare


def refuse_message(checkout: Path) -> str:
    return git(checkout, "push", "origin", "main", check=False).stderr


def test_a_push_touching_workflows_is_refused_without_the_setting(https_checkout):
    checkout, bare = https_checkout
    _push_guard.install(checkout, str(bare), workflows=False)
    workflow = checkout / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: push\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "ci")
    err = refuse_message(checkout)
    assert "push changes .github/workflows/ but the App token lacks the workflows permission" in err
    assert "set [github] workflows = true" in err and "docs/github-app.md#workflows" in err
    assert git(bare, "branch", "--list").stdout == ""


def test_a_push_without_workflow_changes_goes_through_the_app_guard(https_checkout):
    checkout, bare = https_checkout
    _push_guard.install(checkout, str(bare), workflows=False)
    git(checkout, "push", "-q", "origin", "main")
    assert git(bare, "branch", "--format=%(refname:short)").stdout.split() == ["main"]


def test_workflows_are_pushable_when_the_setting_is_on(https_checkout):
    checkout, bare = https_checkout
    _push_guard.install(checkout, str(bare), workflows=None)
    (checkout / ".github" / "workflows").mkdir(parents=True)
    (checkout / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "ci")
    git(checkout, "push", "-q", "origin", "main")


def test_the_deploy_key_hook_is_unchanged(https_checkout):
    checkout, bare = https_checkout
    _push_guard.install(checkout, str(bare))
    hook = Path(git(checkout, "rev-parse", "--git-path", "hooks/pre-push").stdout.strip())
    hook = hook if hook.is_absolute() else checkout / hook
    assert _push_guard._NO_WORKFLOWS_ENV not in hook.read_text()
