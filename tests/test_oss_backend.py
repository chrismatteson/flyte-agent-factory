"""The profile-driven OSS backend: profile keys, endpoint, identity, image builder and the MCP's auth."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, cast

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import _deploy, _profile, _profile_oss, _profile_union, _union_identity
from tests.conftest import TEST_PROFILE

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_OSS = ROOT / "profiles" / "oss-example.toml"
ENDPOINT = "http://flyte-http.flyte.svc.cluster.local:8090"
INJECTED = "_UNION_EAGER_API_KEY"


def _set(text: str, section: str, lines: str, drop: tuple[str, ...] = ()) -> str:
    """Return `text` with `lines` added to `[section]`, replacing any line that sets one of their keys."""
    keys = {*drop, *re.findall(r"^(\w+) =", lines, re.MULTILINE)}
    for key in keys:
        text = re.sub(rf"^{key} = .*\n", "", text, flags=re.MULTILINE)
    return text.replace(f"[{section}]\n", f"[{section}]\n{lines}", 1)


def _union_text(backend: str = "", mcp: str = "") -> str:
    return _set(_set(TEST_PROFILE.read_text(), "backend", backend), "mcp", mcp)


def _oss_text(backend: str = "", mcp: str = "", drop: tuple[str, ...] = ()) -> str:
    return _set(_set(EXAMPLE_OSS.read_text(), "backend", backend, drop), "mcp", mcp)


def _oss(backend: str = "", mcp: str = "", drop: tuple[str, ...] = ()) -> _profile.Profile:
    return _profile.parse(_oss_text(backend, mcp, drop))


@pytest.fixture
def oss_pod(monkeypatch):
    """This process as a pod of the example OSS profile, with no cached identity."""
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(EXAMPLE_OSS))
    for var in (_union_identity.KEY_ENV, _union_identity.TIER_ENV, _union_identity.SECRET_ENV, INJECTED):
        monkeypatch.delenv(var, raising=False)
    _union_identity.current.cache_clear()
    yield
    _union_identity.current.cache_clear()


# --- profile keys -------------------------------------------------------------------------


def test_example_oss_parses_without_a_union_table():
    profile = _profile.load(str(EXAMPLE_OSS))
    assert profile.backend.kind == "oss"
    assert profile.backend.internal_endpoint == ENDPOINT
    assert profile.backend.insecure is True
    assert profile.union.secrets == frozenset()
    assert _profile_oss.keyless(profile)


def test_a_union_profile_has_the_old_defaults():
    profile = _profile.parse(_union_text())
    assert profile.backend.internal_endpoint == ""
    assert profile.backend.insecure is False
    assert _profile_oss.builder(profile.backend) == "remote"
    assert _profile_oss.mcp_auth(profile) == "union"
    assert not _profile_oss.keyless(profile)


def test_oss_defaults_are_the_local_builder_and_proxy_auth():
    profile = _oss()
    assert _profile_oss.builder(profile.backend) == "local"
    assert _profile_oss.mcp_auth(profile) == "proxy"


def test_the_builder_and_mcp_auth_can_be_set_either_way():
    assert _profile_oss.builder(_oss('image_builder = "remote"\n').backend) == "remote"
    assert _profile_oss.builder(_oss('image_builder = "local-podman"\n').backend) == "local-podman"
    assert _profile_oss.builder(_profile.parse(_union_text('image_builder = "local"\n')).backend) == "local"
    assert _profile_oss.mcp_auth(_oss(mcp='auth = "union"\n')) == "union"
    assert _profile_oss.mcp_auth(_profile.parse(_union_text(mcp='auth = "proxy"\n'))) == "proxy"


@pytest.mark.parametrize(
    ("backend_extra", "mcp", "key"),
    [
        ('image_builder = "kaniko"\n', "", "image_builder"),
        ('image_builder = ""\n', "", "image_builder"),
        ("image_builder = 3\n", "", "image_builder"),
        ('insecure = "yes"\n', "", "insecure"),
        ("insecure = 1\n", "", "insecure"),
        ("internal_endpoint = 8090\n", "", "internal_endpoint"),
        ("", 'auth = "basic"\n', "auth"),
        ("", "auth = true\n", "auth"),
        ("", 'auth = ""\n', "auth"),
    ],
)
def test_an_invalid_value_names_its_key(backend_extra, mcp, key):
    with pytest.raises(_profile.ProfileError, match=key):
        _oss(backend_extra, mcp)


def test_an_oss_profile_without_an_internal_endpoint_parses_but_cannot_build_pods():
    # Not a parse error: profiles written before the key (and their fixtures) must keep loading.
    profile = _oss(drop=("internal_endpoint",))
    assert profile.backend.internal_endpoint == ""
    with pytest.raises(_profile.ProfileError, match="internal_endpoint"):
        _profile_oss.endpoint_for_pods(profile.backend)
    with pytest.raises(_profile.ProfileError, match="internal_endpoint"):
        _union_identity.init_for_script(profile)


def test_the_new_keys_round_trip_through_the_deployment_text():
    profile = _oss('image_builder = "local-podman"\n', 'auth = "proxy"\n')
    again = _profile.parse(profile.deployment_text())
    assert again.backend == profile.backend
    assert again.mcp == profile.mcp


# --- pod_env ------------------------------------------------------------------------------


def test_pod_env_points_oss_pods_at_the_internal_endpoint(oss_pod):
    env = _profile.pod_env()
    assert env["_U_EP_OVERRIDE"] == ENDPOINT
    assert env["_U_INSECURE"] == "true"


def test_pod_env_leaves_out_insecure_unless_the_profile_asks(monkeypatch, tmp_path):
    path = tmp_path / "secure.toml"
    path.write_text(_oss_text().replace("insecure = true", "insecure = false"))
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(path))
    env = _profile.pod_env()
    assert env["_U_EP_OVERRIDE"] == ENDPOINT
    assert "_U_INSECURE" not in env


def test_pod_env_adds_nothing_on_union():
    env = _profile.pod_env()
    assert "_U_EP_OVERRIDE" not in env and "_U_INSECURE" not in env


# --- identity -----------------------------------------------------------------------------


@pytest.mark.parametrize("tier", _profile_union.TIERS)
def test_an_oss_pod_mounts_no_key_and_sets_no_auth(tier):
    profile = _oss()
    assert _union_identity.pod_spec(profile, tier) == ([], {})
    assert _union_identity.pod_for(profile, tier) == ([], {})


def test_an_oss_profile_that_still_lists_union_keys_keeps_using_them():
    profile = _profile.parse(
        _oss_text() + '\n[union]\nproject_key = "k-project"\ndeployer_key = "k-deployer"\n'
    )
    assert not _profile_oss.keyless(profile)
    secrets, env = _union_identity.pod_spec(profile, "project")
    assert [s.key for s in secrets] == ["k-project"]
    assert env["FLYTE_AUTH_TYPE"] == "ExternalCommand"


def test_establish_on_oss_returns_the_oss_identity_and_scrubs_the_injected_key(oss_pod, monkeypatch):
    monkeypatch.setenv(INJECTED, "injected")
    monkeypatch.delenv(_union_identity.KEY_ENV, raising=False)

    def no_probe(**_: Any) -> None:
        raise AssertionError("an OSS identity probes nothing")

    monkeypatch.setattr(remote.Run, "listall", no_probe)
    assert _union_identity.establish() is _union_identity.OSS_IDENTITY
    assert INJECTED not in os.environ
    assert _union_identity.current() is _union_identity.OSS_IDENTITY
    assert _union_identity.OSS_IDENTITY.secret == ""


def test_establish_on_union_still_refuses_a_pod_without_its_key_and_scrubs_the_injected_one(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(TEST_PROFILE))
    for var in (_union_identity.KEY_ENV, _union_identity.TIER_ENV, _union_identity.SECRET_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(INJECTED, "injected")
    with pytest.raises(_union_identity.IdentityMissing):
        _union_identity.establish()
    assert INJECTED not in os.environ


def test_establish_with_no_profile_still_refuses(monkeypatch):
    monkeypatch.setattr(_profile, "current", lambda: None)
    monkeypatch.delenv(_union_identity.KEY_ENV, raising=False)
    monkeypatch.setenv(INJECTED, "injected")
    with pytest.raises(_union_identity.IdentityMissing):
        _union_identity.establish()
    assert INJECTED not in os.environ


def test_describe_lists_no_tiers_on_oss():
    assert _union_identity.describe(_oss()) == []


def test_controller_kwargs_on_oss_passes_the_endpoint_explicitly(oss_pod, monkeypatch):
    seen: dict[str, Any] = {}

    def init_in_cluster(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"endpoint": kwargs["endpoint"]}

    monkeypatch.setattr(flyte, "init_in_cluster", init_in_cluster)
    assert _union_identity.controller_kwargs() == {"endpoint": ENDPOINT}
    assert seen == {"endpoint": ENDPOINT, "insecure": True}


@pytest.fixture
def sdk(monkeypatch):
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    for name in ("init", "init_from_config", "init_from_api_key"):
        monkeypatch.setattr(flyte, name, lambda *a, _n=name, **k: calls.append((_n, a, k)))
    return calls


def test_init_for_script_on_oss_without_a_config_connects_to_the_endpoint(sdk):
    profile = _oss(drop=("config",))
    _union_identity.init_for_script(profile, project="p", domain="d", root_dir="r", image_builder="local")
    assert sdk == [
        (
            "init",
            (),
            {
                "endpoint": ENDPOINT,
                "insecure": True,
                "project": "p",
                "domain": "d",
                "root_dir": "r",
                "image_builder": "local",
            },
        )
    ]


def test_init_for_script_on_oss_ignores_a_key_variable(sdk, monkeypatch):
    monkeypatch.setenv("SOME_KEY_VAR", "not-a-key")
    _union_identity.init_for_script(_oss(drop=("config",)), key_env="SOME_KEY_VAR")
    assert [name for name, _, _ in sdk] == ["init"]


def test_init_for_script_on_oss_with_a_config_uses_it(sdk, monkeypatch):
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)  # on a laptop
    profile = _oss()  # the example names a config file
    assert profile.backend.config
    _union_identity.init_for_script(profile)
    [(name, args, kwargs)] = sdk
    assert name == "init_from_config" and args == (str(Path(profile.backend.config).expanduser()),)
    assert kwargs["project"] == profile.backend.project


def test_init_for_script_on_oss_in_a_pod_connects_to_the_endpoint_despite_a_laptop_config(sdk, monkeypatch):
    # The deployer pod has the profile, but not the laptop's config file it names.
    monkeypatch.setenv("FLYTE_INTERNAL_EXECUTION_ID", "r1")
    profile = _oss()
    assert profile.backend.config
    _union_identity.init_for_script(profile)
    [(name, _, kwargs)] = sdk
    assert name == "init" and kwargs["endpoint"] == ENDPOINT and kwargs["insecure"] is True


def test_a_broken_profile_leaves_establish_failing_closed_on_union(monkeypatch, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[backend]\nkind = 'nonsense'\n")
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(bad))
    with pytest.raises(_profile.ProfileError):
        _profile.current()
    assert _union_identity._load_profile_or_none() is None
    for name in ("_UNION_EAGER_API_KEY", "EAGER_API_KEY"):
        monkeypatch.setenv(name, "injected")
    monkeypatch.delenv(_union_identity.KEY_ENV, raising=False)  # no tier key: the injected one is not used
    with pytest.raises(_union_identity.IdentityMissing):
        _union_identity.establish()
    assert not {"_UNION_EAGER_API_KEY", "EAGER_API_KEY"} & set(os.environ)


def test_init_for_script_on_union_is_unchanged(sdk, monkeypatch):
    profile = _profile.parse(_union_text())
    monkeypatch.setenv("SOME_KEY_VAR", "k")
    _union_identity.init_for_script(profile, key_env="SOME_KEY_VAR")
    [(name, _, kwargs)] = sdk
    assert name == "init_from_api_key" and kwargs["api_key"] == "k"


# --- image builder ------------------------------------------------------------------------


def _builder_the_init_asks_for(monkeypatch, profile_path: Path, tmp_path: Path) -> str:
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(profile_path))
    monkeypatch.setattr(sys, "argv", ["child", "", "", "proj", "dom"])
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        _union_identity, "init_for_script", lambda profile, key_env="", **kwargs: seen.update(kwargs)
    )
    scope: dict[str, Any] = {"os": os, "sys": sys, "root": tmp_path}
    exec(_deploy._INIT, scope)  # the code a deploy child runs, as it runs it
    return cast(str, seen["image_builder"])


def test_the_deploy_child_takes_its_image_builder_from_the_profile(monkeypatch, tmp_path):
    assert 'image_builder="remote"' not in _deploy._INIT
    assert _builder_the_init_asks_for(monkeypatch, TEST_PROFILE, tmp_path) == "remote"
    assert _builder_the_init_asks_for(monkeypatch, EXAMPLE_OSS, tmp_path) == "local"
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(_oss_text('image_builder = "local-podman"\n'))
    assert _builder_the_init_asks_for(monkeypatch, explicit, tmp_path) == "local-podman"


# --- the MCP app --------------------------------------------------------------------------


@pytest.fixture
def mcp_under(monkeypatch):
    """Import `_factory_mcp` under a profile; the modules that read the profile at import are put back."""
    import flyte_agent_factory

    names: tuple[str, ...] = (
        "_factory",
        "_factory_mcp",
        "_factory_base",
        "_factory_meters",
        "_factory_policy",
    )
    names += ("_factory_recreate", "_factory_status")
    before = {n: sys.modules.pop(f"flyte_agent_factory.{n}", None) for n in names}
    for name in names:
        monkeypatch.delattr(flyte_agent_factory, name, raising=False)

    def load(profile_path: Path):
        monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(profile_path))
        monkeypatch.delenv("FACTORY_MCP_PUBLIC_URL", raising=False)
        from flyte_agent_factory import _factory_mcp

        return _factory_mcp

    yield load
    for name, module in before.items():
        sys.modules.pop(f"flyte_agent_factory.{name}", None)
        if hasattr(flyte_agent_factory, name):
            delattr(flyte_agent_factory, name)
        if module is not None:
            sys.modules[f"flyte_agent_factory.{name}"] = module
            setattr(flyte_agent_factory, name, module)


def test_the_mcp_app_trusts_its_proxy_on_oss(mcp_under):
    app = mcp_under(EXAMPLE_OSS).app
    assert app.requires_auth is False
    assert app.resources == flyte.Resources()
    assert not app.secrets
    assert app.env_vars is not None and app.env_vars["_U_EP_OVERRIDE"] == ENDPOINT


def test_an_empty_resources_leaves_every_request_unset():
    from flyte._internal.runtime.resources_serde import get_proto_resources

    assert get_proto_resources(flyte.Resources()) is None


def test_the_mcp_app_keeps_union_auth_and_the_profile_resources_on_union(mcp_under):
    module = mcp_under(TEST_PROFILE)
    app = module.app
    assert app.requires_auth is True
    # Since #15 the `mcp` component's requests come from the profile's [resources] table on every backend.
    assert app.resources == module._resources.for_component(module._profile.current(), "mcp").to_flyte()
    assert [s.key for s in cast(list[flyte.Secret], app.secrets)] == ["factory-project-key"]


def test_mcp_auth_can_override_the_kind(mcp_under, tmp_path):
    path = tmp_path / "oss-union-auth.toml"
    path.write_text(_oss_text(mcp='auth = "union"\n'))
    assert mcp_under(path).app.requires_auth is True


# --- end to end, offline ------------------------------------------------------------------


def test_the_example_oss_profile_deploys_offline_without_keys(mcp_under, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(EXAMPLE_OSS))
    profile = _profile.require()
    assert _profile.pod_env()["_U_EP_OVERRIDE"] == ENDPOINT
    for tier in _profile_union.TIERS:
        assert _union_identity.pod_for(profile, tier) == ([], {})
    assert _builder_the_init_asks_for(monkeypatch, EXAMPLE_OSS, tmp_path) == "local"
    app = mcp_under(EXAMPLE_OSS).app
    assert app.requires_auth is False
    assert app.resources == flyte.Resources()
    assert app.resources is not None
    assert (app.resources.cpu, app.resources.memory) == (None, None)


# --- environments that get the endpoint without the profile text ---------------------------


def test_endpoint_env_is_oss_only_and_leaves_out_a_missing_endpoint():
    assert _profile_oss.endpoint_env(_oss()) == {"_U_EP_OVERRIDE": ENDPOINT, "_U_INSECURE": "true"}
    assert _profile_oss.endpoint_env(_profile.parse(_union_text())) == {}
    assert _profile_oss.endpoint_env(_oss(drop=("internal_endpoint",))) == {}


def test_a_personal_browser_app_reaches_flyte_through_the_internal_endpoint():
    from flyte_agent_factory import browser

    app = browser.personal_app(_oss(), _profile.BrowserIdentity("me", "s3://bucket/profile"), "")
    assert app.env_vars is not None
    assert app.env_vars["_U_EP_OVERRIDE"] == ENDPOINT


# --- the documented path, offline ---------------------------------------------------------


def test_the_documented_oss_path_end_to_end(mcp_under, monkeypatch, tmp_path):
    """docs/oss-flyte-compat.md "Running the factory on OSS Flyte", step by step, with no cluster."""
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(EXAMPLE_OSS))
    profile = _profile.require()
    assert profile.backend.kind == "oss"
    env = _profile.pod_env()
    assert env["_U_EP_OVERRIDE"] == ENDPOINT
    assert env["_U_INSECURE"] == "true"
    for tier in _profile_union.TIERS:
        assert _union_identity.pod_for(profile, tier) == ([], {})
    assert _profile_oss.builder(profile.backend) == "local"
    assert _builder_the_init_asks_for(monkeypatch, EXAMPLE_OSS, tmp_path) == "local"
    app = mcp_under(EXAMPLE_OSS).app
    assert app.requires_auth is False
    assert app.resources == flyte.Resources()
    assert app.resources is not None
    assert (app.resources.cpu, app.resources.memory) == (None, None)


# --- the docs cannot drift from the example ------------------------------------------------

COMPAT = ROOT / "docs" / "oss-flyte-compat.md"
NEW_KEYS = ("internal_endpoint", "insecure", "image_builder", "auth")


def _oss_section() -> str:
    text = COMPAT.read_text()
    start = text.index("## Running the factory on OSS Flyte")
    return text[start : text.index("\n## ", start + 1)]


def _toml_blocks(section: str) -> list[dict[str, Any]]:
    import tomllib

    return [tomllib.loads(block) for block in re.findall(r"```toml\n(.*?)```", section, re.DOTALL)]


def test_the_compat_doc_has_the_oss_section_near_the_top():
    text = COMPAT.read_text()
    assert text.index("## Running the factory on OSS Flyte") < text.index("## Evidence and scope")
    assert text.index("## Running the factory on OSS Flyte") < text.index("## Feature matrix")


def test_the_oss_section_names_every_new_key_and_the_proxy_warning():
    section = _oss_section()
    for key in NEW_KEYS:
        assert f"`[backend] {key}`" in section or f"`[mcp] {key}`" in section, key
    assert "**It MUST sit behind an authenticating (OAuth) proxy and must never be exposed on its own:**" in (
        section
    )
    assert "endpoint_for_pods" in section  # when `internal_endpoint` is enforced
    secrets = _profile.load(str(EXAMPLE_OSS)).secrets  # the secret names the example profile asks for
    for name in (secrets.claude_token, secrets.deploy_key):
        assert f"create secret {name} " in section, name
    for script in ("ops/deploy_work.py", "ops/deploy_factory.py"):
        assert script in section


def _quickstart_oss_section() -> str:
    return (ROOT / "docs" / "quickstart.md").read_text().split("### On OSS Flyte")[1].split("\n## ")[0]


@pytest.mark.parametrize("where", ["compat", "quickstart"])
def test_the_oss_section_snippet_is_what_the_example_profile_says(where):
    import tomllib

    example = tomllib.loads(EXAMPLE_OSS.read_text())
    section = _oss_section() if where == "compat" else _quickstart_oss_section()
    (snippet,) = _toml_blocks(section)
    for table in ("backend", "mcp"):
        for key, value in snippet[table].items():
            assert example[table][key] == value, f"[{table}] {key}"
    # Every key the example sets under [backend] is shown, so the doc cannot lose one; it sets all new keys.
    assert set(snippet["backend"]) == set(example["backend"])
    assert {"internal_endpoint", "insecure", "image_builder"} <= set(example["backend"])
    assert set(snippet["mcp"]) >= {"auth", "public_url"}
    # And the snippet's tables load as a profile.
    assert _oss(backend="", mcp="").backend.internal_endpoint == snippet["backend"]["internal_endpoint"]


def test_the_matrix_rows_for_the_oss_changes_cite_their_code():
    text = COMPAT.read_text()
    rows = {
        "| Remote image builder |": "_deploy._INIT",
        "| Local builder": "_profile_oss.py:builder",
        "| Union API keys and endpoint selection |": "controller_kwargs",
        "| Child actions via `child.attach()` |": "_union_identity.controller_kwargs",
        "| Factory MCP server and caller identity |": "_factory_mcp.app",
        "| Label-filtered run listing |": "_factory_base.py:list_runs",
    }
    (header,) = [line for line in text.splitlines() if line.startswith("| Feature |")]
    width = len(header.strip().strip("|").split("|"))
    for prefix, cite in rows.items():
        (row,) = [line for line in text.splitlines() if line.startswith(prefix)]
        cells = row.strip().strip("|").split("|")
        # A row wider than the header loses its extra cells when rendered, the cite with them.
        assert len(cells) == width, prefix
        assert cite in row, prefix


# --- every environment that reaches Flyte gets the endpoint -------------------------------


def test_every_environment_that_reaches_flyte_gets_the_oss_endpoint():
    import ast

    src = ROOT / "src"
    missing = []
    for path in sorted(src.rglob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.keyword) and node.arg == "env_vars"):
                continue
            text = ast.get_source_segment(source, node.value) or ""
            if "pod_env" in text or "endpoint_env" in text:
                continue
            missing.append(f"{path.relative_to(src)}:{node.value.lineno}")
    # Left out on purpose: the probe/doctor environments that run a shell script and never call Flyte, and
    # `native_child_probe`'s leaf `child_env` (they have no `env_vars=` at all, so are not listed here).
    assert missing == [], f"env_vars without the OSS endpoint: {missing}"


def test_the_secrets_the_oss_docs_create_are_exactly_the_ones_the_example_deploy_mounts():
    from flyte_agent_factory import _deploy_child

    profile = _profile.load(str(EXAMPLE_OSS))
    planned = _deploy_child.collect_planned_secrets(profile, ROOT / "src", dict(os.environ))
    mounted = {name for names in planned.values() for name in names}
    sections = {
        "compat": COMPAT.read_text().split("### 2. Secrets on the OSS cluster")[1].split("### 3.")[0],
        "quickstart": _quickstart_oss_section(),
    }
    for where, section in sections.items():
        assert set(re.findall(r"create secret (\S+) ", section)) == mounted, where


def test_the_bootstrap_on_oss_points_at_the_documented_hand_deploy():
    from flyte_agent_factory._bootstrap_finish import _GAP

    assert "docs/oss-flyte-compat.md" in _GAP and "Deploy by hand" in _GAP
    assert '"Running the factory on OSS Flyte"' in _GAP
    assert "## Running the factory on OSS Flyte" in COMPAT.read_text()


_POD_MODULES = (
    "_factory_mcp",
    "agent",
    "_deployer",
    "_webhook",
    "browser",
    "voice",
    "_bench_flow",
    "_loop_flow",
    "_reconcile",
    "_factory",
)

# Run in a fresh interpreter: these modules read the profile once, at import, and the suite's own profile is
# already imported here.
_IMPORT_UNDER_OSS = """
import importlib, json, sys
import flyte
found = {}
for name in sys.argv[1:]:
    module = importlib.import_module("flyte_agent_factory." + name)
    for attr, value in vars(module).items():
        if isinstance(value, flyte.Environment):
            found[f"{name}.{attr}"] = {
                "env": dict(getattr(value, "env_vars", None) or {}),
                "secrets": [str(s) for s in (getattr(value, "secrets", None) or [])],
            }
print(json.dumps(found))
"""


def test_every_pod_module_imports_under_the_oss_example_and_its_environments_reach_flyte():
    import json
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FACTORY_PROFILE")}
    env.update(AGENT_FACTORY_PROFILE=str(EXAMPLE_OSS), PYTHONPATH=str(ROOT / "src"))
    done = subprocess.run(
        [sys.executable, "-c", _IMPORT_UNDER_OSS, *_POD_MODULES],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    found = json.loads(done.stdout.strip().splitlines()[-1])
    assert {name.split(".")[0] for name in found} == set(_POD_MODULES)  # each defines one at least
    profile = _profile.load(str(EXAMPLE_OSS))
    for name, seen in found.items():
        assert seen["env"].get("_U_EP_OVERRIDE") == profile.backend.internal_endpoint, name
        assert seen["env"].get("_U_INSECURE") == "true", name
        assert "FLYTE_AUTH_TYPE" not in seen["env"], name
        assert not any("UNION_KEY" in s for s in seen["secrets"]), name


def test_the_profile_schema_table_names_every_new_key():
    rows = (ROOT / "profiles" / "README.md").read_text().splitlines()
    for table, key in (
        ("backend", "internal_endpoint"),
        ("backend", "insecure"),
        ("backend", "image_builder"),
        ("mcp", "auth"),
    ):
        assert any(r.startswith(f"| `[{table}]` `{key}` |") for r in rows), key
    (kind,) = [r for r in rows if r.startswith("| `[backend]` `kind` |")]
    assert "Recorded for docs" not in kind


def test_the_deploy_tells_how_to_connect_to_the_mcp_app_for_its_auth():
    child = _deploy.FACTORY_CHILD
    assert '_profile_oss.mcp_auth(_profile.require()) == "proxy"' in child
    assert "authenticating proxy" in child and "Bearer <Union API key" in child
