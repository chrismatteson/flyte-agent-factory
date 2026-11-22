"""Every factory entry point acts as its tier's Union key: environments carry it, entries establish it."""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _profile, _union_identity

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "flyte_agent_factory"

# (file, function) pairs: each takes the pod's identity before it touches `flyte.remote`.
ENTRY_POINTS = {
    PKG / "_factory.py": [
        "read_login_meter", "launch", "browser_link", "credential_link", "credentials", "status",
        "queue_list", "queue_cancel", "rotate_token", "set_reserve", "get_reserve", "clear_limit", "sweep",
        "list_targets", "scan_target", "garden_weekly", "plan_pass", "usage",
    ],
    PKG / "_loop_flow.py": ["loop_run", "generate_backlog"],
    PKG / "_reconcile.py": ["reconcile"],
    PKG / "_webhook.py": ["_lifespan", "serve", "process", "wake_loop"],
    PKG / "_bench_flow.py": ["read_login_meter", "run_case", "deploy_engine", "benchmark", "collect_labels"],
    PKG / "voice.py": ["train"],
    SRC / "egress_probe.py": ["probe"],
    SRC / "browser_probe.py": ["probe"],
    SRC / "demo_chain_probe.py": ["probe"],
    SRC / "endpoint_probe.py": ["probe"],
    SRC / "sandbox_probe.py": ["probe"],
}  # fmt: skip


def _is_establish(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "establish"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_union_identity"
    )


def _uses_remote(node: ast.AST) -> bool:
    """`remote.X` or `flyte.remote.X`, which is where an unestablished identity would be used."""
    if not isinstance(node, ast.Attribute):
        return False
    value = node.value
    return (isinstance(value, ast.Name) and value.id == "remote") or (
        isinstance(value, ast.Attribute) and value.attr == "remote"
    )


def _entry_cases():
    return [(path, name) for path, names in ENTRY_POINTS.items() for name in names]


@pytest.mark.parametrize(("path", "name"), _entry_cases(), ids=lambda v: v.name if isinstance(v, Path) else v)
def test_entry_point_establishes_before_using_flyte_remote(path, name):
    tree = ast.parse(path.read_text())
    (func,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]
    nodes = [n for stmt in func.body for n in ast.walk(stmt)]
    establish = [n.lineno for n in nodes if isinstance(n, ast.expr) and _is_establish(n)]
    assert establish, f"{path.name}:{name} never calls _union_identity.establish()"
    first_remote = [n.lineno for n in nodes if isinstance(n, ast.expr) and _uses_remote(n)]
    assert not first_remote or min(establish) < min(first_remote)
    # Straight-line: the call is a top-level statement of the entry, so no branch skips it.
    top_level = [s for s in func.body if isinstance(s, ast.Expr) and _is_establish(s.value)]
    assert top_level, f"{path.name}:{name} calls establish inside a branch"


def _decorated_entries(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for d in n.decorator_list:
            call = d.func if isinstance(d, ast.Call) else d
            if isinstance(call, ast.Attribute) and call.attr in ("task", "server"):
                found.add(n.name)
    return found


def test_the_list_covers_every_task_and_server_of_these_modules():
    # `hidden_check` is the one exception: it runs the agent's code and must hold no Union credential
    # (`_bench_check` refuses to run when one is readable).
    for path in ENTRY_POINTS:
        assert _decorated_entries(path) <= set(ENTRY_POINTS[path]), path.name
    assert _decorated_entries(PKG / "_bench_check.py") == {"hidden_check"}
    assert "establish" not in (PKG / "_bench_check.py").read_text()


def _env(module: str):
    sys.path.insert(0, str(SRC))
    try:
        return importlib.import_module(module)
    finally:
        sys.path.remove(str(SRC))


def _expected_secret(tier: str) -> str:
    return _profile.require().union.key_for(tier)


def _assert_tier(env, tier: str) -> None:
    keys = [s for s in env.secrets if getattr(s, "as_env_var", None) == _union_identity.KEY_ENV]
    assert [k.key for k in keys] == [_expected_secret(tier)], env.name
    assert env.env_vars["FLYTE_AUTH_TYPE"] == "ExternalCommand", env.name
    assert json.loads(env.env_vars["FLYTE_AUTH_COMMAND"]), env.name
    assert env.env_vars[_union_identity.TIER_ENV] == tier, env.name
    assert env.env_vars[_union_identity.SECRET_ENV] == _expected_secret(tier), env.name


ENVIRONMENTS = [
    ("flyte_agent_factory._factory", "env"),
    ("flyte_agent_factory._factory", "scan_env"),
    ("flyte_agent_factory._loop_flow", "env"),
    ("flyte_agent_factory._reconcile", "env"),
    ("flyte_agent_factory._webhook", "issues_env"),
    ("flyte_agent_factory._webhook", "env"),
    ("flyte_agent_factory._factory_mcp", "app"),
    ("flyte_agent_factory._bench_flow", "env"),
    ("flyte_agent_factory.browser", "browser_env"),
    ("flyte_agent_factory.voice", "env"),
    ("egress_probe", "env"),
    ("browser_probe", "env"),
    ("demo_chain_probe", "env"),
    ("endpoint_probe", "env"),
    ("sandbox_probe", "env"),
]


@pytest.mark.parametrize(("module", "attr"), ENVIRONMENTS, ids=lambda v: v)
def test_environment_acts_as_the_queue_tier(module, attr):
    _assert_tier(getattr(_env(module), attr), "queue")


#: Environments of another tier: (module, attribute) -> tier.
OTHER_TIERS = {
    ("flyte_agent_factory._deployer", "env"): "deployer",
    ("flyte_agent_factory.agent", "env"): "project",
    ("flyte_agent_factory.agent", "setup_env"): "project",
    ("flyte_agent_factory.agent", "subagent_env"): "session",
    ("ops/probes/run_listing_order.py", "env"): "queue",
    ("native_child_probe", "env"): "queue",
}

#: Environments whose tasks never talk to Union, so they need no identity, with why.
NO_UNION = {
    ("flyte_agent_factory._bench_check", "env"): "runs the agent's code; must hold no Union credential",
    ("browser_doctor", "env"): "checks the browser image's binaries",
    ("mcp_startup_probe", "env"): "starts playwright-mcp in the browser image",
    ("ops/probes/podman_rootless.py", "env"): "runs podman inside the pod",
    ("ops/probes/reaping.py", "env"): "starts orphans and times the load clock inside the pod",
    ("ops/probes/reviewer_placement.py", "plain"): "returns a constant; the probe is where it is placed",
    ("ops/probes/reviewer_placement.py", "actor"): "returns a constant; the probe is where it is placed",
    ("ops/model_eval/mechanical.py", "env"): "runs a project's checks; deliberately declares no secrets",
    ("ops/probes/run_live_author_session.py", "env"): "runs `claude` on the slot's setup-token, nothing else",
    ("ops/devbox_mcp/task.py", "env"): "runs an OSS devbox in a privileged pod; talks only to that devbox",
    ("ops/devbox_mcp/probe_run.py", "env"): "registered on the OSS devbox, never on Union",
}

#: Functions that build an environment per call, and the test that checks what they build.
FACTORIES = {
    ("flyte_agent_factory.browser", "personal_app"): "test_personal_browser_apps_act_as_the_queue_tier",
    ("ops/seal_e2e.py", "child_env"): "test_the_seal_e2e_child_holds_no_union_key",
}

ROOT = SRC.parent


def _module_key(path: Path) -> str:
    """How the tables above name a file: a dotted module under src/, a repo path under ops/."""
    rel = path.relative_to(ROOT)
    if rel.parts[0] == "src":
        return ".".join(path.relative_to(SRC).with_suffix("").parts)
    return rel.as_posix()


def _is_environment(node: ast.AST, aliases: frozenset[str] = frozenset()) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    # TaskEnvironment, AppEnvironment, FlyteMCPAppEnvironment, or one imported under another name.
    return name.endswith("Environment") or name in aliases


def _environments_in(source: str, key: str) -> set[tuple[str, str]]:
    """(key, name) for each environment `source` builds: the variable it is bound to, or the function."""
    tree = ast.parse(source)
    aliases = frozenset(
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname and alias.name.endswith("Environment")
    )
    found = set()
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign | ast.AnnAssign)
            and stmt.value
            and _is_environment(stmt.value, aliases)
        ):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            found |= {(key, t.id) for t in targets if isinstance(t, ast.Name)}
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(_is_environment(n, aliases) for n in ast.walk(stmt)):
                found.add((key, stmt.name))
        elif any(_is_environment(n, aliases) for n in ast.walk(stmt)):
            found.add((key, f"<line {stmt.lineno}>"))  # some other shape: make the table name it
    return found


def _defined_environments() -> set[tuple[str, str]]:
    """Every environment built anywhere in src/ or ops/."""
    paths = [*sorted(SRC.rglob("*.py")), *sorted((ROOT / "ops").rglob("*.py"))]
    return {found for path in paths for found in _environments_in(path.read_text(), _module_key(path))}


def test_every_environment_in_the_engine_is_accounted_for():
    # A new environment must say which tier's key it acts as, or why it needs none (#183).
    known = {(m, a) for m, a in ENVIRONMENTS} | set(OTHER_TIERS) | set(NO_UNION) | set(FACTORIES)
    defined = _defined_environments()
    assert defined - known == set(), "add these to a table in tests/test_union_entrypoints.py"
    assert known - defined == set(), "these are gone: drop them from the tables"


def test_discovery_sees_every_shape_an_environment_is_built_in():
    source = (
        "import flyte\n"
        "from flyte.app import AppEnvironment as AE\n"
        "env = flyte.TaskEnvironment(name='a')\n"
        "app: object = flyte.app.AppEnvironment(name='b')\n"
        "def make():\n    return AE(name='c')\n"
        "if True:\n    other = flyte.TaskEnvironment(name='d')\n"
        "TEXT = 'env = flyte.TaskEnvironment(name=\"in a string\")'\n"
    )
    assert _environments_in(source, "m") == {("m", "env"), ("m", "app"), ("m", "make"), ("m", "<line 7>")}


def _load(module: str):
    if not module.startswith("ops/"):
        return _env(module)
    spec = importlib.util.spec_from_file_location(Path(module).stem, ROOT / module)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize(("module", "attr"), sorted(OTHER_TIERS), ids=lambda v: v)
def test_environment_acts_as_its_tier(module, attr):
    _assert_tier(getattr(_load(module), attr), OTHER_TIERS[(module, attr)])


_UNION_NAMES = {"remote", "attach", "create_controller", "controller_kwargs", "establish", "with_runcontext"}


def _task_reach(path: Path, env_name: str) -> list[ast.AST]:
    """The bodies of `env_name`'s tasks, and of every module function they call, transitively."""
    tree = ast.parse(path.read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}

    def is_task_of_env(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for d in func.decorator_list:
            call = d.func if isinstance(d, ast.Call) else d
            if (
                isinstance(call, ast.Attribute)
                and isinstance(call.value, ast.Name)
                and call.value.id == env_name
            ):
                return True
        return False

    todo = [f for f in functions.values() if is_task_of_env(f)]
    assert todo, f"{path.name}: {env_name} has no tasks"
    seen: dict[str, ast.AST] = {}
    while todo:
        func = todo.pop()
        if func.name in seen:
            continue
        seen[func.name] = func
        calls = [
            n.func.id for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        todo += [functions[c] for c in calls if c in functions]
    return list(seen.values())


@pytest.mark.parametrize(
    ("module", "attr"),
    sorted(
        k for k in NO_UNION if k[0] not in ("flyte_agent_factory._bench_check", "flyte_agent_factory.agent")
    ),
    ids=lambda v: v,
)
def test_an_environment_without_an_identity_never_talks_to_union(module, attr):
    path = ROOT / module if module.startswith("ops/") else SRC / f"{module.replace('.', '/')}.py"
    used = {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for body in _task_reach(path, attr)
        for n in ast.walk(body)
        if isinstance(n, ast.Attribute | ast.Name)
    }
    assert not used & _UNION_NAMES, f"{module}:{attr} reaches Union; mount a tier key and move it to a table"


def test_personal_browser_apps_act_as_the_queue_tier():
    from flyte_agent_factory import browser

    profile = _profile.require()
    identity = _profile.BrowserIdentity("alice", "alice-token")
    _assert_tier(browser.personal_app(profile, identity, ""), "queue")


def test_the_seal_e2e_child_holds_no_union_key():
    _, env = _load("ops/seal_e2e.py").child_env("registry.invalid/agent:tag")
    assert env.image == "registry.invalid/agent:tag"
    assert not [s for s in env.secrets if getattr(s, "as_env_var", None) == _union_identity.KEY_ENV]
    assert _union_identity.TIER_ENV not in (env.env_vars or {})


def test_bench_check_environment_holds_no_union_credential():
    env = _env("flyte_agent_factory._bench_check").env
    assert not env.secrets
    assert _union_identity.TIER_ENV not in (env.env_vars or {})


def test_bench_check_guard_covers_the_tier_key():
    import inspect

    from flyte_agent_factory import _bench_check

    default = inspect.signature(_bench_check.run_hidden_check).parameters["key_vars"].default
    assert _union_identity.KEY_ENV in default
    assert "_UNION_EAGER_API_KEY" in default  # what Union injects: still refused if present


def _calls(path: Path):
    tree = ast.parse(path.read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def test_no_bare_init_in_cluster_or_rust_controller_in_these_files():
    for path in [*ENTRY_POINTS, PKG / "browser.py", PKG / "_planner_runtime.py"]:
        text = path.read_text()
        assert "init_in_cluster(" not in text, path.name
        assert 'create_controller("rust"' not in text, path.name


@pytest.mark.parametrize(
    ("path", "action"),
    [
        (PKG / "_reviewer.py", "submitting a reviewer"),
        (PKG / "_delegation.py", "submitting a delegated worker"),
        (PKG / "_planner_runtime.py", "launching the planner"),
        (PKG / "_planner_runtime.py", "listing planner runs"),
    ],
    ids=lambda v: v.name if isinstance(v, Path) else v,
)
def test_submissions_and_listings_are_wrapped_in_denied(path, action):
    wrapped = [
        c
        for c in _calls(path)
        if isinstance(c.func, ast.Attribute)
        and c.func.attr == "denied"
        and c.args
        and isinstance(c.args[0], ast.Constant)
        and c.args[0].value == action
    ]
    assert wrapped, f"{path.name} does not wrap {action!r} in _union_identity.denied"
