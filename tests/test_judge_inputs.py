"""What each review judge was shown is recorded in the round's verdict row, and the diff can be recreated."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _checkpoint,
    _contract,
    _judge_inputs,
    _review,
    _reviewer,
    _session_config,
    _timeline,
    _usage,
)
from tests._git import git
from tests.test_judge_outcomes import FAIL_WITH_EVIDENCE, PASS, Reviewers

NAMES = [name for name, _ in _review._JUDGES]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Repo(type(Path())):  # type: ignore[misc]  # a path that also carries its two commits
    base: str
    head: str


@pytest.fixture
def repo(tmp_path):
    """A repository with a base commit and a head commit (a text file, a binary file, a non-ASCII path)."""
    root = Repo(tmp_path / "repo")
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\ntwo\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    (root / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (root / "naïve ファイル.txt").write_text("héllo\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "head")
    head = git(root, "rev-parse", "HEAD")
    # Where the harness keeps its own files, kept out of `git status` as the workspace clone does.
    (root / ".git" / "info" / "exclude").write_text(".claude-flyte/\n")
    (root / ".claude-flyte" / "proj").mkdir(parents=True)
    (root / ".claude-flyte" / "proj" / "base-commit").write_text(base + "\n")
    root.base, root.head = base, head
    return root


def expected_diff(repo: Path, base: str, head: str) -> bytes:
    """The canonical diff computed independently of the module under test."""
    return subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base,
            head,
            "--",
        ],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def evaluate(monkeypatch, repo):
    async def upload(_path):
        return object()

    async def facts():
        return ""

    async def push(*_a, **_k):
        return ""

    async def contract(*_a, **_k):
        return None

    monkeypatch.setattr(_checkpoint, "_upload_workspace", upload)
    monkeypatch.setattr(_review, "_review_facts", facts)
    monkeypatch.setattr(_review, "_push_fact", push)
    monkeypatch.setattr(_contract, "_load_or_freeze_contract", contract)
    monkeypatch.setattr(Dir, "from_local", upload)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda engine: False)
    monkeypatch.setattr(_session_config, "_project_name", "proj")

    def go(reviewers, round_=1, timeline=None):
        monkeypatch.setattr(_reviewer, "_call_reviewer", reviewers)
        timeline = timeline or _timeline._Timeline()
        return _review._evaluate(repo, "brief", "", round_, timeline), timeline

    return go


class Settled(Reviewers):
    """`Reviewers` that also report the resolved model and effort, as `_call_reviewer` does."""

    def __init__(self, replies, engine="claude", model="opus", effort="high"):
        super().__init__(replies, engine)
        self.model, self.effort = model, effort
        self.briefs: dict[str, list[str]] = {}

    async def __call__(self, *, brief, on_setting=None, engine=None, **kw):
        judge = next(j for j, _ in _review._JUDGES if f"**{j}** reviewer" in brief)
        self.briefs.setdefault(judge, []).append(brief)
        if on_setting:
            on_setting(engine or self.engine, self.model, self.effort)
        return await super().__call__(brief=brief, engine=engine, **kw)


# --- recording ---


async def test_every_judge_entry_records_what_it_was_shown(evaluate, repo):
    reviewers = Settled({"correctness": [FAIL_WITH_EVIDENCE]})
    pending, timeline = evaluate(reviewers)
    passed, _ = await pending
    assert not passed
    row = timeline.verdicts[-1]
    diff = expected_diff(repo, repo.base, repo.head)
    assert set(row["judges"]) == set(NAMES)
    for name in NAMES:
        entry = row["judges"][name]
        assert entry["head"] == repo.head
        assert entry["base"] == repo.base
        assert entry["diff_spec"] == _judge_inputs.DIFF_SPEC
        assert entry["diff_argv"] == list(_judge_inputs.DIFF_ARGV)
        assert entry["diff_sha256"] == sha256(diff)
        assert "diff_error" not in entry
        # The first round: what this round changed is everything since the project began.
        assert entry["round_base"] == repo.base
        assert entry["round_diff_sha256"] == sha256(diff)
        assert entry["worktree_clean"] is True
        assert (entry["engine"], entry["provider"]) == ("claude", "anthropic")
        assert (entry["model"], entry["effort"]) == ("opus", "high")
        assert entry["attempts"] == 1 and entry["carried"] is False
        assert entry["prompt_sha256"] == sha256(reviewers.briefs[name][-1].encode())
    assert row["judges"]["correctness"]["outcome"] == "FAIL"
    assert row["judges"]["completeness"]["outcome"] == "PASS"
    json.dumps(row)  # it goes into the metrics file and the snapshot


async def test_an_entry_holds_no_diff_or_prompt_text_and_every_value_is_bounded(evaluate):
    pending, timeline = evaluate(Settled({"correctness": [FAIL_WITH_EVIDENCE]}))
    await pending
    for entry in timeline.verdicts[-1]["judges"].values():
        assert "diff" not in entry and "prompt" not in entry
        for key, value in entry.items():
            assert len(json.dumps(value)) <= 1024, key


async def test_the_judges_reach_the_snapshot_and_survive_a_metrics_restore(evaluate, repo):
    pending, timeline = evaluate(Settled({}))
    await pending
    snapshot = timeline.snapshot()
    assert snapshot["review"]["verdicts"][0]["judges"]["craft"]["head"] == repo.head
    assert snapshot["metrics"]["rounds"][0]["judges"]["craft"]["head"] == repo.head
    assert timeline._save_metrics()
    restored = _timeline._Timeline()
    assert restored.verdicts[0]["judges"] == timeline.verdicts[0]["judges"]


async def test_the_second_round_records_the_round_base_apart_from_the_project_base(evaluate, repo):
    pending, timeline = evaluate(Settled({}))
    await pending
    (repo / "later.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "later")
    head2 = git(repo, "rev-parse", "HEAD")
    pending, _ = evaluate(Settled({}), round_=2, timeline=timeline)
    await pending
    entry = timeline.verdicts[-1]["judges"]["craft"]
    assert entry["head"] == head2
    assert entry["base"] == repo.base
    assert entry["round_base"] == repo.head
    assert entry["round_diff_sha256"] == sha256(expected_diff(repo, repo.head, head2))
    assert entry["diff_sha256"] == sha256(expected_diff(repo, repo.base, head2))


async def test_a_not_run_judge_in_a_failed_round_is_recorded(evaluate):
    reviewers = Settled(
        {"correctness": [FAIL_WITH_EVIDENCE], "craft": [RuntimeError("pod never scheduled")]},
        engine="codex",
        model="gpt-5",
        effort="medium",
    )
    pending, timeline = evaluate(reviewers)
    passed, _ = await pending
    assert not passed
    judges = timeline.verdicts[-1]["judges"]
    assert judges["craft"]["outcome"] == "NOT_RUN"
    assert judges["craft"]["attempts"] == 1
    assert (judges["craft"]["engine"], judges["craft"]["provider"]) == ("codex", "openai")
    assert judges["craft"]["model"] == "gpt-5"
    assert judges["correctness"]["outcome"] == "FAIL"


async def test_a_carried_judge_keeps_where_it_really_ran(evaluate):
    first = Settled({"completeness": [_reviewer._NO_FINAL_MESSAGE]}, model="opus", effort="high")
    pending, timeline = evaluate(first)
    with pytest.raises(_reviewer.ReviewIncomplete):
        await pending
    carried_sha = {n: k["prompt_sha256"] for n, k in timeline.carried.items()}
    assert carried_sha.keys() == {"correctness", "craft"}
    assert all(carried_sha.values())
    # The re-run: only completeness runs, and on another model.
    again = Settled({}, engine="codex", model="gpt-5", effort="low")
    pending, _ = evaluate(again, timeline=timeline)
    passed, _ = await pending
    assert passed and list(again.briefs) == ["completeness"]
    judges = timeline.verdicts[-1]["judges"]
    for name in ("correctness", "craft"):
        assert judges[name]["carried"] is True
        assert (judges[name]["engine"], judges[name]["model"]) == ("claude", "opus")
        assert judges[name]["prompt_sha256"] == carried_sha[name]
    assert judges["completeness"]["carried"] is False
    assert (judges["completeness"]["engine"], judges["completeness"]["model"]) == ("codex", "gpt-5")


async def test_a_carried_judge_keeps_the_attempts_of_the_round_it_ran_in(evaluate):
    # correctness needs two calls (no verdict, then a PASS); completeness never answers: undecided.
    first = Settled(
        {"correctness": [_reviewer._NO_FINAL_MESSAGE, PASS], "completeness": [_reviewer._NO_FINAL_MESSAGE]}
    )
    pending, timeline = evaluate(first)
    with pytest.raises(_reviewer.ReviewIncomplete):
        await pending
    pending, _ = evaluate(Settled({}), timeline=timeline)
    passed, _ = await pending
    assert passed
    judges = timeline.verdicts[-1]["judges"]
    assert judges["correctness"]["carried"] is True
    assert judges["correctness"]["attempts"] == 2


async def test_a_judge_that_raises_after_its_call_keeps_where_the_call_ran(evaluate, monkeypatch):
    real = _review._find_usable_result_gap
    raised: list[bool] = []

    def gap(text, advisory_craft):
        if not raised:
            raised.append(True)
            raise RuntimeError("broke after the call")
        return real(text, advisory_craft)

    monkeypatch.setattr(_review, "_find_usable_result_gap", gap)
    # Two failing judges, so whichever one raises, another FAIL still decides the round.
    failing = {"correctness": [FAIL_WITH_EVIDENCE], "completeness": [FAIL_WITH_EVIDENCE]}
    pending, timeline = evaluate(Settled(failing, model="opus", effort="high"))
    await pending
    broken = [e for e in timeline.verdicts[-1]["judges"].values() if e["outcome"] == "NOT_RUN"]
    assert len(broken) == 1
    assert (broken[0]["engine"], broken[0]["model"], broken[0]["effort"]) == ("claude", "opus", "high")
    assert broken[0]["prompt_sha256"]


async def test_a_no_verdict_retry_on_the_other_engine_records_the_second_engine(evaluate):
    class Switching(Settled):
        async def __call__(self, *, brief, on_setting=None, engine=None, **kw):
            judge = next(j for j, _ in _review._JUDGES if f"**{j}** reviewer" in brief)
            second = judge == "completeness" and engine == "codex"
            if on_setting:
                on_setting(engine or "claude", "gpt-5" if second else "opus", "low" if second else "high")
            return await Reviewers.__call__(self, brief=brief, engine=engine, **kw)

    reviewers = Switching({"completeness": ["no verdict here", PASS]})
    pending, timeline = evaluate(reviewers)
    passed, _ = await pending
    assert passed
    entry = timeline.verdicts[-1]["judges"]["completeness"]
    assert (entry["engine"], entry["model"], entry["effort"], entry["provider"]) == (
        "codex",
        "gpt-5",
        "low",
        "openai",
    )
    assert entry["attempts"] == 2
    assert timeline.verdicts[-1]["judges"]["craft"]["engine"] == "claude"


async def test_an_unknown_base_records_no_hash_and_the_round_is_still_decided(evaluate, repo):
    (repo / ".claude-flyte" / "proj" / "base-commit").write_text("f" * 40 + "\n")
    pending, timeline = evaluate(Settled({}))
    passed, _ = await pending
    assert passed
    entry = timeline.verdicts[-1]["judges"]["correctness"]
    assert entry["diff_sha256"] is None
    assert entry["diff_error"]
    assert entry["round_diff_sha256"] is None
    assert entry["head"] == repo.head and entry["base"] == "f" * 40


@pytest.mark.parametrize(
    "error",
    [
        OSError("disk full"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        subprocess.SubprocessError("gone"),
    ],
)
async def test_a_failure_while_recording_never_fails_the_round(evaluate, monkeypatch, error):
    async def broken(*_a, **_k):
        raise error

    monkeypatch.setattr(_judge_inputs, "_diff_sha256", broken)
    pending, timeline = evaluate(Settled({}))
    passed, _ = await pending
    assert passed
    assert timeline.verdicts[-1]["judges"]["craft"]["diff_sha256"] is None


async def test_a_failing_status_leaves_the_diff_hashes_and_no_diff_error(repo, monkeypatch):
    def broken(_repo):
        raise OSError("status gone")

    monkeypatch.setattr(_judge_inputs, "_worktree_clean_sync", broken)
    inputs = await _judge_inputs.capture_round_inputs(repo, repo.base, repo.head, repo.base)
    assert inputs["diff_sha256"] == sha256(expected_diff(repo, repo.base, repo.head))
    assert inputs["round_diff_sha256"] == inputs["diff_sha256"]
    assert "diff_error" not in inputs and "round_diff_error" not in inputs
    assert inputs["worktree_clean"] is None


async def test_worktree_clean_is_false_for_an_untracked_file(evaluate, repo):
    (repo / "scratch.txt").write_text("not committed\n")
    pending, timeline = evaluate(Settled({}))
    await pending
    assert timeline.verdicts[-1]["judges"]["craft"]["worktree_clean"] is False


async def test_worktree_clean_is_false_for_a_modified_tracked_file(evaluate, repo):
    (repo / "a.txt").write_text("changed\n")
    pending, timeline = evaluate(Settled({}))
    await pending
    assert timeline.verdicts[-1]["judges"]["craft"]["worktree_clean"] is False


async def test_capture_round_inputs_never_raises(tmp_path):
    inputs = await _judge_inputs.capture_round_inputs(tmp_path / "missing", "a" * 40, "b" * 40, "")
    assert inputs["diff_sha256"] is None and inputs["diff_error"]
    assert inputs["worktree_clean"] is None
    assert inputs["round_diff_sha256"] is None
    inputs = await _judge_inputs.capture_round_inputs(tmp_path, "", "", "")
    assert inputs["diff_sha256"] is None and inputs["diff_error"]


def test_resolve_provider_maps_each_engine():
    assert _judge_inputs.resolve_provider("claude") == "anthropic"
    assert _judge_inputs.resolve_provider("codex") == "openai"
    assert _judge_inputs.resolve_provider("other") == ""
    assert _judge_inputs.resolve_provider("") == ""


# --- the settings a call resolved ---


async def test_call_reviewer_reports_the_resolved_setting(monkeypatch):
    seen = []

    async def raw(**kw):
        return "ok", None

    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    await _reviewer._call_reviewer(
        purpose="delegate",
        engine="claude",
        model="opus",
        effort="high",
        on_setting=lambda *args: seen.append(args),
    )
    assert seen == [("claude", "opus", "high")]


# --- reading a row ---


def test_old_rows_have_no_judges():
    assert _judge_inputs.judge_inputs_of({"round": 1, "verdict": "PASS"}) == {}


@pytest.mark.parametrize(
    "row",
    [
        None,
        [],
        "x",
        {"judges": None},
        {"judges": []},
        {"judges": {"craft": "x", "completeness": None, "correctness": 3}},
        {"judges": {"craft": {"diff_argv": "git diff", "attempts": "many", "carried": "yes"}}},
    ],
)
def test_a_malformed_row_never_raises(row):
    result = _judge_inputs.judge_inputs_of(row)
    assert isinstance(result, dict)
    for value in result.values():
        assert isinstance(value, _judge_inputs.JudgeInputs)


def test_judge_inputs_of_reads_a_recorded_row_and_ignores_extra_keys():
    entry = _judge_inputs.build_judge_entry(
        {"head": "h", "base": "b", "diff_sha256": "d", "round_base": "r", "worktree_clean": True},
        engine="claude",
        model="opus",
        effort="high",
        prompt_sha256="p",
        outcome="PASS",
        attempts=2,
        carried=False,
    )
    entry["future_key"] = 1
    inputs = _judge_inputs.judge_inputs_of({"judges": {"craft": entry}})["craft"]
    assert (inputs.head, inputs.base, inputs.diff_sha256, inputs.round_base) == ("h", "b", "d", "r")
    assert (inputs.engine, inputs.provider, inputs.model, inputs.attempts) == (
        "claude",
        "anthropic",
        "opus",
        2,
    )
    assert inputs.worktree_clean is True and inputs.carried is False


# --- old records keep loading ---


def old_carried() -> dict[str, Any]:
    return {
        "sha": "a" * 40,
        "round": 1,
        "outcome": "PASS",
        "report": "fine",
        "nonblocking": [],
        "unverified": [],
        "conflicts": [],
    }


def test_an_old_metrics_file_with_carried_judges_loads(tmp_path):
    timeline = _timeline._Timeline()
    timeline.carried = {"correctness": old_carried()}
    timeline.verdicts.append({"round": 1, "verdict": "FAIL", "blocking": 2, "at": 1.0, "changed_files": []})
    assert timeline._save_metrics()
    restored = _timeline._Timeline()
    assert restored.verdicts[0]["round"] == 1 and "judges" not in restored.verdicts[0]
    kept = _review._take_carried("a" * 40, restored)
    assert kept["correctness"].engine == "" and kept["correctness"].prompt_sha256 == ""
    assert _judge_inputs.judge_inputs_of(restored.verdicts[0]) == {}


def test_a_carried_judge_of_an_old_file_is_kept_with_empty_settings():
    carried = _review._Carried(**old_carried())
    assert (carried.engine, carried.model, carried.effort, carried.prompt_sha256) == ("", "", "", "")


def test_an_old_pass_record_loads_and_builds_rounds_as_before():
    old_row = {"round": 1, "verdict": "PASS", "blocking": 0, "at": 5.0, "changed_files": ["a.py"]}
    _session_config._passed_path().parent.mkdir(parents=True, exist_ok=True)
    _session_config._passed_path().write_text(
        json.dumps({"round": 1, "sha": "a" * 40, "at": 5.0, "verdicts": [old_row]})
    )
    record = _session_config._load_pass_record()
    assert record and record["verdicts"] == [old_row]
    assert _judge_inputs.judge_inputs_of(record["verdicts"][0]) == {}


def test_a_new_row_flows_through_the_pass_record(tmp_path):
    row = {
        "round": 1,
        "verdict": "PASS",
        "blocking": 0,
        "at": 5.0,
        "changed_files": [],
        "judges": {"craft": {"head": "h"}},
    }
    _session_config._passed_path().parent.mkdir(parents=True, exist_ok=True)
    _session_config._passed_path().write_text(json.dumps({"round": 1, "sha": "h", "verdicts": [row]}))
    record = _session_config._load_pass_record()
    assert record and record["verdicts"][0]["judges"]["craft"]["head"] == "h"


# --- recreating the diff ---


def entry_for(repo: Repo, which_base: str | None = None) -> dict[str, object]:
    base = which_base or repo.base
    diff = expected_diff(repo, base, repo.head)
    return _judge_inputs.build_judge_entry(
        {
            "head": repo.head,
            "base": base,
            "diff_spec": _judge_inputs.DIFF_SPEC,
            "diff_argv": list(_judge_inputs.DIFF_ARGV),
            "diff_sha256": sha256(diff),
            "round_base": base,
            "round_diff_sha256": sha256(diff),
            "worktree_clean": True,
        },
        engine="claude",
        model="opus",
        effort="high",
        prompt_sha256="p",
        outcome="PASS",
        attempts=1,
        carried=False,
    )


def test_recreate_round_trips_with_text_binary_and_non_ascii_paths(repo):
    data = _judge_inputs.recreate_diff(repo, entry_for(repo))
    assert data == expected_diff(repo, repo.base, repo.head)
    assert b"GIT binary patch" in data
    assert "naïve ファイル.txt".encode() in data  # not quoted as "na\303\257ve..."
    assert _judge_inputs.recreate_diff(repo, entry_for(repo), which="round") == data


def test_recreate_accepts_a_parsed_entry(repo):
    inputs = _judge_inputs.judge_inputs_of({"judges": {"craft": entry_for(repo)}})["craft"]
    assert _judge_inputs.recreate_diff(repo, inputs) == expected_diff(repo, repo.base, repo.head)


async def test_a_recorded_diff_of_a_live_round_is_recreated(evaluate, repo):
    pending, timeline = evaluate(Settled({}))
    passed, _ = await pending
    assert passed
    entry = timeline.verdicts[-1]["judges"]["correctness"]
    assert sha256(_judge_inputs.recreate_diff(repo, entry)) == entry["diff_sha256"]


def test_an_unreachable_commit_is_reported(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    with pytest.raises(_judge_inputs.CommitsUnreachable):
        _judge_inputs.recreate_diff(other, entry_for(repo))
    assert issubclass(_judge_inputs.CommitsUnreachable, _judge_inputs.RecreateError)
    assert issubclass(_judge_inputs.DiffMismatch, _judge_inputs.RecreateError)


def test_a_tampered_hash_is_a_mismatch(repo):
    entry = entry_for(repo)
    entry["diff_sha256"] = "0" * 64
    with pytest.raises(_judge_inputs.DiffMismatch):
        _judge_inputs.recreate_diff(repo, entry)


def test_an_entry_with_no_recorded_hash_cannot_be_recreated(repo):
    entry = entry_for(repo)
    entry["diff_sha256"] = None
    with pytest.raises(_judge_inputs.RecreateError):
        _judge_inputs.recreate_diff(repo, entry)


def test_a_recorded_argv_that_is_not_the_spec_is_refused(repo, tmp_path, monkeypatch):
    entry = entry_for(repo)
    argv = list(_judge_inputs.DIFF_ARGV)
    altered = argv.index("core.fsmonitor=false")
    argv[altered] = f"core.fsmonitor={tmp_path / 'hook'}"
    assert sum(a != b for a, b in zip(argv, _judge_inputs.DIFF_ARGV, strict=True)) == 1
    entry["diff_argv"] = argv
    monkeypatch.setattr(_judge_inputs, "_stream", lambda *a, **k: pytest.fail("ran git"))
    with pytest.raises(_judge_inputs.RecreateError, match="argv"):
        _judge_inputs.recreate_diff(repo, entry)


def test_an_unknown_diff_spec_is_refused_without_running_git(repo, monkeypatch):
    entry = entry_for(repo)
    entry["diff_spec"] = "git-diff/0"
    monkeypatch.setattr(_judge_inputs, "_stream", lambda *a, **k: pytest.fail("ran git"))
    with pytest.raises(_judge_inputs.RecreateError, match="unknown diff spec"):
        _judge_inputs.recreate_diff(repo, entry)


def test_a_directory_that_is_no_repository_is_refused(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for where in (plain, tmp_path / "missing"):
        with pytest.raises(_judge_inputs.RecreateError, match="not a git repository") as raised:
            _judge_inputs.recreate_diff(where, entry_for(repo))
        assert type(raised.value) is _judge_inputs.RecreateError


def test_hostile_repository_and_user_config_still_match_the_recorded_hash(repo, tmp_path, monkeypatch):
    marker = tmp_path / "ran-external"
    script = tmp_path / "ext.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\necho EXTERNAL\n")
    script.chmod(0o755)
    textconv = tmp_path / "conv.sh"
    textconv.write_text(f"#!/bin/sh\ntouch {marker}\necho CONVERTED\n")
    textconv.chmod(0o755)
    entry = entry_for(repo)
    want = expected_diff(repo, repo.base, repo.head)
    for key, value in [
        ("diff.external", str(script)),
        ("color.ui", "always"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.renames", "copies"),
        ("diff.algorithm", "patience"),
        ("diff.context", "9"),
        ("diff.interHunkContext", "20"),
        ("core.quotepath", "true"),
        ("diff.txt.textconv", str(textconv)),
    ]:
        git(repo, "config", key, value)
    (repo / ".git" / "info" / "attributes").write_text("*.txt diff=txt\n")
    # The user's own config, too.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[diff]\n\tnoprefix = true\n\tcolorMoved = zebra\n[color]\n\tui = always\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))
    assert _judge_inputs.recreate_diff(repo, entry) == want
    assert not marker.exists()


async def test_the_hash_recorded_under_hostile_config_matches_a_clean_recreation(repo, tmp_path, monkeypatch):
    script = tmp_path / "ext.sh"
    script.write_text("#!/bin/sh\necho EXTERNAL\n")
    script.chmod(0o755)
    want = expected_diff(repo, repo.base, repo.head)
    for key, value in [("diff.external", str(script)), ("color.ui", "always"), ("diff.noprefix", "true")]:
        git(repo, "config", key, value)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))
    inputs = await _judge_inputs.capture_round_inputs(repo, repo.base, repo.head, repo.base)
    assert inputs["diff_sha256"] == sha256(want)
    assert "diff_error" not in inputs


async def test_working_tree_attributes_do_not_change_the_recorded_or_recreated_diff(repo, tmp_path):
    # `git diff A B` in a clone reads `.gitattributes` from the working tree and `info/attributes`, neither
    # of which is part of the two commits: a recorded hash must not depend on them.
    want = expected_diff(repo, repo.base, repo.head)  # before any attribute exists
    (repo / ".gitattributes").write_text("a.txt binary\n")  # uncommitted
    (repo / ".git" / "info" / "attributes").write_text("*.txt -diff\n")
    assert expected_diff(repo, repo.base, repo.head) != want  # git itself would diff differently here
    inputs = await _judge_inputs.capture_round_inputs(repo, repo.base, repo.head, repo.base)
    assert inputs["diff_sha256"] == sha256(want)
    clone = tmp_path / "fresh-clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    entry = _judge_inputs.build_judge_entry(
        inputs, engine="", model="", effort="", prompt_sha256="", outcome="PASS", attempts=1, carried=False
    )
    assert _judge_inputs.recreate_diff(clone, entry) == want
    assert _judge_inputs.recreate_diff(repo, entry) == want


# --- the command line ---


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._judge_inputs", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def write_report(path: Path, repo: Repo, shape: str) -> Path:
    row = {"round": 2, "verdict": "PASS", "blocking": 0, "judges": {"craft": entry_for(repo)}}
    report = {
        "snapshot": {"review": {"verdicts": [row]}},
        "metrics": {"metrics": {"rounds": [row]}},
        "verdicts": {"verdicts": [row]},
        "rounds": {"rounds": [row]},
    }[shape]
    if shape == "snapshot":
        report = {"review": {"verdicts": [row]}}
    if shape == "metrics":
        report = {"metrics": {"rounds": [row]}}
    path.write_text(json.dumps(report))
    return path


@pytest.mark.parametrize("shape", ["snapshot", "metrics", "verdicts", "rounds"])
def test_the_cli_finds_the_round_row_wherever_the_json_keeps_it(repo, tmp_path, shape):
    report = write_report(tmp_path / "report.json", repo, shape)
    out = tmp_path / "diff.patch"
    done = run_cli(
        "recreate", "--report", str(report), "--round", "2", "--judge", "craft", "--repo", str(repo),
        "--out", str(out), cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert out.read_bytes() == expected_diff(repo, repo.base, repo.head)


def test_the_cli_writes_the_diff_to_stdout_without_a_file(repo, tmp_path):
    report = write_report(tmp_path / "report.json", repo, "snapshot")
    done = subprocess.run(
        [
            sys.executable, "-m", "flyte_agent_factory._judge_inputs", "recreate",
            "--report", str(report), "--round", "2", "--judge", "craft", "--repo", str(repo),
            "--which", "round",
        ],
        capture_output=True,
    )  # fmt: skip
    assert done.returncode == 0 and done.stdout == expected_diff(repo, repo.base, repo.head)


def test_the_cli_exits_with_its_code_and_one_line_on_failures(repo, tmp_path):
    report = write_report(tmp_path / "report.json", repo, "snapshot")
    base = ["recreate", "--report", str(report), "--repo", str(repo)]
    craft = ["--round", "2", "--judge", "craft"]
    missing_round = run_cli(*base, "--round", "9", "--judge", "craft", cwd=tmp_path)
    missing_judge = run_cli(*base, "--round", "2", "--judge", "nobody", cwd=tmp_path)
    not_json = tmp_path / "bad.json"
    not_json.write_text("{")
    bad_file = run_cli(
        "recreate", "--report", str(not_json), "--round", "2", "--judge", "craft", cwd=tmp_path
    )
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"review": {"verdicts": [{"round": 2, "verdict": "PASS", "blocking": 0}]}}))
    old_row = run_cli("recreate", "--report", str(old), *craft, "--repo", str(repo), cwd=tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    no_repo = run_cli("recreate", "--report", str(report), *craft, "--repo", str(plain), cwd=tmp_path)
    no_dir = run_cli(
        "recreate", "--report", str(report), *craft, "--repo", str(tmp_path / "missing"), cwd=tmp_path
    )
    unwritable = run_cli(*base, *craft, "--out", str(tmp_path / "no" / "such" / "x.patch"), cwd=tmp_path)
    tampered = json.loads(report.read_text())
    tampered["review"]["verdicts"][0]["judges"]["craft"]["diff_sha256"] = "0" * 64
    report.write_text(json.dumps(tampered))
    mismatch = run_cli(*base, "--round", "2", "--judge", "craft", cwd=tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q")
    unreachable = run_cli(
        "recreate", "--report", str(report), "--round", "2", "--judge", "craft", "--repo", str(elsewhere),
        cwd=tmp_path,
    )  # fmt: skip
    expected = {
        "missing round": (missing_round, 2),
        "missing judge": (missing_judge, 2),
        "unparsable report": (bad_file, 2),
        "old row": (old_row, 2),
        "not a repository": (no_repo, 2),
        "missing --repo": (no_dir, 2),
        "unwritable --out": (unwritable, 2),
        "unreachable commit": (unreachable, 3),
        "tampered hash": (mismatch, 4),
    }
    for case, (done, code) in expected.items():
        assert done.returncode == code, (case, done.stderr)
        assert len(done.stderr.strip().splitlines()) == 1, (case, done.stderr)
        assert "Traceback" not in done.stderr, case
    assert "no recorded judge inputs" in old_row.stderr
    assert "not a git repository" in no_repo.stderr and "not a git repository" in no_dir.stderr


def test_importing_the_module_runs_nothing():
    done = subprocess.run(
        [sys.executable, "-c", "import flyte_agent_factory._judge_inputs"], capture_output=True, text=True
    )
    assert done.returncode == 0 and done.stdout == ""
