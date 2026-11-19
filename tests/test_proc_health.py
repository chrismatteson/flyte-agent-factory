"""`_proc_health`: what the pod's process table looks like, and where that is said."""

import ast
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_tests,
    _load_clock,
    _proc_health,
    _supervise_support,
    _timeline,
)

# The conftest fixture replaces `_proc_health.snapshot` for every test; keep the real one for the /proc tests.
_REAL_SNAPSHOT = _proc_health.snapshot


def _fake_proc(root: Path, *, init: str | None = "tini", states: dict[int, str]) -> Path:
    for pid, state in states.items():
        (root / str(pid)).mkdir(parents=True)
        # A command name with a space and a parenthesis, as real ones have.
        (root / str(pid) / "stat").write_text(f"{pid} (name with ) paren) {state} 1 1 1 0 -1\n")
    (root / "self").mkdir()
    (root / "cpuinfo").write_text("not a process\n")
    if init is not None:
        (root / "1").mkdir(exist_ok=True)
        (root / "1" / "comm").write_text(init + "\n")
    return root


def _health(zombies: int, init: str | None = "a0") -> _proc_health.ProcHealth:
    return _proc_health.ProcHealth(processes=300, zombies=zombies, init=init)


def test_snapshot_counts_processes_and_zombies_of_a_fake_proc(tmp_path):
    proc = _fake_proc(tmp_path, states={1: "S", 7: "Z", 8: "R", 9: "Z"})

    health = _REAL_SNAPSHOT(proc)

    assert health == _proc_health.ProcHealth(processes=4, zombies=2, init="tini")
    assert health.describe() == "PID 1 tini, 4 processes, 2 zombies"


def test_snapshot_skips_a_process_that_vanished_or_has_a_broken_stat(tmp_path):
    proc = _fake_proc(tmp_path, states={1: "S", 5: "Z"})
    (proc / "6").mkdir()  # gone between listing and reading: no stat
    (proc / "7").mkdir()
    (proc / "7" / "stat").write_text("")  # no fields at all
    (proc / "8").mkdir()
    (proc / "8" / "stat").write_text("8 (x) ")

    assert _REAL_SNAPSHOT(proc) == _proc_health.ProcHealth(processes=2, zombies=1, init="tini")


def test_snapshot_has_no_init_when_pid_1_cannot_be_read(tmp_path):
    health = _REAL_SNAPSHOT(_fake_proc(tmp_path, init=None, states={5: "S"}))

    assert health.init is None
    assert health.describe() == "PID 1 unknown, 1 processes, 0 zombies"


def test_snapshot_of_the_real_proc_sees_this_process():
    health = _REAL_SNAPSHOT()

    assert health.processes >= 1
    assert health.zombies >= 0


def test_a_pod_is_burdened_from_the_threshold():
    below = _proc_health.ZOMBIES_WORTH_NAMING - 1
    assert not _health(below).burdened
    assert _health(_proc_health.ZOMBIES_WORTH_NAMING).burdened


def _timed_out(tmp_path) -> tuple[int, str]:
    # A limit already spent: the first poll of the load clock says so.
    class Expired(_load_clock.LoadClock):
        def poll(self):
            return True

    import pytest

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_load_clock, "LoadClock", Expired)
        return _gate_base._run(["sleep", "30"], tmp_path, timeout=1)


def test_a_timeout_names_the_zombies_when_the_pod_is_burdened(tmp_path, monkeypatch):
    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: _health(4321))

    code, message = _timed_out(tmp_path)

    assert code == 124
    assert message.startswith("command timed out after 1 seconds (")
    assert "4321 zombie processes under PID 1 a0" in message


def test_a_timeout_on_a_healthy_pod_is_the_plain_message(tmp_path):
    code, message = _timed_out(tmp_path)

    assert code == 124
    assert message.startswith("command timed out after 1 seconds (") and message.endswith(")")
    assert "zombie" not in message


def _row(monkeypatch, health: _proc_health.ProcHealth) -> str:
    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: health)
    work = _gate_tests.GateWork()
    return _supervise_support._pre_review_row(212.4, 140.2, work)


def test_the_pre_review_row_is_unchanged_on_a_healthy_pod(monkeypatch):
    assert "PID 1" not in _row(monkeypatch, _health(3, init="tini"))


@pytest.mark.parametrize("init", ["docker-init", "catatonit", "dumb-init", "systemd"])
def test_the_pre_review_row_is_unchanged_under_another_reaping_init(monkeypatch, init):
    assert "PID 1" not in _row(monkeypatch, _health(0, init=init))


def test_the_pre_review_row_names_the_zombies_of_a_burdened_pod(monkeypatch):
    assert _row(monkeypatch, _health(900, init="tini")).endswith("[PID 1 tini, 300 processes, 900 zombies]")


def test_the_pre_review_row_names_an_init_that_does_not_reap(monkeypatch):
    assert _row(monkeypatch, _health(0, init="a0")).endswith("[PID 1 a0, 300 processes, 0 zombies]")


def test_zombies_are_reported_once_when_they_pass_the_threshold(monkeypatch):
    timeline = _timeline._Timeline()
    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: _health(10))
    warned = _supervise_support._warn_on_zombies(timeline, False)
    assert not warned and not timeline._rows

    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: _health(60))
    warned = _supervise_support._warn_on_zombies(timeline, warned)
    again = _supervise_support._warn_on_zombies(timeline, warned)

    assert warned and again
    assert [text for _, _, text in timeline._rows if "60 zombies" in text] != []
    assert len(timeline._rows) == 1


def test_the_slot_report_opens_with_what_its_pid_1_is(monkeypatch):
    timeline = _timeline._Timeline()
    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: _health(0, init="tini"))
    _supervise_support._write_identity_rows(timeline, "logged in as the slot")
    assert [(emoji, text) for _, emoji, text in timeline._rows] == [
        ("🔑", "logged in as the slot"),
        ("🧹", "PID 1 tini, 300 processes, 0 zombies"),
    ]


def test_the_slot_writes_those_rows_when_it_starts():
    tree = ast.parse((Path(_supervise_support.__file__).parent / "agent.py").read_text())
    (project,) = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "project"]
    called = [
        n.func.attr
        for n in ast.walk(project)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "_write_identity_rows" in called
