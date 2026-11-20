"""A factory deploy leaves `factory-mcp` running when what it would run did not change. Offline.

The chat connector drops whenever the app restarts; its spec differs on every deploy by the code bundle's
`--tgz` and `--version` and the deploy's `--image-cache`, so the digest of its code and a comparison without
those three decide.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyteidl2.app import app_definition_pb2 as pb  # type: ignore[import-untyped]  # no py.typed marker

from flyte_agent_factory import _code_closure, _deploy_apps

SRC = Path(_code_closure.__file__).parent


SLOT_MODULES = ("agent", "_supervise", "_review", "_pre_review", "_findings", "_subagent_run")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    target = tmp_path / "flyte_agent_factory"
    shutil.copytree(SRC, target, ignore=shutil.ignore_patterns("__pycache__", "_assets"))
    return target


def digest(package: Path, module: str = "_factory_mcp") -> str:
    return _code_closure.hash_import_closure(package, module)


def append(package: Path, module: str) -> None:
    with (package / f"{module}.py").open("a") as f:
        f.write("\n# changed\n")


def test_the_closure_of_factory_mcp_leaves_out_the_slot_code():
    names = {p.stem for p in _code_closure.find_import_closure(SRC, "_factory_mcp")}
    assert {"_factory_mcp", "_factory", "__init__"} <= names
    for slot in SLOT_MODULES:
        assert slot not in names


def test_the_closure_of_factory_webhook_leaves_out_the_slot_code():
    names = {p.stem for p in _code_closure.find_import_closure(SRC, "_webhook")}
    assert {"_webhook", "__init__"} <= names
    for slot in SLOT_MODULES:
        assert slot not in names


@pytest.fixture(scope="module", params=["_factory_mcp", "_webhook"])
def app_module(request) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def before(app_module) -> str:
    """The digest of the real package; the copies in `package` are equal to it (only `.py` files count)."""
    return digest(SRC, app_module)


@pytest.mark.parametrize("module", ["agent", "_supervise", "_review"])
def test_a_change_to_slot_code_keeps_the_digest(package, before, app_module, module):
    append(package, module)
    assert digest(package, app_module) == before


def test_a_change_to_the_closure_changes_the_digest(package, before, app_module):
    for module in (app_module, "__init__"):
        append(package, module)
        assert digest(package, app_module) != before
        shutil.copy(SRC / f"{module}.py", package / f"{module}.py")
    assert digest(package, app_module) == before


def test_function_level_and_absolute_imports_are_followed_and_type_checking_ones_are_not(tmp_path):
    pkg = tmp_path / "flyte_agent_factory"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "top.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "from . import rel\n"
        "import flyte_agent_factory.absolute\n"
        "from flyte_agent_factory.fromabs import x\n"
        "if TYPE_CHECKING:\n    from . import hidden\nelse:\n    from . import other\n"
        "def f():\n    from .late import y\n"
    )
    for name in ("rel", "absolute", "fromabs", "late", "hidden", "other", "unrelated"):
        (pkg / f"{name}.py").write_text("")
    names = {p.stem for p in _code_closure.find_import_closure(pkg, "top")}
    assert names == {"__init__", "top", "rel", "absolute", "fromabs", "late", "other"}


def test_an_init_import_is_followed(tmp_path):
    pkg = tmp_path / "flyte_agent_factory"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from . import eager\n")
    (pkg / "top.py").write_text("")
    for name in ("eager", "unrelated"):
        (pkg / f"{name}.py").write_text("")
    names = {p.stem for p in _code_closure.find_import_closure(pkg, "top")}
    assert names == {"__init__", "top", "eager"}


def test_an_import_only_under_type_checking_is_not_followed(package):
    (package / "_factory_mcp.py").write_text(
        (package / "_factory_mcp.py").read_text()
        + "\nif TYPE_CHECKING:\n    from . import _supervise  # noqa: F401\n"
    )
    before = digest(package)
    append(package, "_supervise")
    assert digest(package) == before


# --- `is_unchanged` ---------------------------------------------------------------------------------------

NAME = "factory-mcp"


def make_spec(
    *,
    tgz: str = "t1",
    version: str = "v1",
    cache: str = "c1",
    image: str = "img",
    cpu: str = "1",
    env: str = "d1",
    **_: Any,
) -> Any:
    spec = pb.Spec(desired_state=pb.Spec.DESIRED_STATE_ACTIVE)
    spec.ingress.private = False
    spec.creator.SetInParent()
    spec.container.image = image
    spec.container.command.extend(
        ["fserve", "--version", version, "--project", "p", "--image-cache", cache]
        + ["--tgz", tgz, "--dest", "."]
    )
    spec.container.args.extend(["--version", version, "x"])
    spec.container.resources.limits.add().value = cpu
    spec.container.env.add(key=_deploy_apps.CODE_DIGEST_ENV, value=env)
    return spec


class FakeApps:
    """`flyte.deploy` (dry run) and `remote.App` for one app: the `deployed` spec, and the `new` one."""

    def __init__(
        self, deployed: Any | None, new: Any, *, active: bool = True, error: Exception | None = None
    ):
        self.deployed, self.new, self.active, self.error = deployed, new, active, error
        self.deploys: list[bool] = []

    def deploy(self, env: Any, dry_run: bool = False) -> list[Any]:
        self.deploys.append(dry_run)
        planned = SimpleNamespace(
            get_name=lambda: env.name, deployed_app=SimpleNamespace(pb2=SimpleNamespace(spec=self.new))
        )
        return [SimpleNamespace(envs={env.name: planned})]

    def get(self, name: str) -> Any:
        if self.error:
            raise self.error
        if self.deployed is None:
            raise ConnectError(Code.NOT_FOUND, "no such app")
        return SimpleNamespace(pb2=SimpleNamespace(spec=self.deployed), is_active=lambda: self.active)

    async def _app_specs_are_equal(self, old: Any, new: Any) -> bool:
        return bool(old == new)


def check(apps: FakeApps) -> bool:
    return _deploy_apps.is_unchanged(SimpleNamespace(name=NAME), deploy=apps.deploy, app_cls=apps)


def test_a_spec_that_differs_only_in_the_bundle_is_unchanged():
    assert check(FakeApps(make_spec(), make_spec(tgz="t2", version="v2")))


def test_a_spec_that_differs_only_in_the_image_cache_is_unchanged():
    # The deployed spec's cache holds every environment of the deploy it shipped in (and their build runs); a
    # dry run of factory-mcp alone holds one. Its own image is `container.image`, compared above.
    assert check(FakeApps(make_spec(), make_spec(cache="c2")))


@pytest.mark.parametrize("change", [{"env": "d2"}, {"image": "other"}, {"cpu": "2"}])
def test_a_real_difference_is_a_change(change):
    assert not check(FakeApps(make_spec(), make_spec(tgz="t2", version="v2", **change)))


def test_an_inactive_app_is_not_unchanged():
    assert not check(FakeApps(make_spec(), make_spec(), active=False))


def test_a_missing_app_is_not_unchanged():
    assert not check(FakeApps(None, make_spec()))


@pytest.mark.parametrize("error", [ConnectError(Code.UNAVAILABLE, "down"), OSError("reset")])
def test_a_lookup_error_is_not_unchanged_and_does_not_raise(error):
    assert not check(FakeApps(make_spec(), make_spec(), error=error))


def test_the_creator_is_the_deployed_ones():
    deployed = make_spec()
    deployed.creator.user.spec.email = "someone@example.com"
    assert check(FakeApps(deployed, make_spec(tgz="t2")))


# --- factory-mcp's real spec, as Flyte serializes and compares it ----------------------------------------


def real_spec(tgz: str, version: str, images: dict[str, str]) -> Any:
    from flyte._internal.image_cache import ImageCache
    from flyte.app._runtime.app_serde import translate_app_env_to_idl
    from flyte.models import CodeBundle, SerializationContext

    from flyte_agent_factory import _factory_mcp

    context = SerializationContext(
        project="p",
        domain="d",
        org="o",
        version=version,
        code_bundle=CodeBundle(tgz=tgz, destination=".", computed_version=version),
        image_cache=ImageCache(image_lookup=images),
        root_dir=SRC.parent,
    )
    return translate_app_env_to_idl(_factory_mcp.app, context).spec


def flyte_equal(old: Any, new: Any) -> bool:
    from flyte.remote import App

    return bool(asyncio.run(App._app_specs_are_equal(old, new)))


@pytest.fixture(params=["container", "pod"])
def real_mcp(request, monkeypatch):
    """factory-mcp as the deploy builds it: a container, or a pod template (`[state] backend = volume`)."""
    import flyte
    from kubernetes.client import V1Container, V1PodSpec

    from flyte_agent_factory import _factory_mcp

    monkeypatch.setattr(_factory_mcp.app, "env_vars", {**(_factory_mcp.app.env_vars or {})})
    _deploy_apps.stamp_code_digest(_factory_mcp.app, SRC, "_factory_mcp")
    if request.param == "pod":
        template = flyte.PodTemplate(
            primary_container_name="app", pod_spec=V1PodSpec(containers=[V1Container(name="app")])
        )
        monkeypatch.setattr(_factory_mcp.app, "pod_template", template)
    return _factory_mcp.app


def test_a_real_spec_from_another_deploy_is_unchanged(real_mcp):
    deployed = real_spec("s3://b/one.tgz", "v1", {"factory-mcp": "img:1", "factory": "f:1"})
    new = real_spec("s3://b/two.tgz", "v2", {"factory-mcp": "img:1"})
    assert not flyte_equal(deployed, new)  # why Flyte's own skip never fires
    assert flyte_equal(
        _deploy_apps._strip_per_deploy_flags(deployed), _deploy_apps._strip_per_deploy_flags(new)
    )


@pytest.mark.parametrize("change", ["digest", "image"])
def test_a_real_spec_with_new_code_or_image_is_a_change(real_mcp, change):
    deployed = real_spec("s3://b/one.tgz", "v1", {"factory-mcp": "img:1"})
    if change == "digest":
        real_mcp.env_vars[_deploy_apps.CODE_DIGEST_ENV] = "another"
    new = real_spec("s3://b/two.tgz", "v2", {"factory-mcp": "img:2" if change == "image" else "img:1"})
    stripped = _deploy_apps._strip_per_deploy_flags(deployed), _deploy_apps._strip_per_deploy_flags(new)
    assert not flyte_equal(*stripped)
    # Stripping keeps what the app runs from (writing to the other member of the oneof would clear it).
    assert [s.WhichOneof("app_payload") for s in stripped] == [deployed.WhichOneof("app_payload")] * 2


# --- the stage's selection -------------------------------------------------------------------------------


class PerApp:
    """`flyte.deploy` (dry run) and `remote.App` for several apps: one `FakeApps` per name."""

    def __init__(self, **apps: FakeApps):
        self.apps = apps

    def deploy(self, env: Any, dry_run: bool = False) -> list[Any]:
        return self.apps[env.name].deploy(env, dry_run=dry_run)

    def get(self, name: str) -> Any:
        return self.apps[name].get(name)

    async def _app_specs_are_equal(self, old: Any, new: Any) -> bool:
        return bool(old == new)


@pytest.mark.parametrize(
    ("mcp_same", "webhook_same"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_the_deploy_leaves_out_every_unchanged_app_and_keeps_the_rest(mcp_same, webhook_same, capsys):
    mcp, webhook = SimpleNamespace(name="factory-mcp"), SimpleNamespace(name="factory-webhook")
    others = (SimpleNamespace(name="factory"), mcp, webhook, SimpleNamespace(name="factory-reconcile"))

    def apps_for(same: bool) -> FakeApps:
        return FakeApps(make_spec(), make_spec(tgz="t2", **({} if same else {"env": "d2"})))

    server = PerApp(**{"factory-mcp": apps_for(mcp_same), "factory-webhook": apps_for(webhook_same)})
    chosen, kept = _deploy_apps.drop_unchanged_apps(
        others, [mcp, webhook], deploy=server.deploy, app_cls=server
    )
    out = capsys.readouterr().out
    expected_kept = [n for n, same in (("factory-mcp", mcp_same), ("factory-webhook", webhook_same)) if same]
    assert kept == expected_kept
    assert [e.name for e in chosen] == [e.name for e in others if e.name not in expected_kept]
    assert ("factory-mcp unchanged: not redeployed, the connector keeps running" in out) == mcp_same
    assert ("factory-webhook unchanged: not redeployed" in out) == webhook_same


@pytest.mark.parametrize("module", ["_factory_mcp", "_webhook"])
def test_stamping_puts_the_digest_in_the_env_vars(package, module):
    app = SimpleNamespace(env_vars={"A": "b"})
    _deploy_apps.stamp_code_digest(app, package, module)
    assert app.env_vars == {"A": "b", _deploy_apps.CODE_DIGEST_ENV: digest(package, module)}
