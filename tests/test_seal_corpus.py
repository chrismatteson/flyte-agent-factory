from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _seal_record
from tests import seal_corpus as corpus

ROOT = Path(__file__).resolve().parents[1]
#: The hard cases the audit is known to get wrong, and only those.
KNOWN_MISSES = {"hard-obfuscated-fetch-succeeded", "hard-fetch-failed-silently"}


def test_case_names_are_unique_and_labelled():
    names = [c.name for c in corpus.CASES]
    assert len(names) == len(set(names))
    for c in corpus.CASES:
        assert c.expected in ("clean", "void")
        assert (c.expected == "void") == bool(c.reason), c.name


def test_known_misses_are_exactly_the_hard_cases():
    assert {c.name for c in corpus.CASES if c.known_miss} == KNOWN_MISSES
    assert all(c.note for c in corpus.CASES if c.known_miss)


def test_every_route_has_a_positive_and_a_negative():
    names = {c.name for c in corpus.CASES}
    for route in (
        "git-fetch", "git-ls-remote", "curl", "gh-api", "web-search", "web-fetch",
        "pip-download", "other-attempt",
    ):  # fmt: skip
        assert any(route in n and n.startswith("void-route") for n in names), route
        assert any(route in n and n.startswith("clean-attempt") for n in names), route


@pytest.mark.parametrize("case", corpus.CASES, ids=lambda c: c.name)
def test_audit_agrees_with_the_label(case, tmp_path):
    verdict = corpus.audit_case(case, tmp_path / "rec")
    if case.known_miss:
        assert verdict.verdict != case.expected, f"{case.name} is now right: drop known_miss and re-measure"
        return
    assert verdict.verdict == case.expected, [f.to_dict() for f in verdict.reasons]
    if case.expected == "void":
        assert case.reason in {f.kind for f in verdict.reasons}


def test_end_json_hashes_every_file_but_itself(tmp_path):
    record = corpus.Record()
    corpus.work(record.role("author-1"))
    record.write(tmp_path)
    role_dir = tmp_path / "roles" / "author-1"
    end = json.loads((role_dir / "end.json").read_text())
    assert set(end["files"]) == {"role.json", "proxy.jsonl", "guard.jsonl", "transcript/session.jsonl"}
    assert end["files"]["proxy.jsonl"]["lines"] == 0


def test_rates_script_check_passes():
    done = subprocess.run(
        [sys.executable, "ops/seal_audit_rates.py", "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    assert "FP rate" in done.stdout


def test_rates_script_json_matches_the_recorded_counts():
    done = subprocess.run(
        [sys.executable, "ops/seal_audit_rates.py", "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
        env={"PATH": "/usr/bin:/bin"},
    )
    rates = json.loads(done.stdout)
    assert rates == json.loads((ROOT / "ops" / "seal_audit_rates.expected.json").read_text())
    assert set(rates["misclassified"]) == KNOWN_MISSES


REAL_CASES = [c for c in corpus.CASES if c.real]


def test_there_are_real_writer_cases_clean_and_void_for_both_engines():
    names = {c.name for c in REAL_CASES}
    assert {
        "clean-real-writer-with-subagent",
        "void-reached-real-writer-in-subagent-file",
        "clean-real-writer-codex",
        "void-reached-real-writer-codex",
    } <= names


def test_real_writer_records_are_made_by_seal_record(tmp_path):
    """The record is `register`/`collect_claude`/`finish` output: its end.json hashes, the transcripts'
    paths under the config dir, and the sub-agent file with its meta are what the audit then reads."""
    case = next(c for c in REAL_CASES if c.name == "clean-real-writer-with-subagent")
    corpus.build_case(case, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [r["role_id"] for r in manifest["roles"]] == ["author-1", "subagent-1"]
    assert manifest["spec_sha256"] == corpus.SPEC_SHA
    role_dir = tmp_path / "roles" / "author-1"
    transcripts = sorted(
        p.relative_to(role_dir).as_posix() for p in (role_dir / "transcript").rglob("*") if p.is_file()
    )
    assert [t for t in transcripts if "subagents" in t] == [
        t for t in transcripts if "/subagents/agent-" in t
    ] and len([t for t in transcripts if "subagents" in t]) == 2
    assert any(t.endswith(".meta.json") for t in transcripts)
    assert all(t.startswith("transcript/projects/") for t in transcripts)
    end = json.loads((role_dir / "end.json").read_text())
    assert end["files"].keys() == {*transcripts, "role.json", "proxy.jsonl", "guard.jsonl"}
    first = json.loads((role_dir / transcripts[0]).read_text().splitlines()[0])
    assert first["type"] == "queue-operation"  # the real envelope lines are there, and are ignored
    assert corpus.audit_case(case, tmp_path / "again").verdict == "clean"


def test_real_writer_codex_rollout_is_collected_under_sessions(tmp_path):
    case = next(c for c in REAL_CASES if c.name == "clean-real-writer-codex")
    corpus.build_case(case, tmp_path)
    paths = _seal_record.role_paths(tmp_path, "author-1")
    assert [p.relative_to(paths.dir).parts[:3] for p in paths.transcript_dir.rglob("*.jsonl")] == [
        ("transcript", "sessions", "2026")
    ]
    role = json.loads((paths.dir / "role.json").read_text())
    assert role["engine"] == "codex" and role["seal"]["guard"] is False


def test_real_writer_agrees_with_the_plain_writer_on_the_verdict(tmp_path):
    for case in REAL_CASES:
        plain = corpus.Case(case.name, case.build, case.expected, case.reason)
        assert (
            corpus.audit_case(case, tmp_path / "r" / case.name).verdict
            == corpus.audit_case(plain, tmp_path / "p" / case.name).verdict
        )


def test_the_operator_note_states_the_measured_rates():
    expected = json.loads((ROOT / "ops" / "seal_audit_rates.expected.json").read_text())
    text = " ".join((ROOT / "docs" / "sealed-runs.md").read_text().split())
    matrix = expected["matrix"]
    void, clean = matrix["true_void"] + matrix["false_clean"], matrix["true_clean"] + matrix["false_void"]
    stated = re.search(r"Measured on (\d+) cases \((\d+) void, (\d+) clean\)", text)
    assert stated, "docs/sealed-runs.md no longer states the corpus size"
    assert tuple(map(int, stated.groups())) == (expected["cases"], void, clean)
    fp = re.search(r"false-positive rate (\d+)/(\d+) = ([\d.]+)%", text)
    fn = re.search(r"false-negative rate (\d+)/(\d+) = ([\d.]+)%", text)
    assert fp and fn, "docs/sealed-runs.md no longer states both rates"
    assert (int(fp[1]), int(fp[2]), fp[3]) == (
        matrix["false_void"],
        clean,
        f"{matrix['false_void'] / clean:.1%}"[:-1],
    )
    assert (int(fn[1]), int(fn[2]), fn[3]) == (
        matrix["false_clean"],
        void,
        f"{matrix['false_clean'] / void:.1%}"[:-1],
    )
    per_reason = re.search(r"Per reason: ([^.]+)\.", text)
    assert per_reason, "docs/sealed-runs.md no longer states the per-reason figures"
    stated_reasons = {
        m[0]: (int(m[2]), int(m[1])) for m in re.findall(r"([\w-]+) (\d+)/(\d+)", per_reason[1])
    }
    assert stated_reasons == {k: (v["cases"], v["found"]) for k, v in expected["per_reason"].items()}
