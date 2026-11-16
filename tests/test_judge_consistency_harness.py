"""The judge-consistency harness: study loading, prompts, the paid run (all with fakes) and its budget."""

import asyncio
import json
import runpy
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.judge_consistency_support import (
    CLAIM_REPLY,
    CLAIM_REPRODUCED,
    FINDING_REPLY,
    NOT_REPRODUCED,
    PASS_REPLY,
    REPRODUCED,
    STORED_INTRO,
    STORED_OUTRO,
    ScriptedCall,
    decision_row,
    make_run,
    make_study,
    prompts,
    read_rep,
    rep_record,
    reply,
    run_mod,
    stored_prompt,
    study_mod,
)

# --- study.py --------------------------------------------------------------------------------------------


def test_load_gives_the_study_modules_and_reloads_another_directory(tmp_path: Path) -> None:
    one = make_study(tmp_path / "one", [decision_row(1)])
    assert one.dir == tmp_path / "one" / "study"
    assert one.analyze.__file__ and Path(one.analyze.__file__).parent == one.dir
    two = make_study(tmp_path / "two", [decision_row(2)])
    assert Path(two.rejudge.__file__).parent == two.dir  # not the module cached from the first directory
    assert two.rejudge.make_decision_stem("a:r1:b") == "a__r1__b"


def test_load_a_missing_directory_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no study at"):
        study_mod.load(tmp_path / "nowhere")


def test_findings_is_the_changed_engines_module_and_is_cached() -> None:
    fm = study_mod.load_findings_module()
    assert study_mod.load_findings_module() is fm
    assert "flyte_agent_factory" not in fm.__name__
    assert (
        callable(fm.decide) and fm.render_finding_instructions("correctness") != fm.CRAFT_FINDING_INSTRUCTIONS
    )


def test_finding_check_setting_is_read_from_the_engine_policy() -> None:
    from flyte_agent_factory import _policy

    setting = _policy.DEFAULTS["finding_check"]
    assert study_mod.read_finding_check_setting() == (setting.model, setting.effort)


def test_finding_check_setting_fails_loudly_when_the_policy_changes_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src" / "flyte_agent_factory").mkdir(parents=True)
    (tmp_path / "src" / "flyte_agent_factory" / "_policy.py").write_text('DEFAULTS = {"judge": 1}\n')
    monkeypatch.setattr(study_mod, "ENGINE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="finding_check"):
        study_mod.read_finding_check_setting()


def test_default_paths_keep_private_output_outside_the_project() -> None:
    assert study_mod.ENGINE_DIR not in study_mod.DEFAULT_OUT_DIR.parents
    assert study_mod.DEFAULT_OUT_DIR.parts[-3:] == (".claude-flyte", "judge-consistency", "rejudge-after")
    assert study_mod.DEFAULT_STUDY_DIR.name == "judge-self-agreement"


# --- prompts.py ------------------------------------------------------------------------------------------


def test_after_prompt_swaps_only_the_instruction_block() -> None:
    fm = study_mod.load_findings_module()
    stored = STORED_INTRO + fm.CRAFT_FINDING_INSTRUCTIONS + STORED_OUTRO
    new = prompts.build_after_prompt(stored, "correctness", fm)
    assert new == STORED_INTRO + fm.QA_FINDING_INSTRUCTIONS + STORED_OUTRO
    assert prompts.build_after_prompt(stored, "craft", fm) == stored


@pytest.mark.parametrize("copies", [0, 2])
def test_after_prompt_raises_unless_the_block_occurs_exactly_once(copies: int) -> None:
    fm = study_mod.load_findings_module()
    stored = "x" + fm.CRAFT_FINDING_INSTRUCTIONS * copies
    with pytest.raises(ValueError, match=f"occur {copies} times"):
        prompts.build_after_prompt(stored, "correctness", fm)


def test_check_inputs_come_from_the_study_snapshot(tmp_path: Path) -> None:
    row = decision_row(1)
    study = make_study(tmp_path, [row])
    assert prompts.rebuild_check_inputs(row, study.dir, study.inputs) == (
        "Brief of demo1-1-0-abc121.",
        "C1. Criterion of demo1-1-0-abc121.",
    )
    given = {"already": "loaded"}
    assert prompts.rebuild_check_inputs(row, study.dir, study.inputs, given)[0].startswith("Brief of")


def test_verify_prompts_accepts_sound_stored_prompts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    study = make_study(tmp_path, [decision_row(1), decision_row(2)])
    assert prompts.verify_stored_prompts(study, study_mod.load_findings_module(), expect=2) == []
    assert run_mod.main(["verify-prompts", "--study", str(study.dir), "--expect", "2"]) == 0
    assert "all sound" in capsys.readouterr().out


def test_verify_prompts_names_every_unsound_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [decision_row(1), decision_row(2), decision_row(3)]
    study = make_study(tmp_path, rows)
    fm = study_mod.load_findings_module()
    inputs = study.dir / "data" / "inputs"

    def store(row: dict[str, Any], prompt: str) -> None:
        stem = row["decision_id"].replace(":", "__")
        (inputs / f"{stem}.json").write_text(
            json.dumps({"decision_id": row["decision_id"], "prompt": prompt})
        )

    store(rows[0], STORED_INTRO + fm.CRAFT_FINDING_INSTRUCTIONS + STORED_OUTRO)  # no brief, no acceptance
    store(rows[1], stored_prompt(rows[1], fm.CRAFT_FINDING_INSTRUCTIONS * 2))  # the block twice
    store(rows[2], stored_prompt(rows[2], "") + fm.QA_FINDING_INSTRUCTIONS)  # the block missing
    problems = prompts.verify_stored_prompts(study, fm, expect=50)
    assert problems == [
        "3 decisions, expected 50",
        f"{rows[0]['decision_id']}: the brief is not in the stored prompt",
        f"{rows[0]['decision_id']}: the acceptance is not in the stored prompt",
        f"{rows[1]['decision_id']}: the old finding instructions occur 2 times in the stored prompt, "
        "not once",
        f"{rows[2]['decision_id']}: the old finding instructions occur 0 times in the stored prompt, "
        "not once",
    ]
    assert run_mod.main(["verify-prompts", "--study", str(study.dir)]) == 1
    assert "the brief is not in the stored prompt" in capsys.readouterr().out


def test_verify_prompts_catches_an_old_block_that_survives_the_swap(tmp_path: Path) -> None:
    row = decision_row(1)
    study = make_study(tmp_path, [row])
    fm = study_mod.load_findings_module()

    swaps_in_the_old_block = types.ModuleType("findings")
    vars(swaps_in_the_old_block).update(
        CRAFT_FINDING_INSTRUCTIONS=fm.CRAFT_FINDING_INSTRUCTIONS,
        render_finding_instructions=lambda judge: "NEW " + fm.CRAFT_FINDING_INSTRUCTIONS,
    )

    problems = prompts.verify_stored_prompts(study, swaps_in_the_old_block)
    assert problems == [f"{row['decision_id']}: the old finding instructions survive the swap"]


# --- estimates -------------------------------------------------------------------------------------------


def test_estimate_numbers_and_no_decision_ids(tmp_path: Path) -> None:
    rows = [decision_row(1), decision_row(2)]
    reps = {
        rows[0]["decision_id"]: (
            rep_record(rows[0]["decision_id"], 1, "PASS", 1.0),
            rep_record(rows[0]["decision_id"], 2, "FAIL", 3.0),
        ),
        rows[1]["decision_id"]: (
            rep_record(rows[1]["decision_id"], 1, "PASS", 2.0),
            rep_record(rows[1]["decision_id"], 2, "PASS", 2.0),
        ),
    }
    study = make_study(tmp_path, rows, reps)
    results = tmp_path / "results"
    estimate = run_mod.write_estimate(study, rows, cap=10.0, out=tmp_path / "out", results=results)
    # judge: (mean 2.0 x 1.15 x 2 reps) for both decisions = 4.6 + 4.6; one study FAIL rep = $0.40 check.
    assert estimate["judge_usd"] == 9.2
    assert estimate["check_usd"] == 0.4
    assert estimate["total_usd"] == 9.6
    assert estimate["reserved_usd"] == 15.36  # 9.6 x 1.6
    assert estimate["decisions"] == 2 and estimate["reps"] == 4
    # run order is by hash; the decision with the check costs (4.6 + 0.4) x 1.6 = 8.0, the other 7.36:
    # whichever is first fits under $10, both together (15.36) do not.
    assert estimate["decisions_fitting_under_cap_in_run_order"] == 1
    assert estimate["decisions_expected_within_cap_in_run_order"] == 2  # 9.6 of estimates is under $10
    text = (results / "cost_estimate.json").read_text()
    assert json.loads(text) == estimate
    for row in rows:
        assert row["decision_id"] not in text and row["run"] not in text


# --- one rep ---------------------------------------------------------------------------------------------


def one_decision(tmp_path: Path, judge: list[Any], check: list[Any] | None = None, cap: float = 100.0):
    row = decision_row(1)
    study = make_study(tmp_path, [row])
    call = ScriptedCall(judge, check)
    return row, call, make_run(tmp_path, study, call, cap)


async def test_a_pass_reply_makes_no_check_call(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, 1.5)] * 2)
    assert await run_mod.run_decision(run, row) is True
    assert len(call.calls) == 2 and not call.of("sonnet")
    rec = read_rep(run, row, 1)
    assert (rec["verdict"], rec["raw_verdict"], rec["check_ran"], rec["check_needed"]) == (
        "PASS",
        "PASS",
        False,
        False,
    )
    assert (rec["judge_usd"], rec["check_usd"], rec["usd"]) == (1.5, 0.0, 1.5)
    assert rec["n_findings"] == rec["n_blocking"] == rec["n_kept"] == rec["n_dropped"] == 0
    assert rec["models"] == ["claude-opus-5-5"] and rec["attempts"] == 1
    assert rec["actions"] == ["action-1"] and rec["text"] == PASS_REPLY and rec["check_text"] is None
    assert rec["usage"]["judge"]["input_tokens"] == 10 and rec["usage"]["check"]["input_tokens"] == 0
    assert run.budget.spent == pytest.approx(3.0) and run.budget.reserved == pytest.approx(0.0)


async def test_the_judge_call_uses_the_after_prompt_in_a_fresh_workspace_at_the_head_sha(
    tmp_path: Path,
) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY)] * 2)
    await run_mod.run_decision(run, row)
    prompt, workdir, setting = call.calls[0]
    assert prompt == stored_prompt(row, study_mod.load_findings_module().QA_FINDING_INSTRUCTIONS)
    assert setting == run_mod.CallSetting("opus", "high", 120)
    assert workdir == run.work_root / f"{row['decision_id'].replace(':', '__')}-rep1"
    assert not workdir.exists()  # cleaned up
    assert [sha for sha, _ in run.study.rejudge.prepared][-2:] == [row["head_sha"]] * 2


@pytest.mark.parametrize(
    ("check_reply", "verdict", "kept", "dropped"),
    [(NOT_REPRODUCED, "PASS", 0, 1), (REPRODUCED, "FAIL", 1, 0)],
)
async def test_an_evidenced_finding_is_checked_and_the_check_decides(
    tmp_path: Path, check_reply: str, verdict: str, kept: int, dropped: int
) -> None:
    row, call, run = one_decision(
        tmp_path, [reply(FINDING_REPLY, 2.0)] * 2, [reply(check_reply, 0.25, ("claude-sonnet-5-5",))] * 2
    )
    await run_mod.run_decision(run, row)
    assert len(call.of("opus")) == 2 and len(call.of("sonnet")) == 2
    prompt, workdir, setting = call.of("sonnet")[0]
    assert setting == run_mod.CallSetting("sonnet", "low", 40)
    fm = study_mod.load_findings_module()
    expected = fm.build_check_prompt(
        "correctness",
        "Brief of demo1-1-0-abc121.",
        "C1. Criterion of demo1-1-0-abc121.",
        fm.select_blocking(fm.parse_findings(FINDING_REPLY)),
        reply=FINDING_REPLY,
    )
    assert prompt == expected
    assert workdir.name.endswith("-rep1-check")
    rec = read_rep(run, row, 1)
    assert (rec["verdict"], rec["raw_verdict"], rec["n_blocking"]) == (verdict, "FAIL", 1)
    assert (rec["n_kept"], rec["n_dropped"], rec["check_ran"], rec["check_needed"]) == (
        kept,
        dropped,
        True,
        True,
    )
    assert (rec["judge_usd"], rec["check_usd"], rec["usd"]) == (2.0, 0.25, 2.25)
    assert rec["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert rec["usage"]["check"]["input_tokens"] == 10 and rec["check_text"] == check_reply


async def test_a_check_that_fails_twice_keeps_the_finding(tmp_path: Path) -> None:
    row, call, run = one_decision(
        tmp_path, [reply(FINDING_REPLY, 2.0)] * 2, [RuntimeError("no pod"), RuntimeError("no pod")] * 2
    )
    await run_mod.run_decision(run, row)
    assert len(call.of("sonnet")) == 4  # two attempts per rep
    rec = read_rep(run, row, 1)
    assert rec["verdict"] == "FAIL" and rec["check_ran"] is False and rec["check_needed"] is True
    assert rec["check_reason"] == "the finding check did not run" and rec["check_text"] is None
    assert rec["check_usd"] == 0.0 and run.budget.reserved == pytest.approx(0.0)


async def test_a_check_that_fails_once_is_retried(tmp_path: Path) -> None:
    row, call, run = one_decision(
        tmp_path, [reply(FINDING_REPLY)] * 2, [RuntimeError("x"), reply(NOT_REPRODUCED, 0.3)] * 2
    )
    await run_mod.run_decision(run, row)
    rec = read_rep(run, row, 2)
    assert rec["verdict"] == "PASS" and rec["check_usd"] == 0.3


@pytest.mark.parametrize(("check_reply", "verdict"), [(CLAIM_REPRODUCED, "FAIL"), (NOT_REPRODUCED, "PASS")])
async def test_a_fail_without_a_finding_goes_to_the_check_for_its_claims(
    tmp_path: Path, check_reply: str, verdict: str
) -> None:
    row, call, run = one_decision(tmp_path, [reply(CLAIM_REPLY)] * 2, [reply(check_reply, 0.2)] * 2)
    await run_mod.run_decision(run, row)
    assert "The judge's reply" in call.of("sonnet")[0][0]
    rec = read_rep(run, row, 1)
    assert (rec["verdict"], rec["n_blocking"], rec["check_needed"]) == (verdict, 0, True)


async def test_no_verdict_is_retried_up_to_three_attempts(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply("I ran out of turns.", 1.0)] * 6)
    await run_mod.run_decision(run, row)
    assert len(call.calls) == 6
    rec = read_rep(run, row, 1)
    assert (rec["verdict"], rec["raw_verdict"], rec["attempts"], rec["usd"]) == ("NO_VERDICT", None, 3, 3.0)
    assert (rec["check_needed"], rec["check_reason"], rec["n_kept"]) == (False, "", 0)


async def test_a_verdict_on_the_second_attempt_stops_the_retries(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply("no verdict", 1.0), reply(PASS_REPLY, 2.0)] * 2)
    await run_mod.run_decision(run, row)
    rec = read_rep(run, row, 1)
    assert (rec["verdict"], rec["attempts"], rec["judge_usd"]) == ("PASS", 2, 3.0)


async def test_a_reply_without_usage_is_charged_its_estimate_and_marked(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, None)] * 2)
    await run_mod.run_decision(run, row)
    rec = read_rep(run, row, 1)
    assert rec["cost_measured"] is False and rec["usd"] == pytest.approx(1.15)  # study mean $1 x 1.15
    assert rec["models"] == []


# --- budget ----------------------------------------------------------------------------------------------


async def test_a_decision_that_would_pass_the_cap_is_not_started(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY)] * 2, cap=3.0)  # needs 2 x 1.15 x 1.6 = 3.68
    assert await run_mod.run_decision(run, row) is False
    assert call.calls == [] and run.budget.stopped
    assert not run.study.rejudge.build_rep_path(run.out, row["decision_id"], 1).exists()


async def test_a_call_that_would_pass_the_cap_is_not_started(tmp_path: Path) -> None:
    # Admitted ($3.68 of $5), but the first rep costs $4: a second call would pass the cap.
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, 4.0)] * 2, cap=5.0)
    assert await run_mod.run_decision(run, row) is False
    assert len(call.calls) == 1
    assert run.study.rejudge.build_rep_path(run.out, row["decision_id"], 1).exists()
    assert not run.study.rejudge.build_rep_path(run.out, row["decision_id"], 2).exists()
    assert run.budget.stopped and run.budget.reserved == pytest.approx(0.0)


async def test_a_rep_stopped_after_its_judge_call_keeps_the_spend_as_unrecorded(tmp_path: Path) -> None:
    # The judge call costs $4.5, so the check (which needs $0.64 more) does not fit under $5.
    row, call, run = one_decision(tmp_path, [reply(FINDING_REPLY, 4.5)], cap=5.0)
    assert await run_mod.run_decision(run, row) is False
    assert call.of("sonnet") == []
    assert not run.study.rejudge.build_rep_path(run.out, row["decision_id"], 1).exists()
    assert run.budget.unrecorded == pytest.approx(4.5)
    assert json.loads((run.out / "spend.json").read_text())["unrecorded_usd"] == 4.5


async def test_a_decision_admitted_finishes_its_reps_after_another_is_refused(tmp_path: Path) -> None:
    rows = [decision_row(1), decision_row(2)]
    study = make_study(tmp_path, rows)
    call = ScriptedCall([reply(PASS_REPLY, 1.0)] * 4)
    call.pause = True  # lets the second decision be refused while the first is mid-call
    run = make_run(tmp_path, study, call, cap=5.0)  # one decision needs 3.68; two do not fit
    code = await run_mod.run_all(run, rows, parallel=2)
    assert code == run_mod.EXIT_BUDGET
    first, second = rows
    for row, expected in ((first, True), (second, False)):
        for rep in (1, 2):
            assert run.study.rejudge.build_rep_path(run.out, row["decision_id"], rep).exists() is expected
    assert run.budget.spent == pytest.approx(2.0)


async def test_resume_skips_existing_reps_and_counts_their_spend(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, 1.0)])
    rejudge = run.study.rejudge
    rejudge.write_json_atomic(
        rejudge.build_rep_path(run.out, row["decision_id"], 1), {"usd": 2.5, "verdict": "PASS"}
    )
    resumed = rejudge.load_budget(run.out, 100.0)
    assert resumed.spent == 2.5
    run.budget = resumed
    assert await run_mod.run_decision(run, row) is True
    assert len(call.calls) == 1  # only rep 2
    assert run.budget.spent == pytest.approx(3.5)
    assert await run_mod.run_decision(run, row) is True  # both on disk now: nothing to do
    assert len(call.calls) == 1


async def test_an_error_in_a_call_is_reported_and_its_spend_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [decision_row(1), decision_row(2)]
    study = make_study(tmp_path, rows)
    # Decision 1: its first judge reply has no verdict (costs $1), then the retry raises.
    call = ScriptedCall([reply("no verdict", 1.0), RuntimeError("pod lost")] + [reply(PASS_REPLY)] * 2)
    run = make_run(tmp_path, study, call)
    ordered = study.rejudge.order_runs(rows, run.out)
    code = await run_mod.run_all(run, ordered, parallel=1)
    assert code == 1
    err = capsys.readouterr().err
    assert "failed: RuntimeError: pod lost" in err
    assert run.budget.unrecorded == pytest.approx(1.0)
    assert run.study.rejudge.build_rep_path(
        run.out, ordered[1]["decision_id"], 2
    ).exists()  # the other went on


async def test_a_cancellation_is_not_a_failed_decision(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await run_mod.run_all(run, [row], parallel=1)


async def test_run_all_with_everything_done_returns_zero(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY)] * 2)
    assert await run_mod.run_all(run, [row], parallel=2) == 0
    assert await run_mod.run_all(run, [row], parallel=2) == 0
    assert len(call.calls) == 2


# --- the real call, the CLI ------------------------------------------------------------------------------


async def test_call_engine_submits_the_reviewer_with_the_given_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flyte_agent_factory import _reviewer, child

    sent: dict[str, Any] = {}

    class FakeDir:
        @staticmethod
        async def from_local(path: str) -> str:
            return f"Dir({path})"

    async def raw(**kwargs: Any) -> tuple[str, str]:
        sent.update(kwargs)
        child_sink.append("submitted-action")
        return "the reply", "dir"

    child_sink: list[str] = []
    monkeypatch.setattr(run_mod, "Dir", FakeDir)  # the name `call_engine` binds
    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)

    from contextlib import contextmanager

    @contextmanager
    def track(sink: Any) -> Any:
        child_sink.clear()
        yield
        for name in child_sink:
            sink(name)

    monkeypatch.setattr(child, "track_submissions", track)
    text, names = await run_mod.call_engine("PROMPT", tmp_path, run_mod.CallSetting("sonnet", "low", 40))
    assert (text, names) == ("the reply", ["submitted-action"])
    assert sent == {
        "brief": "PROMPT",
        "workdir": f"Dir({tmp_path})",
        "role": "verify",
        "model": "sonnet",
        "effort": "low",
        "max_turns": 40,
        "answer_only": True,
        "engine": "claude",
        "routed": True,
    }


def cli_args(tmp_path: Path, study: Any, *more: str) -> list[str]:
    return [
        "--study", str(study.dir), "--out", str(tmp_path / "out"), "--results", str(tmp_path / "results"),
        "--work-root", str(tmp_path / "work"), "--source-repo", str(tmp_path / "source"), *more,
    ]  # fmt: skip


def test_main_estimate_writes_only_the_aggregate_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    study = make_study(tmp_path, [decision_row(1), decision_row(2)])
    assert run_mod.main(["estimate", *cli_args(tmp_path, study, "--cap", "50")]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"] == 2
    assert [p.name for p in (tmp_path / "results").iterdir()] == ["cost_estimate.json"]
    assert not (tmp_path / "out").exists()


def test_main_dry_run_prepares_a_workspace_and_prints_the_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [decision_row(1), decision_row(2)]
    study = make_study(tmp_path, rows)
    assert run_mod.main([*cli_args(tmp_path, study, "--dry-run", "--limit", "1")]) == 0
    out = capsys.readouterr().out
    assert out.count("would call subagent") == 1
    assert "model='opus', effort='high', max_turns=120" in out
    assert "check with model='sonnet', effort='low', max_turns=40" in out
    assert len(list((tmp_path / "work").iterdir())) == 1  # the rep-1 workspace, left for inspection
    assert not (tmp_path / "out").exists()


def test_main_runs_the_selected_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from flyte_agent_factory import child

    attached: list[bool] = []
    monkeypatch.setattr(child, "attach", lambda: attached.append(True))
    rows = [decision_row(1), decision_row(2)]
    study = make_study(tmp_path, rows)
    # `--only` names no sampled decision, so nothing is submitted; the rest of the path still runs.
    assert run_mod.main(cli_args(tmp_path, study, "--only", "nothing:r1:correctness", "--parallel", "1")) == 0
    assert attached == [True]
    assert "judge image: fake-image" in capsys.readouterr().err


def test_select_decisions_keeps_only_pairs_with_two_verdicts(tmp_path: Path) -> None:
    rows = [decision_row(1), decision_row(2), decision_row(3)]
    a, b, c = (r["decision_id"] for r in rows)
    reps = {
        a: (rep_record(a, 1, "PASS"), rep_record(a, 2, "FAIL")),
        b: (rep_record(b, 1, "PASS"), rep_record(b, 2, "NO_VERDICT")),
        c: (rep_record(c, 1, "PASS"), None),
    }
    study = make_study(tmp_path, rows, reps)
    assert [d["decision_id"] for d in run_mod.select_decisions(study)] == [a]


def test_run_py_works_as_a_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    study = make_study(tmp_path, [decision_row(1)])
    monkeypatch.setattr(sys, "argv", ["run.py", "estimate", *cli_args(tmp_path, study)])
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(Path(run_mod.__file__)), run_name="__main__")
    assert stop.value.code == 0
    assert (tmp_path / "results" / "cost_estimate.json").exists()


def test_take_refuses_a_call_whose_shortfall_cannot_be_reserved(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [], cap=5.0)
    assert run.budget.reserve(4.9)  # held by other decisions: within the cap, but only just
    held = [0.0]
    assert run_mod._take_from_held(run, held, 1.84) is False
    assert held == [0.0] and run.budget.stopped


async def test_decisions_after_a_budget_stop_are_not_started(tmp_path: Path) -> None:
    rows = [decision_row(1), decision_row(2)]
    study = make_study(tmp_path, rows)
    call = ScriptedCall([])
    run = make_run(tmp_path, study, call, cap=1.0)
    assert await run_mod.run_all(run, rows, parallel=1) == run_mod.EXIT_BUDGET
    assert call.calls == []


# --- the committed spend ledger ---------------------------------------------------------------------------


def test_the_ledger_is_a_floor_under_the_budget(tmp_path: Path) -> None:
    study = make_study(tmp_path, [decision_row(1)])
    ledger = tmp_path / "results" / run_mod.LEDGER_FILE
    budget = study.rejudge.Budget(150.0, tmp_path / "out" / "spend.json", spent=10.0)
    run_mod.apply_ledger(budget, ledger)  # no ledger yet: nothing changes
    assert budget.spent == 10.0
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"total_usd": 42.5, "cap_usd": 150.0}))
    run_mod.apply_ledger(budget, ledger)  # the private rep files were lost: the ledger's total still counts
    assert budget.spent == 42.5 and budget.unrecorded == pytest.approx(32.5)
    run_mod.apply_ledger(budget, ledger)
    assert budget.spent == 42.5  # never counted twice
    budget.spent = 50.0
    run_mod.apply_ledger(budget, ledger)
    assert budget.spent == 50.0  # never lowered either
    # charging interrupted calls never lowers the committed total either
    low = run_mod.charge_interrupted(ledger, [1.0, 2.0], calls=1, cap=150.0)
    assert low["total_usd"] == 42.5


async def test_run_all_writes_the_ledger_after_each_decision(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, 1.0), reply(PASS_REPLY, 1.0)])
    run.ledger = tmp_path / "results" / run_mod.LEDGER_FILE
    assert await run_mod.run_all(run, [row], parallel=1) == 0
    assert json.loads(run.ledger.read_text()) == {"total_usd": 2.0, "cap_usd": 100.0}
    run.ledger = None
    run_mod.write_ledger(run)  # a dry run has no ledger and writes none


def test_charge_interrupted_records_count_basis_and_raises_the_total(tmp_path: Path) -> None:
    ledger = tmp_path / run_mod.LEDGER_FILE
    ledger.write_text(json.dumps({"total_usd": 10.0, "cap_usd": 150.0}))
    updated = run_mod.charge_interrupted(ledger, [1.0, 2.0, 5.0], calls=4, cap=150.0)
    assert updated == {
        "total_usd": 28.0,  # measured 8.0 + 4 calls x the worst round, 5.0
        "cap_usd": 150.0,
        "measured_usd": 8.0,
        "interrupted_calls": 4,
        "charge_per_interrupted_call_usd": 5.0,
        "interrupted_charge_usd": 20.0,
    }
    with pytest.raises(ValueError, match="no rep files"):
        run_mod.charge_interrupted(ledger, [], calls=4, cap=150.0)


def test_charge_interrupted_refuses_fewer_than_one_call(tmp_path: Path) -> None:
    ledger = tmp_path / "spend_ledger.json"
    for calls in (0, -10):
        with pytest.raises(ValueError, match="at least 1"):
            run_mod.charge_interrupted(ledger, [1.0, 2.0], calls=calls, cap=150.0)


def test_main_charge_interrupted_writes_the_ledger_from_the_rep_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [decision_row(1)]
    study = make_study(tmp_path, rows)
    for rep, usd in ((1, 3.0), (2, 1.0)):
        path = study.rejudge.build_rep_path(tmp_path / "out", rows[0]["decision_id"], rep)
        study.rejudge.write_json_atomic(path, rep_record(rows[0]["decision_id"], rep, "PASS", usd))
    args = cli_args(tmp_path, study)
    with pytest.raises(SystemExit, match="--calls"):
        run_mod.main(["charge-interrupted", *args])
    assert run_mod.main(["charge-interrupted", "--calls", "2", *args]) == 0
    ledger = json.loads((tmp_path / "results" / run_mod.LEDGER_FILE).read_text())
    assert ledger["total_usd"] == 10.0 and ledger["interrupted_charge_usd"] == 6.0  # 4.0 measured + 2 x 3.0
    assert json.loads(capsys.readouterr().out)["interrupted_calls"] == 2


async def test_write_ledger_keeps_the_interrupted_charge(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [reply(PASS_REPLY, 1.0), reply(PASS_REPLY, 1.0)])
    run.ledger = tmp_path / "results" / run_mod.LEDGER_FILE
    run.ledger.parent.mkdir(parents=True)
    run.ledger.write_text(json.dumps(run_mod.charge_interrupted(run.ledger, [1.0, 2.0], 2, 100.0)))
    run_mod.apply_ledger(run.budget, run.ledger)  # as `main` does before a resume: the floor is 7.0
    assert await run_mod.run_all(run, [row], parallel=1) == 0
    kept = json.loads(run.ledger.read_text())
    assert kept["interrupted_calls"] == 2 and kept["interrupted_charge_usd"] == 4.0
    assert kept["measured_usd"] == 3.0
    assert kept["total_usd"] == 9.0  # the floor, plus the resumed decision's 2.0


async def test_a_call_that_raises_is_charged_its_estimate(tmp_path: Path) -> None:
    row, call, run = one_decision(tmp_path, [], [RuntimeError("pod lost")])
    est = 1.84

    async def once(call: Any, prompt: str, workdir: Path) -> Any:  # no retries: the first raise propagates
        return await call(prompt, workdir)

    assert run.budget.admit(est)
    before = run.budget.spent
    with pytest.raises(RuntimeError, match="pod lost"):
        await run_mod._call_and_settle(
            run, est, 0.0, run.check_setting, "prompt", tmp_path, once, run_mod._RepState(), "judge"
        )
    assert run.budget.spent == pytest.approx(before + est)
    assert run.budget.reserved == pytest.approx(0.0)
