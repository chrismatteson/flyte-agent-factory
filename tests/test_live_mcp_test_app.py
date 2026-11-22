"""The live MCP test app owns its lifecycle: it is deleted after a failed deploy, and after the hold."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _app_tools, _profile, _profile_oss, _union_identity
from ops import live_mcp_test_app as app

NAME = "factory-mcp-test-abc1234"


@pytest.fixture
def calls(monkeypatch):
    """Replace the network steps; return the list of ("deploy" | "delete" | "hold", ...) calls made."""
    made: list[tuple[str, ...]] = []

    def deploy(name, url):
        made.append(("deploy", name, url))
        return "https://t.example"

    monkeypatch.setattr(app, "deploy", deploy)
    monkeypatch.setattr(app, "delete", lambda name: made.append(("delete", name)))
    monkeypatch.setattr(app, "hold", lambda: made.append(("hold",)))
    monkeypatch.setattr(_profile, "require", lambda: SimpleNamespace(backend="oss"))
    monkeypatch.setattr(_union_identity, "init_for_script", lambda *a, **k: None)
    monkeypatch.setattr(_profile_oss, "builder", lambda backend: None)
    return made


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["live_mcp_test_app.py", "--name", NAME, *argv])
    return app.main()


def test_a_failed_second_deploy_deletes_the_app_once(monkeypatch, calls):
    def deploy(name, url):
        calls.append(("deploy", name, url))
        if url:
            raise SystemExit("second deploy failed")
        return "https://t.example"

    monkeypatch.setattr(app, "deploy", deploy)
    with pytest.raises(SystemExit, match="second deploy failed"), app.deploy_test_app(NAME):
        pytest.fail("the body must not run after a failed deploy")
    assert [c for c in calls if c[0] == "delete"] == [("delete", NAME)]


def test_keep_leaves_the_app_and_names_the_delete_command(monkeypatch, calls, capsys):
    assert _main(monkeypatch, "--keep") == 0
    assert ("delete", NAME) not in calls and ("hold",) not in calls
    assert f"--delete --name {NAME}" in capsys.readouterr().out


def test_keep_still_deletes_after_a_failed_deploy(monkeypatch, calls):
    monkeypatch.setattr(app, "deploy", lambda name, url: (_ for _ in ()).throw(SystemExit("no")))
    with pytest.raises(SystemExit):
        _main(monkeypatch, "--keep")
    assert calls == [("delete", NAME)]


def test_the_production_name_is_refused_before_any_deploy(calls):
    with pytest.raises(SystemExit, match="refusing"), app.deploy_test_app(_app_tools.DEFAULT_OWN_APP):
        pytest.fail("the body must not run for the production name")
    assert calls == []


def test_the_default_mode_deletes_after_the_hold_returns(monkeypatch, calls, capsys):
    assert _main(monkeypatch) == 0
    assert [c[0] for c in calls] == ["deploy", "deploy", "hold", "delete"]
    out = capsys.readouterr().out
    assert "https://t.example/flyte-mcp/mcp" in out


def test_deleting_an_app_that_is_gone_is_not_an_error(monkeypatch):
    import flyte.remote as remote
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    def get(name):
        raise ConnectError(Code.NOT_FOUND, "no such app")

    monkeypatch.setattr(remote.App, "get", get)
    deleted: list[str] = []
    monkeypatch.setattr(remote.App, "delete", lambda name: deleted.append(name))
    app.delete(NAME)
    assert deleted == []


def test_a_parallel_burst_of_status_calls_has_four_distinct_inputs():
    # Identical arguments join into one run on the server (a feature); the burst must not rely on that.
    from ops import mcp_burst

    seen: list[int] = []

    class FakeSoak:
        async def call(self, session, index, tool, arguments):
            seen.append(arguments["inputs"]["rows"])

    arguments: dict[str, Any] = {"name": "factory.status", "inputs": {"name": "p", "rows": 20}}
    asyncio.run(mcp_burst.run_burst(cast(Any, FakeSoak()), cast(Any, None), 3, "run_task", arguments, 4))
    assert sorted(seen) == [20, 21, 22, 23]
    assert arguments["inputs"]["rows"] == 20


def test_the_soak_records_the_inputs_each_run_task_call_had():
    from ops import mcp_soak

    class FakeSession:
        async def call_tool(self, tool, arguments, read_timeout_seconds):
            return SimpleNamespace(isError=False, content=[], structuredContent={"status": "ok"})

    soak = mcp_soak.Soak(lambda text: text)
    asyncio.run(soak.call(cast(Any, FakeSession()), 0, "run_task", {"name": "x", "inputs": {"rows": 21}}))
    asyncio.run(soak.call(cast(Any, FakeSession()), 0, "get_app", {"name": "a"}))
    assert [c.inputs for c in soak.calls] == [{"rows": 21}, {}]
