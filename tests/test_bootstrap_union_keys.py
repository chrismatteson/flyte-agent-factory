"""`union-keys` on a machine that cannot make keys (macOS, no plugin): paste, validate, store, never print."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from flyte_agent_factory import _bootstrap
from flyte_agent_factory._bootstrap_keys import PastedKeyMaker, PluginKeyMaker, UnionKeysStep
from tests._bootstrap_fakes import KEY_SECRET, World, make_context
from tests.test_bootstrap import person  # noqa: F401 -- autouse fixture: a git identity and a home per test
from tests.test_bootstrap_keys import ARGS, DEPLOYER_SECRET, GRANTED_BINDING, PROJECT_SECRET

# What the console shows once: base64 of `endpoint:client_id:client_secret:org`.
SECRET_PART = "console-made-secret-0f3a"  # gitleaks:allow (a test sentinel, not a key)


def pasted_key(client_id: str) -> str:
    return base64.b64encode(f"flyte.example.test:{client_id}:{SECRET_PART}:".encode()).decode()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    (tmp_path / "flyte-config.yaml").write_text("admin:\n  endpoint: dns:///flyte.example.test\n")
    return w


def run_pasting(world, answers, *, platform="darwin"):
    """One `--union-keys` run; `answers` are what the person pastes. Returns (code, out, prompts)."""
    out: list[str] = []
    prompts: list[str] = []
    pending = iter(answers)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        answer: str = next(pending)
        return answer

    def make(settings):
        ctx = make_context(world, settings, out)
        ctx.platform, ctx.ask_secret = platform, ask
        return ctx

    code = _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), "--union-keys"], make_context=make)
    return code, out, prompts


def files_holding(root: Path, text: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and text.encode() in p.read_bytes()]


def test_on_macos_the_pasted_keys_are_stored_and_only_client_ids_are_printed(world, capsys, tmp_path):
    world.can_manage_keys = False
    project, deployer = pasted_key("client-project"), pasted_key("client-deployer")
    code, out, prompts = run_pasting(world, [project, deployer])
    assert code == 3, out  # no plugin on this machine: the grants are an admin's, below
    assert world.secrets == {PROJECT_SECRET: project, DEPLOYER_SECRET: deployer}
    shown = "\n".join(out) + "".join(prompts) + "".join(capsys.readouterr())
    assert SECRET_PART not in shown and project not in shown and deployer not in shown
    assert "client client-project" in shown and "client client-deployer" in shown
    assert "create a key named factory-project" in shown and "no default policies" in shown
    assert world.union_keys == {} and world.policies == {} and world.tty == [] and world.commands == []


def test_a_pasted_key_is_never_written_to_disk_by_the_step(world, tmp_path, monkeypatch):
    world.can_manage_keys = False
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    key = pasted_key("client-project")
    run_pasting(world, [key, pasted_key("client-deployer")])
    # The fake secret store is memory; nothing under the test's home or the world's root holds the key.
    assert files_holding(tmp_path, SECRET_PART) == []


def test_pasting_then_an_admin_grants_when_the_machine_cannot(world):
    world.can_manage_keys = False
    code, out, _ = run_pasting(world, [pasted_key("client-project"), pasted_key("client-deployer")])
    text = "\n".join(out)
    assert code == 3 and out[0].startswith("[todo] union-keys")
    assert (
        "--grant factory-project client-project" in text
        and "--grant factory-deployer client-deployer" in text
    )
    assert world.assignments == []


def test_a_pasted_key_is_granted_when_the_person_may_manage_access(world):
    """A Mac with the plugin absent cannot; a person on Linux who pastes (a key from the console) can."""
    code, out, _ = run_pasting(
        world, [pasted_key("client-project"), pasted_key("client-deployer")], platform="darwin"
    )
    assert code == 0, out
    assert world.policies == {"factory-project": GRANTED_BINDING, "factory-deployer": GRANTED_BINDING}
    assert sorted(world.assignments) == [
        ("client-deployer", "factory-deployer"),
        ("client-project", "factory-project"),
    ]
    assert world.union_keys == {}, "nothing was made by this tool: the keys are the person's own"


def test_something_that_is_not_a_key_is_refused_without_quoting_it(world):
    code, out, _ = run_pasting(world, ["not-a-key-" + SECRET_PART])
    assert code == 3 and out[0].startswith("[blocked] union-keys")
    assert "not a Union API key" in out[0] and SECRET_PART not in "\n".join(out)
    assert world.secrets == {}


def test_a_secret_that_cannot_be_stored_leaves_the_pasted_key_the_persons_own(world):
    world.secret_error = PROJECT_SECRET
    code, out, _ = run_pasting(world, [pasted_key("client-project")])
    assert code == 3 and out[0].startswith("[blocked] union-keys")
    assert world.secrets == {} and world.union_keys == {}


def test_linux_with_the_plugin_makes_keys_instead_of_asking(world):
    code, out, prompts = run_pasting(world, [], platform="linux")
    assert code == 0, out
    assert prompts == [] and world.secrets == {PROJECT_SECRET: KEY_SECRET, DEPLOYER_SECRET: KEY_SECRET}


def test_macos_with_the_plugin_installed_still_pastes(world):
    """The plugin does not make a Mac a supported key maker: the paste path is the one that works there."""
    ctx = make_context(world, _settings(), [])
    ctx.platform = "darwin"
    assert isinstance(UnionKeysStep()._make_key_maker(ctx), PastedKeyMaker)
    ctx.platform = "linux"
    assert isinstance(UnionKeysStep()._make_key_maker(ctx), PluginKeyMaker)
    world.can_manage_keys = False
    assert isinstance(UnionKeysStep()._make_key_maker(ctx), PastedKeyMaker)


def _settings():
    from flyte_agent_factory._bootstrap_base import Settings

    return Settings(
        endpoint="flyte.example.test",
        project="my-agents",
        workspace="pat/agent-workspace",
        state_uri="s3://example-bucket/state",
    )


def test_the_step_on_an_open_source_backend_has_nothing_to_do(world):
    settings = _settings()
    settings.backend = "oss"
    check = UnionKeysStep().check(make_context(world, settings, []))
    assert check.ok and "no Union API keys" in check.detail


def test_the_union_keys_flag_fills_a_profile_that_lacks_the_names(world, tmp_path):
    clone = world.root / "clone"
    clone.mkdir()
    (clone / "profile.toml").write_text('name = "x"\n')
    code, out, _ = run_pasting(world, [], platform="linux")
    text = (clone / "profile.toml").read_text()
    assert (
        code == 0
        and f'project_key = "{PROJECT_SECRET}"' in text
        and f'deployer_key = "{DEPLOYER_SECRET}"' in text
    )
    assert "commit and push it" in "\n".join(out)
