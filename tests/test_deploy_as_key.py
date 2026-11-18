"""`ops/deploy_as_key.py`: the one-off deploy that acts as the deployer's own key, and its docs."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _deploy, _profile, _pull_requests, _union_identity
from tests._git import git

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_as_key", ROOT / "ops" / "deploy_as_key.py")
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

KEY = "sekret-deployer-key-value"
PLANNED = ["agent-one", "factory", "factory-mcp"]


def _repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.org")
    git(path, "config", "user.name", "t")
    return path


def _commit(path: Path) -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "x")
    return str(git(path, "rev-parse", "HEAD"))


@pytest.fixture
def direction(tmp_path):
    repo = _repo(tmp_path / "direction")
    (repo / "profile.toml").write_text((ROOT / "tests" / "profiles" / "work.toml").read_text())
    _commit(repo)
    return repo


@pytest.fixture
def engine(tmp_path):
    repo = _repo(tmp_path / "engine")
    (repo / "tracked.txt").write_text("a")
    _commit(repo)
    return repo


class _Calls(list[tuple[str, tuple[Any, ...], dict[str, Any]]]):
    state: dict[str, Any]


@pytest.fixture
def calls(monkeypatch):
    """Fake both stages; each call is recorded as (name, args, kwargs). `plan`/`done`/`fail` steer them."""
    log = _Calls()
    state: dict[str, Any] = {
        "plan": list(PLANNED),
        "done": list(PLANNED),
        "check_raises": None,
        "raises": None,
        "warnings": [],
    }

    def check(*a, **kw):
        log.append(("check", a, kw))
        if kw.get("warnings") is not None:
            kw["warnings"][:] = state["warnings"]
        if state["check_raises"]:
            raise state["check_raises"]
        return state["plan"]

    def deploy(*a, deployed=None, **kw):
        log.append(("deploy", a, {**kw, "deployed": deployed}))
        if deployed is not None:
            deployed[:] = state["done"]
        if state["raises"]:
            raise state["raises"]
        return state["done"]

    monkeypatch.setattr(_deploy, "check_in_tree", check)
    monkeypatch.setattr(_deploy, "deploy_in_tree", deploy)
    monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", KEY)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "injected-1")
    monkeypatch.setenv("EAGER_API_KEY", "injected-2")
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", "work")
    monkeypatch.setenv("AGENT_FACTORY_PROFILE_TOML", "name = 'x'")
    log.state = state
    return log


def _run(direction, engine, *extra) -> int:
    return int(script.main(["--direction", str(direction), "--engine", str(engine), *extra]))


def _tail(capsys) -> list[str]:
    return list(capsys.readouterr().out.strip().splitlines())


def test_both_stages_get_the_key_environment_and_the_specified_arguments(calls, direction, engine):
    assert _run(direction, engine) == 0
    assert [name for name, _, _ in calls] == ["check", "deploy"]
    ref, sha = git(engine, "rev-parse", "HEAD"), git(direction, "rev-parse", "HEAD")
    for _, args, kw in calls:
        src, text, where = args
        assert src == engine / "src"
        assert 'name = "work"' in text
        assert where == f"profile.toml at {sha}"
        assert kw["tree"] is None
        env = kw["base_env"]
        assert env[_deploy.DEPLOY_KEY_ENV] == "AGENT_FACTORY_UNION_KEY"
        assert env["AGENT_FACTORY_UNION_KEY"] == KEY
        assert env[_union_identity.TIER_ENV] == "deployer"
        assert env[_union_identity.SECRET_ENV] == "factory-deployer-key"
        for gone in (*_union_identity.INJECTED_VARS, "AGENT_FACTORY_PROFILE", "AGENT_FACTORY_PROFILE_TOML"):
            assert gone not in env
        assert [v for v in env.values() if v == KEY] == [KEY]  # under its own name only
    _, _, kw = calls[1]
    assert (kw["project"], kw["domain"], kw["ref"]) == ("proj-work", "development", ref)


def test_a_custom_key_variable_is_named_for_the_children(calls, direction, engine, monkeypatch):
    monkeypatch.setenv("MY_KEY", KEY)
    assert _run(direction, engine, "--key-env", "MY_KEY") == 0
    env = calls[0][2]["base_env"]
    assert env[_deploy.DEPLOY_KEY_ENV] == "MY_KEY" and env["MY_KEY"] == KEY


def test_check_never_deploys(calls, direction, engine, capsys):
    assert _run(direction, engine, "--check") == 0
    assert [name for name, _, _ in calls] == ["check"]
    assert _tail(capsys)[-1] == "check only: would deploy agent-one, factory, factory-mcp; nothing deployed"


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_or_empty_key_refuses_before_anything_runs(
    calls, direction, engine, monkeypatch, capsys, value
):
    monkeypatch.delenv("AGENT_FACTORY_UNION_KEY")
    if value is not None:
        monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", value)
    assert _run(direction, engine) == 2
    assert not calls
    assert "AGENT_FACTORY_UNION_KEY" in capsys.readouterr().err


def test_the_key_value_is_never_printed(calls, direction, engine, capsys, tmp_path):
    calls.state["raises"] = _deploy.DeployError("deploy of abc failed: boom")
    calls.state["done"] = ["agent-one"]
    for extra in ((), ("--check",)):
        _run(direction, engine, *extra)
    # The refusals too: a missing profile, a checkout that is not a commit, uncommitted changes.
    _run(direction, engine, "--profile", "nope.toml")
    _run(tmp_path, engine)
    (engine / "tracked.txt").write_text("changed")
    _run(direction, engine)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


def test_values_only_the_deployer_sets_and_other_credentials_do_not_reach_the_children(
    calls, direction, engine, monkeypatch
):
    """The deploy child bakes `DEPLOYER_ONLY_ENV` into every environment: this command ran no canary."""
    leaked = {
        "AGENT_FACTORY_GITHUB_PUSH": "app",
        "AGENT_FACTORY_GITHUB_ISOLATION": "isolated",
        _profile.IDENTITIES_ENV: '[{"tier": "stale"}]',
        _union_identity.SESSION_KEY_ENV: "session-key",
        "AGENT_FACTORY_UNION_KEY_SESSION": "session-key",
        "AGENT_FACTORY_UNION_KEY_PROJECT": "project-key",
        "GITHUB_TOKEN": "token",
        "GH_TOKEN": "token",
        _union_identity.TIER_ENV: "session",
        _union_identity.SECRET_ENV: "session-secret",
    }
    assert set(_profile.DEPLOYER_ONLY_ENV) <= set(leaked)
    for name, value in leaked.items():
        monkeypatch.setenv(name, value)
    assert _run(direction, engine) == 0
    for _, _, kw in calls:
        env = kw["base_env"]
        assert not {name for name in leaked if name in env} - {
            _union_identity.TIER_ENV,
            _union_identity.SECRET_ENV,
        }
        assert env[_union_identity.TIER_ENV] == "deployer"
        assert env[_union_identity.SECRET_ENV] == "factory-deployer-key"
        assert env["AGENT_FACTORY_UNION_KEY"] == KEY


def test_a_custom_key_variable_drops_the_default_one(calls, direction, engine, monkeypatch):
    """With `--key-env`, a key also present under the default name is not passed on."""
    monkeypatch.setenv("MY_KEY", "my-key-value")
    assert _run(direction, engine, "--key-env", "MY_KEY") == 0
    env = calls[0][2]["base_env"]
    assert "AGENT_FACTORY_UNION_KEY" not in env and env["MY_KEY"] == "my-key-value"


NOT_VERIFIED = "secrets not verified: listing them failed (Error)"


def test_a_check_that_could_not_list_the_secrets_fails_and_says_so(calls, direction, engine, capsys):
    calls.state["warnings"] = [NOT_VERIFIED]
    assert _run(direction, engine, "--check") == 1
    assert [name for name, _, _ in calls] == ["check"]
    out = capsys.readouterr()
    assert NOT_VERIFIED in out.err
    assert out.out.strip().splitlines()[-2:] == [
        "check failed: would deploy agent-one, factory, factory-mcp; nothing deployed",
        f"reason: {NOT_VERIFIED}",
    ]


def test_a_deploy_whose_check_could_not_verify_the_key_stops_before_deploying(
    calls, direction, engine, capsys
):
    """The deployer goes on (it retries every tick); a person runs this once and should rerun it."""
    calls.state["warnings"] = [NOT_VERIFIED]
    assert _run(direction, engine) == 1
    assert [name for name, _, _ in calls] == ["check"]
    out = capsys.readouterr()
    assert NOT_VERIFIED in out.err
    assert out.out.strip().splitlines()[-3:] == [
        "deployed: none",
        "not deployed: agent-one, factory, factory-mcp",
        f"reason: {NOT_VERIFIED}",
    ]


def test_check_in_tree_hands_back_what_a_passing_check_printed(monkeypatch, tmp_path):
    def run(src, args, *, text, base_env, timeout, capture):
        return 0, '["agent-one"]', f"{NOT_VERIFIED}\n\n"

    monkeypatch.setattr(_deploy, "_run_in_tree", run)
    warnings: list[str] = ["stale"]
    assert _deploy.check_in_tree(tmp_path, "", "w", tree=None, base_env={}, warnings=warnings) == [
        "agent-one"
    ]
    assert warnings == [NOT_VERIFIED]
    assert _deploy.check_in_tree(tmp_path, "", "w", tree=None, base_env={}) == ["agent-one"]


def test_uncommitted_tracked_changes_refuse(calls, direction, engine, capsys):
    (engine / "tracked.txt").write_text("changed")
    assert _run(direction, engine) == 2
    assert not calls
    assert "uncommitted" in capsys.readouterr().err


def test_an_untracked_file_does_not_refuse(calls, direction, engine):
    (engine / "scratch.txt").write_text("x")
    assert _run(direction, engine) == 0


def test_untracked_code_in_the_engine_src_refuses(calls, direction, engine, capsys):
    """It would be deployed under a commit that does not contain it."""
    (engine / "src").mkdir()
    (engine / "src" / "extra.py").write_text("x = 1\n")
    assert _run(direction, engine) == 2
    assert not calls
    assert "uncommitted" in capsys.readouterr().err


@pytest.mark.parametrize("untracked", [False, True])
def test_an_uncommitted_profile_refuses(calls, direction, engine, capsys, untracked):
    """`where` names the direction commit; a profile that differs from it would deploy under a false name."""
    if untracked:
        (direction / "other.toml").write_text((direction / "profile.toml").read_text())
        args: tuple[str, ...] = ("--profile", "other.toml")
    else:
        (direction / "profile.toml").write_text((direction / "profile.toml").read_text() + "\n# edited\n")
        args = ()
    assert _run(direction, engine, *args) == 2
    assert not calls
    assert str(direction) in capsys.readouterr().err


@pytest.mark.parametrize("which", ["direction", "engine", "no-commit"])
def test_a_checkout_that_is_not_a_git_commit_refuses_without_a_traceback(
    calls, direction, engine, tmp_path, capsys, which, monkeypatch
):
    real_run, kwargs = subprocess.run, []

    def record(*a, **kw):
        if "-C" in a[0]:  # the script's git calls, not the fixture's
            kwargs.append(kw)
        return real_run(*a, **kw)

    monkeypatch.setattr(subprocess, "run", record)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "profile.toml").write_text((direction / "profile.toml").read_text())
    if which == "no-commit":
        git(plain, "init", "-q")
    if which == "engine":
        code = _run(direction, plain)
    else:
        code = _run(plain, engine)
    assert code == 2
    assert not calls
    err = capsys.readouterr().err
    assert "refused:" in err and str(plain) in err and "Traceback" not in err
    assert kwargs and all("timeout" in kw for kw in kwargs)  # a hung git must not hang the command


def test_a_hanging_git_is_refused_without_a_traceback(calls, direction, engine, monkeypatch, capsys):
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=60)

    monkeypatch.setattr(subprocess, "run", hang)
    assert _run(direction, engine) == 2
    assert not calls
    err = capsys.readouterr().err
    assert "refused:" in err and "Traceback" not in err


def test_the_prefixed_second_name_of_every_tier_secret_does_not_reach_the_children(monkeypatch, direction):
    """C17: the key stays under `<key-env>` only; Flyte mounts each secret under a second, prefixed name."""
    profile = _profile.load_from_direction(direction, "profile.toml", tolerate_unknown=True)
    base = {"AGENT_FACTORY_UNION_KEY": KEY, "KEEP": "1"}
    secrets = {row["secret"] for row in _union_identity.describe(profile)}
    assert {"factory-deployer-key"} <= secrets and len(secrets) > 1
    for secret in secrets:
        base[_pull_requests.prefixed_key_env_var(secret, base)] = "tier-key"
    env = script.build_child_env(base, profile, "AGENT_FACTORY_UNION_KEY")
    assert env["AGENT_FACTORY_UNION_KEY"] == KEY and env["KEEP"] == "1"
    assert "tier-key" not in env.values()


def test_an_oss_profile_is_refused_and_pointed_at_the_hand_deploy_scripts(calls, direction, engine, capsys):
    text = (direction / "profile.toml").read_text()
    text = text.replace('kind = "union"', 'kind = "oss"')
    text = re.sub(r"(?ms)^\[union\]\n.*?(?=^\[)", "", text)
    (direction / "profile.toml").write_text(text)
    assert _run(direction, engine) == 2
    assert not calls
    assert "ops/deploy_work.py" in capsys.readouterr().err


def test_a_missing_profile_refuses(calls, direction, engine, capsys):
    assert _run(direction, engine, "--profile", "nope.toml") == 2
    assert not calls
    assert "nope.toml" in capsys.readouterr().err


def test_full_success_report(calls, direction, engine, capsys):
    assert _run(direction, engine) == 0
    assert _tail(capsys)[-2:] == ["deployed: agent-one, factory, factory-mcp", "not deployed: none"]


def test_a_success_that_missed_a_planned_environment_fails(calls, direction, engine, capsys):
    calls.state["done"] = ["agent-one", "factory"]
    assert _run(direction, engine) == 1
    last = _tail(capsys)[-3:]
    assert last[:2] == ["deployed: agent-one, factory", "not deployed: factory-mcp"]
    assert last[2].startswith("reason: ") and "factory-mcp" in last[2]


def test_a_slot_failure_reports_what_was_deployed_first(calls, direction, engine, capsys):
    calls.state["done"] = []
    calls.state["raises"] = _deploy.DeployError("deploy of abc failed: slot deploy of abc failed: boom")
    assert _run(direction, engine) == 1
    assert _tail(capsys)[-3:] == [
        "deployed: none",
        "not deployed: agent-one, factory, factory-mcp",
        "reason: deploy of abc failed: slot deploy of abc failed: boom",
    ]


def test_a_factory_stage_failure_reports_the_slots_as_deployed(calls, direction, engine, capsys):
    calls.state["done"] = ["agent-one"]
    calls.state["raises"] = _deploy.DeployError("deploy of abc failed: factory deploy of abc failed: boom")
    assert _run(direction, engine) == 1
    assert _tail(capsys)[-3:] == [
        "deployed: agent-one",
        "not deployed: factory, factory-mcp",
        "reason: deploy of abc failed: factory deploy of abc failed: boom",
    ]


def test_a_refused_check_deploys_nothing(calls, direction, engine, capsys):
    calls.state["check_raises"] = _deploy.DeployError("the engine being deployed refuses x")
    assert _run(direction, engine) == 1
    assert [name for name, _, _ in calls] == ["check"]
    assert _tail(capsys)[-3:] == [
        "deployed: none",
        "not deployed: all (the check did not complete, so nothing was planned)",
        "reason: the engine being deployed refuses x",
    ]


def test_missing_secrets_exit_3_and_name_them(calls, direction, engine, capsys):
    calls.state["check_raises"] = _deploy.MissingSecrets("secrets are missing", {"api_key_x": ["agent-one"]})
    assert _run(direction, engine) == _deploy.MISSING_SECRETS_EXIT
    out = capsys.readouterr()
    assert "api_key_x" in out.err and "agent-one" in out.err
    assert "deployed: none" in out.out
    assert "not deployed: all (the check did not complete" in out.out
    assert [name for name, _, _ in calls] == ["check"]


def test_deploy_in_tree_fills_deployed_even_when_the_deploy_fails(monkeypatch, tmp_path):
    def run(src, args, *, text, base_env, timeout, capture):
        Path(base_env[_deploy.DEPLOY_REPORT_ENV]).write_text("agent-one\nagent-one\nsub-one\n")
        return 1, "", ""

    monkeypatch.setattr(_deploy, "_run_in_tree", run)
    done: list[str] = []
    kw: dict[str, Any] = dict(tree=None, project="p", domain="d", ref="abc", base_env={})
    with pytest.raises(_deploy.DeployError, match="deploy of abc failed"):
        _deploy.deploy_in_tree(tmp_path, "", "where", deployed=done, **kw)
    assert done == ["agent-one", "sub-one"]
    # Without the keyword nothing changes: it still raises, and a success still returns the names.
    with pytest.raises(_deploy.DeployError):
        _deploy.deploy_in_tree(tmp_path, "", "where", **kw)
    monkeypatch.setattr(_deploy, "_run_in_tree", lambda src, args, **k: run(src, args, **k) and (0, "", ""))
    ok: list[str] = []
    assert _deploy.deploy_in_tree(tmp_path, "", "where", deployed=ok, **kw) == ["agent-one", "sub-one"]
    assert ok == ["agent-one", "sub-one"]


def test_deploy_in_tree_fills_deployed_when_the_child_times_out(monkeypatch, tmp_path):
    def run(src, args, *, text, base_env, timeout, capture):
        Path(base_env[_deploy.DEPLOY_REPORT_ENV]).write_text("agent-one\n")
        raise _deploy.DeployError("deploy in the engine tree did not finish; killed it")

    monkeypatch.setattr(_deploy, "_run_in_tree", run)
    done: list[str] = []
    with pytest.raises(_deploy.DeployError, match="killed it"):
        _deploy.deploy_in_tree(
            tmp_path, "", "w", tree=None, project="p", domain="d", ref="abc", base_env={}, deployed=done
        )
    assert done == ["agent-one"]


# --- docs match code -------------------------------------------------------------------------------------

SETUP = (ROOT / "docs" / "setup.md").read_text()


def _section(title: str, flat: bool = False) -> str:
    match = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", SETUP)
    assert match, f"setup.md has no '## {title}' section"
    return " ".join(match.group(1).split()) if flat else match.group(1)


def _parser_flags() -> set[str]:
    import argparse

    seen: list[argparse.ArgumentParser] = []
    orig = argparse.ArgumentParser.parse_args

    def spy(self, *a, **kw):
        seen.append(self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = spy  # type: ignore[method-assign]
    try:
        script.main(["--direction", "x"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig  # type: ignore[method-assign]
    return {opt for action in seen[0]._actions for opt in action.option_strings}


def test_setup_migration_names_a_command_and_flags_the_script_has():
    migration = _section("Migrating an existing install")
    assert "python ops/deploy_as_key.py" in migration
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", migration)) & {
        "--direction",
        "--profile",
        "--engine",
        "--key-env",
        "--check",
    }
    assert {"--direction", "--check"} <= flags
    mentioned = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", migration)) - {
        "--ref",
        "--detach",
        "--python",
        "--frozen",
    }
    assert mentioned <= _parser_flags() | {"--union-keys"}, mentioned - _parser_flags()


def test_setup_migration_no_longer_claims_the_old_credential_deploys_the_engine():
    assert "old credential for the last time" not in _section("Migrating an existing install")


def test_setup_pod_howto_names_what_a_task_needs():
    migration = _section("Migrating an existing install", flat=True)
    for needle in (
        "AGENT_FACTORY_UNION_KEY",
        "GIT_SSH_COMMAND",
        "IdentitiesOnly=yes",
        "[repo_keys]",
        "uv sync",
        "deploy_work.py",
        "deploy_factory.py",
        "Environment",
    ):
        assert needle in migration, needle


def test_setup_union_keys_lists_what_the_deployer_key_can_do():
    keys = _section("Union keys", flat=True)
    for needle in (
        "remote image builder",
        "register tasks",
        "deploy, update, stop and delete apps",
        "read their status",
        "create, update and delete triggers",
        "list secrets",
        "polling",
        "app watch stream",
    ):
        assert needle in keys, needle
