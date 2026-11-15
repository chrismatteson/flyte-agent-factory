"""`_factory_base`: label-filtered run listings, with a client-side fallback where the server rejects them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote

from flyte_agent_factory import _factory_base

OSS_ERROR = "failed to convert filters: invalid filter field: labels.factory-project"


def _run(name: str, **labels: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, pb2=SimpleNamespace(labels=labels))


class FakeListall:
    """`Run.listall` as the SDK has it: calling it iterates, `.aio` iterates asynchronously.

    With `reject` the server cannot filter on labels: it raises on the first read, like the SDK.
    """

    def __init__(self, runs, *, reject: Exception | None = None):
        self.runs = runs
        self.reject = reject
        self.calls: list[dict[str, Any]] = []
        self.read = 0  # runs handed out, to show the fallback stops reading once it has enough

    def _rows(self, kw):
        self.calls.append(kw)
        if self.reject and ("with_labels" in kw or "with_label_keys" in kw):
            raise self.reject
        limit = kw.get("limit", 100)
        for i, run in enumerate(self.runs):
            if i >= limit:
                return
            self.read += 1
            yield run

    def __call__(self, **kw):
        return self._rows(kw)

    async def aio(self, **kw):
        for run in self._rows(kw):
            yield run


def _install_both(monkeypatch, runs, reject=None) -> FakeListall:
    fake = FakeListall(runs, reject=reject)

    class Listall:
        def __call__(self, **kw):
            return fake(**kw)

        aio = staticmethod(fake.aio)

    monkeypatch.setattr(remote.Run, "listall", Listall())
    return fake


def _oss_error() -> ConnectError:
    return ConnectError(Code.INVALID_ARGUMENT, OSS_ERROR)


def _mixed():
    # Non-matching runs come first, so a limit applied before filtering would return nothing.
    return [
        *(_run(f"other{i}", **{"factory-project": "b"}) for i in range(5)),
        _run("m1", **{"factory-project": "a", "factory-gen": "1"}),
        _run("skip", **{"factory-project": "a", "factory-gen": "2"}),
        _run("m2", **{"factory-project": "a", "factory-gen": "1"}),
        _run("m3", **{"factory-project": "a", "factory-gen": "1"}),
    ]


async def _aio(labels, **kw):
    return await _factory_base.list_runs_aio(labels, **kw)


def _names(runs):
    return [r.name for r in runs]


def test_the_server_path_passes_everything_through(monkeypatch):
    fake = _install_both(monkeypatch, _mixed())
    _factory_base.list_runs(
        {"factory-project": "a"}, limit=3, in_phase=("RUNNING",), sort_by=("created_at", "desc")
    )
    assert fake.calls == [
        {
            "with_labels": {"factory-project": "a"},
            "limit": 3,
            "in_phase": ("RUNNING",),
            "sort_by": ("created_at", "desc"),
        }
    ]


async def test_the_server_path_passes_everything_through_async(monkeypatch):
    fake = _install_both(monkeypatch, [])
    await _aio({"k": "v"}, limit=2, task_name="t")
    assert fake.calls == [{"with_labels": {"k": "v"}, "limit": 2, "task_name": "t"}]


def test_no_limit_is_not_sent(monkeypatch):
    fake = _install_both(monkeypatch, [])
    _factory_base.list_runs({"k": "v"})
    assert "limit" not in fake.calls[0]


def test_rejected_labels_are_filtered_here_in_server_order_with_limit_after_filtering(monkeypatch):
    fake = _install_both(monkeypatch, _mixed(), reject=_oss_error())
    found = _factory_base.list_runs(
        {"factory-project": "a", "factory-gen": "1"}, limit=2, in_phase=("RUNNING",)
    )
    assert _names(found) == ["m1", "m2"]
    retry = fake.calls[1]
    assert "with_labels" not in retry
    assert retry["in_phase"] == ("RUNNING",)
    assert retry["limit"] > 100  # the SDK's default of 100 must not cut the unfiltered listing short
    assert fake.read == 8  # stopped at m2, the eighth run: m3 was never read


def test_rejected_labels_without_a_limit_return_every_match_up_to_the_sdk_default(monkeypatch):
    _install_both(monkeypatch, _mixed(), reject=_oss_error())
    assert _names(_factory_base.list_runs({"factory-project": "a"})) == ["m1", "skip", "m2", "m3"]


def test_rejected_labels_without_a_limit_stop_at_the_sdk_default_of_100(monkeypatch):
    # Without a limit the server path returns at most 100 runs; the fallback must not return more.
    runs = [_run(f"m{i}", **{"factory-project": "a"}) for i in range(150)]
    fake = _install_both(monkeypatch, runs, reject=_oss_error())
    assert len(_factory_base.list_runs({"factory-project": "a"})) == 100
    assert fake.read == 100


async def test_rejected_labels_without_a_limit_stop_at_the_sdk_default_async(monkeypatch):
    runs = [_run(f"m{i}", **{"factory-project": "a"}) for i in range(150)]
    _install_both(monkeypatch, runs, reject=_oss_error())
    assert len(await _factory_base.list_runs_aio({"factory-project": "a"})) == 100


def _keyed() -> list[SimpleNamespace]:
    return [
        _run("plain"),
        _run("i1", **{"factory-issue": "7"}),
        _run("other", **{"factory-project": "a"}),
        _run("i2", **{"factory-issue": "9", "factory-project": "b"}),
    ]


def test_label_keys_are_sent_to_the_server_when_it_accepts_them(monkeypatch):
    fake = _install_both(monkeypatch, _keyed())
    _factory_base.list_runs({}, keys=["factory-issue"], limit=5, task_name="t")
    assert fake.calls == [{"with_label_keys": ["factory-issue"], "limit": 5, "task_name": "t"}]


def test_rejected_label_keys_keep_runs_that_carry_the_key_whatever_its_value(monkeypatch):
    oss = ConnectError(Code.INVALID_ARGUMENT, "failed to convert filters: invalid filter field: labels")
    fake = _install_both(monkeypatch, _keyed(), reject=oss)
    found = _factory_base.list_runs({}, keys=["factory-issue"], limit=5, task_name="t")
    assert _names(found) == ["i1", "i2"]
    assert "with_label_keys" not in fake.calls[1] and fake.calls[1]["task_name"] == "t"


def test_rejected_labels_and_keys_must_both_match(monkeypatch):
    _install_both(monkeypatch, _keyed(), reject=_oss_error())
    found = _factory_base.list_runs({"factory-project": "b"}, keys=["factory-issue"])
    assert _names(found) == ["i2"]


async def test_rejected_label_keys_async_with_limit(monkeypatch):
    _install_both(monkeypatch, _keyed(), reject=_oss_error())
    assert _names(await _factory_base.list_runs_aio({}, keys=["factory-issue"], limit=1)) == ["i1"]


def test_rejected_labels_with_fewer_matches_than_the_limit(monkeypatch):
    _install_both(monkeypatch, _mixed(), reject=_oss_error())
    assert _names(_factory_base.list_runs({"factory-gen": "2"}, limit=5)) == ["skip"]


def test_a_zero_limit_returns_nothing_on_the_fallback(monkeypatch):
    _install_both(monkeypatch, _mixed(), reject=_oss_error())
    assert _factory_base.list_runs({"factory-project": "a"}, limit=0) == []


async def test_rejected_labels_are_filtered_here_async(monkeypatch):
    fake = _install_both(monkeypatch, _mixed(), reject=_oss_error())
    found = await _aio({"factory-project": "a", "factory-gen": "1"}, limit=2, sort_by=("created_at", "desc"))
    assert _names(found) == ["m1", "m2"]
    assert "with_labels" not in fake.calls[1]
    assert fake.calls[1]["sort_by"] == ("created_at", "desc")
    assert fake.read == 8


async def test_iter_runs_aio_stops_reading_when_the_caller_stops(monkeypatch):
    fake = _install_both(monkeypatch, _mixed(), reject=_oss_error())
    async for run in _factory_base.iter_runs_aio({"factory-project": "a"}):
        assert run.name == "m1"
        break
    assert fake.read == 6


@pytest.mark.parametrize(
    "error",
    [
        ConnectError(Code.UNAVAILABLE, "connection refused"),
        ConnectError(Code.INVALID_ARGUMENT, "invalid filter field: phase"),
        RuntimeError(OSS_ERROR),  # right words, not the server's error type
    ],
)
def test_any_other_error_propagates(monkeypatch, error):
    fake = _install_both(monkeypatch, _mixed(), reject=error)
    with pytest.raises(type(error)) as caught:
        _factory_base.list_runs({"factory-project": "a"}, limit=1)
    assert caught.value is error
    assert len(fake.calls) == 1


@pytest.mark.parametrize("error", [ConnectError(Code.UNAVAILABLE, "down"), RuntimeError(OSS_ERROR)])
async def test_any_other_error_propagates_async(monkeypatch, error):
    fake = _install_both(monkeypatch, _mixed(), reject=error)
    with pytest.raises(type(error)) as caught:
        await _aio({"factory-project": "a"}, limit=1)
    assert caught.value is error
    assert len(fake.calls) == 1


def test_a_call_site_works_against_a_server_that_rejects_label_filters(monkeypatch):
    runs = [
        _run("other", **{_factory_base.LABEL_PROJECT: "b"}),
        _run("old", **{_factory_base.LABEL_PROJECT: "a", _factory_base.LABEL_GEN: "1"}),
        _run("new", **{_factory_base.LABEL_PROJECT: "a", _factory_base.LABEL_GEN: "2"}),
    ]
    fake = _install_both(monkeypatch, runs, reject=_oss_error())
    assert _names(_factory_base._labelled_runs("a", 2, in_phase=("RUNNING",))) == ["new"]
    assert fake.calls[1]["sort_by"] == ("created_at", "desc")
    assert _names(_factory_base._labelled_runs("a")) == ["old", "new"]


def _slot_run(name: str, task: str, **labels: str) -> SimpleNamespace:
    run = _run(name, **labels)
    run.to_dict = lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}}
    return run


def test_reconcile_lists_issue_runs_on_a_server_that_rejects_label_filters(monkeypatch):
    from flyte_agent_factory import _reconcile

    slot = _factory_base.SLOTS[0]
    runs = [
        _slot_run("bare", slot),
        _slot_run("issue", slot, **{"factory-issue": "3"}),
        _slot_run("elsewhere", "not-a-slot", **{"factory-issue": "4"}),
    ]
    fake = _install_both(monkeypatch, runs, reject=_oss_error())
    assert _names(_reconcile.project_runs()) == ["issue"]
    retries = [c for c in fake.calls if "with_label_keys" not in c]
    assert len(retries) == 2 and all(c["sort_by"] == ("created_at", "desc") for c in retries)


def test_loop_spend_lists_loop_runs_on_a_server_that_rejects_label_filters(monkeypatch):
    from flyte_agent_factory import _loop_flow

    runs = [_slot_run("bare", "x"), _slot_run("loop", "x", **{"factory-loop": "1"})]
    fake = _install_both(monkeypatch, runs, reject=_oss_error())
    picked: list[str] = []

    class Skip:
        """`exclude_run` that records each listed run `_spend` reaches, then skips it."""

        def __eq__(self, name):
            picked.append(name)
            return True

    _loop_flow._spend(0.0, exclude_run=Skip())  # type: ignore[arg-type]
    assert set(picked) == {"loop", "bare"}  # "loop" by its key, "bare" by the task-name listing
    assert any("with_label_keys" in c for c in fake.calls)


class FailsAfterFirstRun(FakeListall):
    """A listing that hands out one run and then fails, as a later page would."""

    def _rows(self, kw):
        self.calls.append(kw)
        self.read += 1
        yield self.runs[0]
        assert self.reject is not None
        raise self.reject


@pytest.mark.parametrize(
    "error", [ConnectError(Code.UNAVAILABLE, "down"), ConnectError(Code.INVALID_ARGUMENT, OSS_ERROR)]
)
def test_an_error_after_the_first_run_propagates_without_relisting(monkeypatch, error):
    fake = FailsAfterFirstRun([_run("m1", **{"factory-project": "a"})], reject=error)
    monkeypatch.setattr(remote.Run, "listall", fake)
    with pytest.raises(ConnectError) as raised:
        _factory_base.list_runs({"factory-project": "a"}, limit=5)
    assert raised.value is error and len(fake.calls) == 1


@pytest.mark.parametrize(
    "error", [ConnectError(Code.UNAVAILABLE, "down"), ConnectError(Code.INVALID_ARGUMENT, OSS_ERROR)]
)
async def test_an_error_after_the_first_run_propagates_without_relisting_async(monkeypatch, error):
    fake = FailsAfterFirstRun([_run("m1", **{"factory-project": "a"})], reject=error)

    class Listall:
        aio = staticmethod(fake.aio)

    monkeypatch.setattr(remote.Run, "listall", Listall())
    seen = []
    with pytest.raises(ConnectError) as raised:
        async for run in _factory_base.iter_runs_aio({"factory-project": "a"}, limit=5):
            seen.append(run.name)
    assert raised.value is error and seen == ["m1"] and len(fake.calls) == 1


def test_the_server_path_stops_reading_at_the_limit(monkeypatch):
    # The SDK fetches the next page when asked for a run past a full one; at the limit nothing is asked.
    runs = [_run(f"r{i}", **{"k": "v"}) for i in range(10)]

    class Unlimited(FakeListall):
        def _rows(self, kw):
            self.calls.append(kw)
            for run in self.runs:
                self.read += 1
                yield run

    fake = Unlimited(runs)
    monkeypatch.setattr(remote.Run, "listall", fake)
    assert len(_factory_base.list_runs({"k": "v"}, limit=4)) == 4
    assert fake.read == 4
