"""A deploy as a key scoped to one project and domain: no watch stream, the deployer last, one refusal.

Such a key (role `contributor`) can do everything a deploy needs except open the app watch stream, which the
SDK's `App.watch`, `activate(wait=True)` and `deactivate(wait=True)` all do. Offline: fakes for the control
plane, as in `test_deploy_apps.py` and `test_deploy_factory.py`.
"""

from __future__ import annotations

import ast
import base64
import inspect
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flyte
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import _deploy, _deploy_apps, _deploy_child, _union_identity
from tests.conftest import TEST_PROFILE

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORK = TEST_PROFILE.read_text()


# --- factory-mcp is waited for by polling ------------------------------------------------------------------


class FakeApp:
    def __init__(self, active: bool, messages: tuple[str, ...] = ()):
        self.active = active
        self.endpoint = "https://public.example"
        self.deployment_status = None
        self.pb2 = SimpleNamespace(
            status=SimpleNamespace(conditions=[SimpleNamespace(message=m) for m in messages])
        )

    def is_active(self) -> bool:
        return self.active

    def watch(self, **kwargs: Any) -> Any:
        raise ConnectError(Code.UNAUTHENTICATED, "Unauthorized")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def fake_app_cls(*apps: FakeApp) -> Any:
    """A `remote.App` whose `get` returns each of `apps` in turn, then the last again."""
    queue = list(apps)
    seen: list[str] = []

    def get(name: str) -> FakeApp:
        seen.append(name)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return SimpleNamespace(get=get, seen=seen)


def test_a_wait_that_polls_succeeds_where_the_watch_is_refused():
    clock = Clock()
    apps = fake_app_cls(FakeApp(False), FakeApp(False), FakeApp(True))
    app = _deploy_apps.wait_until_active("factory-mcp", app_cls=apps, sleep=clock.sleep, clock=clock)
    assert app.endpoint == "https://public.example"
    assert apps.seen == ["factory-mcp"] * 3
    assert clock.now == 2 * _deploy_apps.POLL_SECONDS


def test_an_app_that_never_activates_fails_naming_it_the_timeout_and_its_conditions():
    clock = Clock()
    apps = fake_app_cls(FakeApp(False, ("waiting for image",)))
    with pytest.raises(_deploy.DeployError) as caught:
        _deploy_apps.wait_until_active(
            "factory-mcp", app_cls=apps, sleep=clock.sleep, clock=clock, timeout=60
        )
    assert "factory-mcp" in str(caught.value) and "60 s" in str(caught.value)
    assert "waiting for image" in str(caught.value)
    assert clock.now <= 60 + _deploy_apps.POLL_SECONDS


@pytest.mark.parametrize("code", [Code.UNAUTHENTICATED, Code.PERMISSION_DENIED])
def test_a_key_refused_the_app_itself_fails_at_once_and_says_so(code):
    """Not after the whole timeout as "did not become active": no later poll would be let in either."""
    clock = Clock()

    def get(name):
        raise ConnectError(code, "not allowed")

    with pytest.raises(_deploy.DeployError) as caught:
        _deploy_apps.wait_until_active(
            "factory-mcp", app_cls=SimpleNamespace(get=get), sleep=clock.sleep, clock=clock
        )
    assert clock.now == 0
    assert "factory-mcp" in str(caught.value) and code.name in str(caught.value)
    assert "not allowed" in str(caught.value)


def test_a_blip_while_polling_is_still_waited_out():
    clock = Clock()
    answers = iter([ConnectError(Code.UNAVAILABLE, "blip"), FakeApp(True)])

    def get(name):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    app = _deploy_apps.wait_until_active(
        "factory-mcp", app_cls=SimpleNamespace(get=get), sleep=clock.sleep, clock=clock
    )
    assert app.endpoint == "https://public.example"


def test_the_default_timeout_is_the_apps():
    assert inspect.signature(_deploy_apps.wait_until_active).parameters["timeout"].default == (
        _deploy_apps.ACTIVE_TIMEOUT_SECONDS
    )


def test_deploy_apps_imports_without_kubernetes():
    """The deployer image may lack `kubernetes`; the factory stage now imports `_deploy_apps` there."""
    import subprocess

    # None in `sys.modules` makes `import kubernetes` raise ImportError.
    code = "import sys; sys.modules['kubernetes'] = None; import flyte_agent_factory._deploy_apps"
    done = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(SRC)}, capture_output=True)
    assert done.returncode == 0, done.stderr.decode()


# --- no call opens the watch stream ------------------------------------------------------------------------

#: `(file, line-independent description)` of a call that is allowed to watch; none today. Add one here, with
#: the reason, only for code that runs as a person's own login and never as the scoped key.
ALLOWED: set[tuple[str, str]] = set()

CHECKED = ("_deploy.py", "_deploy_child.py", "_deploy_apps.py")


def _sources(path: Path) -> list[tuple[str, ast.AST]]:
    """The module's tree, and the tree of every string constant in it that is Python (the child scripts)."""
    trees: list[tuple[str, ast.AST]] = []
    module = ast.parse(path.read_text())
    trees.append((path.name, module))
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "\n" in node.value:
            try:
                trees.append((f"{path.name}:script", ast.parse(node.value)))
            except SyntaxError:
                continue  # prose or a template with placeholders, not a script
    return trees


def _watching_calls(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr == "watch":
            found.append("watch()")
        elif attr in ("activate", "deactivate"):
            # `wait` is the methods' only parameter (`activate(self, wait=False)`): by keyword or by position.
            wait = [kw.value for kw in node.keywords if kw.arg in ("wait", None)] + list(node.args)
            literal_false = len(wait) == 1 and isinstance(wait[0], ast.Constant) and wait[0].value is False
            if wait and not literal_false:  # a bare call takes the SDK default, which is False
                found.append(f"{attr}(wait=...)")
    return found


def _checked_files() -> list[Path]:
    files = [SRC / "flyte_agent_factory" / name for name in CHECKED]
    extra = ROOT / "ops" / "deploy_as_key.py"  # another worker's script, checked once it exists
    return files + ([extra] if extra.exists() else [])


@pytest.mark.parametrize("path", _checked_files(), ids=lambda p: p.name)
def test_no_deploy_code_opens_the_watch_stream(path):
    offences = [
        (label, what)
        for label, tree in _sources(path)
        for what in _watching_calls(tree)
        if (path.name, what) not in ALLOWED
    ]
    assert offences == []


def test_the_child_scripts_are_checked_as_python():
    for script in (_deploy.SLOTS_CHILD, _deploy.FACTORY_CHILD, _deploy.APPS_CHILD):
        assert _watching_calls(ast.parse(script)) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "app.watch(wait_for='x')",
        "a.activate(wait=True)",
        "a.deactivate(wait=w)",
        "a.activate(True)",
        "a.deactivate(w)",
        "a.activate(**kw)",
    ],
)
def test_the_scan_trips_on_a_planted_wait(snippet):
    assert _watching_calls(ast.parse(snippet))


def test_the_scan_leaves_a_non_waiting_call_alone():
    assert _watching_calls(ast.parse("a.activate()\na.deactivate(wait=False)\na.activate(False)")) == []


def test_the_sdk_default_is_not_to_wait():
    for method in (remote.App.activate, remote.App.deactivate):
        parameters = inspect.signature(method).parameters
        assert list(parameters) == ["self", "wait"]  # the scan reads a positional argument as `wait`
        assert parameters["wait"].default is False


# --- the deployer is deployed last -------------------------------------------------------------------------


class FactoryRun:
    """The factory child against a fake `flyte.deploy` and `App.get`: what was deployed, in which call."""

    def __init__(self, monkeypatch, tmp_path, *, mcp_active: bool, dry: bool = False):
        self.calls: list[list[str]] = []
        self.report = tmp_path / "report"
        self.dry = dry
        monkeypatch.setenv(_deploy.DEPLOY_REPORT_ENV, str(self.report))
        monkeypatch.setenv("AGENT_FACTORY_DEPLOY_KEY_ENV", "AGENT_FACTORY_UNION_KEY")
        monkeypatch.setenv("AGENT_FACTORY_UNION_KEY", "operator-key")
        monkeypatch.setattr(flyte, "init_from_api_key", lambda **kw: None)
        monkeypatch.setattr(flyte, "deploy", self._deploy)
        # The helper's defaults are bound at definition, so patching `time` would not reach them.
        ticks = iter(range(0, 10**6, 400))  # each look at the clock is 400 s later: the wait gives up fast
        fast = {**(_deploy_apps.wait_until_active.__kwdefaults__ or {}), "sleep": lambda s: None}
        fast["clock"] = lambda: next(ticks)
        monkeypatch.setattr(_deploy_apps.wait_until_active, "__kwdefaults__", fast)
        app = FakeApp(mcp_active)
        monkeypatch.setattr(remote.App, "get", staticmethod(lambda name: app))
        retired: list[str] = []
        monkeypatch.setattr(_deploy, "delete_retired_triggers", lambda profile: retired.append("done"))
        self.retired = retired
        monkeypatch.setattr(sys, "argv", ["child", str(SRC), "1" if dry else "0", "project", "domain"])

    def _deploy(self, *envs: Any, dry_run: bool) -> list[Any]:
        self.calls.append([env.name for env in envs])
        deployed = [
            SimpleNamespace(get_name=lambda n=env.name: n, deployed_app=SimpleNamespace(endpoint="https://w"))
            for env in envs
        ]
        return [SimpleNamespace(summary_repr=lambda: "deployed", envs={d.get_name(): d for d in deployed})]

    def run(self) -> None:
        exec(_deploy.FACTORY_CHILD, {})

    def recorded(self) -> list[str]:
        return self.report.read_text().split() if self.report.exists() else []


def test_a_factory_mcp_that_does_not_come_up_leaves_the_deployer_undeployed(monkeypatch, tmp_path, capsys):
    run = FactoryRun(monkeypatch, tmp_path, mcp_active=False)
    with pytest.raises(_deploy.DeployError, match="factory-mcp"):
        run.run()
    assert len(run.calls) == 1 and "factory-deployer" not in run.calls[0]
    assert "factory-mcp" in run.calls[0]
    assert "factory-deployer" not in run.recorded()
    assert {"factory", "factory-mcp"} <= set(run.recorded())  # what was deployed is still reported
    assert run.retired == []


def test_the_deployer_is_in_the_last_deploy_after_the_wait_and_the_webhook_lines(
    monkeypatch, tmp_path, capsys
):
    run = FactoryRun(monkeypatch, tmp_path, mcp_active=True)
    run.run()
    assert [c for c in run.calls[-1]] == ["factory-deployer"]
    assert all("factory-deployer" not in call for call in run.calls[:-1])
    assert {"factory", "factory-mcp", "factory-deployer"} <= set(run.recorded())
    out = capsys.readouterr().out
    assert "factory-mcp public URL: https://public.example" in out
    assert "Connector URL: https://public.example/flyte-mcp/mcp" in out
    assert run.retired == ["done"]


def test_a_dry_run_deploys_both_and_waits_for_nothing(monkeypatch, tmp_path, capsys):
    run = FactoryRun(monkeypatch, tmp_path, mcp_active=False)
    monkeypatch.setattr(sys, "argv", ["child", str(SRC), "1", "project", "domain"])
    run.run()
    assert {n for call in run.calls for n in call} >= {"factory", "factory-mcp", "factory-deployer"}
    assert "public URL" not in capsys.readouterr().out
    assert run.retired == []
    assert not run.report.exists()


def test_an_environment_that_depends_on_the_deployer_is_refused_before_anything_is_deployed(
    monkeypatch, tmp_path
):
    from flyte_agent_factory import _deployer, _factory_mcp

    run = FactoryRun(monkeypatch, tmp_path, mcp_active=True)
    monkeypatch.setattr(_factory_mcp.app, "depends_on", [_deployer.env])
    with pytest.raises(SystemExit, match="must deploy last"):
        run.run()
    assert run.calls == []


# --- one message, at the start ------------------------------------------------------------------------------


@pytest.fixture
def pod(monkeypatch, tmp_path):
    """A deploy child in a pod: an execution id, no key variables, a home without a CLI config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLYTE_INTERNAL_EXECUTION_ID", "abc")
    monkeypatch.delenv(_deploy.DEPLOY_KEY_ENV, raising=False)
    monkeypatch.delenv("AGENT_FACTORY_UNION_KEY", raising=False)


def _main(monkeypatch, tmp_path, step: str, text: str = WORK) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    error = tmp_path / "error"
    argv = [step, "--src", str(SRC), "--where", "t", "--error", str(error)]
    if step == "deploy":
        argv += ["--project", "p", "--domain", "d", "--ref", "r"]
    code = _deploy_child.main(argv)
    return code, error.read_text()


@pytest.fixture
def nothing_runs(monkeypatch):
    def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the preflight must stop the deploy before it imports or deploys anything")

    monkeypatch.setattr(_deploy_child, "collect_planned_secrets", fail)
    monkeypatch.setattr(_deploy_child, "deploy", fail)
    monkeypatch.setattr(_deploy_child, "find_missing_secrets", fail)


@pytest.mark.parametrize("step", ["check", "deploy"])
def test_a_pod_with_no_key_and_no_config_fails_once_saying_the_deployer_is_too_old(
    monkeypatch, tmp_path, capsys, pod, nothing_runs, step
):
    code, error = _main(monkeypatch, tmp_path, step)
    assert code == 1
    for needle in ("no deployer key", "no CLI config", "too old", "ops/deploy_as_key.py", "[union]"):
        assert needle in error
    assert "docs/setup.md#migrating-an-existing-install" in error
    assert "Traceback" not in capsys.readouterr().err


def test_a_named_key_variable_that_is_empty_is_named(monkeypatch, tmp_path, pod, nothing_runs):
    monkeypatch.setenv(_deploy.DEPLOY_KEY_ENV, "MY_KEY")
    monkeypatch.setenv("MY_KEY", "")
    code, error = _main(monkeypatch, tmp_path, "check")
    assert code == 1 and "MY_KEY is not set" in error and _deploy.DEPLOY_KEY_ENV in error


def test_a_named_key_variable_whose_value_is_not_a_key_is_named(
    monkeypatch, tmp_path, capsys, pod, nothing_runs
):
    monkeypatch.setenv(_deploy.DEPLOY_KEY_ENV, "MY_KEY")
    monkeypatch.setenv("MY_KEY", "not-a-key")
    for step in ("check", "deploy"):
        code, error = _main(monkeypatch, tmp_path, step)
        assert code == 1 and "MY_KEY" in error and "not a Union API key" in error
        assert "not-a-key" not in error
    assert "Traceback" not in capsys.readouterr().err


def test_the_preflight_never_quotes_a_value_that_is_not_a_key(monkeypatch, pod):
    from flyte_agent_factory import _profile

    monkeypatch.setenv("MY_KEY", "garbage-value-123")
    with pytest.raises(_union_identity.IdentityMissing, match="not a Union API key") as raised:
        _union_identity.check_script_identity(_profile.parse(WORK), "MY_KEY")
    assert "garbage-value-123" not in str(raised.value)


def test_a_key_in_the_pod_passes_the_preflight(monkeypatch, pod):
    monkeypatch.setenv("MY_KEY", base64.b64encode(b"dns:///example.invalid:client:secret:org").decode())
    from flyte_agent_factory import _profile

    _union_identity.check_script_identity(_profile.parse(WORK), "MY_KEY")


def test_a_pod_with_a_cli_config_passes_the_preflight(monkeypatch, tmp_path, pod):
    from flyte_agent_factory import _profile

    profile = _profile.parse(WORK)
    config = Path(profile.backend.config).expanduser()
    config.parent.mkdir(parents=True)
    config.write_text("admin: {}")
    _union_identity.check_script_identity(profile, "")


@pytest.mark.parametrize("where", ["home", "FLYTECTL_CONFIG", "nowhere"])
def test_a_profile_naming_no_config_is_checked_where_the_sdk_looks(monkeypatch, tmp_path, pod, where):
    """`[backend] config` is optional: then the SDK searches its own places, and so does this."""
    from flyte_agent_factory import _profile

    monkeypatch.chdir(tmp_path)  # the SDK also looks in the working directory and its git root
    monkeypatch.delenv("FLYTECTL_CONFIG", raising=False)
    profile = _profile.parse("\n".join(line for line in WORK.splitlines() if not line.startswith("config =")))
    assert not profile.backend.config
    if where == "home":
        (tmp_path / ".flyte").mkdir()
        (tmp_path / ".flyte" / "config.yaml").write_text("admin: {}")
    elif where == "FLYTECTL_CONFIG":
        (tmp_path / "elsewhere.yaml").write_text("admin: {}")
        monkeypatch.setenv("FLYTECTL_CONFIG", str(tmp_path / "elsewhere.yaml"))
    if where == "nowhere":
        with pytest.raises(_union_identity.IdentityMissing, match="too old"):
            _union_identity.check_script_identity(profile, "")
    else:
        _union_identity.check_script_identity(profile, "")


def test_a_laptop_without_a_config_is_left_to_the_sdk(monkeypatch, tmp_path):
    from flyte_agent_factory import _profile

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)
    _union_identity.check_script_identity(_profile.parse(WORK), "")


def test_an_oss_profile_is_not_checked(monkeypatch, pod):
    from flyte_agent_factory import _profile
    from tests.test_oss_backend import _oss

    profile: _profile.Profile = _oss()
    _union_identity.check_script_identity(profile, "")
    monkeypatch.setenv(_deploy.DEPLOY_KEY_ENV, "UNSET_VAR")
    _union_identity.check_script_identity(profile, "UNSET_VAR")


def test_init_for_script_makes_the_same_check_for_every_caller(monkeypatch, pod):
    from flyte_agent_factory import _profile

    monkeypatch.setattr(flyte, "init_from_config", lambda *a, **kw: pytest.fail("no SDK traceback"))
    with pytest.raises(_union_identity.IdentityMissing, match="too old"):
        _union_identity.init_for_script(_profile.parse(WORK))


def test_an_older_caller_gets_one_line_not_a_traceback(monkeypatch, pod):
    """The child scripts' `_INIT` turns the refusal into a SystemExit with the message."""
    script = _deploy._INIT
    assert "except IdentityMissing as exc:" in script and "raise SystemExit(str(exc))" in script


def test_the_too_old_message_names_a_command_and_a_section_that_exist():
    """The refusal sends the owner to `ONE_OFF_DEPLOY` at `ONE_OFF_DEPLOY_DOC`: both must be real."""
    assert (ROOT / _union_identity.ONE_OFF_DEPLOY).is_file()
    page, anchor = _union_identity.ONE_OFF_DEPLOY_DOC.split("#")
    headings = [
        line.lstrip("#").strip() for line in (ROOT / page).read_text().splitlines() if line.startswith("#")
    ]
    slugs = {"-".join("".join(c for c in h.lower() if c.isalnum() or c in " -").split()) for h in headings}
    assert anchor in slugs
    section = (ROOT / page).read_text().split("## Migrating an existing install", 1)[1].split("\n## ", 1)[0]
    assert f"python {_union_identity.ONE_OFF_DEPLOY}" in section
