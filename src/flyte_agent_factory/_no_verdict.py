"""Judge replies with no parseable verdict: classify them, keep them privately, count them, report on them.

Why a reply has no verdict is unknowable once it is thrown away, so every one is kept in a JSONL file in the
private state store (`STATE_ROOT/metrics/no_verdict.jsonl`, checkpointed with the rest of the state): never in
the workspace, the project folder or the public repo. A record holds the round, judge, engine, model, effort,
prompt hash, attempt, when (`ts`, seconds since the Unix epoch, UTC: `time.time()`), why (`classification`),
the failure `shape`, the redacted reply (bounded) and what recovery was tried (`recovery`) and whether a full
judge re-run followed (`rerun`).

Retention, enforced on every write: at most `MAX_RECORDS` records, none older than `MAX_AGE_DAYS`, and a
reply of at most `MAX_REPLY_CHARS` characters (head and tail kept, the middle cut). Credentials are redacted
with `_bridge.redact_tokens` before the reply is cut or written.

A write failure logs once and never breaks a review: this is measurement, not part of the verdict.

    python -m flyte_agent_factory._no_verdict report [--json] [--path P]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flyte_agent_factory import _reviewer, _session_config, _timeline, _usage
from flyte_agent_factory._bridge import redact_tokens

MAX_RECORDS = 500
MAX_AGE_DAYS = 30
MAX_REPLY_CHARS = 20_000
_HEAD_CHARS, _TAIL_CHARS = 15_000, 5_000


#: A reply shorter than this whose first line starts the way a CLI or API error does is that error.
_ERROR_ONLY_CHARS = 400
#: How a CLI or API error begins. Only a reply whose first non-empty line starts this way is the error
#: itself; a review that mentions a rate limit or an error, even at the start of a later line, is a review.
_ERROR_START = re.compile(r"\[?(?:API Error|HTTP 429|You'?ve hit)", re.IGNORECASE)

SHAPES = (
    "empty",
    "error",
    "turn-limit",
    "near-miss-verdict",
    "findings-no-verdict",
    "truncated",
    "prose-only",
)


def _is_only_an_error(flat: str) -> bool:
    """Return whether a stripped, non-empty reply is a tool or limit error rather than a review."""
    first = flat.splitlines()[0]
    if first.startswith(_usage.USAGE_LIMIT_MARK):
        return True
    return len(flat) < _ERROR_ONLY_CHARS and bool(_ERROR_START.match(first))


def classify(text: str) -> str:
    """Return why a judge's text holds no verdict: no output, turn limit, tool or limit error, unparseable.

    A call that raised a timeout never gets here: that is NOT_RUN with reason `timeout` (see `_one_judge`).
    Shared by the supervisor (`_review._classify_no_verdict`) and the sub-agent's verdict follow-up, which
    must agree on what is worth asking again.
    """
    flat = (text or "").strip()
    if not flat or flat == _reviewer._NO_FINAL_MESSAGE:
        return "no output"
    # On a turn limit the CLI may exit 0 and the "text" is its raw result JSON.
    if "error_max_turns" in flat:
        return "turn limit"
    if _is_only_an_error(flat):
        return "tool or limit error"
    return "unparseable"


# --- failure shape ---

# A line that looks like a verdict the parser rejected: it starts with the label, or has the label and a
# verdict word on one line.
_NEAR_MISS = re.compile(
    r"^[ \t>#*_`-]*(?:(?:final|overall)[ \t*_`]+)?verdict\b[^\n]*$"
    r"|^[^\n]*\bverdict\b[^\n]*\b(?:pass|fail)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FINDING_SECTION = re.compile(
    r"^[ \t>#*_`-]*(?:FINDING|BLOCKING|NON-?BLOCKING)\b", re.IGNORECASE | re.MULTILINE
)
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
_SENTENCE_END = (".", "!", "?", ":", ")", "]", "}", '"', "'", "`", "*", "|", "-", "_")


def failure_shape(text: str, reason: str = "") -> str:
    """Return the shape of a reply with no verdict, one of `SHAPES`.

    `reason` is `classify(text)` when the caller already has it. A reply with a verdict-looking line the
    parser rejected is `near-miss-verdict` before anything else: that is the one `_findings.VERDICT` may
    need to learn.
    """
    reason = reason or classify(text)
    if reason == "no output":
        return "empty"
    if reason == "tool or limit error":
        return "error"
    if reason == "turn limit":
        return "turn-limit"
    flat = (text or "").strip()
    if _NEAR_MISS.search(flat):
        return "near-miss-verdict"
    if _FINDING_SECTION.search(flat):
        return "findings-no-verdict"
    if len(_FENCE.findall(flat)) % 2 == 1 or not flat.endswith(_SENTENCE_END):
        return "truncated"
    return "prose-only"


# --- the evidence store ---

_write_failed = False


def store_path() -> Path:
    """Return the evidence file; read at call time so a test or a restored state root is honoured."""
    return _session_config.STATE_ROOT / "metrics" / "no_verdict.jsonl"


def _bounded(reply: str) -> str:
    """Return the redacted `reply`, cut to `MAX_REPLY_CHARS` keeping the head and the tail."""
    reply = redact_tokens(reply or "")
    if len(reply) <= MAX_REPLY_CHARS:
        return reply
    cut = len(reply) - _HEAD_CHARS - _TAIL_CHARS
    return f"{reply[:_HEAD_CHARS]}\n[... {cut} chars cut ...]\n{reply[-_TAIL_CHARS:]}"


def read_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Return the stored records, oldest first; a missing file or a damaged line yields nothing for it."""
    try:
        # `replace`: a file with bytes that are not UTF-8 loses only the damaged lines, not every record.
        lines = (path or store_path()).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def append_record(record: dict[str, Any], *, now: float | None = None, path: Path | None = None) -> bool:
    """Append `record` and apply the retention; return whether it was written (a failure logs once)."""
    global _write_failed
    now = time.time() if now is None else now
    path = path or store_path()
    try:
        oldest = now - MAX_AGE_DAYS * 86400
        # A record dated far in the future (a clock that was wrong) would outlive the age limit forever.
        newest = now + 86400
        kept = [r for r in read_records(path) if _is_number(r.get("ts")) and oldest <= r["ts"] <= newest]
        kept = [*kept, record][-MAX_RECORDS:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        if not _write_failed:
            _timeline._log_line(
                f"[no-verdict] could not keep the evidence: {type(e).__name__}: {e}", err=True
            )
        _write_failed = True
        return False
    return True


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def make_record(
    *,
    round_: int,
    judge: str,
    engine: str,
    model: str,
    effort: str,
    prompt_sha256: str,
    attempt: int,
    classification: str,
    reply: str,
    recovery: str,
    rerun: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Return one evidence record for a reply that had no verdict (the original text, not a recovered one).

    `ts` is seconds since the Unix epoch, UTC (`time.time()`, or `now` when given).
    """
    return {
        "ts": time.time() if now is None else now,
        "round": round_,
        "judge": judge,
        "engine": engine,
        "model": model,
        "effort": effort,
        "prompt_sha256": prompt_sha256,
        "attempt": attempt,
        "classification": classification,
        "shape": failure_shape(reply, classification),
        "reply_chars": len(reply or ""),
        "reply": _bounded(reply),
        "recovery": recovery,
        "rerun": rerun,
    }


def recovered_suffix(verdict: str) -> str:
    """Return what a follow-up appends to a reply: one verdict line, as the judge would have ended it.

    Nothing else: the verdict line ends a finding (`_findings._is_section`), and any other text after the
    reply could join the last finding's evidence.
    """
    return f"\n\nVERDICT: {verdict}\n"


_RECOVERED_SUFFIX = re.compile(r"\n\nVERDICT: (?:PASS|FAIL)\n\Z")


def original_reply(text: str, recovered: bool = True) -> str:
    """Return `text` without the verdict a follow-up appended, i.e. the reply as the judge wrote it.

    Only a reply the sub-agent reported as recovered has one; any other text is returned as it is.
    """
    return _RECOVERED_SUFFIX.sub("", text, count=1) if recovered else text


def note_reply(
    timeline: _timeline._Timeline,
    *,
    judge: str,
    engine: str,
    round_: int,
    attempt: int,
    text: str,
    reason: str,
    followup: str,
    seen: Mapping[str, object],
    last: bool,
) -> bool:
    """Count one judge reply, and keep it when it had no verdict; return whether a follow-up recovered it.

    `reason` is `_find_usable_result_gap(text)`: empty when the text is usable. `followup` is what the
    sub-agent said of its verdict follow-up: empty (none), `recovered`, or `failed: <why>`. A reply that a
    follow-up recovered is usable, but its original had no verdict, so it still counts as one and is kept.
    """
    recovered = followup == "recovered" and not reason and bool(_RECOVERED_SUFFIX.search(text))
    timeline.count_judge_call(judge, engine)
    if recovered or followup.startswith("failed"):  # a call was made; `skipped: ...` made none
        timeline.count_followup(judge, engine, recovered=recovered)
    if not reason and not recovered:
        return False
    original = original_reply(text, recovered)
    classification = reason or classify(original)
    if recovered:
        recovery = "followup-recovered"
    elif followup.startswith("failed"):
        recovery = f"followup-{followup}"
    elif followup:
        recovery = f"not-attempted ({followup})"
    else:
        recovery = "not-attempted"
    rerun = bool(reason) and not last
    timeline.count_no_verdict(judge, engine)
    if rerun:
        timeline.count_rerun(judge, engine)
    append_record(
        make_record(
            round_=round_,
            judge=judge,
            engine=engine,
            model=str(seen.get("model", "")),
            effort=str(seen.get("effort", "")),
            prompt_sha256=str(seen.get("prompt_sha256", "")),
            attempt=attempt,
            classification=classification,
            reply=original,
            recovery=recovery,
            rerun=rerun,
        )
    )
    return recovered


# --- the report ---


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return counts of `records` by failure shape, judge and engine, and how the follow-ups did."""

    def count(key: str, rows: list[dict[str, Any]] = records) -> dict[str, int]:
        return dict(sorted(Counter(str(r.get(key) or "unknown") for r in rows).items()))

    def split(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        # The shape `factory.status` uses for `followups` and `reruns`, so one set of names serves both.
        return {"by_judge": count("judge", rows), "by_engine": count("engine", rows)}

    made_a_call = ("followup-recovered", "followup-failed")
    attempted = [r for r in records if str(r.get("recovery", "")).startswith(made_a_call)]
    recovered = [r for r in attempted if r.get("recovery") == "followup-recovered"]
    by_judge_shape: dict[str, dict[str, int]] = {}
    for r in records:
        shapes = by_judge_shape.setdefault(str(r.get("judge") or "unknown"), {})
        shape = str(r.get("shape") or "unknown")
        shapes[shape] = shapes.get(shape, 0) + 1
    return {
        "records": len(records),
        "by_shape": count("shape"),
        "by_judge": count("judge"),
        "by_engine": count("engine"),
        "by_judge_shape": {j: dict(sorted(s.items())) for j, s in sorted(by_judge_shape.items())},
        "followup": {
            "attempted": len(attempted),
            "recovered": len(recovered),
            "recovery_rate": round(len(recovered) / len(attempted), 3) if attempted else None,
        },
        "full_reruns": sum(1 for r in records if r.get("rerun")),
        "followups": {"attempted": split(attempted), "recovered": split(recovered)},
        "reruns": split([r for r in records if r.get("rerun")]),
    }


def format_report(report: Mapping[str, Any]) -> str:
    """Return `build_report`'s result as text."""
    lines = [f"{report['records']} replies with no verdict"]
    for title, key in (("shape", "by_shape"), ("judge", "by_judge"), ("engine", "by_engine")):
        lines.append(f"by {title}:")
        lines += [f"  {name}: {n}" for name, n in report[key].items()] or ["  (none)"]
    lines.append("by judge and shape:")
    lines += [
        f"  {judge}: " + ", ".join(f"{s} {n}" for s, n in shapes.items())
        for judge, shapes in report["by_judge_shape"].items()
    ] or ["  (none)"]
    fu = report["followup"]
    rate = "n/a" if fu["recovery_rate"] is None else f"{fu['recovery_rate']:.0%}"
    lines.append(f"follow-ups: {fu['attempted']} attempted, {fu['recovered']} recovered ({rate})")
    attempted, recovered = report["followups"]["attempted"], report["followups"]["recovered"]
    for title, key in (("judge", "by_judge"), ("engine", "by_engine")):
        lines.append(f"follow-ups by {title}:")
        lines += [
            f"  {name}: {n} attempted, {recovered[key].get(name, 0)} recovered"
            for name, n in attempted[key].items()
        ] or ["  (none)"]
    lines.append(f"full judge re-runs: {report['full_reruns']}")
    for title, key in (("judge", "by_judge"), ("engine", "by_engine")):
        lines.append(f"full judge re-runs by {title}:")
        lines += [f"  {name}: {n}" for name, n in report["reruns"][key].items()] or ["  (none)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run `report`: print the evidence store's counts, as text or `--json`."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._no_verdict")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="group stored no-verdict replies by failure shape, judge and engine")
    rep.add_argument("--json", action="store_true", help="print JSON instead of text")
    rep.add_argument("--path", type=Path, default=None, help="evidence file (default: the state store's)")
    args = parser.parse_args(argv)
    report = build_report(read_records(args.path))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
