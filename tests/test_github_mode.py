"""`_github_mode` (one word and one sentence) and `_isolation_canary` (the measurement behind `app`)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from flyte_agent_factory import _factory_status, _github_mode, _isolation_canary
from flyte_agent_factory._isolation_canary import LaunchRefused, Verdict


def profile(*, configured=True, push=None):
    github = SimpleNamespace(configured=configured, private_key_secret="factory-app-key", app_id="1")
    if push is not None:
        github.push = push
    return SimpleNamespace(github=github)


APP = {"AGENT_FACTORY_GITHUB_PUSH": "app"}


@pytest.mark.parametrize(
    ("prof", "env", "expected"),
    [
        (profile(configured=False, push="app"), APP, "deploy-keys"),
        (profile(push="app"), APP, "app"),
        (profile(push="app"), {}, "app-read"),  # a hand deploy ran no canary
        (profile(push="app"), {"AGENT_FACTORY_GITHUB_PUSH": "deploy_key"}, "app-read"),
        (profile(push="deploy_key"), APP, "app-read"),  # the owner did not opt in
        (profile(), APP, "app-read"),  # no `push` attribute yet: deploy_key
    ],
)
def test_mode(prof, env, expected):
    assert _github_mode.mode(prof, env) == expected


def test_describe_says_what_each_mode_means():
    assert _github_mode.describe(profile(push="app"), APP) == (
        "GitHub App: CI visible; pushes by repo-scoped token"
    )
    assert _github_mode.describe(profile(configured=False), {}) == (
        "deploy keys only: no CI visibility, no PRs, no factory/verifier"
    )
    text = _github_mode.describe(profile(push="app"), {}, "a pod mounted the key")
    assert text == (
        "GitHub App: CI visible; pushes by deploy key; App key reachable from sessions: a pod mounted the "
        "key; factory/verifier advisory"
    )
    assert "isolation not proven" in _github_mode.describe(profile(push="app"), {})


def test_describe_says_the_app_key_is_reachable_and_the_verifier_advisory():
    text = _github_mode.describe(profile(push="deploy_key"), {}, "a pod carries the key Union injects")
    assert text == (
        "GitHub App: CI visible; pushes by deploy key; App key reachable from sessions: "
        "a pod carries the key Union injects; factory/verifier advisory"
    )
    # Without a canary result the sentence stays truthful about not knowing, and still says advisory.
    unknown = _github_mode.describe(profile(push="deploy_key"), {})
    assert "isolation not proven" in unknown and unknown.endswith("factory/verifier advisory")
    assert _github_mode.describe(profile(push="app"), {}).endswith("factory/verifier advisory")


def test_an_isolated_deploy_is_not_described_as_a_reachable_key():
    env = {_github_mode.ISOLATION_ENV_VAR: f"{_github_mode.ISOLATED}; the canary saw no App key"}
    text = _github_mode.describe(profile(push="deploy_key"), env)
    expected = (
        "GitHub App: CI visible; pushes by deploy key (isolated: the canary saw no App key; "
        'set [github] push = "app")'
    )
    assert text == expected
    assert "reachable" not in text and "advisory" not in text


def test_the_status_reads_the_reasons_the_deployer_recorded():
    env = {_github_mode.ISOLATION_ENV_VAR: "a task mounted the App key"}
    fields = _factory_status.github_fields(profile(push="deploy_key"), env)
    assert fields["github_mode"] == "app-read"
    assert "App key reachable from sessions: a task mounted the App key" in fields["github"]


def test_the_status_carries_mode_and_sentence():
    fields = _factory_status.github_fields(profile(push="app"), APP)
    assert fields["github_mode"] == "app" and "repo-scoped token" in fields["github"]


def launching(answer=None, error=None):
    def launch(secret_key):
        assert secret_key == "factory-app-key"
        if error:
            raise error
        return answer

    return launch


def test_a_refused_launch_is_isolated():
    assert _isolation_canary.check(profile(), launching(error=LaunchRefused("PERMISSION_DENIED"))).isolated


def test_a_pod_that_sees_no_key_is_isolated():
    verdict = _isolation_canary.check(
        profile(),
        launching({"app_key_mounted": False, "injected_key_present": False, "union_key_injected": False}),
    )
    assert verdict.isolated and verdict.reasons == ["the canary saw no App key"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            {"app_key_mounted": True, "injected_key_present": False, "union_key_injected": False},
            "mounted the App key",
        ),
        (
            {"app_key_mounted": False, "injected_key_present": True, "union_key_injected": False},
            "injected App key",
        ),
        # Measured live 2026-11-12: Union injects its key into pods a scoped key launches. Such a pod can
        # launch anything, the App key's secret included, so the session is not isolated.
        (
            {"app_key_mounted": False, "injected_key_present": False, "union_key_injected": True},
            "Union injects",
        ),
    ],
)
def test_a_pod_that_sees_a_key_is_not_isolated_and_says_which(answer, reason):
    verdict = _isolation_canary.check(profile(), launching(answer))
    assert not verdict.isolated and reason in verdict.reasons[0]


@pytest.mark.parametrize(
    "launch",
    [
        launching(error=RuntimeError("cluster unreachable")),
        launching({}),
        launching({"app_key_mounted": False}),
        launching({"app_key_mounted": "no", "injected_key_present": 0}),
        # An older canary's two-flag answer says nothing about the injected Union key: fail closed.
        launching({"app_key_mounted": False, "injected_key_present": False}),
    ],
)
def test_anything_unreadable_is_not_isolated(launch):
    verdict = _isolation_canary.check(profile(), launch)
    assert verdict.isolated is False and verdict.reasons


def test_no_app_is_not_isolated():
    assert _isolation_canary.check(profile(configured=False), launching({})) == Verdict(
        False, ["no GitHub App is configured"]
    )


def test_probe_reports_booleans_never_the_key():
    env = {"GITHUB_APP_PRIVATE_KEY": "pem", "_UNION_FACTORY_APP_KEY": "pem"}
    assert _isolation_canary.probe("factory_app_key", {"GITHUB_APP_PRIVATE_KEY": "pem"}) == {
        "app_key_mounted": True,
        "injected_key_present": False,
        "union_key_injected": False,
    }
    assert _isolation_canary.probe("", env)["injected_key_present"] is False
    assert _isolation_canary.probe("", {}) == {
        "app_key_mounted": False,
        "injected_key_present": False,
        "union_key_injected": False,
    }
    for injected in ("_UNION_EAGER_API_KEY", "EAGER_API_KEY"):
        assert _isolation_canary.probe("", {injected: "a-key"})["union_key_injected"] is True


def test_union_fields_show_tier_to_secret_and_the_recorded_client_ids():
    from flyte_agent_factory import _profile
    from tests.conftest import TEST_PROFILE

    prof = _profile.parse(TEST_PROFILE.read_text())
    bare = _factory_status.union_fields(prof)["union"]
    assert [(row["tier"], row["secret"]) for row in bare] == [
        ("project", "factory-project-key"),
        ("session", "factory-project-key"),
        ("queue", "factory-project-key"),
        ("deployer", "factory-deployer-key"),
    ]
    assert all("client_id" not in row for row in bare)
    output = {
        "identities": [{"tier": "deployer", "secret": "factory-deployer-key", "client_id": "c-1"}, "junk"]
    }
    shown = {row["tier"]: row.get("client_id") for row in _factory_status.union_fields(prof, output)["union"]}
    assert shown == {"project": None, "session": None, "queue": None, "deployer": "c-1"}
    assert _factory_status.union_fields(prof, {"identities": "bad"})["union"] == bare


def test_status_fields_read_the_mode_and_identities_the_deployer_passed_to_the_slot():
    import dataclasses
    import json

    from flyte_agent_factory import _profile
    from tests.conftest import TEST_PROFILE

    parsed = _profile.parse(TEST_PROFILE.read_text())
    prof = dataclasses.replace(
        parsed,
        github=dataclasses.replace(parsed.github, push="app", app_id="1", private_key_secret="app-key"),
    )
    recorded = [{"tier": "project", "secret": "factory-project-key", "client_id": "c-9"}]
    fields = _factory_status.status_fields(prof, {**APP, _profile.IDENTITIES_ENV: json.dumps(recorded)})
    assert fields["github_mode"] == "app"
    assert {row["tier"]: row.get("client_id") for row in fields["union"]}["project"] == "c-9"
    # A hand deploy (no deployer env) or a garbled value still shows tier -> secret, never fails status.
    for env in ({}, {_profile.IDENTITIES_ENV: "{not json"}):
        shown = _factory_status.status_fields(prof, env)
        assert shown["github_mode"] == "app-read"
        assert shown["union"] == _factory_status.union_fields(prof)["union"]


def test_pod_env_passes_the_recorded_identities_through(monkeypatch):
    from flyte_agent_factory import _profile

    # Recorded by the deployer's deploy child, never read from the deploying shell's environment (#179).
    monkeypatch.setattr(_profile, "_deployer_env", {})
    monkeypatch.setenv(_profile.IDENTITIES_ENV, '[{"tier": "stray"}]')
    assert _profile.IDENTITIES_ENV not in _profile.pod_env()
    _profile.set_deployer_env({_profile.IDENTITIES_ENV: "[]"})
    assert _profile.pod_env()[_profile.IDENTITIES_ENV] == "[]"
