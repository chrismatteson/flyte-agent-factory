"""Session push by App token is on only behind `AGENT_FACTORY_GITHUB_PUSH=app`; otherwise nothing changes."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import cast

import pytest

from flyte_agent_factory import _bootstrap_github, _github, _session_config, _session_setup, _timeline


class StubApp:
    minted: list[tuple[str, dict[str, str]]] = []

    def __init__(self, app_id, key):
        self.fail = key == "bad"

    def installation_token(self, repo, permissions=None, *, fresh=False):
        if self.fail:
            raise _github.GithubError("github: not installed on acme/widgets", 404)
        StubApp.minted.append((repo, dict(permissions)))
        return "ghs_pushtoken"

    def redact(self, text):
        return text


@pytest.fixture
def armed(monkeypatch, tmp_path):
    StubApp.minted = []
    github = SimpleNamespace(configured=True, app_id="1", private_key_secret="k", push="app", workflows=False)
    monkeypatch.setattr(_session_config, "_PROFILE", SimpleNamespace(github=github, repo=""))
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_github, "GithubApp", StubApp)
    monkeypatch.setattr(_session_setup, "_token_refresh", None)
    monkeypatch.setenv("AGENT_FACTORY_GITHUB_PUSH", "app")
    return github


async def test_app_push_mints_a_repo_scoped_contents_write_token(armed, tmp_path):
    slug = await _session_setup._arm_https_push(
        "pem", "git@github.com:acme/widgets.git", _timeline._Timeline()
    )
    task = _session_setup._token_refresh
    assert task is not None
    task.cancel()
    assert slug == "acme/widgets"
    assert StubApp.minted == [("acme/widgets", {"contents": "write"})]
    store = tmp_path / "github-push-credentials"
    assert store.read_text() == "https://x-access-token:ghs_pushtoken@github.com\n"


async def test_the_workflows_setting_adds_the_workflows_permission(armed):
    armed.workflows = True
    await _session_setup._arm_https_push("pem", "git@github.com:acme/widgets.git", _timeline._Timeline())
    assert StubApp.minted[0][1] == {"contents": "write", "workflows": "write"}
    assert _session_setup._token_refresh is not None
    _session_setup._token_refresh.cancel()


@pytest.mark.parametrize("env", ["", "deploy_key", "yes"])
async def test_any_other_value_keeps_the_deploy_key_untouched(armed, monkeypatch, env):
    monkeypatch.setenv("AGENT_FACTORY_GITHUB_PUSH", env)
    slug = await _session_setup._arm_https_push(
        "pem", "git@github.com:acme/widgets.git", _timeline._Timeline()
    )
    assert slug == "" and StubApp.minted == [] and _session_setup._token_refresh is None


async def test_no_key_or_no_app_keeps_the_deploy_key(armed):
    assert (
        await _session_setup._arm_https_push("", "git@github.com:acme/widgets.git", _timeline._Timeline())
        == ""
    )
    armed.configured = False
    assert (
        await _session_setup._arm_https_push("pem", "git@github.com:acme/widgets.git", _timeline._Timeline())
        == ""
    )


async def test_a_failed_first_mint_falls_back_to_the_deploy_key_with_a_row(armed, capsys):
    slug = await _session_setup._arm_https_push(
        "bad", "git@github.com:acme/widgets.git", _timeline._Timeline()
    )
    assert slug == "" and _session_setup._token_refresh is None
    assert "using the deploy key" in capsys.readouterr().err


def test_the_manifest_asks_for_actions_read_and_for_write_only_when_pushing_by_app():
    def permissions(**kw: bool) -> dict[str, str]:
        return cast(
            dict[str, str],
            _bootstrap_github.build_manifest("acme", "w", "http://x", **kw)["default_permissions"],
        )

    default = permissions()
    assert default["actions"] == "read" and default["contents"] == "read" and "workflows" not in default
    app = permissions(push_app=True)
    assert app["contents"] == "write" and "workflows" not in app
    assert permissions(push_app=True, workflows=True)["workflows"] == "write"


# --- Reading a private repo by App token, in the default deploy-key push mode (#179) ---------------------


@pytest.fixture
def reading(monkeypatch, tmp_path):
    """A session in deploy-key mode with the App configured, cloning through a fake `git clone`."""
    StubApp.minted = []
    github = SimpleNamespace(
        configured=True, app_id="1", private_key_secret="k", push="deploy_key", workflows=False
    )
    profile = SimpleNamespace(
        github=github,
        repo="git@github.com:acme/agent-workspace.git",
        repo_key_vars={"git@github.com:acme/keyed-lib.git": "GIT_DEPLOY_KEY_KEYED_LIB"},
        canonical_repo=lambda url: url,
    )
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "workspace")
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(_github, "GithubApp", StubApp)
    monkeypatch.setattr(_session_setup, "_token_refresh", None)
    monkeypatch.delenv("AGENT_FACTORY_GITHUB_PUSH", raising=False)
    clones: list[tuple[str, object]] = []

    async def fake_clone(repo, branch, dest, credential=None):
        clones.append((repo, credential))
        subprocess.run(["git", "init", "-q", str(dest)], check=True)

    monkeypatch.setattr(_session_setup, "_clone", fake_clone)
    yield SimpleNamespace(github=github, clones=clones, store=tmp_path / "config" / "github-read-credentials")
    if _session_setup._token_refresh is not None:
        _session_setup._token_refresh.cancel()


async def _prepare(repo: str, app_key: str = "pem") -> None:
    await _session_setup._prepare_workspace(
        brief="b", repo=repo, branch="", resuming=False, timeline=_timeline._Timeline(), app_key=app_key
    )


async def test_a_private_repo_without_a_deploy_key_is_cloned_with_a_contents_read_token(reading):
    await _prepare("git@github.com:acme/private-lib.git")
    assert StubApp.minted == [("acme/private-lib", {"contents": "read"})]
    assert reading.clones == [("https://github.com/acme/private-lib.git", reading.store)]
    assert reading.store.read_text() == "https://x-access-token:ghs_pushtoken@github.com\n"
    assert oct(reading.store.stat().st_mode & 0o777) == "0o600"
    origin = subprocess.run(
        ["git", "-C", str(_session_config.WORKSPACE), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert origin == "https://github.com/acme/private-lib.git", "later fetches use the same token"
    assert _session_setup._token_refresh is not None, "the read token is re-minted like the push token"


@pytest.mark.parametrize(
    "repo", ["git@github.com:acme/agent-workspace.git", "git@github.com:acme/keyed-lib.git"]
)
async def test_a_repo_a_deploy_key_reaches_keeps_the_deploy_key(reading, repo):
    await _prepare(repo)
    assert StubApp.minted == [] and reading.clones == [(repo, None)]


async def test_a_repo_the_app_is_not_installed_on_falls_back_to_the_deploy_key(reading, capsys):
    await _prepare("git@github.com:acme/private-lib.git", app_key="bad")
    assert reading.clones == [("git@github.com:acme/private-lib.git", None)]
    assert "cloning with the deploy key" in capsys.readouterr().err


async def test_without_the_app_nothing_is_minted(reading):
    reading.github.configured = False
    await _prepare("git@github.com:acme/private-lib.git")
    assert StubApp.minted == [] and reading.clones == [("git@github.com:acme/private-lib.git", None)]


def test_the_session_hands_the_app_key_to_the_workspace_setup():
    """Without it neither the push nor the read token is ever minted in a real session."""
    import ast
    from pathlib import Path

    source = (Path(_session_setup.__file__).parent / "agent.py").read_text()
    [call] = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_session_setup._prepare_workspace"
    ]
    assert {kw.arg: ast.unparse(kw.value) for kw in call.keywords}["app_key"] == "app_key"
