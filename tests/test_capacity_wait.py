"""A pre-review that finds no cluster capacity for the test lanes waits and retries; it is not a bounce."""

from __future__ import annotations

import functools
import json

import pytest

from flyte_agent_factory import _capacity, _gates, _supervise_support
from tests import test_release
from tests.test_release import settle

#: The supervisor rig of test_release, as this module's fixture.
rig = test_release.rig

REASON = "slow lane: no capacity: 2 of 5 shards (2 CPUs) not placed within 2m spot + 3m on-demand"


class Checks:
    """A fake `_run_declared_checks`: the first `no_capacity` runs find no capacity, then it passes."""

    def __init__(self, no_capacity: int | float) -> None:
        self.no_capacity = no_capacity
        self.calls = 0

    async def __call__(self, *args, **kwargs) -> str:
        self.calls += 1
        if self.calls > self.no_capacity:
            return ""
        return _capacity.ChecksNoCapacity("`checks.sh` failed: " + _capacity.explain(REASON), REASON)


def use(monkeypatch, checks: Checks) -> None:
    monkeypatch.setattr(_supervise_support, "_run_declared_checks", checks)


async def test_a_capacity_only_pre_review_retries_by_itself_and_then_reviews(rig, monkeypatch):
    checks = Checks(1)
    use(monkeypatch, checks)

    rig.start()
    await rig.advance(120)
    retry_row = f"⏳ pre-review: no capacity ({REASON}); retrying in 5 min (1/3)"
    # The checks return before the retry row is logged: settle on the row, not on the call.
    await settle(rig, lambda: checks.calls >= 1 and retry_row in rig.rows())

    rows = rig.rows()
    assert any(r == retry_row for r in rows), rows
    assert rig.timeline.preflight_bounces == 0 and rig.proc.submitted == []
    assert rig.timeline._phase["kind"] == "waiting" and checks.calls == 1

    await rig.advance(5 * 60)
    await settle(rig, lambda: rig.timeline._phase["kind"] == "done")

    assert checks.calls == 2 and rig.timeline._phase["kind"] == "done"
    assert rig.timeline.preflight_bounces == 0  # the review's own pass message is all the agent hears
    assert not any("Not done yet" in m for m in rig.proc.submitted)
    await rig.finish()


async def test_three_capacity_retries_escalate_to_a_human_with_capacity_wording(rig, monkeypatch):
    checks = Checks(float("inf"))
    use(monkeypatch, checks)

    def logged(text: str) -> bool:
        return any(text in r for r in rig.rows())

    # One backoff at a time, each once its retry row is logged: a single long advance can end
    # before the first pre-review lands, and then schedules the retries past the clock.
    rig.start()
    await rig.advance(120)
    await settle(rig, lambda: logged("retrying in 5 min (1/3)"))
    for minutes, row in ((5, "retrying in 10 min (2/3)"), (10, "retrying in 20 min (3/3)")):
        await rig.advance(minutes * 60)
        await settle(rig, functools.partial(logged, row))
    await rig.advance(20 * 60)
    await settle(rig, lambda: bool(rig.proc.submitted))

    rows = rig.rows()
    for step, minutes in enumerate((5, 10, 20), 1):
        assert f"retrying in {minutes} min ({step}/3)" in " ".join(rows), rows
    assert checks.calls == 4 and _supervise_support._escalated
    assert rig.timeline.preflight_bounces == 0
    (message,) = rig.proc.submitted
    assert "cluster capacity for the test lanes" in message and "not a failure of your change" in message
    assert any("🚨" in r and "no capacity" in r for r in rows)
    await rig.advance(60 * 60)
    assert checks.calls == 4, "an escalated pre-review is not retried again"
    await rig.finish()


async def test_a_capacity_outcome_in_a_mixed_pre_review_bounces_as_today(rig, monkeypatch):
    use(monkeypatch, Checks(1))
    monkeypatch.setattr(_gates, "run", lambda *a, **k: "standards: ruff found 3 problems")

    rig.start()
    await rig.advance(60)
    await settle(rig, lambda: bool(rig.proc.submitted))

    assert rig.timeline.preflight_bounces == 1
    (message,) = rig.proc.submitted
    assert "ruff found 3 problems" in message and "no capacity" in message
    assert not any("retrying in" in r for r in rig.rows())
    await rig.advance(30 * 60)
    assert rig.timeline.preflight_bounces == 1, "no automatic retry after a bounce"
    await rig.finish()


async def test_the_gate_reporting_no_capacity_waits_too(rig, monkeypatch):
    gates = []

    def gate(*args, **kwargs):
        work = args[9]
        gates.append(work)
        work.capacity = "" if len(gates) > 1 else "no capacity: 1 of 2 shards not placed"
        return ""

    monkeypatch.setattr(_gates, "run", gate)

    rig.start()
    await rig.advance(120)
    await settle(rig, lambda: bool(gates))
    assert any("retrying in 5 min (1/3)" in r and "1 of 2 shards" in r for r in rig.rows()), rig.rows()
    assert rig.timeline.preflight_bounces == 0 and rig.proc.submitted == []

    await rig.advance(5 * 60)
    await settle(rig, lambda: rig.timeline._phase["kind"] == "done")
    assert len(gates) == 2
    await rig.finish()


FAULT = "scope: harness fault: cannot compute the project's changes"


def gate_with_fault(problem: str):
    """A fake `_gates.run` that hits a fault of the factory's and returns `problem`."""

    def gate(*args, **kwargs):
        args[9].harness_faults.append(FAULT)
        return problem

    return gate


async def test_a_harness_fault_beside_no_capacity_goes_to_the_operator_at_once_with_both(rig, monkeypatch):
    """Retrying for capacity would only delay a fault only the operator can fix: it is held at once."""
    checks = Checks(float("inf"))
    use(monkeypatch, checks)
    monkeypatch.setattr(_gates, "run", gate_with_fault(""))

    rig.start()
    await rig.advance(120)
    await settle(rig, lambda: bool(rig.proc.submitted))

    rows = rig.rows()
    assert not any("retrying in" in r for r in rows), rows
    assert rig.timeline.preflight_bounces == 0 and _supervise_support._escalated
    (message,) = rig.proc.submitted
    assert "not your failure" in message and "/review-again" in message
    assert "cannot compute the project's changes" in message and REASON in message
    assert any("🚨" in r and "harness fault" in r for r in rows)
    await rig.advance(60 * 60)
    assert checks.calls == 1, "a held pre-review is not retried for capacity"
    await rig.finish()


async def test_a_harness_fault_and_no_capacity_beside_a_project_problem_bounce_listing_all_three(
    rig, monkeypatch
):
    use(monkeypatch, Checks(1))
    monkeypatch.setattr(_gates, "run", gate_with_fault("standards: ruff found 3 problems"))

    rig.start()
    await rig.advance(60)
    await settle(rig, lambda: bool(rig.proc.submitted))

    assert rig.timeline.preflight_bounces == 1 and not _supervise_support._escalated
    (message,) = rig.proc.submitted
    assert "ruff found 3 problems" in message and "no capacity" in message
    assert "fault of the factory's" in message and "cannot compute the project's changes" in message
    assert not any("retrying in" in r for r in rig.rows())
    await rig.finish()


async def test_a_new_done_while_waiting_runs_the_pre_review_at_once(rig, monkeypatch):
    checks = Checks(1)
    use(monkeypatch, checks)

    rig.start()
    await rig.advance(120)
    # Waiting, not only checked: the checks return before the retry is scheduled.
    await settle(rig, lambda: checks.calls == 1 and any("retrying in 5 min (1/3)" in r for r in rig.rows()))
    rig.bridge._notes.append("DONE: pushed a fix")
    await rig.advance(60)
    await settle(rig, lambda: rig.timeline._phase["kind"] == "done")

    assert checks.calls == 2 and rig.timeline._phase["kind"] == "done"
    await rig.advance(30 * 60)
    assert checks.calls == 2, "the superseded retry did not run again"
    await rig.finish()


async def test_waiting_does_not_block_stop(rig, monkeypatch):
    use(monkeypatch, Checks(float("inf")))

    rig.start()
    await rig.advance(120)
    await settle(rig, lambda: rig.timeline._phase["kind"] == "waiting")
    await rig.finish()  # asserts the pump ends within 30 s of `/stop`


async def test_the_backoff_constants_can_be_patched(rig, monkeypatch):
    monkeypatch.setattr(_capacity, "RETRY_BACKOFF_S", (60, 60, 60))
    checks = Checks(1)
    use(monkeypatch, checks)

    rig.start()
    await rig.advance(120)
    # The first pre-review lands in real time; only then is its 60 s retry scheduled on the fake clock, and
    # the checks return before that: settle on the retry row, not on the call.
    await settle(rig, lambda: checks.calls >= 1 and any("retrying in 1 min (1/3)" in r for r in rig.rows()))
    await rig.advance(120)
    await settle(rig, lambda: rig.timeline._phase["kind"] == "done")

    assert checks.calls == 2
    await rig.finish()


def _done(note: str) -> bool:
    return note.startswith("DONE")


def test_the_wait_counts_and_cancels():
    wait = _capacity.CapacityWait(note="DONE")

    assert wait.schedule(100.0) == 300 and wait.retry([], 399.0, True, _done) == []
    assert wait.retry([], 400.0, False, _done) == [], "not while a review runs"
    assert wait.retry([], 400.0, True, _done) == ["DONE"] and wait.retry([], 401.0, True, _done) == []
    wait.schedule(0.0)
    assert wait.retry(["DONE: again"], 10_000.0, True, _done) == [] and wait.attempts == 2
    wait.schedule(0.0)
    assert wait.exhausted()
    wait.reset()
    assert wait.attempts == 0 and not wait.exhausted()


def test_a_progress_note_while_waiting_does_not_strand_the_retry():
    """Only a new DONE cancels the retry: nothing else would run the pre-review again."""
    wait = _capacity.CapacityWait(note="DONE: the work")
    wait.schedule(0.0)

    assert wait.retry(["checked CI"], 10.0, True, _done) == []
    assert wait.retry(["still idle"], 300.0, True, _done) == ["still idle", "DONE: the work"]


@pytest.mark.parametrize("reason", ["", "no capacity: x"])
def test_reasons_are_read_from_the_capacity_files(tmp_path, reason):
    (tmp_path / "fast.xml.capacity").write_text(reason)
    (tmp_path / "slow.xml.capacity").write_text("no capacity: y\n")

    expected = "slow lane: no capacity: y" + (f"; fast lane: {reason}" if reason else "")
    assert sorted(_capacity.read_reasons(tmp_path).split("; ")) == sorted(expected.split("; "))


async def _run_checks(tmp_path, body: str):
    from flyte_agent_factory import _session_config, _timeline

    checks = tmp_path / _session_config._checks_rel(_session_config._project_name)
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text(body)
    junit = tmp_path / "junit"
    junit.mkdir()
    return await _supervise_support._run_declared_checks(tmp_path, _timeline._Timeline(), junit)


async def test_a_lane_that_failed_without_a_junit_makes_the_checks_a_failure_not_capacity(tmp_path):
    """A lane that ran out leaves a record and no junit; the other lane's capacity file must not hide it."""
    ran_out = '{"completed": false, "outcome": "ran out", "code": 1, "reason": "shard 1/2 ran out"}'
    problem = await _run_checks(
        tmp_path,
        "echo 'slow lane: shard 1/2 ran out of its 600s in its own 2-CPU child' >&2\n"
        f"echo '{ran_out}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/fast.xml.result\"\n"
        f'echo "{REASON}" > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/slow.xml.capacity"; exit 1\n',
    )

    assert not isinstance(problem, _capacity.ChecksNoCapacity) and "failed with exit code 1" in problem


async def test_a_lane_that_ran_out_of_memory_beside_a_capacity_file_makes_the_checks_a_failure(tmp_path):
    """Supervisor-level guard: the record of a change that hit its memory limit is a failure, not capacity.

    The lane launcher never writes a `.capacity` file for it; this proves that even with one beside it the
    record decides, as for "ran out".
    """
    oom = (
        '{"completed": false, "outcome": "out of memory", "code": 1, "reason": "shard 1/2 (tests/x.py) 5Gi"}'
    )
    problem = await _run_checks(
        tmp_path,
        "echo 'slow lane: shard 1/2 ran out of its 5Gi memory in its own 2-CPU child' >&2\n"
        f"echo '{oom}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/fast.xml.result\"\n"
        f'echo "{REASON}" > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/slow.xml.capacity"; exit 1\n',
    )

    assert not isinstance(problem, _capacity.ChecksNoCapacity) and "failed with exit code 1" in problem


async def test_a_command_lane_that_found_no_capacity_makes_the_checks_no_capacity(tmp_path):
    """A project's `checks.sh` passes `--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/checks.xml"` to the launcher.

    What the launcher leaves for a command is the file pair a pytest lane leaves, so the pre-review reads it
    the same way; the lane is named for its file.
    """
    record = '{"completed": false, "outcome": "capacity", "code": 69, "reason": "no capacity: 1 of 1 shards"}'
    problem = await _run_checks(
        tmp_path,
        f"echo '{record}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/checks.xml.result\"\n"
        'echo "no capacity: 1 of 1 shards" > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/checks.xml.capacity"\n'
        "exit 69\n",
    )

    assert isinstance(problem, _capacity.ChecksNoCapacity)
    assert "checks lane: no capacity: 1 of 1 shards" in problem.capacity


async def test_a_lane_that_passed_beside_a_lane_with_no_capacity_is_capacity(tmp_path):
    problem = await _run_checks(
        tmp_path,
        'echo \'{"completed": true, "code": 0}\' > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/fast.xml.result"\n'
        f'echo "{REASON}" > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/slow.xml.capacity"; exit 1\n',
    )

    assert isinstance(problem, _capacity.ChecksNoCapacity)


def test_a_lane_whose_record_was_rejected_and_left_no_capacity_file_is_a_failure(tmp_path):
    """It was killed by the checks' time limit, or its record is unreadable: nothing says it was capacity."""
    (tmp_path / "slow.xml.capacity").write_text("no capacity: y\n")
    (tmp_path / "fast.xml.result").write_text("{not json")

    assert _capacity.decide_checks_outcome(tmp_path) == ""

    (tmp_path / "fast.xml.result").write_text('{"completed": true, "code": 0}')

    assert _capacity.decide_checks_outcome(tmp_path) == "slow lane: no capacity: y"


async def test_a_lane_that_could_not_set_up_is_a_failure_that_says_it_was_the_environment(tmp_path):
    """Setup is not capacity (no waiting, no retry), but the bounce must not read as failing tests."""
    setup = '{"completed": false, "outcome": "setup", "code": 78, "reason": "2 of 2 shards: git: not found"}'
    problem = await _run_checks(
        tmp_path,
        f"echo '{setup}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/fast.xml.result\"\n"
        f'echo "{REASON}" > "$AGENT_FACTORY_PYTEST_JUNIT_DIR/slow.xml.capacity"; exit 78\n',
    )

    assert not isinstance(problem, _capacity.ChecksNoCapacity)
    assert "failed with exit code 78" in problem
    assert "fast lane could not set up its environment (infrastructure, not the change's tests)" in problem


async def test_a_lane_whose_tree_could_not_be_uploaded_is_a_failure_that_says_so(tmp_path):
    upload = '{"completed": false, "outcome": "upload", "code": 74, "reason": "upload of the tree failed"}'
    problem = await _run_checks(
        tmp_path,
        f"echo '{upload}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/checks.xml.result\"; exit 74\n",
    )

    assert not isinstance(problem, _capacity.ChecksNoCapacity)
    assert "checks lane could not upload the tree to the cluster (infrastructure" in problem


async def test_a_failure_of_the_changes_own_tests_gets_no_infrastructure_label(tmp_path):
    ran_out = '{"completed": false, "outcome": "ran out", "code": 1, "reason": "shard 1/2 ran out"}'
    problem = await _run_checks(
        tmp_path, f"echo '{ran_out}' > \"$AGENT_FACTORY_PYTEST_JUNIT_DIR/fast.xml.result\"; exit 1\n"
    )

    assert "infrastructure" not in problem


def test_setup_and_upload_records_are_not_capacity(tmp_path):
    (tmp_path / "slow.xml.capacity").write_text("no capacity: y\n")
    for outcome in ("setup", "upload"):
        (tmp_path / "fast.xml.result").write_text(json.dumps({"completed": False, "outcome": outcome}))

        assert _capacity.decide_checks_outcome(tmp_path) == ""
        assert "infrastructure" in _capacity.label_infrastructure_failures(tmp_path)
