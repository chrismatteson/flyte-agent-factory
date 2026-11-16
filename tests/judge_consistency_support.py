"""Shared by the judge-consistency harness tests: a fake study directory and a scripted reviewer call."""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

HARNESS_DIR = Path(__file__).resolve().parents[1] / "factory-improvement" / "judge_consistency"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "judge_study"
# The harness modules import each other by bare name, as they do when run as scripts.
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

import analyze_after  # noqa: E402 -- the harness dir must be on sys.path first
import prompts  # noqa: E402 -- the harness dir must be on sys.path first
import run as run_mod  # noqa: E402 -- the harness dir must be on sys.path first
import study as study_mod  # noqa: E402 -- the harness dir must be on sys.path first

__all__ = ["analyze_after", "prompts", "run_mod", "study_mod"]  # re-exported for the tests

USAGE_MARK = "\n@@USAGE@@"

PASS_REPLY = "Everything in the brief works.\n\nVERDICT: PASS"
FINDING_REPLY = (
    "FINDING: login crashes on an empty name\n"
    "Breaks: C1\n"
    "Evidence: command: pytest tests/test_login.py | output: 1 failed\n\n"
    "VERDICT: FAIL"
)
# The raw verdict is FAIL but no finding carries evidence: the check must restate and re-run the claim.
CLAIM_REPLY = "The export is incomplete.\n\nVERDICT: FAIL"
NOT_REPRODUCED = "CHECK 1: NOT REPRODUCED -- ran it, behaved correctly"
REPRODUCED = "CHECK 1: REPRODUCED -- the test failed as described"
CLAIM_REPRODUCED = "CLAIM 1: the export is incomplete\nCHECK 1: REPRODUCED -- rows are missing"

STORED_INTRO, STORED_OUTRO = "You are the reviewer.\n\n", "\nThe work to review follows.\n"


def stored_prompt(row: dict[str, Any], block: str) -> str:
    """Return a fake stored prompt: it carries the decision's brief and acceptance text, as real ones do."""
    return f"{STORED_INTRO}Brief of {row['run']}.\nC1. Criterion of {row['run']}.\n\n{block}{STORED_OUTRO}"


def decision_row(n: int, **kw: Any) -> dict[str, Any]:
    """Return a sampled-decision row for a made-up run (`demo-n`); `kw` overrides fields."""
    row = {
        "decision_id": f"demo{n}-1-0-abc12{n}:r1:correctness",
        "run": f"demo{n}-1-0-abc12{n}",
        "group": f"pr-{n}",
        "head_sha": f"{n:040x}",
        "judge": "correctness",
        "original_verdict": "FAIL",
        "recorded_usd": 2.0,
        "round": 1,
        "round_kind": "first",
        "confidence": "high",
        "swe": False,
    }
    return {**row, **kw}


def rep_record(did: str, rep: int, verdict: str, usd: float = 1.0, **kw: Any) -> dict[str, Any]:
    """Return a study rep file: the fields the harness reads."""
    return {
        "decision_id": did,
        "rep": rep,
        "verdict": verdict,
        "raw_verdict": verdict,
        "evidenced_findings": 1 if verdict == "FAIL" else 0,
        "text": FINDING_REPLY if verdict == "FAIL" else PASS_REPLY,
        "usd": usd,
        **kw,
    }


def make_study(
    tmp_path: Path, rows: list[dict[str, Any]], reps: dict[str, tuple[Any, Any]] | None = None
) -> Any:
    """Build a fake study directory under `tmp_path` and return it loaded.

    `reps` maps a decision id to the (rep1, rep2) study records; default both PASS at $1.
    """
    from flyte_agent_factory import _findings  # the test's own copy: the old block is in every stored prompt

    root = tmp_path / "study"
    shutil.copytree(FIXTURES, root)
    (root / "data" / "inputs").mkdir(parents=True)
    (root / "data" / "snapshots").mkdir()
    (root / "data" / "sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for row in rows:
        did = row["decision_id"]
        stem = did.replace(":", "__")
        prompt = stored_prompt(row, _findings.CRAFT_FINDING_INSTRUCTIONS)
        (root / "data" / "inputs" / f"{stem}.json").write_text(
            json.dumps({"decision_id": did, "prompt": prompt})
        )
        snapshot = {"brief": f"Brief of {row['run']}.", "acceptance": f"C1. Criterion of {row['run']}."}
        (root / "data" / "snapshots" / f"{row['run']}.json").write_text(json.dumps(snapshot))
        pair = (reps or {}).get(did, (rep_record(did, 1, "PASS"), rep_record(did, 2, "PASS")))
        for n, rec in enumerate(pair, 1):
            if rec is not None:
                path = root / "results" / "rejudge" / stem / f"rep{n}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rec))
    return study_mod.load(root)


def reply(text: str, usd: float | None = 1.0, models: tuple[str, ...] = ("claude-opus-5-5",)) -> str:
    """Return reviewer text with the usage trailer the fake `split_usage` reads."""
    if usd is None:
        return text
    return text + USAGE_MARK + json.dumps({"usd": usd, "models": list(models), "input_tokens": 10})


class ScriptedCall:
    """A fake reviewer call: pops the next scripted reply for the judge (opus) or the check (sonnet)."""

    def __init__(self, judge: list[Any], check: list[Any] | None = None) -> None:
        self.script = {"opus": list(judge), "sonnet": list(check or [])}
        self.calls: list[tuple[str, Path, run_mod.CallSetting]] = []
        self.pause = False

    async def __call__(
        self, prompt: str, workdir: Path, setting: run_mod.CallSetting
    ) -> tuple[str, list[str]]:
        import asyncio

        self.calls.append((prompt, workdir, setting))
        assert workdir.is_dir(), "each call gets a prepared workspace"
        if self.pause:
            await asyncio.sleep(0)
        item = self.script[setting.model].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, [f"action-{len(self.calls)}"]

    def of(self, model: str) -> list[tuple[str, Path, run_mod.CallSetting]]:
        return [c for c in self.calls if c[2].model == model]


def make_run(tmp_path: Path, study: Any, call: Any, cap: float = 100.0) -> run_mod.Run:
    """Return a `Run` over the fake study, writing into `tmp_path/out`."""
    out = tmp_path / "out"
    return run_mod.Run(
        study=study,
        findings=study_mod.load_findings_module(),
        budget=study.rejudge.Budget(cap, out / "spend.json"),
        call=call,
        out=out,
        source=tmp_path / "source",
        work_root=tmp_path / "work",
        check_setting=run_mod.CallSetting("sonnet", "low", 40),
    )


def read_rep(run: run_mod.Run, row: dict[str, Any], rep: int) -> dict[str, Any]:
    """Return the saved after-rep record."""
    path = run.study.rejudge.build_rep_path(run.out, row["decision_id"], rep)
    data: dict[str, Any] = json.loads(path.read_text())
    return data
