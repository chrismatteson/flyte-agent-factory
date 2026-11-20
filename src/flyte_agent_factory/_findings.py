"""Reading a reviewer's findings, and the one mechanical rule that turns them into a verdict.

Pure: standard library only, so the engine and an offline measurement harness apply the same rule to the
same text. The findings decide the verdict, not the reply's `VERDICT:` line: a finding blocks when it is not
advisory and its evidence is in a form someone else can re-run and has content: a `command:` or `test:` with
its `| output:`, an `Output:` line or a fenced block, a `missing:`, `clause:`, `reproduction:` or `file:`
with text after the label (a label may sit behind list or emphasis markup, `- **test:**`), a fenced block, or
a `path:line` whose path has a letter in it; a bare label or prose is "no evidence".
The parser is tolerant about the markup around a finding and strict about that substance.

A second, cheaper call (the finding check) re-runs each blocking finding's evidence in a fresh copy and can
only *remove* findings that do not reproduce. A check that cannot run removes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# One verdict per line, last line wins. Tolerates the markdown a model wraps around the line
# (`**VERDICT:** PASS`, `**VERDICT: PASS**`, backticks, a quote or heading prefix), a `-` or dash for the
# colon, trailing punctuation and one short parenthetical. A line that merely mentions both words is not
# a verdict: the word must follow the separator directly, and nothing else may trail it.
# This is also what ends a finding (`_is_section`): keep it exactly this narrow, or a per-finding
# `Verdict: FAIL - reproduced` line would cut a finding off from its evidence.
VERDICT = re.compile(
    r"^[ \t>#*_`]*VERDICT[*_`]*[ \t]*[:\-–—][*_`]*[ \t]*[*_`]*(PASS|FAIL)[*_`]*[ \t]*[.!]?"
    r"[ \t]*(?:\([^()\n]{0,80}\))?[ \t]*[.!]?[*_`]*[ \t\r]*$",
    re.MULTILINE | re.IGNORECASE,
)
# The extra forms a judge writes for the same line: `Final verdict: PASS`, `Overall verdict = FAIL`, `=` as
# the separator. No free-text reason tail: a reason can hedge ("PASS - but it fails on X"), and the
# same-session follow-up recovers those replies. Read through `verdict_words`, never for a section boundary.
VERDICT_VARIANT = re.compile(
    r"^[ \t>#*_`]*(?:(?:FINAL|OVERALL)[ \t*_`]+)?VERDICT[*_`]*[ \t]*[:=\-–—][*_`]*[ \t]*[*_`]*(PASS|FAIL)"
    r"[*_`]*[ \t]*[.!]?[ \t]*(?:\([^()\n]{0,80}\))?[ \t]*[.!]?[*_`]*[ \t\r]*$",
    re.MULTILINE | re.IGNORECASE,
)


def verdict_words(text: str) -> list[str]:
    """Return the verdict words in `text`, in order: the strict lines if there are any, else a variant.

    A variant never overrides a strict line, so a reply with a strict `VERDICT:` line reads exactly as it
    did before the variants existed. A variant counts only as the reply's last non-blank line, outside a
    code fence and not `>`-quoted: the judge's own closing line, never one it quoted from the author.
    """
    strict = VERDICT.findall(text or "")
    if strict:
        return strict
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines or lines[-1].lstrip().startswith(">"):
        return []
    if sum(1 for line in lines if _FENCE.match(line)) % 2:
        return []
    return VERDICT_VARIANT.findall(lines[-1])


REPRODUCED, NOT_REPRODUCED = "REPRODUCED", "NOT_REPRODUCED"
CANNOT_CHECK = "CANNOT_CHECK"
# `_bridge.redact_tokens` writes this over anything shaped like a credential before a judge's reply is read
# (`redact` writes the `, N chars` form). A finding that carries it reached the checker with that line masked,
# so the checker did not see what the judge saw: its answer about that finding is not a verdict on it.
REDACTION_MARKER = re.compile(r"<redacted(?:, \d+ chars)?>")

# One string for both places that say it: the decision's reason, and the timeline row when the check fails.
NOTHING_TO_KEEP_REASON = "FAIL without an evidenced finding and the check did not run: nothing to keep, PASS"

# Anything a model puts before a keyword: indentation, quote, heading, emphasis, list bullet or number.
_LEAD = r"(?:[ \t>#*_`-]|\d+[.)][ \t]*)*"
# The qualifier is only a number, a parenthetical and markup (`FINDING 2 (blocking):**`), so prose that
# starts with the word ("Finding the cause was hard: ...") is not a header.
_HEADER = re.compile(
    rf"^{_LEAD}FINDING(?![A-Za-z0-9])"
    r"(?P<qualifier>[ \t*_`#]*(?:\d+)?[ \t*_`]*(?:\([^()\n]{0,50}\))?[ \t*_`]*):(?P<title>.*)$",
    re.IGNORECASE,
)
# A header written as a list item (`- FINDING: x`) belongs to the section it sits in; one at the start of
# a line starts afresh, so an earlier NON-BLOCKING section cannot demote it.
_LIST_ITEM = re.compile(r"^(?:[ \t]+|[ \t>]*(?:[-*+]|\d+[.)])[ \t])")
_EVIDENCE = re.compile(
    rf"^{_LEAD}Evidence(?:[ \t]*\([^()\n]{{0,60}}\))?[*_`]*[ \t]*:[*_`]*[ \t]*(?P<rest>.*)$", re.IGNORECASE
)
_BREAKS = re.compile(rf"^{_LEAD}Breaks[*_`]*[ \t]*:[*_`]*[ \t]*(?P<rest>.*)$", re.IGNORECASE)
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
# A section line is a header, not a sentence that starts with the word: the keyword, at most a short
# label (NON-BLOCKING only: findings/issues/notes/items, as `_review._NONBLOCKING` reads it) or a
# parenthetical, then a colon or nothing but markup to the end of the line. `BLOCKING: <n>` and `CRITERIA:`
# count only when nothing but markup follows. "Non-blocking in isolation but they break C3", "Criteria: C3
# says Z.", "BLOCKING: 1 of these two paths crash." and "- non-blocking aside: slow" stay inside the finding.
_PAREN = r"(?:[ \t]*\([^()\n]{0,80}\))?"
_TAIL = r"[*_`]*[ \t\r]*$"
_NONBLOCKING_SECTION = re.compile(
    rf"^[ \t>#*_`]*NON-?BLOCKING(?![A-Za-z])(?:[ \t]+(?:findings|issues|notes|items))?{_PAREN}"
    rf"[*_`]*[ \t]*(?::.*|{_TAIL})$",
    re.IGNORECASE,
)
# The other lines that end a finding, or end a NON-BLOCKING section.
_SECTION = re.compile(
    rf"^{_LEAD}(?:"
    rf"(?:UNVERIFIED FROM HERE|DESIGN CONFLICTS?|NEW REQUIREMENTS?)(?![A-Za-z]){_PAREN}[*_`]*[ \t]*"
    rf"(?::.*|{_TAIL})"
    rf"|CRITERIA(?![A-Za-z]){_PAREN}(?:[*_`]*[ \t]*:)?{_TAIL}"
    rf"|BLOCKING[*_`]*[ \t]*:[*_`]*[ \t]*`?\d+`?{_TAIL}"
    r")$",
    re.IGNORECASE,
)
_ADVISORY_LABEL = re.compile(r"(?<!not )(?<!non-)\b(?:minor|nit|advisory|non-?blocking)\b", re.IGNORECASE)
# A qualifier that also says the finding blocks (`blocking, not a nit`) is never advisory.
_BLOCKING_QUALIFIER = re.compile(r"(?<!non-)(?<!non )\bblocking\b", re.IGNORECASE)
# Evidence that says there is none is not evidence.
_NO_EVIDENCE = re.compile(r"^[\s*_`]*(?:none|n/?a|nothing|-+|—)[\s.*_`]*$", re.IGNORECASE)
# Evidence in a form someone else can re-run, as the judge prompt asks. A label alone is not evidence: it
# needs content after it, and `command:`/`test:` need the observed output (`| output: <text>` or an
# `Output:` line) or a fence.
# The `path:line` form needs a letter in the path so a ratio like `1.5:2` is not taken for one.
# A label may sit behind list or emphasis markup (`- **test:** ...`), as judges write it.
_MARKUP = r"[ \t>*_`+-]*"
_LABEL = re.compile(rf"^{_MARKUP}(?P<label>command|test|file|missing|clause|reproduction)\b", re.IGNORECASE)
_CONTENT_LABEL = re.compile(
    rf"^{_MARKUP}(?:file|missing|clause|reproduction)[*_`]*\s*:[ \t*_`]*[^\s*_`]",
    re.IGNORECASE | re.MULTILINE,
)
_OUTPUT = re.compile(rf"(?:\||^{_MARKUP})\s*output[*_`]*\s*:[ \t*_`]*[^\s*_`]", re.IGNORECASE | re.MULTILINE)
_PATH_LINE = re.compile(r"(?<![\w.])[\w./-]*[A-Za-z][\w./-]*\.[A-Za-z]\w*:\d+")
# A finding the reviewer itself labelled minor stays advisory however good its evidence is.
_ADVISORY_TITLE = re.compile(r"(?:minor|nit|advisory|non-?blocking):", re.IGNORECASE)


def _is_advisory_qualifier(qualifier: str) -> bool:
    """Return whether a `FINDING (...)` qualifier marks it advisory without also calling it blocking."""
    return bool(_ADVISORY_LABEL.search(qualifier)) and not _BLOCKING_QUALIFIER.search(qualifier)


@dataclass(frozen=True)
class Finding:
    """One `FINDING` block of a reviewer's reply, as parsed."""

    title: str
    body: str  # everything after the header line, up to the end of the block
    breaks: str  # the `Breaks:` line's text, or ""
    evidence: str  # what follows `Evidence:`, which may run over several lines, or ""
    advisory: bool
    in_nonblocking_section: bool = False  # it sat under a NON-BLOCKING heading, so the PASS path lists it

    def render(self) -> str:
        """Return the finding as a reviewer wrote it, for the author and for the finding check."""
        return f"FINDING: {self.title}\n{self.body}".strip()


def _is_redacted(finding: Finding) -> bool:
    """Return whether the engine masked part of this finding's block before the check saw it."""
    return bool(REDACTION_MARKER.search(finding.render()))


def _is_section(line: str) -> bool:
    return bool(_SECTION.match(line) or _NONBLOCKING_SECTION.match(line) or VERDICT.match(line))


def _read_keyword_content(lines: list[str]) -> tuple[str, str]:
    """Return (the `Breaks:` line's text, the text of every `Evidence:` entry) from one block's lines."""
    breaks = ""
    entries: list[list[str]] = []
    current: list[str] | None = None
    fenced = False
    for line in lines:
        if not fenced and (m := _EVIDENCE.match(line)):
            current = [m.group("rest")]
            entries.append(current)
        elif not fenced and (m := _BREAKS.match(line)):
            breaks = breaks or m.group("rest").strip()
            current = None
        elif current is not None:
            current.append(line)
        if _FENCE.match(line):
            fenced = not fenced
    texts = ["\n".join(entry).strip() for entry in entries]
    # A bare fence or bullet is not evidence: something has to be written.
    return breaks, "\n".join(t for t in texts if re.search(r"\w", t) and not _NO_EVIDENCE.match(t))


def parse_findings(text: str) -> list[Finding]:
    """Return every `FINDING` block in a reviewer's reply, in order."""
    found: list[Finding] = []
    header: re.Match[str] | None = None
    lines: list[str] = []
    advisory_section = False
    fenced = False

    def close_block() -> None:
        if header is None:
            return
        breaks, evidence = _read_keyword_content(lines)
        title = header.group("title").strip(" \t*_`")
        advisory = (
            advisory_section
            or _is_advisory_qualifier(header.group("qualifier"))
            or bool(_ADVISORY_TITLE.match(title))
        )
        found.append(Finding(title, "\n".join(lines).strip(), breaks, evidence, advisory, advisory_section))

    for line in (text or "").splitlines():
        if not fenced:
            if m := _HEADER.match(line):
                close_block()
                header, lines = m, []
                advisory_section = advisory_section and bool(_LIST_ITEM.match(line))
                continue
            if _is_section(line):
                close_block()
                header, lines = None, []
                # A NON-BLOCKING heading opens a section that lasts until another one begins.
                advisory_section = bool(_NONBLOCKING_SECTION.match(line))
                continue
        if _FENCE.match(line):
            fenced = not fenced
        if header is not None:
            lines.append(line)
    close_block()
    return found or _synthesize_header_less_finding(text)


def _synthesize_header_less_finding(text: str) -> list[Finding]:
    """Return the one finding a reply that fails without any `FINDING` header stands for, or none.

    The rule this replaced treated the whole reply as the finding, so a FAIL whose evidence is runnable but
    whose header is missing still blocks (and the check runs on it). Prose alone has no evidence to run.
    """
    if not _last_verdict_is_fail(text):
        return []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not _EVIDENCE.match(line):
            continue
        breaks, evidence = _read_keyword_content(lines[i:])
        if not has_runnable_evidence(Finding("", "", breaks, evidence, False)):
            continue
        title = next((t.strip(" \t*_`#") for t in reversed(lines[:i]) if t.strip(" \t*_`#")), "")
        return [Finding(title, (text or "").strip(), breaks, evidence, False)]
    return []


def _has_fence(text: str) -> bool:
    return any(_FENCE.match(line) for line in text.splitlines())


def has_runnable_evidence(finding: Finding) -> bool:
    """Return whether the finding's evidence is in a form someone else can re-run, as the prompt requires."""
    text = finding.evidence
    if not text:
        return False
    if _has_fence(text) or _PATH_LINE.search(text):
        return True
    # `command: pytest` alone is a label with a command and no observed failure; the output is what a
    # checker compares against.
    return bool(_CONTENT_LABEL.search(text) or _OUTPUT.search(text))


def classify_evidence_kind(finding: Finding) -> str:
    """Return which form the evidence takes, as one of a fixed set of labels, for the metric."""
    text = finding.evidence
    if m := _LABEL.match(text):
        return m.group("label").lower()
    if _has_fence(text):
        return "fence"
    if _PATH_LINE.search(text):
        return "path"
    return "none"


def select_blocking(findings: list[Finding]) -> list[Finding]:
    """Return the findings that block: not advisory, and with evidence someone else can re-run.

    A missing `Breaks:` line does not make a finding non-blocking: a reviewer who forgets the line has not
    made the defect any less real, and demoting it would weaken the review. Prose evidence ("it looked
    wrong") is "no evidence", exactly as a missing `Evidence:` line is.
    """
    return [f for f in findings if has_runnable_evidence(f) and not f.advisory]


def _last_verdict_is_fail(text: str) -> bool:
    verdicts = verdict_words(text)
    return bool(verdicts) and verdicts[-1].upper() == "FAIL"


def needs_check(text: str) -> bool:
    """Return whether the reply's verdict depends on the finding check."""
    return bool(select_blocking(parse_findings(text))) or _last_verdict_is_fail(text)


# --- the finding check ---

_CHECK = re.compile(
    rf"^{_LEAD}CHECK[ \t]*(?P<n>\d+)[*_`]*[ \t]*[:.\-)][*_`]*[ \t]*[*_`]*"
    r"(?P<result>NOT[ _-]*REPRODUCED|CANNOT[ _-]*CHECK|REPRODUCED)[*_`]*[ \t]*"
    r"(?:(?:--|—|–|-|:)[ \t]*(?P<reason>.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
_CLAIM = re.compile(
    rf"^{_LEAD}CLAIM[ \t]*(?P<n>\d+)[*_`]*[ \t]*[:.\-)][*_`]*[ \t]*(?P<title>.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_check(text: str) -> dict[int, tuple[str, str]]:
    """Return finding number (1-based) -> (REPRODUCED | NOT_REPRODUCED | "CANNOT_CHECK", reason)."""
    out: dict[int, tuple[str, str]] = {}
    for m in _CHECK.finditer(text or ""):
        result = re.sub(r"[ _-]+", "_", m.group("result").upper())
        out[int(m.group("n"))] = (result, (m.group("reason") or "").strip(" \t*_`"))
    return out


def parse_claims(text: str) -> dict[int, str]:
    """Return claim number (1-based) -> the blocking problem the checker says the reply claims."""
    return {int(m.group("n")): m.group("title").strip(" \t*_`") for m in _CLAIM.finditer(text or "")}


_CHECK_ANSWER = (
    "Answer with one line per number, nothing else that you need me to read, exactly:\n\n"
    "    CHECK <n>: REPRODUCED -- <what you saw>\n"
    "    CHECK <n>: NOT REPRODUCED -- <what you ran and saw instead>\n"
    "    CHECK <n>: CANNOT CHECK -- <why it cannot be run here>\n\n"
    "`REPRODUCED` only if you ran it and saw the wrong behaviour described. `NOT REPRODUCED` only if you "
    "ran it as written, correctly, and the behaviour was right. If you could not run it (no network, no "
    "cluster, a missing tool), say `CANNOT CHECK`: that is not a verdict on the finding. Do not fix "
    "anything, do not look for other problems, and do not judge whether the finding matters: only whether "
    "it reproduces."
)


def build_check_prompt(
    judge: str, brief: str, acceptance: str, findings: list[Finding], reply: str = ""
) -> str:
    """Return what the finding checker is told.

    With `findings` it re-runs each one's evidence. Without any it is given the judge's `reply`, which failed
    the work without writing a finding it could be held to; the checker first restates each blocking problem
    the reply claims, so there is something concrete to run.
    """
    head = (
        f"You are checking findings that the {judge} judge wrote about someone else's work. "
        "You have your own copy of the workspace; nothing you change is kept. "
        "You are not reviewing the work.\n\n"
        f"## What was asked for\n\n{brief.strip()}\n\n"
        + (
            f'## What the author claims "done" means\n\n{acceptance.strip()}\n\n'
            if acceptance.strip()
            else ""
        )
    )
    if findings:
        numbered = "\n\n".join(f"### Finding {i}\n\n{f.render()}" for i, f in enumerate(findings, 1))
        return (
            head + "## Findings to check\n\n"
            "For each, re-run the evidence it gives, as written, in this fresh copy.\n\n"
            f"{numbered}\n\n## Your answer\n\n{_CHECK_ANSWER}"
        )
    return (
        head + "## The judge's reply\n\n"
        "It ended in `VERDICT: FAIL` but wrote no finding with evidence. Read it, then restate each "
        "*blocking* problem it claims (not style, not suggestions, not things it says it could not "
        "verify), one per line, exactly:\n\n    CLAIM <n>: <one line: what is wrong>\n\n"
        "Then try to reproduce each claim yourself in this fresh copy.\n\n"
        f"{reply.strip()}\n\n## Your answer\n\nFirst the `CLAIM` lines, then a `CHECK` line for each.\n\n"
        f"{_CHECK_ANSWER}"
    )


# --- the verdict ---


@dataclass
class Decision:
    """The verdict on one reply, with the findings or claims that make it and the ones the check dropped."""

    verdict: str  # "PASS" | "FAIL"
    kept: list[str] = field(default_factory=list)  # the blocking findings, as text, when the verdict is FAIL
    dropped: list[str] = field(default_factory=list)  # findings the check did not reproduce
    reason: str = ""
    # The same entries as `kept`, as objects: findings on the finding path, claim strings on the claims path.
    kept_findings: list[Finding] = field(default_factory=list)
    kept_claims: list[str] = field(default_factory=list)


def decide(text: str, check_text: str | None) -> Decision:
    """Return the verdict for a correctness or completeness reply: the single mechanical rule.

    Findings decide, not the `VERDICT:` line: a blocking, evidenced finding under `VERDICT: PASS` is a FAIL
    (the prompt tells the judge FINDING blocks are only for blocking findings). `check_text` is the finding
    check's reply, or None when it did not run or failed, in which case nothing is dropped.
    """
    bl = select_blocking(parse_findings(text))
    checks = parse_check(check_text) if check_text is not None else {}
    if bl:
        if check_text is None:
            return Decision(
                "FAIL",
                [f.render() for f in bl],
                reason="the finding check did not run",
                kept_findings=list(bl),
            )
        answers = {
            i: (CANNOT_CHECK, "evidence redacted before the check")
            if _is_redacted(f)
            else checks.get(i, ("", ""))
            for i, f in enumerate(bl, 1)
        }
        dropped = [f.render() for i, f in enumerate(bl, 1) if answers[i][0] == NOT_REPRODUCED]
        kept_findings = [f for i, f in enumerate(bl, 1) if answers[i][0] != NOT_REPRODUCED]
        if kept_findings:
            return Decision("FAIL", [f.render() for f in kept_findings], dropped, kept_findings=kept_findings)
        return Decision("PASS", [], dropped, "every finding failed to reproduce")
    if _last_verdict_is_fail(text):
        claims = parse_claims(check_text) if check_text is not None else {}
        kept = [
            f"{title} -- {checks[n][1]}" if checks[n][1] else title
            for n, title in claims.items()
            if n in checks and checks[n][0] == REPRODUCED
        ]
        if kept:
            return Decision(
                "FAIL", kept, reason="a claim without an evidenced finding reproduced", kept_claims=list(kept)
            )
        if check_text is None:
            return Decision(
                "PASS",
                reason=NOTHING_TO_KEEP_REASON,
            )
        return Decision("PASS", reason="FAIL without an evidenced finding, and no claim reproduced")
    return Decision("PASS")


# --- the judge prompt ---

# The craft judge's instruction, unchanged: craft is advisory and keeps the old evidence forms.
CRAFT_FINDING_INSTRUCTIONS = (
    "For each blocking finding use a separate block beginning `FINDING:` followed by "
    "`Evidence: command: <command> | output: <observed failure>`, "
    "`Evidence: test: <test> | output: <observed failure>`, "
    "`Evidence: missing: <file/path>`, `Evidence: clause: <quoted brief/contract clause> | deliverable: "
    "<missing deliverable>`, "
    "or `Evidence: reproduction: <input> -> <wrong output>`. "
    "Style/design opinions without failing checks are advisory.\n\n"
)

QA_FINDING_INSTRUCTIONS = (
    "## How to report findings\n\n"
    "Each blocking finding is its own block, exactly:\n\n"
    "    FINDING: <one line: what is wrong>\n"
    '    Breaks: <contract id like C7 | acceptance: "<quoted criterion>" | brief: "<quoted words>" | '
    "rule: <which blocking rule below: security, data loss, race, wrong result on valid input, failing "
    "test, regression>>\n"
    "    Evidence: <command: ... | output: ...> or <test: ... | output: ...> or "
    "<file: path:line -- what it shows> or <missing: path> or <clause: ... | deliverable: ...> or "
    "<reproduction: input -> wrong output>\n\n"
    "Evidence may continue on the next lines or in a fenced block, and must be re-runnable as written by "
    "someone else in a fresh copy.\n\n"
    "The supervisor computes the verdict from these blocks, not from the VERDICT line: the round fails if "
    "and only if at least one FINDING block has evidence, and a separate check re-runs each block's "
    "evidence in a fresh copy and drops a finding whose evidence does not reproduce. So FINDING blocks are "
    "only for blocking findings; everything else goes under `NON-BLOCKING:`. Keep the `BLOCKING: <n>` line "
    "and the final `VERDICT: PASS|FAIL` line: the VERDICT line still marks a usable reply, and it should "
    "agree with your blocks.\n\n"
    "Consistency rules. Whether a finding blocks depends only on what it breaks and whether there is "
    "evidence -- not on how likely the input is, how hard the fix is, whether the base commit or an "
    "upstream fix has the same flaw, whether existing tests cover it, or whether the author's own criteria "
    "excuse it (the brief outranks them). When the brief's own example or reporter's case still fails, it "
    "is blocking even if the cause predates this work. A defect in code this work did not change, in "
    "behaviour the brief does not ask for, is non-blocking. Nothing goes under NON-BLOCKING because it "
    '"seems minor"; only because it breaks nothing above, or because there is no evidence.\n\n'
    "Criteria walk, before looking for anything else: go through the criteria your concern covers (the "
    "contract's assertions when there is a contract, otherwise each of the author's criteria and each "
    "requirement of the brief), check each directly, and where the brief gives a concrete example (an "
    "input, a command, an error, a reporter's case) run that exact example rather than a variant. Record "
    "one line each under `CRITERIA:` (`- <id or short quote>: MET (how checked) | NOT MET (FINDING title) "
    "| UNVERIFIED`). Then look for other defects. Keep it proportionate: run the full test suite once, "
    "not repeatedly.\n\n"
)


def render_finding_instructions(judge: str) -> str:
    """Return the prompt section that tells a judge how to write findings."""
    return CRAFT_FINDING_INSTRUCTIONS if judge == "craft" else QA_FINDING_INSTRUCTIONS
