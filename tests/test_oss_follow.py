"""Following a run on OSS Flyte: real log attempt, named gaps, report fallbacks, openable URLs (26-29)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import _factory, _factory_base, _oss_follow, _profile
from tests.conftest import TEST_PROFILE
from tests.test_queue import status_run

ROOT = Path(__file__).resolve().parents[1]
NO_URL_TEXT = _oss_follow.NO_URL_TEXT
INTERNAL = "http://flyte-http.flyte:8090/v2/domain/development/project/my-agents/runs/r-abc"


def _oss(console_host: str = "") -> _profile.Profile:
    text = (ROOT / "profiles" / "oss-example.toml").read_text()
    if console_host:
        text = text.replace("[backend]\n", f'[backend]\nconsole_host = "{console_host}"\n', 1)
    return _profile.parse(text)


UNION = _profile.parse(TEST_PROFILE.read_text())


def _run(coroutine):
    return asyncio.run(coroutine)


def _details(attempts: list[int], pod: str = "r-abc-a0-0"):
    """What `ActionDetails.get_details` returns: `pb2.attempts` as the server lists them."""
    log_context = SimpleNamespace(
        primary_pod_name=pod,
        pods=[SimpleNamespace(namespace="flyte", pod_name=pod, primary_container_name="primary")],
    )
    return SimpleNamespace(
        pb2=SimpleNamespace(attempts=[SimpleNamespace(attempt=a, log_context=log_context) for a in attempts])
    )


class _Callable:
    """`get_details` as syncify gives it: callable, with `.aio` for the async form."""

    def __init__(self, sync, aio):
        self.sync, self.aio = sync, aio

    def __call__(self, *args):
        return self.sync(*args)


def _serve_details(monkeypatch, details) -> list[Any]:
    asked: list[Any] = []

    async def get_details(action_id):
        asked.append(action_id)
        return details

    def get_details_sync(action_id):
        asked.append(action_id)
        return details

    monkeypatch.setattr(remote.ActionDetails, "get_details", _Callable(get_details_sync, get_details))
    return asked


class Stock:
    """A stock `get_logs` that records its calls and answers with `answer` (or raises it)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(
        self, run_name, action_name=None, attempt=None, max_lines=200, project=None, domain=None
    ):
        self.calls.append({"run_name": run_name, "action_name": action_name, "attempt": attempt})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


LINES = {"run_name": "r-abc", "attempt": 1, "available": True, "logs": "hello\nworld", "lines_returned": 2}


# --- criterion 26: logs -------------------------------------------------------------------------------


def test_oss_logs_use_the_real_attempt_when_the_details_say_zero(monkeypatch):
    asked = _serve_details(monkeypatch, _details([1, 2]))
    stock = Stock(LINES)
    result = _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc", action_name="a0"))
    assert result == LINES
    assert stock.calls == [{"run_name": "r-abc", "action_name": "a0", "attempt": 2}]
    assert asked[0].name == "a0" and asked[0].run.name == "r-abc"


def test_an_attempt_the_caller_gave_is_not_replaced(monkeypatch):
    asked = _serve_details(monkeypatch, _details([2]))
    stock = Stock(LINES)
    _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc", attempt=1))
    assert stock.calls[0]["attempt"] == 1 and asked == []


def test_a_missing_log_stream_gives_the_gap_and_its_alternatives(monkeypatch):
    _serve_details(monkeypatch, _details([1]))
    stock = Stock(RuntimeError("Log stream not available for action a0 in run r-abc."))
    result = _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc", action_name="a0"))
    assert result["lines"] == [] and result["attempt"] == 1
    assert "no log stream" in result["gap"] and "r-abc/a0" in result["gap"]
    assert result["gap"] != "Log stream not available for action a0 in run r-abc."
    assert result["pod"] == "r-abc-a0-0" and result["namespace"] == "flyte"
    text = " ".join(result["alternatives"])
    assert "kubectl logs -n flyte r-abc-a0-0 -c primary" in text
    assert "get_action" in text and "factory.status" in text


@pytest.mark.parametrize(
    "answer, cause",
    [
        (
            {"run_name": "r-abc", "available": True, "logs": "", "message": "No log lines were returned."},
            "No log lines were returned.",
        ),
        ({"error": "no log context found"}, "no log context found"),
    ],
)
def test_a_result_without_lines_gives_the_gap(monkeypatch, answer, cause):
    _serve_details(monkeypatch, _details([1]))
    result = _run(_oss_follow.wrap_get_logs(Stock(answer), _oss())(run_name="r-abc"))
    assert result["lines"] == [] and result["alternatives"] and "gap" in result
    assert result["cause"] == cause


_NOT_YET = (
    "Logs are not available yet for action 'a1' in run 'r-abc' (phase QUEUED). Call get_logs again once it is"
    " running, or pass wait_for_logs=true to block."
)


def test_a_not_yet_running_action_keeps_the_stocks_phase_and_advice(monkeypatch):
    _serve_details(monkeypatch, _details([1]))
    answer = {"run_name": "r-abc", "available": False, "logs": "", "message": _NOT_YET}
    result = _run(_oss_follow.wrap_get_logs(Stock(answer), _oss())(run_name="r-abc", action_name="a1"))
    assert result == answer
    assert "phase QUEUED" in result["message"] and "wait_for_logs" in result["message"]
    assert "gap" not in result


def test_a_read_that_ran_out_of_time_is_the_stocks_answer_not_a_gap(monkeypatch):
    _serve_details(monkeypatch, _details([1]))
    answer = {
        "run_name": "r-abc",
        "logs": "",
        "stopped": "time_limit",
        "message": "No log lines arrived within 170s.",
    }
    result = _run(_oss_follow.wrap_get_logs(Stock(answer), _oss())(run_name="r-abc"))
    assert result == answer and "gap" not in result


def test_the_gap_does_not_need_the_attempt_lookup(monkeypatch):
    async def refuse(action_id):
        raise ConnectError(Code.UNAVAILABLE, "down")

    monkeypatch.setattr(remote.ActionDetails, "get_details", SimpleNamespace(aio=refuse))
    stock = Stock(RuntimeError("Log stream not available"))
    result = _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc"))
    assert stock.calls[0]["attempt"] is None
    assert result["alternatives"] and "pod" not in result
    assert any("<pod>" in a for a in result["alternatives"])


def test_a_run_or_action_that_does_not_exist_is_not_found_not_a_gap(monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    async def missing(action_id):
        raise ConnectError(Code.NOT_FOUND, "run r-typo not found")

    monkeypatch.setattr(remote.ActionDetails, "get_details", SimpleNamespace(aio=missing))
    stock = Stock(ConnectError(Code.NOT_FOUND, "run r-typo not found"))
    with pytest.raises(ToolError, match="run r-typo not found"):
        _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-typo"))
    assert stock.calls == []

    _serve_details(monkeypatch, _details([1]))
    stock = Stock(ConnectError(Code.NOT_FOUND, "action a9 not found"))
    with pytest.raises(ToolError, match="action a9 in run r-abc not found"):
        _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc", action_name="a9"))


def test_a_stock_error_text_is_kept_as_the_gaps_cause(monkeypatch):
    _serve_details(monkeypatch, _details([1]))
    stock = Stock({"error": "no log context found"})
    result = _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc"))
    assert "gap" in result and result["cause"] == "no log context found"
    stock = Stock(ConnectError(Code.UNAVAILABLE, "down"))
    result = _run(_oss_follow.wrap_get_logs(stock, _oss())(run_name="r-abc"))
    assert "gap" in result and result["cause"] == "ConnectError:UNAVAILABLE"


def test_real_attempt_names_the_pod_of_the_newest_attempt(monkeypatch):
    asked = _serve_details(monkeypatch, _details([1, 3], pod="p"))
    attempt, pod = _run(
        _oss_follow.resolve_real_attempt("r-abc", None, org="", project="my-agents", domain="development")
    )
    assert attempt == 3 and pod == {"pod": "p", "namespace": "flyte", "container": "primary"}
    assert asked[0].name == "a0"
    assert (asked[0].run.project, asked[0].run.domain) == ("my-agents", "development")


def test_union_logs_are_the_stock_tool_untouched(monkeypatch):
    async def refuse(action_id):
        raise AssertionError("the attempt lookup is OSS only")

    monkeypatch.setattr(remote.ActionDetails, "get_details", SimpleNamespace(aio=refuse))
    for profile in (UNION, None):
        stock = Stock(RuntimeError("Log stream not available for action a0 in run r-abc."))
        assert _oss_follow.wrap_get_logs(stock, profile) is stock
    ok = Stock({"available": False})
    assert _oss_follow.wrap_get_logs(ok, UNION) is ok


# --- criterion 27: report -----------------------------------------------------------------------------


def _unavailable(exc: BaseException) -> str:
    return _oss_follow.report_unavailable_reason(exc)


def test_report_failures_are_classified():
    assert _unavailable(ConnectError(Code.PERMISSION_DENIED, "no")) == "permission"
    assert _unavailable(RuntimeError("AccessDenied: signing denied")) == "permission"
    assert _unavailable(ConnectError(Code.NOT_FOUND, "attempt 0 not found")) == "missing_report"
    assert _unavailable(RuntimeError("No report is available")) == "missing_report"
    assert _unavailable(httpx.ConnectError("localhost:30002 refused")) == "state_store"
    assert _unavailable(OSError("read failed")) == "state_store"
    assert _unavailable(ConnectError(Code.UNAVAILABLE, "down")) == "unreachable"


def _oss_world(flyte_world, monkeypatch, console_host: str = ""):
    monkeypatch.setattr(_factory_base, "PROFILE", _oss(console_host))
    run = status_run(flyte_world, "run")
    run.url = "http://flyte-http.flyte:8090/v2/domain/development/project/my-agents/runs/run"
    return run


def test_a_readable_oss_report_gives_the_timeline_and_open_questions(flyte_world, monkeypatch):
    _oss_world(flyte_world, monkeypatch, "console.example.com")
    result = _factory.read_status("alpha", rows=3)
    assert "error" not in result and len(result["timeline"]) == 3
    assert result["open_questions"] == [
        {"condition": None, "phase": None, "text": "approve?", "timed_out": False}
    ]
    assert result["url"] == "https://console.example.com/v2/domain/development/project/my-agents/runs/run"


def _conditions(monkeypatch, *prompts: str) -> None:
    rows = [
        SimpleNamespace(name=f"ask-{i}", phase="PAUSED", prompt=p, run_name="run")
        for i, p in enumerate(prompts)
    ]

    monkeypatch.setattr(remote.Condition, "listall", staticmethod(lambda **kw: rows))


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (ConnectError(Code.PERMISSION_DENIED, "https://signed.example/x?X-Amz-Signature=abc"), "permission"),
        (RuntimeError("No report is available"), "missing_report"),
        (httpx.ConnectError("signed url http://localhost:30002/b?sig=zzz"), "state_store"),
    ],
)
def test_an_unreadable_oss_report_says_why_and_still_has_the_questions(
    flyte_world, monkeypatch, raised, reason
):
    run = _oss_world(flyte_world, monkeypatch)
    run.report = None
    monkeypatch.setattr(_factory_base, "report_html", lambda r: (_ for _ in ()).throw(raised))
    monkeypatch.setattr(_oss_follow, "read_stored_report", lambda r, p: None)
    _conditions(monkeypatch, "approve the plan?")
    result = _factory.read_status("alpha")
    assert result["reason"] == reason and reason.replace("_", " ") in result["error"]
    assert result["open_questions"] == [
        {"condition": "ask-0", "phase": "PAUSED", "text": "approve the plan?", "timed_out": None}
    ]
    text = json.dumps(result)
    assert "signed.example" not in text and "30002" not in text and "flyte-http.flyte:8090" not in text


def test_an_oss_report_the_signed_link_cannot_reach_is_read_from_storage(flyte_world, monkeypatch):
    run = _oss_world(flyte_world, monkeypatch)
    page = run.report

    def refused(r):
        raise httpx.ConnectError("localhost:30002")

    monkeypatch.setattr(_factory_base, "report_html", refused)
    monkeypatch.setattr(_oss_follow, "read_stored_report", lambda r, p: page)
    result = _factory.read_status("alpha", rows=2)
    assert "error" not in result and result["activity"] == "reviewing" and len(result["timeline"]) == 2


def test_the_storage_read_uses_the_newest_attempts_report_uri(monkeypatch):
    run = SimpleNamespace(name="r")
    details = _details([1, 2])
    details.pb2.attempts[-1].outputs = SimpleNamespace(report_uri="s3://bucket/r/report.html")
    _serve_details(monkeypatch, details)
    read: list[str] = []

    async def read_uri(uri):
        read.append(uri)
        return "<html>page</html>"

    monkeypatch.setattr(_oss_follow, "read_report_via_storage", read_uri)
    assert _oss_follow.read_stored_report(cast(Any, run), _oss()) == "<html>page</html>"
    assert read == ["s3://bucket/r/report.html"]


def test_no_report_uri_is_no_report(monkeypatch):
    details = _details([1])
    details.pb2.attempts[-1].outputs = SimpleNamespace(report_uri="")
    _serve_details(monkeypatch, details)
    assert _oss_follow.read_stored_report(cast(Any, SimpleNamespace(name="r")), _oss()) is None


def test_storage_read_failure_is_none(monkeypatch):
    import flyte.storage

    async def broken(path, **kw):
        yield b""
        raise OSError("no route")

    monkeypatch.setattr(flyte.storage, "get_stream", broken)
    assert _run(_oss_follow.read_report_via_storage("s3://b/report.html")) is None
    assert _run(_oss_follow.read_report_via_storage("")) is None


def test_the_storage_read_returns_the_text(monkeypatch):
    import flyte.storage

    async def chunks(path, **kw):
        yield b"<ht"
        yield b"ml>"

    monkeypatch.setattr(flyte.storage, "get_stream", chunks)
    assert _run(_oss_follow.read_report_via_storage("s3://b/report.html")) == "<html>"


# --- criterion 28: URLs -------------------------------------------------------------------------------


def test_a_console_host_gives_the_console_url():
    url, note = _oss_follow.find_openable_url(INTERNAL, _oss("console.example.com"))
    assert url == "https://console.example.com/v2/domain/development/project/my-agents/runs/r-abc"
    assert note is None


def test_without_a_console_host_there_is_no_url_and_a_note():
    url, note = _oss_follow.find_openable_url(INTERNAL, _oss())
    assert url is None and note and "console_host" in note and "no openable URL" in note


@pytest.mark.parametrize(
    "host",
    ["flyte-http.flyte:8090", "flyte-http", "x.flyte.svc:80", "a.svc.cluster.local", "localhost:30080"],
)
def test_internal_hosts_are_never_returned(host):
    url, _ = _oss_follow.find_openable_url(f"http://{host}/v2/runs/r-abc", _oss())
    assert url is None


def test_an_external_url_is_kept():
    for url in ("https://github.com/o/r/pull/1", "https://console.example.com/x"):
        assert _oss_follow.find_openable_url(url, _oss()) == (url, None)


def test_fix_urls_rewrites_nested_url_keys_only():
    result = {
        "url": INTERNAL,
        "run": {"run_url": INTERNAL, "name": "r-abc", "pr_url": "https://github.com/o/r/pull/1"},
        "runs": [{"url": INTERNAL}],
        "note": INTERNAL,
    }
    fixed = _oss_follow.fix_urls(result, _oss("c.example.com"))
    assert isinstance(fixed, dict)
    want = "https://c.example.com/v2/domain/development/project/my-agents/runs/r-abc"
    assert fixed["url"] == want and fixed["run"]["run_url"] == want and fixed["runs"][0]["url"] == want
    assert fixed["run"]["pr_url"] == "https://github.com/o/r/pull/1"
    assert "flyte-http.flyte" not in fixed["note"]  # a URL in any other string is replaced in place


def test_fix_urls_without_a_console_says_there_is_none():
    fixed: Any = _oss_follow.fix_urls({"url": INTERNAL, "name": "n"}, _oss())
    assert fixed["url"] is None and "no openable URL" in fixed["url_note"]
    assert "flyte-http.flyte:8090" not in json.dumps(fixed)


def test_status_never_returns_the_internal_url(flyte_world, monkeypatch):
    _oss_world(flyte_world, monkeypatch)
    result = _factory.read_status("alpha")
    assert result["url"] is None and "console_host" in result["url_note"]
    assert "flyte-http.flyte:8090" not in json.dumps(result)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8090",
        "10.0.3.7:8090",
        "172.16.0.1",
        "192.168.1.5",
        "169.254.169.254",
        "100.64.0.1",
        "[::1]:8090",
        "[fe80::1]",
        "[fd00::5]:80",
        "[::ffff:10.0.0.1]",
        "minio.flyte:9000",
        "flyte-http.flyte",
        "foo.default",
        "db.internal",
        "printer.local",
        "app.localhost",
        "localhost.localdomain",
        "LOCALHOST:30080",
        "minio.flyte.:9000",
        "2130706433",
        "10.1",
    ],
)
def test_every_internal_host_shape_is_not_openable(host):
    url, note = _oss_follow.find_openable_url(f"http://{host}/bucket/report.html?X-Amz-Signature=abc", _oss())
    assert url is None and note


@pytest.mark.parametrize(
    "host", ["flyte.example.com", "console.mycorp.io", "github.com", "8.8.8.8", "[2001:4860::1]"]
)
def test_public_hosts_stay_openable(host):
    url = f"https://{host}/v2/runs/r1"
    assert _oss_follow.find_openable_url(url, _oss()) == (url, None)


def test_a_localhost_run_url_is_rewritten_when_console_host_is_set():
    url, note = _oss_follow.find_openable_url("http://localhost:30080/v2/runs/r1", _oss("c.example.com"))
    assert url == "https://c.example.com/v2/domain/development/project/my-agents/runs/r1" and note is None


def test_an_ip_literal_run_url_is_rewritten_when_console_host_is_set():
    url, _ = _oss_follow.find_openable_url("http://10.0.3.7:8090/v2/runs/r1", _oss("c.example.com"))
    assert url == "https://c.example.com/v2/domain/development/project/my-agents/runs/r1"


@pytest.mark.parametrize("key", ["endpoint", "link", "uri", "href", "report_uri", "app_link", "Href"])
def test_fix_urls_covers_every_link_key(key):
    fixed: Any = _oss_follow.fix_urls({key: "http://10.0.3.7:8090/v2/runs/r1"}, _oss())
    assert fixed[key] is None and "no openable URL" in fixed["url_note"]
    assert "10.0.3.7" not in json.dumps(fixed)


def test_fix_urls_rewrites_urls_inside_text():
    text = (
        f"Run started: {INTERNAL}. Report at http://minio.flyte:9000/b/r.html?sig=zz, "
        "see https://github.com/o/r/pull/1 (docs: http://[::1]:8080/x)"
    )
    fixed: Any = _oss_follow.fix_urls({"message": text, "items": [text]}, _oss("c.example.com"))
    want = (
        "Run started: https://c.example.com/v2/domain/development/project/my-agents/runs/r-abc. "
        "Report at <no openable URL>, see https://github.com/o/r/pull/1 (docs: <no openable URL>)"
    )
    assert fixed["message"] == want and fixed["items"] == [want]


def test_fix_urls_keeps_storage_uris_but_not_internal_http_links_to_them():
    result = {
        "report_uri": "s3://bucket/report.html",
        "output_uri": "gs://bucket/out",
        "message": "stored at s3://bucket/report.html",
        "signed": "http://localhost:30002/bucket/report.html?X-Amz-Signature=abc",
    }
    fixed: Any = _oss_follow.fix_urls(result, _oss())
    assert fixed["report_uri"] == result["report_uri"] and fixed["output_uri"] == result["output_uri"]
    assert fixed["message"] == result["message"]
    assert fixed["signed"] == NO_URL_TEXT


def test_text_urls_are_untouched_on_union():
    result = {"message": f"at {INTERNAL}", "endpoint": "http://10.0.0.1/x"}
    assert _oss_follow.fix_urls(result, UNION) is result


def test_oss_status_without_a_report_page_says_why_and_has_the_questions(flyte_world, monkeypatch):
    run = _oss_world(flyte_world, monkeypatch)
    run.report = None
    monkeypatch.setattr(_factory_base, "report_html", lambda r: "None")
    _conditions(monkeypatch, "approve the plan?")
    result = _factory.read_status("alpha")
    assert "no report page" in result["error"] and result["reason"] == "missing_report"
    assert result["open_questions"] == [
        {"condition": "ask-0", "phase": "PAUSED", "text": "approve the plan?", "timed_out": None}
    ]


def test_both_oss_status_paths_give_the_same_question_keys(flyte_world, monkeypatch):
    _oss_world(flyte_world, monkeypatch)
    readable = _factory.read_status("alpha")["open_questions"]
    monkeypatch.setattr(_factory_base, "report_html", lambda r: "")
    _conditions(monkeypatch, "q?")
    empty = _factory.read_status("alpha")["open_questions"]
    keys = {"condition", "phase", "text", "timed_out"}
    assert all(q.keys() == keys for q in readable + empty) and readable and empty


# --- criterion 29: Union is unchanged -----------------------------------------------------------------


def test_fix_urls_is_the_identity_on_union():
    result = {"url": INTERNAL, "nested": [{"url": INTERNAL}]}
    for profile in (UNION, None):
        assert _oss_follow.fix_urls(result, profile) is result
        assert _oss_follow.find_openable_url(INTERNAL, profile) == (INTERNAL, None)


def test_union_status_keeps_its_url_and_its_message_and_takes_no_fallback(flyte_world, monkeypatch):
    run = status_run(flyte_world, "run")
    assert _factory_base.PROFILE.backend.kind == "union"

    def refused(r):
        raise httpx.ConnectError("down")

    def forbidden(*args, **kwargs):
        raise AssertionError("the OSS fallback ran on Union")

    monkeypatch.setattr(_factory_base, "report_html", refused)
    for name in ("read_stored_report", "report_unavailable"):
        monkeypatch.setattr(_oss_follow, name, forbidden)
    result = _factory.read_status("alpha")
    assert result["error"] == "Run report unavailable; open the run URL for details"
    assert result["url"] == run.url and not {"reason", "open_questions", "url_note"} & result.keys()


# --- project lookup by label: OSS list responses carry no labels ---------------------------------------


def _labelled_world(monkeypatch, labels_by_run: dict[str, dict[str, str]]):
    """A server whose listings have no labels and whose run details do. Returns the names it was asked for."""
    asked: list[str] = []
    listed = [SimpleNamespace(name=n, pb2=SimpleNamespace(labels={})) for n in labels_by_run]
    seen: dict[str, Any] = {}

    def listall(**kwargs):
        seen.update(kwargs)
        return iter(listed[: kwargs["limit"]])

    def details(name):
        asked.append(name)
        values = labels_by_run[name]
        return SimpleNamespace(
            pb2=SimpleNamespace(run_spec=SimpleNamespace(labels=SimpleNamespace(values=values)))
        )

    monkeypatch.setattr(remote.Run, "listall", staticmethod(listall))
    monkeypatch.setattr(remote.RunDetails, "get", staticmethod(details))
    return asked, seen, listed


def test_a_project_is_found_by_the_label_on_its_run_details_on_oss(monkeypatch):
    asked, seen, listed = _labelled_world(
        monkeypatch,
        {
            "r3": {},
            "r2": {_factory_base.LABEL_PROJECT: "alpha"},
            "r1": {_factory_base.LABEL_PROJECT: "alpha"},
        },
    )
    found = _oss_follow.find_run_by_label(_oss(), {_factory_base.LABEL_PROJECT: "alpha"})
    assert found is listed[1]  # the newest match: the listing is newest first
    assert asked == ["r3", "r2"]
    assert seen["sort_by"] == ("created_at", "desc") and "with_labels" not in seen


def test_the_label_lookup_reads_at_most_max_runs_details(monkeypatch):
    asked, _, _ = _labelled_world(monkeypatch, {f"r{i}": {} for i in range(10)})
    assert _oss_follow.find_run_by_label(_oss(), {"k": "v"}, max_runs=4) is None
    assert asked == ["r0", "r1", "r2", "r3"]


def test_the_label_lookup_stops_when_its_time_is_up(monkeypatch):
    asked, _, _ = _labelled_world(monkeypatch, {f"r{i}": {} for i in range(10)})
    read = remote.RunDetails.get

    def slow(name):
        time.sleep(0.06)
        return read(name=name)

    monkeypatch.setattr(remote.RunDetails, "get", staticmethod(slow))
    assert _oss_follow.find_run_by_label(_oss(), {"k": "v"}, budget_s=0.05) is None
    assert asked == ["r0"]


def test_the_label_lookup_skips_a_run_whose_details_fail(monkeypatch):
    _labelled_world(monkeypatch, {"r2": {}, "r1": {"k": "v"}})

    def details(name):
        if name == "r2":
            raise ConnectError(Code.NOT_FOUND, "gone")
        return SimpleNamespace(
            pb2=SimpleNamespace(run_spec=SimpleNamespace(labels=SimpleNamespace(values={"k": "v"})))
        )

    monkeypatch.setattr(remote.RunDetails, "get", staticmethod(details))
    found = _oss_follow.find_run_by_label(_oss(), {"k": "v"})
    assert found is not None and found.name == "r1"


def test_the_label_lookup_reports_unreachable_when_the_listing_fails(monkeypatch):
    def down(**kwargs):
        raise ConnectError(Code.UNAVAILABLE, "no route")

    monkeypatch.setattr(remote.Run, "listall", staticmethod(down))
    found = _oss_follow.find_run_by_label(_oss(), {"k": "v"})
    assert isinstance(found, dict) and found["why"] == "unreachable"


def test_status_says_unreachable_not_not_found_when_the_label_lookup_cannot_ask(flyte_world, monkeypatch):
    _oss_world(flyte_world, monkeypatch)
    flyte_world.runs.pop("run", None)
    monkeypatch.setattr(
        _oss_follow, "find_run_by_label", lambda profile, labels: {"why": "unreachable", "error": "down"}
    )
    result = _factory.read_status("gone-project")
    assert result["why"] == "unreachable" and result["name"] == "gone-project"
    assert "No project or run found" not in result["error"]


def test_the_label_lookup_is_never_used_on_union(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the SDK was called on a Union profile")

    monkeypatch.setattr(remote.Run, "listall", staticmethod(refuse))
    monkeypatch.setattr(remote.RunDetails, "get", staticmethod(refuse))
    assert _oss_follow.find_run_by_label(UNION, {"k": "v"}) is None
    assert _oss_follow.find_run_by_label(None, {"k": "v"}) is None


def test_status_by_project_name_uses_the_label_lookup_on_oss(flyte_world, monkeypatch):
    run = _oss_world(flyte_world, monkeypatch)
    wanted = {_factory_base.LABEL_PROJECT: "unlabelled-in-listings"}
    flyte_world.runs.pop("run", None)
    seen: list[Any] = []

    def by_label(profile, labels):
        seen.append(labels)
        return run

    monkeypatch.setattr(_oss_follow, "find_run_by_label", by_label)
    result = _factory.read_status("unlabelled-in-listings")
    assert seen == [wanted] and result["run"] == "run"


def test_status_by_project_name_never_uses_the_label_lookup_on_union(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory_base, "PROFILE", UNION)
    flyte_world.runs.pop("run", None)

    def refuse(*args, **kwargs):
        raise AssertionError("the OSS label lookup ran on a Union profile")

    monkeypatch.setattr(remote.RunDetails, "get", staticmethod(refuse))
    assert _factory.read_status("nobody")["error"] == "No project or run found for this name"


#: The modules that run only inside the factory MCP server, whose image has `mcp`.
MCP_SERVER_MODULES = {"_app_tools", "_factory_mcp", "_mcp_budget"}


def test_only_the_mcp_server_modules_need_the_mcp_package():
    # Measured on the devbox (#49): `_factory` imported `_oss_follow`, which imported `mcp` at the top; the
    # factory's task image has no `mcp`, so every factory task (factory.status included) failed to load.
    probe = textwrap.dedent(
        """
        import importlib, pkgutil, sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "mcp":
                    raise ModuleNotFoundError("blocked " + name, name=name)

        sys.meta_path.insert(0, Block())
        import flyte_agent_factory

        needs = []
        for info in pkgutil.iter_modules(flyte_agent_factory.__path__):
            try:
                importlib.import_module("flyte_agent_factory." + info.name)
            except ModuleNotFoundError as e:
                if e.name and e.name.split(".")[0] == "mcp":
                    needs.append(info.name)
            except Exception:
                pass
        print(",".join(sorted(needs)))
        """
    )
    src = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True)
    assert set(filter(None, out.stdout.strip().splitlines()[-1].split(","))) == MCP_SERVER_MODULES
