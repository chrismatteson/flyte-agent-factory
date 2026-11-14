"""Union identity: the pod's secrets and environment, establishing who it is, and the token command."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from flyte_agent_factory import _profile, _union_identity, _union_token
from tests.conftest import TEST_PROFILE

SRC = str(Path(__file__).resolve().parents[1] / "src")
SECRET_VALUE = "very-secret-client-value"
KEY = base64.b64encode(f"union.example.org:acme-client-1:{SECRET_VALUE}:".encode()).decode()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _union_identity.current.cache_clear()
    for var in (
        _union_identity.KEY_ENV,
        _union_identity.SESSION_KEY_ENV,
        _union_identity.TIER_ENV,
        _union_identity.SECRET_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    _union_identity.current.cache_clear()


def _profile_with(extra: str = "") -> _profile.Profile:
    text = TEST_PROFILE.read_text().replace(
        'deployer_key = "factory-deployer-key"\n', 'deployer_key = "factory-deployer-key"\n' + extra, 1
    )
    return _profile.parse(text)


def _pod(monkeypatch, tier="project", secret="factory-project-key", key=KEY):
    monkeypatch.setenv(_union_identity.KEY_ENV, key)
    monkeypatch.setenv(_union_identity.TIER_ENV, tier)
    monkeypatch.setenv(_union_identity.SECRET_ENV, secret)


# --- pod_spec ---------------------------------------------------------------------------------------


def test_pod_spec_mounts_the_tier_key_and_sets_the_external_command():
    secrets, env = _union_identity.pod_spec(_profile_with(), "deployer")
    assert [(s.key, s.as_env_var) for s in secrets] == [("factory-deployer-key", "AGENT_FACTORY_UNION_KEY")]
    assert env["FLYTE_AUTH_TYPE"] == "ExternalCommand"
    assert json.loads(env["FLYTE_AUTH_COMMAND"]) == ["python", "-m", "flyte_agent_factory._union_token"]
    assert (env["AGENT_FACTORY_UNION_TIER"], env["AGENT_FACTORY_UNION_SECRET"]) == (
        "deployer",
        "factory-deployer-key",
    )


def test_pod_spec_never_puts_a_key_or_the_injected_name_in_the_environment():
    _, env = _union_identity.pod_spec(_profile_with(), "queue")
    assert not set(env) & set(_union_identity.INJECTED_VARS)
    assert _union_identity.KEY_ENV not in env


def test_project_pod_also_gets_the_session_key_only_when_it_differs():
    secrets, _ = _union_identity.pod_spec(_profile_with(), "project")
    assert [s.as_env_var for s in secrets] == ["AGENT_FACTORY_UNION_KEY"]
    secrets, _ = _union_identity.pod_spec(_profile_with('session_key = "factory-session-key"\n'), "project")
    assert [(s.key, s.as_env_var) for s in secrets] == [
        ("factory-project-key", "AGENT_FACTORY_UNION_KEY"),
        ("factory-session-key", "AGENT_FACTORY_SESSION_UNION_KEY"),
    ]


def test_session_pod_mounts_only_its_own_key():
    secrets, _ = _union_identity.pod_spec(_profile_with('session_key = "factory-session-key"\n'), "session")
    assert [(s.key, s.as_env_var) for s in secrets] == [("factory-session-key", "AGENT_FACTORY_UNION_KEY")]


def test_pod_spec_rejects_an_unknown_tier():
    with pytest.raises(ValueError, match="unknown Union tier"):
        _union_identity.pod_spec(_profile_with(), "review")


# --- establish --------------------------------------------------------------------------------------


def test_establish_drops_the_injected_keys_and_reads_the_client_id(monkeypatch):
    _pod(monkeypatch)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")
    monkeypatch.setenv("EAGER_API_KEY", "injected")
    identity = _union_identity.establish(probe=False)
    assert identity == _union_identity.Identity("project", "factory-project-key", "acme-client-1")
    assert "_UNION_EAGER_API_KEY" not in os.environ and "EAGER_API_KEY" not in os.environ
    assert SECRET_VALUE not in repr(identity)


def test_missing_key_fails_fast_naming_tier_and_secret_and_never_falls_back(monkeypatch):
    monkeypatch.setenv(_union_identity.TIER_ENV, "queue")
    monkeypatch.setenv(_union_identity.SECRET_ENV, "factory-queue-key")
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish(probe=False)
    message = str(raised.value)
    assert "no Union identity for tier queue: secret factory-queue-key is not mounted" in message
    assert "python ops/bootstrap.py --union-keys" in message and "never falls back" in message
    assert "injected" not in message.replace("injected key", "")


def test_a_malformed_key_is_reported_without_quoting_it(monkeypatch):
    _pod(monkeypatch, key="not-base64-at-all!!")
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish(probe=False)
    assert "tier project (secret factory-project-key)" in str(raised.value)
    assert "not-base64-at-all" not in str(raised.value)


def test_establish_probes_under_denied(monkeypatch):
    _pod(monkeypatch)
    calls = []
    monkeypatch.setattr(_union_identity, "_probe", lambda: calls.append("probe"))
    _union_identity.establish()
    assert calls == ["probe"]
    _union_identity.establish(probe=False)
    assert calls == ["probe"]


def test_refused_probe_names_who_was_refused_and_does_not_fall_back(monkeypatch):
    _pod(monkeypatch)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")

    def refuse():
        raise ConnectError(Code.PERMISSION_DENIED, "no")

    monkeypatch.setattr(_union_identity, "_probe", refuse)
    with pytest.raises(_union_identity.IdentityDenied) as raised:
        _union_identity.establish()
    message = str(raised.value)
    assert (
        "Union refused listing runs for project identity acme-client-1 (secret factory-project-key)"
        in message
    )
    assert "PERMISSION_DENIED" in message and "docs/setup.md#union-keys" in message
    assert "does not fall back to the pod's injected key" in message
    assert SECRET_VALUE not in message
    assert "_UNION_EAGER_API_KEY" not in os.environ


def test_current_is_cached_and_probe_free(monkeypatch):
    _pod(monkeypatch)
    monkeypatch.setattr(_union_identity, "_probe", lambda: pytest.fail("current() must not probe"))
    first = _union_identity.current()
    monkeypatch.delenv(_union_identity.KEY_ENV)
    assert _union_identity.current() is first


def test_current_does_not_cache_a_failure(monkeypatch):
    with pytest.raises(_union_identity.IdentityMissing):
        _union_identity.current()
    _pod(monkeypatch)
    assert _union_identity.current().client_id == "acme-client-1"


# --- denied -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [Code.PERMISSION_DENIED, Code.UNAUTHENTICATED])
def test_denied_converts_refusals(monkeypatch, code):
    _pod(monkeypatch, tier="session", secret="factory-session-key")
    with pytest.raises(
        _union_identity.IdentityDenied, match=f"submitting a run for session identity.*{code.name}"
    ):
        with _union_identity.denied("submitting a run"):
            raise ConnectError(code, "x")


def test_denied_leaves_other_errors_alone(monkeypatch):
    _pod(monkeypatch)
    with pytest.raises(ConnectError):
        with _union_identity.denied("x"):
            raise ConnectError(Code.NOT_FOUND, "x")
    with pytest.raises(KeyError):
        with _union_identity.denied("x"):
            raise KeyError("x")


def test_denied_converts_a_failed_token_command():
    from flyte.remote._client.auth.errors import AuthenticationError

    with pytest.raises(_union_identity.IdentityDenied, match="token command failed"):
        with _union_identity.denied("x"):
            raise AuthenticationError("boom")


# --- session_key, describe, init_for_script, controller_kwargs ---------------------------------------


def test_session_key_prefers_the_held_apart_key():
    assert (
        _union_identity.session_key({"AGENT_FACTORY_UNION_KEY": "p", "AGENT_FACTORY_SESSION_UNION_KEY": "s"})
        == "s"
    )
    assert _union_identity.session_key({"AGENT_FACTORY_UNION_KEY": "p"}) == "p"
    assert _union_identity.session_key({}) == ""


def test_describe_lists_every_tier_and_its_secret_only():
    assert _union_identity.describe(_profile_with('queue_key = "factory-queue-key"\n')) == [
        {"tier": "project", "secret": "factory-project-key"},
        {"tier": "session", "secret": "factory-project-key"},
        {"tier": "queue", "secret": "factory-queue-key"},
        {"tier": "deployer", "secret": "factory-deployer-key"},
    ]


def test_init_for_script_initialises_from_the_named_variable(monkeypatch):
    import flyte

    seen = {}
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kw: seen.update(kw))
    monkeypatch.setenv("MY_DEPLOY_KEY", "k")
    profile = _profile_with()
    _union_identity.init_for_script(profile, "MY_DEPLOY_KEY")
    assert seen == {"api_key": "k", "project": profile.backend.project, "domain": profile.backend.domain}


def test_init_for_script_with_an_unset_named_variable_names_it(monkeypatch):
    monkeypatch.delenv("MY_DEPLOY_KEY", raising=False)
    with pytest.raises(_union_identity.IdentityMissing, match="MY_DEPLOY_KEY is not set"):
        _union_identity.init_for_script(_profile_with(), "MY_DEPLOY_KEY")


def test_init_for_script_without_a_named_key_uses_the_cli_login(monkeypatch):
    """A hand deploy on a laptop: the user's CLI login, never an API key and never the injected one."""
    import flyte

    seen: dict[str, Any] = {}
    monkeypatch.setattr(flyte, "init_from_config", lambda path=None, **kw: seen.update(path=path, **kw))
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kw: pytest.fail("no API key on a laptop"))
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected")
    profile = _profile_with()
    _union_identity.init_for_script(profile)
    config = str(Path(profile.backend.config).expanduser()) if profile.backend.config else None
    assert seen == {"path": config, "project": profile.backend.project, "domain": profile.backend.domain}


def test_init_for_script_passes_extra_init_arguments_through(monkeypatch):
    import flyte

    seen = {}
    monkeypatch.setattr(flyte, "init_from_api_key", lambda **kw: seen.update(kw))
    monkeypatch.setenv("MY_DEPLOY_KEY", "k")
    _union_identity.init_for_script(_profile_with(), "MY_DEPLOY_KEY", project="other", image_builder="remote")
    assert seen["project"] == "other" and seen["image_builder"] == "remote" and seen["api_key"] == "k"


def test_controller_kwargs_is_init_in_clusters_result(monkeypatch):
    import flyte

    _pod(monkeypatch)
    monkeypatch.setattr(flyte, "init_in_cluster", lambda: {"endpoint": "e", "auth_type": "ExternalCommand"})
    assert _union_identity.controller_kwargs() == {"endpoint": "e", "auth_type": "ExternalCommand"}


def test_controller_kwargs_refuses_a_pod_without_its_tier_key(monkeypatch):
    import flyte

    called = []

    def init_in_cluster() -> dict[str, str]:
        called.append(True)
        return {}

    monkeypatch.setattr(flyte, "init_in_cluster", init_in_cluster)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", KEY)  # what init_in_cluster would otherwise fall back to
    with pytest.raises(_union_identity.IdentityMissing, match="never falls back"):
        _union_identity.controller_kwargs()
    assert called == []


def test_controller_kwargs_scrubs_the_injected_key_before_initialising(monkeypatch):
    import flyte

    _pod(monkeypatch)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", KEY)
    seen = []

    def init_in_cluster() -> dict[str, str]:
        seen.append("_UNION_EAGER_API_KEY" in os.environ)
        return {}

    monkeypatch.setattr(flyte, "init_in_cluster", init_in_cluster)
    _union_identity.controller_kwargs()
    assert seen == [False]


# --- the token command ------------------------------------------------------------------------------


def test_token_command_matches_what_the_sdk_authenticator_runs():
    """`TOKEN_COMMAND` is a JSON argv the SDK executes; its module has a `main` that prints the token."""
    argv = json.loads(_union_identity.TOKEN_COMMAND)
    assert (
        argv[:2] == ["python", "-m"] and argv[2] == _union_token.__name__.rsplit(".", 1)[0] + "._union_token"
    )


def test_token_command_prints_only_the_token(monkeypatch, capsys):
    _pod(monkeypatch)

    async def fake(key):
        assert key == KEY
        return "access-token-1"

    monkeypatch.setattr(_union_token, "_token", fake)
    assert _union_token.main() == 0
    out = capsys.readouterr()
    assert out.out == "access-token-1\n" and out.err == ""


def test_token_command_exits_2_naming_tier_and_secret_without_the_key(monkeypatch, capsys):
    _pod(monkeypatch, tier="deployer", secret="factory-deployer-key")

    async def fail(key):
        raise RuntimeError(f"server said {key} {SECRET_VALUE}")

    monkeypatch.setattr(_union_token, "_token", fail)
    assert _union_token.main() == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "union token for tier deployer (secret factory-deployer-key) failed: RuntimeError\n"
    assert SECRET_VALUE not in out.err


def test_token_command_without_a_key_exits_2(monkeypatch, capsys):
    monkeypatch.setenv(_union_identity.TIER_ENV, "queue")
    monkeypatch.setenv(_union_identity.SECRET_ENV, "factory-queue-key")
    assert _union_token.main() == 2
    assert "tier queue (secret factory-queue-key) failed: RuntimeError" in capsys.readouterr().err


def test_token_exchange_uses_the_sdks_client_credentials_flow(monkeypatch):
    """`_token` builds the SDK's authenticator from the decoded key and returns its access token."""
    from flyte.remote._client.auth._authenticators import client_credentials

    seen = {}

    class FakeAuthenticator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def refresh_credentials(self):
            seen["refreshed"] = True

        def get_credentials(self):
            return SimpleNamespace(access_token="tok")

    monkeypatch.setattr(client_credentials, "ClientCredentialsAuthenticator", FakeAuthenticator)
    assert asyncio.run(_union_token._token(KEY)) == "tok"
    assert seen["client_id"] == "acme-client-1" and seen["client_credentials_secret"] == SECRET_VALUE
    assert seen["endpoint"] == "https://union.example.org" and seen["disable_keyring"] is True
    assert seen["refreshed"] is True


def test_token_command_as_a_subprocess_fails_cleanly_without_a_key():
    """What the SDK sees: a real process, non-zero exit, nothing on stdout."""
    env = {k: v for k, v in os.environ.items() if k != _union_identity.KEY_ENV}
    env.update(
        PYTHONPATH=SRC, AGENT_FACTORY_UNION_TIER="project", AGENT_FACTORY_UNION_SECRET="factory-project-key"
    )
    result = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._union_token"], env=env, capture_output=True, text=True
    )
    assert (result.returncode, result.stdout) == (2, "")
    assert "tier project (secret factory-project-key)" in result.stderr
