"""The bootstrap's state bucket: found from a flag, the environment or Terraform, and never Flyte's own."""

from __future__ import annotations

import json
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from flyte_agent_factory import _bootstrap, _bootstrap_state, _profile
from flyte_agent_factory._bootstrap_base import BootstrapError
from tests._bootstrap_fakes import CLAUDE_TOKEN, World, make_context
from tests.test_bootstrap import ARGS, person, run  # noqa: F401 -- `person` is the autouse home fixture

FLYTE = "gs://flyte-data/raw"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_bootstrap_state.ENV_BUCKET, raising=False)


def _profile_text(world: World) -> str:
    return str((world.root / "clone/profile.toml").read_text())


def _state(world: World) -> dict[str, object]:
    state: dict[str, object] = tomllib.loads(_profile_text(world)).get("state", {})
    return state


def _fake_terraform(
    tmp_path: Path, monkeypatch, outputs: Mapping[str, object] | None, *, code: int = 0
) -> Path:
    """Put a `terraform` on PATH that prints `outputs`; it records its arguments, and `apply` would fail."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "terraform-calls"
    script = bin_dir / "terraform"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\n'
        f"case \"$*\" in *apply*) exit 99;; esac\ncat <<'EOF'\n{json.dumps(outputs)}\nEOF\nexit {code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return calls


def test_a_state_bucket_flag_goes_into_the_profile(world):
    code, out = run(
        world, "--no-app", "--state-bucket", "s3://acme-factory/prod", "--state-region", "eu-west-1"
    )
    assert code == 0, out
    assert _state(world) == {"bucket": "s3://acme-factory/prod", "region": "eu-west-1"}
    _profile.load(str(world.root / "clone/profile.toml"))  # and it is a profile the engine loads
    assert any("versioning" in line for line in out)


def test_the_environment_names_the_bucket(world, monkeypatch):
    monkeypatch.setenv(_bootstrap_state.ENV_BUCKET, "gs://acme-factory/prod")
    code, out = run(world, "--no-app")
    assert code == 0
    assert _state(world)["bucket"] == "gs://acme-factory/prod"
    assert any("from AGENT_FACTORY_STATE_BUCKET" in line for line in out)


def test_the_flag_wins_over_the_environment(world, monkeypatch):
    monkeypatch.setenv(_bootstrap_state.ENV_BUCKET, "s3://from-env/x")
    run(world, "--no-app", "--state-bucket", "s3://from-flag/x")
    assert _state(world)["bucket"] == "s3://from-flag/x"


def test_terraform_output_is_read_only_when_asked_for(world, tmp_path, monkeypatch):
    outputs = {
        "factory_bucket": {"value": "s3://acme-factory", "type": "string"},
        "factory_bucket_region": {"value": "eu-west-1", "type": "string"},
        "factory_bucket_endpoint": {"value": "http://localhost:9000", "type": "string"},
        "unrelated": {"value": "x"},
    }
    calls = _fake_terraform(tmp_path, monkeypatch, outputs)
    code, out = run(world, "--no-app", "--terraform-dir", str(tmp_path / "tf"))
    assert code == 0, out
    assert _state(world) == {
        "bucket": "s3://acme-factory",
        "region": "eu-west-1",
        "endpoint": "http://localhost:9000",
    }
    assert calls.read_text().strip() == f"-chdir={tmp_path / 'tf'} output -json"  # nothing but `output`


def test_terraform_service_accounts_go_into_the_profile(world, tmp_path, monkeypatch):
    outputs = {
        "factory_bucket": {"value": "s3://acme-factory"},
        "factory_service_accounts": {
            "value": {"factory": {"name": "flyte-factory"}, "browser": {"name": "flyte-browser"}}
        },
    }
    _fake_terraform(tmp_path, monkeypatch, outputs)
    code, out = run(world, "--no-app", "--terraform-dir", str(tmp_path / "tf"))
    assert code == 0, out
    assert _state(world) == {
        "bucket": "s3://acme-factory",
        "factory_service_account": "flyte-factory",
        "browser_service_account": "flyte-browser",
    }
    state = _profile.load(str(world.root / "clone/profile.toml")).state
    assert (state.factory_service_account, state.browser_service_account) == (
        "flyte-factory",
        "flyte-browser",
    )


def test_a_service_account_flag_wins_over_terraform(world, tmp_path, monkeypatch):
    outputs = {
        "factory_bucket": {"value": "s3://acme-factory"},
        "factory_service_accounts": {"value": {"factory": {"name": "from-terraform"}}},
    }
    _fake_terraform(tmp_path, monkeypatch, outputs)
    code, out = run(
        world,
        *("--no-app", "--terraform-dir", str(tmp_path / "tf")),
        *("--factory-service-account", "from-flag", "--browser-service-account", "flag-browser"),
    )
    assert code == 0, out
    assert _state(world)["factory_service_account"] == "from-flag"
    assert _state(world)["browser_service_account"] == "flag-browser"


def test_terraform_is_not_run_without_the_flag(world, tmp_path, monkeypatch):
    calls = _fake_terraform(tmp_path, monkeypatch, {"factory_bucket": {"value": "s3://acme-factory"}})
    run(world, "--no-app")
    assert not calls.exists()
    assert _state(world) == {}


@pytest.mark.parametrize(
    ("outputs", "code", "why"),
    [
        ({"other": {"value": "x"}}, 0, "no factory_bucket"),
        ({"factory_bucket": {"value": "s3://b"}}, 1, "failed"),
        (None, 0, "not a dict"),
    ],
)
def test_without_a_usable_terraform_output_the_git_store_is_kept_and_one_line_says_so(
    world, tmp_path, monkeypatch, outputs, code, why
):
    _fake_terraform(tmp_path, monkeypatch, outputs, code=code)
    status, out = run(world, "--no-app", "--terraform-dir", str(tmp_path / "tf"))
    assert status == 0, out
    assert _state(world) == {}
    lines = [line for line in out if line.startswith("state:")]
    assert len(lines) == 1 and "recommended for production" in lines[0] and "--state-bucket" in lines[0]


def test_no_bucket_found_says_how_to_add_one(world):
    _, out = run(world, "--no-app")
    (line,) = [line for line in out if line.startswith("state:")]
    assert "git branch" in line and "recommended for production" in line
    assert "state" not in tomllib.loads(_profile_text(world))


def test_a_missing_terraform_is_the_same_one_line(world, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it
    _, out = run(world, "--no-app", "--terraform-dir", str(tmp_path))
    (line,) = [line for line in out if line.startswith("state:")]
    assert "terraform is not installed" in line


@pytest.mark.parametrize(
    ("bucket", "flyte"),
    [
        ("gs://flyte-data/state", FLYTE),
        ("gs://flyte-data", FLYTE),
        ("gs://flyte-data/raw/deeper", FLYTE),
        ("gs://flyte-data/state", "gcs://flyte-data/raw"),  # scheme alias
        ("s3://flyte-data/state", "s3a://flyte-data"),
    ],
)
def test_the_state_bucket_may_not_be_flytes(world, bucket, flyte, capsys):
    with pytest.raises(SystemExit) as refused:
        _bootstrap.main(
            [*ARGS, "--no-app", "--dir", str(world.root / "clone"), "--state-bucket", bucket]
            + ["--flyte-bucket", flyte],
            make_context=lambda s: make_context(world, s, [], CLAUDE_TOKEN),
        )
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "Flyte's own data bucket" in err
    assert not (world.root / "clone/profile.toml").exists()


def test_a_different_bucket_passes_and_flytes_is_recorded(world):
    code, out = run(world, "--no-app", "--state-bucket", "gs://acme-factory/prod", "--flyte-bucket", FLYTE)
    assert code == 0, out
    assert _state(world) == {"bucket": "gs://acme-factory/prod", "flyte_bucket": [FLYTE]}
    with pytest.raises(_profile.ProfileError, match="Flyte's own data bucket"):
        _profile.parse(_profile_text(world).replace("gs://acme-factory/prod", "gs://flyte-data/x"), "t")


def test_flytes_bucket_is_read_from_the_flyte_config_when_it_names_one(world, tmp_path):
    config = tmp_path / "flyte-config.yaml"
    config.write_text(f"admin:\n  endpoint: dns:///x\nraw_data_path: {FLYTE}\n")
    assert _bootstrap_state.find_flyte_buckets(config) == (FLYTE,)
    assert _bootstrap_state.find_flyte_buckets(tmp_path / "missing.yaml", [FLYTE, FLYTE]) == (FLYTE,)


def test_a_bucket_with_credentials_is_refused_without_echoing_it(world, capsys):
    with pytest.raises(SystemExit):
        _bootstrap.main(
            [*ARGS, "--dir", str(world.root / "clone"), "--state-bucket", "s3://KEYID:SECRET@bucket/x"],
            make_context=lambda s: make_context(world, s, [], CLAUDE_TOKEN),
        )
    assert "SECRET" not in capsys.readouterr().err


def test_a_profile_that_chose_a_backend_is_not_given_a_bucket(world):
    run(world, "--no-app")
    path = world.root / "clone/profile.toml"
    path.write_text(
        path.read_text() + '\n[state]\nbackend = "volume"\nvolume = "/data/state"\nvolume_claim = "c"\n'
    )
    run(world, "--no-app", "--state-bucket", "s3://acme-factory/prod", "--flyte-bucket", FLYTE)
    state = tomllib.loads(path.read_text())["state"]
    assert state["backend"] == "volume" and "bucket" not in state
    assert state["flyte_bucket"] == [FLYTE]
    _profile.load(str(path))


@pytest.mark.parametrize("extra", [[], ["--state-bucket", "gs://acme/x"]])
def test_a_bare_flyte_bucket_is_refused_at_bootstrap(world, extra, capsys):
    with pytest.raises(SystemExit) as refused:
        _bootstrap.main(
            [*ARGS, "--no-app", "--dir", str(world.root / "clone"), *extra, "--flyte-bucket", "flyte-data"],
            make_context=lambda s: make_context(world, s, [], CLAUDE_TOKEN),
        )
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "--flyte-bucket" in err and "scheme://bucket" in err
    assert not (world.root / "clone/profile.toml").exists()
    with pytest.raises(BootstrapError):
        _bootstrap_state.check_state_bucket(
            _bootstrap_state.StateBucket("s3://flyte-data/factory"), ("flyte-data",)
        )


def test_a_raw_data_path_without_a_scheme_in_the_config_is_dropped(tmp_path):
    config = tmp_path / "flyte-config.yaml"
    config.write_text("raw_data_path: /var/flyte/raw\n")
    assert _bootstrap_state.find_flyte_buckets(config) == ()
