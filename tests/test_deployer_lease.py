"""The deploy lock lives on the state branch (#83): the App stays read-only on code. Offline.

`World` (tests/test_deployer.py) serves the lease from a real bare repository, and models GitHub's answer to a
token request for more than the installation was granted (`World.granted`).
"""

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from flyte_agent_factory import (
    _deployer,
    _state_store,
)
from flyte_agent_factory._deploy import DeployError
from tests.test_deployer import SHA, make_direction_profile
from tests.test_deployer import public as public
from tests.test_deployer import world as world

TOKEN_422 = (
    "deploy f00 failed: github POST /app/installations/77/access_tokens: HTTP 422 "
    "The level of access for permissions requested are not granted to this installation."
)
TOKEN_RATE_LIMITED = (
    "github POST /app/installations/77/access_tokens: HTTP 403 API rate limit exceeded for installation."
)


# --- the lease is on the state branch, whatever [state] backend says ------------------------------------


def test_a_bucket_backend_still_leases_on_a_git_state_branch(world):
    bucket = dataclasses.replace(world.profile.state, backend="bucket", bucket="factory-state-bucket")
    profile = dataclasses.replace(world.profile, state=bucket)
    store = _deployer._open_lease_store(profile)
    assert isinstance(store, _state_store.StateStore) and store.remote == profile.repo
    assert store.branch == _state_store.default_branch(profile.backend.project, profile.backend.domain)
    assert _deployer._acquire_lease(profile, world.app, "first") and world.lease()["run"] == "first"


def test_a_configured_state_branch_is_the_lease_branch(world):
    state = dataclasses.replace(world.profile.state, branch="factory-state/mine")
    store = cast(
        _state_store.StateStore, _deployer._open_lease_store(dataclasses.replace(world.profile, state=state))
    )
    assert store.branch == "factory-state/mine"


# --- the tick's preflight -----------------------------------------------------------------------------------


async def test_the_tick_refuses_without_the_deploy_key_before_submitting(world):
    world.deploy_key = None
    with pytest.raises(DeployError, match=r"GIT_DEPLOY_KEY is not mounted in factory-deployer.*deploy_key"):
        await world.tick()
    assert not world.submissions and not world.deployed


async def test_the_tick_refuses_when_the_state_remote_cannot_be_read(world, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", world.profile.repo)  # keep the mapping...
    monkeypatch.setenv(
        "GIT_CONFIG_KEY_0", f"url.{tmp_path / 'gone.git'}.insteadOf"
    )  # ...to a remote that is gone
    with pytest.raises(DeployError, match="state branch .* cannot be read with the deploy key"):
        await world.tick()
    assert not world.submissions


async def test_a_tick_with_nothing_to_deploy_needs_no_deploy_key(world):
    world.deploy_key = None
    world.record(SHA)
    assert await world.tick() == f"up to date at {SHA}"


def test_the_deployer_mounts_the_deploy_key():
    secrets: object = _deployer.env.secrets
    assert isinstance(secrets, list)
    mounted = {secret.key: secret.as_env_var for secret in secrets}
    assert mounted.get(_deployer.PROFILE.secrets.deploy_key) == "GIT_DEPLOY_KEY"


# --- deploy children never get the deploy key ---------------------------------------------------------------


async def test_deploy_children_never_get_the_deploy_key(world, monkeypatch):
    for name in ("GIT_DEPLOY_KEY", "GIT_DEPLOY_KEY_FACTORY", "_UNION_DEPLOY_KEY_X", "GIT_SSH_COMMAND"):
        monkeypatch.setenv(name, "-----BEGIN OPENSSH PRIVATE KEY-----")  # gitleaks:allow
    monkeypatch.setenv("KEEP_ME", "1")
    assert "KEEP_ME" in _deployer._child_env(world.profile)
    await world.tick()
    for env in (
        _deployer._child_env(world.profile),
        world.slot_calls[0]["base_env"],
        world.factory_calls[0]["env"],
        world.profile_checks[0]["base_env"],
    ):
        assert not {
            "GIT_DEPLOY_KEY",
            "GIT_DEPLOY_KEY_FACTORY",
            "_UNION_DEPLOY_KEY_X",
            "GIT_SSH_COMMAND",
        } & set(env)


# --- migration: the lock ref an older deployer wrote --------------------------------------------------------


@pytest.mark.parametrize("repo", ["acme/factory", "acme/direction"])
def test_a_live_legacy_holder_stands_the_pair_down(world, repo, monkeypatch):
    world.profile = make_direction_profile(f"file://{world.state}")
    world.add_run("RUNNING", name="old-code-pair")
    world.legacy.hold(repo, "old-code-pair")
    assert not _deployer._acquire_lease(world.profile, world.app, "new-pair")
    assert world.lease() is None  # nothing was taken
    assert any("lease held by old-code-pair (legacy lock)" in said for said in world.said)


@pytest.mark.parametrize("phase", ["SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"])
def test_a_finished_legacy_holder_is_ignored(world, phase):
    world.profile = make_direction_profile(f"file://{world.state}")
    world.add_run(phase, name="old-code-pair")
    world.legacy.hold("acme/factory", "old-code-pair")
    assert _deployer._acquire_lease(world.profile, world.app, "new-pair")
    assert world.lease()["run"] == "new-pair"


def test_a_legacy_ref_older_than_any_pair_can_run_is_not_looked_up(world, monkeypatch):
    # Flyte may have forgotten a holder that old; looking it up would fail every later deploy closed.
    world.profile = make_direction_profile(f"file://{world.state}")
    world.legacy.hold("acme/direction", "forgotten-pair", taken="2026-01-01T00:00:00Z")
    looked_up: list[str] = []

    def is_live(name: str) -> bool:
        looked_up.append(name)
        return True

    monkeypatch.setattr(_deployer, "_is_live", is_live)
    assert _deployer._acquire_lease(world.profile, world.app, "new-pair")
    assert world.lease()["run"] == "new-pair" and "forgotten-pair" not in looked_up


def test_a_recent_legacy_ref_is_still_honoured(world):
    world.profile = make_direction_profile(f"file://{world.state}")
    world.add_run("RUNNING", name="old-code-pair")
    taken = datetime.now(timezone.utc).isoformat()
    world.legacy.hold("acme/direction", "old-code-pair", taken=taken)
    assert not _deployer._acquire_lease(world.profile, world.app, "new-pair")


def test_a_legacy_ref_that_is_not_a_lock_record_needs_the_operator(world):
    world.legacy.hold("acme/factory", "x", message="not json")
    with pytest.raises(DeployError, match="operator resolution required"):
        _deployer._acquire_lease(world.profile, world.app, "new-pair")


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"profile": "work"}', b'{"run": null}'])
def test_a_lease_record_that_is_not_one_needs_the_operator(world, body):
    _deployer._open_lease_store(world.profile).write(
        {_deployer._build_lease_key(world.profile): body}, "garbage"
    )
    with pytest.raises(DeployError, match="operator resolution required"):
        _deployer._acquire_lease(world.profile, world.app, "new-pair")


def test_a_legacy_ref_held_by_this_run_is_ignored(world):
    world.legacy.hold("acme/factory", "new-pair")
    assert _deployer._acquire_lease(world.profile, world.app, "new-pair")


def test_no_legacy_ref_is_ignored_quietly(world):
    assert _deployer._acquire_lease(world.profile, world.app, "new-pair")
    assert world.legacy.requests and all(method == "GET" for method, _ in world.legacy.requests)


def test_the_legacy_ref_is_read_with_contents_read_only(world):
    world.profile = make_direction_profile(f"file://{world.state}")
    world.granted = {"contents": "read", "metadata": "read"}  # a 422 for anything more
    world.add_run("RUNNING", name="old-code-pair")
    world.legacy.hold("acme/direction", "old-code-pair")
    assert not _deployer._acquire_lease(world.profile, world.app, "new-pair")
    assert world.token_requests and all(wanted == {"contents": "read"} for wanted in world.token_requests)


async def test_a_pair_stands_down_for_a_legacy_holder_and_deploys_nothing(world):
    world.add_run("RUNNING", name="old-code-pair")
    world.legacy.hold("acme/factory", "old-code-pair")
    run = world.add_run(
        "RUNNING",
        labels=_deployer._pair_labels(world.profile, SHA, ""),
        inputs={"sha": SHA, "direction_sha": ""},
    )
    await world.attempt(run)
    assert run.output == {"deployed": False} and not world.deployed


# --- failures are visible at the tick (#83 requirement 3) ---------------------------------------------------


def _failed(world, error, *, times=1):
    labels = _deployer._pair_labels(world.profile, SHA, "")
    for _ in range(times):
        run = world.add_run("FAILED", labels=labels)
        run.error = error
    return run


async def test_the_tick_names_the_failed_run_and_its_redacted_error(world):
    failed = _failed(
        world, "deploy failed: git fetch https://x-access-token:ghs_secret@github.com/acme/factory.git"
    )
    message = await _deployer.run_once(world.profile, "run-me", world.app)
    assert message.startswith(f"submitted {SHA}")  # one failure is retried, and said
    assert failed.name in message and "https://x-access-token:***@github.com/acme/factory.git" in message
    assert "ghs_secret" not in message


async def test_giving_up_names_the_latest_failed_run_and_its_error(world):
    failed = _failed(world, "deploy failed: slot agent-one: exit 1", times=_deployer.MAX_ATTEMPTS)
    message = await world.tick()
    assert message.startswith(f"giving up on {SHA} after {_deployer.MAX_ATTEMPTS} attempts")
    assert failed.name in message and "slot agent-one: exit 1" in message
    assert not world.submissions


async def test_a_long_error_is_cut_and_one_line(world):
    _failed(world, "first line\n" + "x" * 5000)
    message = await _deployer.run_once(world.profile, "run-me", world.app)
    assert "first line xxx" in message and "..." in message and "\n" not in message and len(message) < 1500


async def test_an_unreadable_error_does_not_fail_the_tick(world):
    failed = _failed(world, "")

    async def broken():
        raise OSError("run service down")

    failed.first_failure = SimpleNamespace(aio=broken)
    message = await _deployer.run_once(world.profile, "run-me", world.app)
    assert message.startswith("submitted") and "error unreadable: OSError" in message


@pytest.mark.parametrize(
    ("error", "fix"),
    [
        (TOKEN_422, "GitHub App"),
        ("github POST /app/installations/77/access_tokens: HTTP 403 Resource not accessible", "GitHub App"),
        ("github POST /app/installations/77/access_tokens: HTTP 401 Bad credentials", "GitHub App"),
        (
            "git push failed for git@github.com:a/b.git: git@github.com: Permission denied (publickey).",
            "deploy key",
        ),
        ("remote: ERROR: The key you are authenticating with has been marked as read only.", "deploy key"),
    ],
)
async def test_a_credential_failure_is_not_retried_and_names_what_to_fix(world, error, fix):
    failed = _failed(world, error)  # one failure: not yet the three that make it give up
    message = await world.tick()
    assert "credential or permission problem" in message and fix in message and failed.name in message
    assert "Not submitting again until a head moves" in message
    assert not world.submissions and not world.deployed


async def test_a_credential_failure_is_retried_when_forced(world):
    _failed(world, TOKEN_422)
    message = await _deployer.run_once(world.profile, "run-me", world.app, force=True)
    assert message.startswith("submitted")


async def test_a_credential_failure_at_an_older_head_does_not_stop_a_new_head(world):
    labels = _deployer._pair_labels(world.profile, "b" * 40, "")
    world.add_run("FAILED", labels=labels).error = TOKEN_422
    assert (await world.tick()).startswith(f"deployed {SHA}")


async def test_an_ordinary_failure_is_not_a_credential_problem(world):
    _failed(world, "deploy failed: factory deploy of aaa exited 1")
    assert (await _deployer.run_once(world.profile, "run-me", world.app)).startswith("submitted")


async def test_a_rate_limited_token_request_is_retried_without_force(world):
    # A rate limit passes on its own: the next tick resubmits (attempt 2 of `MAX_ATTEMPTS`) and it deploys.
    _failed(world, TOKEN_RATE_LIMITED)
    message = await _deployer.run_once(world.profile, "run-me", world.app)
    assert message.startswith("submitted") and "credential or permission problem" not in message
    await world.attempt(world.runs[-1])
    assert world.deployed


async def test_a_forced_retry_after_a_credential_failure_deploys(world):
    # The owner fixed the App and forced: the submitted pair must not refuse what the tick let through.
    _failed(world, TOKEN_422)
    assert (await _deployer.run_once(world.profile, "run-me", world.app, force=True)).startswith("submitted")
    await world.attempt(world.runs[-1])
    assert world.deployed


async def test_force_is_not_offered_once_the_attempt_limit_is_reached(world):
    _failed(world, "deploy failed: slot agent-one: exit 1", times=2)
    _failed(world, TOKEN_422)
    message = await _deployer.run_once(world.profile, "run-me", world.app)
    assert message.startswith("giving up") and "force" not in message and "GitHub App" in message
    forced = await _deployer.run_once(world.profile, "run-me", world.app, force=True)
    assert forced.startswith("giving up") and not world.submissions


def test_the_decision_note_offers_force_only_under_the_attempt_limit():
    """The note must not promise that force retries a credential failure past ``MAX_ATTEMPTS``."""
    note = " ".join(
        (Path(__file__).resolve().parents[1] / "docs" / "deploy-lock-decision.md").read_text().split()
    )
    visible = note[note.index("**Visible failures.**") : note.index("**Migration of existing installs.**")]
    assert "(or `deploy_if_changed(force=True)`)" not in visible
    assert "only while the heads' failed attempts are under `MAX_ATTEMPTS`" in visible
    assert "force does not submit either" in visible


@pytest.mark.parametrize("date", ["2026-01-01T00:00:00", "not a date", None])
def test_a_legacy_date_without_a_zone_or_unreadable_is_handled(date):
    taken = _deployer._parse_taken_at({"committer": {"date": date}})
    assert taken is None or taken.tzinfo is not None


@pytest.mark.parametrize(
    "error",
    [
        "ssh: connect to host github.com port 22: Connection timed out\r\n"
        "fatal: Could not read from remote repository.",
        "ssh: Could not resolve hostname github.com: Temporary failure in name resolution\n"
        "fatal: Could not read from remote repository.",
        "OSError: [Errno 13] Permission denied: '/tmp/build/out'",
        TOKEN_RATE_LIMITED,
        "github POST /app/installations/77/access_tokens: HTTP 403 You have exceeded a secondary rate limit. "
        "Please wait a few minutes before you try again.",
        "github POST /app/installations/77/access_tokens: HTTP 429 API rate limit exceeded",
    ],
)
def test_a_network_or_local_failure_is_not_a_credential_problem(error):
    assert _deployer._find_credential_fix(error) == ""


def test_the_fix_for_each_credential_failure_is_named():
    assert "GitHub App" in _deployer._find_credential_fix(TOKEN_422)
    assert "deploy key" in _deployer._find_credential_fix("Permission denied (publickey)")
    assert _deployer._find_credential_fix("slot deploy exited 1") == ""
