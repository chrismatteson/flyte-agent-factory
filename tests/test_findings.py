"""Finding parsing, the mechanical verdict and the judge prompt. Every fixture here is synthetic text."""

from __future__ import annotations

import pytest

from flyte_agent_factory import _findings, _review

EV = "command: app --empty | output: Traceback"


def one(text: str) -> _findings.Finding:
    found = _findings.parse_findings(text)
    assert len(found) == 1, found
    return found[0]


# --- headers ---


@pytest.mark.parametrize(
    "header",
    [
        "FINDING: crash on empty",
        "FINDING (blocking): crash on empty",
        "## FINDING (blocking): crash on empty",
        "### FINDING 1 (blocking): crash on empty",
        "**FINDING 1 (blocking):** crash on empty",
        "**FINDING:** crash on empty",
        "- FINDING: crash on empty",
        "1. **FINDING 2:** crash on empty",
        "> FINDING: crash on empty",
        "  _FINDING_: crash on empty",
    ],
)
def test_header_forms_parse(header):
    f = one(f"{header}\nBreaks: C7\nEvidence: {EV}\n")
    assert f.title == "crash on empty"
    assert f.breaks == "C7" and f.evidence == EV and not f.advisory


@pytest.mark.parametrize("line", ["FINDINGS: none", "Finding the cause took a while", "no finding here"])
def test_lines_that_are_not_headers_do_not_parse(line):
    assert _findings.parse_findings(line) == []


# --- evidence ---


@pytest.mark.parametrize(
    "evidence",
    [
        "command: python app.py | output: ValueError: empty input",
        "test: test_bad_input | output: AssertionError: expected 2 got 1",
        "missing: app/report.csv",
        'clause: "export a CSV" | deliverable: no CSV exporter',
        "reproduction: input 2 -> output 1",
        "file: app/main.py:12 -- the loop never checks for an empty list",
    ],
)
def test_the_strict_evidence_forms_count(evidence):
    assert one(f"FINDING: bug\nEvidence: {evidence}").evidence == evidence


@pytest.mark.parametrize(
    "line",
    [
        "Evidence: x",
        "**Evidence:** x",
        "- Evidence (command): x",
        "  * **Evidence (file):** x",
        "> Evidence: x",
    ],
)
def test_evidence_label_forms(line):
    assert one(f"FINDING: bug\n{line}").evidence == "x"


def test_evidence_runs_over_following_lines_up_to_the_next_label():
    f = one("FINDING: bug\nEvidence: command: app --empty\n  output: Traceback\nmore output\nBreaks: C1\n")
    assert f.evidence == "command: app --empty\n  output: Traceback\nmore output"
    assert f.breaks == "C1"


def test_a_bare_evidence_label_followed_by_a_fence_counts():
    f = one("FINDING: bug\nEvidence:\n```\n$ app --empty\nTraceback\n```\nBLOCKING: 1\n")
    assert "Traceback" in f.evidence
    assert _findings.select_blocking([f]) == [f]


def test_a_bare_evidence_label_followed_by_indented_lines_counts():
    assert one("FINDING: bug\nEvidence:\n    app --empty -> Traceback\n").evidence


@pytest.mark.parametrize("body", ["Evidence:\n", "Evidence:\n```\n```\n", "no evidence line at all\n"])
def test_empty_or_missing_evidence_is_not_evidence(body):
    f = one(f"FINDING: bug\n{body}")
    assert f.evidence == "" and _findings.select_blocking([f]) == []


def test_a_missing_breaks_line_does_not_make_a_finding_non_blocking():
    f = one(f"FINDING: bug\nEvidence: {EV}")
    assert f.breaks == "" and _findings.select_blocking([f]) == [f]


def test_a_label_inside_a_fence_is_output_not_a_label():
    f = one("FINDING: bug\nEvidence:\n```\nEvidence: nested\nBreaks: nested\n```\n")
    assert f.breaks == "" and "nested" in f.evidence


# --- block boundaries ---


def test_blocks_end_at_the_next_header():
    a, b = _findings.parse_findings(f"FINDING: one\nEvidence: {EV}\nFINDING: two\nEvidence: missing: x\n")
    assert (a.title, b.title) == ("one", "two")
    assert "two" not in a.body and "missing" not in a.evidence


@pytest.mark.parametrize(
    "section",
    [
        "NON-BLOCKING:",
        "**NON-BLOCKING:**",
        "UNVERIFIED FROM HERE:",
        "DESIGN CONFLICT: x",
        "DESIGN CONFLICTS:",
        "NEW REQUIREMENT: x",
        "NEW REQUIREMENTS:",
        "CRITERIA:",
        "BLOCKING: 1",
        "VERDICT: FAIL",
        "**VERDICT:** PASS",
        "## UNVERIFIED FROM HERE",
        "**Non-blocking findings**",
        "## NON-BLOCKING (notes): none",
        "**CRITERIA:**",
        "## CRITERIA",
        "**BLOCKING:** 2",
        "BLOCKING: `0`",
        "UNVERIFIED FROM HERE (every item below needs the head commit checked out; none needs a cluster):",
    ],
)
def test_blocks_end_at_a_section_line(section):
    f = one(f"FINDING: bug\nEvidence: {EV}\n{section}\n- tail text\n")
    assert f.evidence == EV and "tail text" not in f.body


def test_a_heading_that_is_a_finding_header_starts_a_finding_instead():
    a, b = _findings.parse_findings(
        f"FINDING: one\nEvidence: {EV}\n## FINDING 2 (blocking): two\nEvidence: {EV}"
    )
    assert (a.title, b.title) == ("one", "two")


def test_a_fence_does_not_end_a_block_and_hides_headers_and_sections():
    f = one(
        f"FINDING: bug\nEvidence: {EV}\n```\n## heading in output\nVERDICT: PASS\n"
        "FINDING: quoted\n```\nafter\n"
    )
    assert "after" in f.body and "quoted" in f.body


# --- advisory ---


@pytest.mark.parametrize(
    "header",
    [
        "FINDING (minor): x",
        "FINDING (nit): x",
        "FINDING (advisory): x",
        "FINDING (non-blocking): x",
        "**FINDING (Minor):** x",
    ],
)
def test_advisory_header_labels(header):
    assert one(f"{header}\nEvidence: {EV}").advisory


@pytest.mark.parametrize("label", ["minor:", "nit:", "advisory:", "non-blocking:", "Nonblocking:"])
def test_advisory_title_labels(label):
    f = one(f"FINDING: {label} wording\nEvidence: {EV}")
    assert f.advisory and _findings.select_blocking([f]) == []


@pytest.mark.parametrize("title", ["Advisory lock leaks", "minor bug", "nit picker fails", "unit tests fail"])
def test_a_label_needs_its_colon_in_the_title(title):
    assert not one(f"FINDING: {title}\nEvidence: {EV}").advisory


def test_a_blocking_label_is_not_advisory():
    assert not one(f"FINDING (blocking): x\nEvidence: {EV}").advisory


def test_findings_under_a_non_blocking_section_are_advisory_until_the_next_section():
    text = (
        f"FINDING: real\nEvidence: {EV}\n"
        f"NON-BLOCKING:\n- FINDING: small\n  Evidence: {EV}\n"
        f"BLOCKING: 1\nFINDING: late\nEvidence: {EV}\nVERDICT: FAIL"
    )
    real, small, late = _findings.parse_findings(text)
    assert (real.advisory, small.advisory, late.advisory) == (False, True, False)


def test_a_top_level_finding_after_a_non_blocking_section_still_blocks():
    # The section order is not prescribed: a NON-BLOCKING list first must not demote the real finding,
    # or a failed check would turn an evidenced FAIL into a PASS.
    text = (
        f"NON-BLOCKING:\n- nit: naming\n\n"
        f"FINDING: crash\nBreaks: C1\nEvidence: {EV}\n\nBLOCKING: 1\nVERDICT: FAIL"
    )
    assert [f.title for f in _findings.select_blocking(_findings.parse_findings(text))] == ["crash"]
    assert _findings.decide(text, None).verdict == "FAIL"


def test_a_listed_finding_inside_a_non_blocking_section_stays_advisory():
    text = "NON-BLOCKING:\n- FINDING: style\n  Evidence: file: a.py:3 -- naming\nVERDICT: PASS"
    assert one(text).advisory


@pytest.mark.parametrize(
    "line",
    ["- non-blocking aside: slow", "Non-blocking? No, it breaks C2.", "Non-blocking, since nothing breaks."],
)
def test_a_sentence_or_bullet_starting_non_blocking_is_not_a_section(line):
    text = f"FINDING: real\nBreaks: C2\n{line}\nEvidence: {EV}\nFINDING: second\nEvidence: {EV}"
    assert [(f.title, f.advisory, bool(f.evidence)) for f in _findings.parse_findings(text)] == [
        ("real", False, True),
        ("second", False, True),
    ]


PROSE_LINES = [
    "New requirements aside",
    "Unverified from here is whether the fix holds.",
    "Design conflicts with the brief's rule that A.",
    "Criteria: C3 says Z.",
    "BLOCKING: 1 of these two paths crash.",
    "Non-blocking in isolation but they break C3",
    "**Non-blocking** in isolation but they break C3",
]


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
@pytest.mark.parametrize("line", PROSE_LINES)
def test_prose_starting_with_a_section_word_stays_inside_the_finding(line, verdict):
    text = f"FINDING: crash\nBreaks: C1\n{line}\nEvidence: {EV}\nBLOCKING: 1\nVERDICT: {verdict}"
    (finding,) = _findings.parse_findings(text)
    assert line in finding.body and finding.evidence == EV and not finding.advisory
    assert _findings.select_blocking([finding]) == [finding]
    assert _findings.decide(text, None).verdict == "FAIL"
    assert _findings.decide(text, "CLAIM 1: x\nCHECK 1: CANNOT CHECK -- y").verdict == "FAIL"


@pytest.mark.parametrize("line", PROSE_LINES)
def test_prose_starting_with_a_section_word_does_not_open_a_non_blocking_section(line):
    text = f"FINDING: real\nBreaks: C2\n{line}\nEvidence: {EV}\nFINDING: second\nEvidence: {EV}"
    assert [(f.title, f.advisory, f.evidence == EV) for f in _findings.parse_findings(text)] == [
        ("real", False, True),
        ("second", False, True),
    ]


def test_a_negated_advisory_label_is_not_advisory():
    assert not one(f"FINDING (blocking, not minor): x\nEvidence: {EV}").advisory


@pytest.mark.parametrize("value", ["none", "N/A", "n/a.", "-", "**none**"])
def test_evidence_that_says_there_is_none_is_not_evidence(value):
    assert not one(f"FINDING: x\nEvidence: {value}").evidence


@pytest.mark.parametrize("line", ["Finding the cause was hard: it took long", "Findings: none"])
def test_prose_starting_with_the_word_is_not_a_header(line):
    assert _findings.parse_findings(f"{line}\nVERDICT: PASS") == []


def test_a_numbered_hash_header_parses():
    assert one(f"### FINDING #2: crash\nEvidence: {EV}").title == "crash"


# --- the verdict ---

FAIL = f"FINDING: crash\nBreaks: C1\nEvidence: {EV}\nBLOCKING: 1\nVERDICT: FAIL"
TWO = f"FINDING: one\nEvidence: {EV}\nFINDING: two\nEvidence: {EV}\nBLOCKING: 2\nVERDICT: FAIL"


def test_a_pass_reply_passes_without_a_check():
    assert not _findings.needs_check("looks right\nBLOCKING: 0\nVERDICT: PASS")
    d = _findings.decide("looks right\nVERDICT: PASS", None)
    assert d.verdict == "PASS" and d.kept == [] and d.dropped == []


def test_an_evidenced_fail_needs_the_check_and_fails_closed_without_one():
    assert _findings.needs_check(FAIL)
    d = _findings.decide(FAIL, None)
    assert d.verdict == "FAIL" and len(d.kept) == 1 and d.dropped == []


def test_all_reproduced_fails_with_every_finding_kept():
    d = _findings.decide(TWO, "CHECK 1: REPRODUCED -- saw it\nCHECK 2: REPRODUCED -- saw it")
    assert d.verdict == "FAIL" and len(d.kept) == 2 and d.dropped == []


def test_one_not_reproduced_is_dropped_and_the_rest_kept():
    d = _findings.decide(TWO, "CHECK 1: NOT REPRODUCED -- fine\nCHECK 2: REPRODUCED -- saw it")
    assert d.verdict == "FAIL"
    assert [k.splitlines()[0] for k in d.kept] == ["FINDING: two"]
    assert [k.splitlines()[0] for k in d.dropped] == ["FINDING: one"]


def test_all_not_reproduced_passes():
    d = _findings.decide(TWO, "CHECK 1: NOT REPRODUCED -- a\nCHECK 2: NOT REPRODUCED -- b")
    assert d.verdict == "PASS" and d.kept == [] and len(d.dropped) == 2
    assert d.reason == "every finding failed to reproduce"


@pytest.mark.parametrize("marker", ["<redacted>", "<redacted, 70 chars>"])
def test_a_finding_whose_evidence_was_redacted_is_kept_as_cannot_check(marker):
    text = (
        f"FINDING: one\nEvidence: reproduction: tail -2 ~/.ssh/id_ed25519\n{marker} -> returned unchanged\n"
        f"FINDING: two\nEvidence: {EV}\nVERDICT: FAIL"
    )
    d = _findings.decide(text, "CHECK 1: NOT REPRODUCED -- a\nCHECK 2: NOT REPRODUCED -- b")
    assert d.verdict == "FAIL"
    assert [k.splitlines()[0] for k in d.kept] == ["FINDING: one"]
    # the guard is per finding: the sibling with nothing masked still drops
    assert [k.splitlines()[0] for k in d.dropped] == ["FINDING: two"]


def test_the_redaction_marker_is_what_the_bridge_writes():
    from flyte_agent_factory import _bridge

    assert _findings.REDACTION_MARKER.search(_bridge.redact("x SECRETVALUE y", "SECRETVALUE"))
    token = "AKIA" + "Q" * 16  # assembled here so no credential shape sits in the file (C31)
    assert _findings.REDACTION_MARKER.search(_bridge.redact_tokens(f"export K={token}"))


def test_cannot_check_and_unanswered_findings_are_kept():
    d = _findings.decide(TWO, "CHECK 1: CANNOT CHECK -- no network")
    assert d.verdict == "FAIL" and len(d.kept) == 2 and d.dropped == []


def test_a_check_reply_with_no_check_lines_keeps_everything():
    assert _findings.decide(FAIL, "I looked into it.").verdict == "FAIL"


NO_EVIDENCE_FAIL = "The error handling is poor.\nFINDING: vague\nBLOCKING: 1\nVERDICT: FAIL"


def test_a_fail_without_evidence_needs_the_check_and_passes_without_a_reproduced_claim():
    assert _findings.needs_check(NO_EVIDENCE_FAIL)
    for check in (
        None,
        "",
        "CLAIM 1: poor handling\nCHECK 1: NOT REPRODUCED -- fine",
        "CLAIM 1: x\nCHECK 1: CANNOT CHECK -- y",
    ):
        assert _findings.decide(NO_EVIDENCE_FAIL, check).verdict == "PASS", check


def test_a_fail_without_evidence_fails_when_a_claim_reproduces():
    check = (
        "CLAIM 1: poor handling\nCLAIM 2: other\n"
        "CHECK 1: NOT REPRODUCED -- fine\nCHECK 2: REPRODUCED -- crashed"
    )
    d = _findings.decide(NO_EVIDENCE_FAIL, check)
    assert d.verdict == "FAIL" and d.kept == ["other -- crashed"]


def test_a_check_line_without_its_claim_does_not_fail_the_round():
    assert _findings.decide(NO_EVIDENCE_FAIL, "CHECK 1: REPRODUCED -- x").verdict == "PASS"


def test_an_evidenced_finding_under_verdict_pass_is_a_fail():
    text = f"FINDING: crash\nEvidence: {EV}\nBLOCKING: 0\nVERDICT: PASS"
    assert _findings.needs_check(text)
    assert _findings.decide(text, None).verdict == "FAIL"
    assert _findings.decide(text, "CHECK 1: NOT REPRODUCED -- ok").verdict == "PASS"


def test_an_advisory_or_unevidenced_finding_never_blocks():
    text = f"FINDING: minor: typo\nEvidence: {EV}\nFINDING: rename\nVERDICT: FAIL"
    # Still a FAIL with no evidenced blocking finding: the no-evidence rule applies.
    assert _findings.decide(text, None).verdict == "PASS"


# --- parse_check ---


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("CHECK 1: REPRODUCED -- saw it", ("REPRODUCED", "saw it")),
        ("**CHECK 1:** **NOT REPRODUCED** -- fine", ("NOT_REPRODUCED", "fine")),
        ("- check 1: not reproduced - fine", ("NOT_REPRODUCED", "fine")),
        ("CHECK 1 - CANNOT CHECK: no network", ("CANNOT_CHECK", "no network")),
        ("CHECK 1: NOT_REPRODUCED", ("NOT_REPRODUCED", "")),
        ("> **CHECK 1**: Cannot Check — no cluster", ("CANNOT_CHECK", "no cluster")),
        ("CHECK 1. REPRODUCED", ("REPRODUCED", "")),
    ],
)
def test_parse_check_tolerates_markup(line, expected):
    assert _findings.parse_check(f"intro\n{line}\n") == {1: expected}


def test_parse_check_numbers_and_ignores_prose():
    text = (
        "CHECK 2: REPRODUCED -- a\nI will CHECK 3: later\n"
        "CHECK 1: CANNOT CHECK -- b\nCHECK 2: NOT REPRODUCED -- c"
    )
    assert _findings.parse_check(text) == {2: ("NOT_REPRODUCED", "c"), 1: ("CANNOT_CHECK", "b")}


def test_parse_claims():
    assert _findings.parse_claims("CLAIM 1: a **bold** problem\n- **CLAIM 2:** other\nclaims: none") == {
        1: "a **bold** problem",
        2: "other",
    }


def test_check_prompt_lists_findings_or_asks_for_claims():
    f = _findings.select_blocking(_findings.parse_findings(FAIL))
    with_findings = _findings.build_check_prompt("correctness", "the brief", "", f)
    assert "### Finding 1" in with_findings and "CHECK <n>: NOT REPRODUCED" in with_findings
    claims = _findings.build_check_prompt("completeness", "the brief", "criteria", [], reply=NO_EVIDENCE_FAIL)
    assert "CLAIM <n>" in claims and "The error handling is poor." in claims and "criteria" in claims


def test_the_review_wrapper_and_verdict_regex_are_the_same_as_before():
    assert _review._VERDICT is _findings.VERDICT
    blocking, other = _review._evidenced_findings(f"FINDING: a\nEvidence: {EV}\nFINDING: b\nVERDICT: FAIL")
    assert len(blocking) == 1 and blocking[0].startswith("FINDING: a") and len(other) == 1


# --- the prompt ---

RUBRIC_SENTENCES = [
    "Sort every finding into one of two kinds",
    "a requirement of the brief not met; a test that runs and fails",
    "a wrong result, crash or hang on valid",
    "data lost or corrupted when the process is",
    "a security flaw (injection, path traversal, auth bypass",
    "a race or ordering bug, even an intermittent one",
    "Rarity does not make a defect non-blocking.",
    "environment faults outside",
    "(1) the brief asks for robustness to it",
    "Hostile or malformed *inputs*",
    "**Without concrete evidence, it is advisory.**",
    "Do not demote a real",
]


def prompt(judge: str) -> str:
    return _review._qa_prompt(judge, "do the thing", "the brief", "", 1, contract="C1: it works")


@pytest.mark.parametrize("judge", ["correctness", "completeness"])
def test_qa_prompts_carry_the_format_the_rules_and_the_walk(judge):
    text = prompt(judge)
    for needle in (
        "FINDING: <one line: what is wrong>",
        "Breaks: <contract id like C7",
        "Evidence: <command: ... | output: ...>",
        "file: path:line -- what it shows",
        "not from the VERDICT line",
        "drops a finding whose evidence does not reproduce",
        "everything else goes under `NON-BLOCKING:`",
        "Consistency rules",
        "whether the base commit or an upstream fix has the same flaw",
        "Criteria walk",
        "run that exact example rather than a variant",
        "`CRITERIA:`",
        "`BLOCKING: <n>`",
        "`VERDICT: PASS|FAIL`",
        "VERDICT: PASS\n    VERDICT: FAIL",
        *RUBRIC_SENTENCES,
    ):
        assert needle in text, needle


def test_the_craft_prompt_keeps_its_old_instruction_and_rubric():
    text = prompt("craft")
    assert text.count("For each blocking finding use a separate block beginning `FINDING:` followed by") == 1
    assert "Criteria walk" not in text and "Consistency rules" not in text
    for needle in RUBRIC_SENTENCES:
        assert needle in text, needle


def test_prose_evidence_is_not_evidence():
    assert (
        _findings.decide("FINDING: crash\n- Evidence: it looked wrong\nVERDICT: PASS", None).verdict == "PASS"
    )
    opinion = (
        "FINDING: the design is overcomplicated\n"
        "Evidence: I think a simpler approach would be better\nVERDICT: PASS"
    )
    decision = _findings.decide(opinion, "CHECK 1: CANNOT CHECK -- nothing to run")
    assert decision.verdict == "PASS"


@pytest.mark.parametrize(
    "evidence",
    [
        "file: src/a.py:12 -- the guard is missing",
        "see src/a.py:12",
        "app --empty | output: Traceback",
        "```\n$ app --empty\nTraceback\n```",
        "Missing: tests/test_a.py",
    ],
)
def test_runnable_evidence_forms_block(evidence):
    assert _findings.select_blocking([one(f"FINDING: bug\nEvidence: {evidence}")])


def test_a_bare_fail_with_no_check_says_that_no_check_ran():
    decision = _findings.decide("x\nVERDICT: FAIL", None)
    assert decision.verdict == "PASS" and "the check did not run" in decision.reason


def test_a_header_less_evidenced_fail_blocks_as_the_old_rule_did():
    text = "**1. Crash on empty input**\nEvidence: command: app --empty | output: Traceback\nVERDICT: FAIL"
    assert _findings.decide(text, None).verdict == "FAIL"
    assert _findings.needs_check(text)
    (finding,) = _findings.parse_findings(text)
    assert finding.title == "1. Crash on empty input" and finding.body == text and not finding.advisory
    assert _findings.parse_findings("Looks broken.\nVERDICT: FAIL") == []
    # An evidenced reply that passes has no finding to speak of.
    assert _findings.parse_findings(text.replace("FAIL", "PASS")) == []
    # A header-less FAIL whose only evidence is prose is still the claims path.
    assert _findings.parse_findings("Broken.\nEvidence: it looked wrong\nVERDICT: FAIL") == []


def test_a_markdown_heading_inside_a_finding_keeps_its_evidence():
    text = f"FINDING: crash\n\n### Detail\nEvidence: {EV}\nVERDICT: FAIL"
    (finding,) = _findings.select_blocking(_findings.parse_findings(text))
    assert finding.title == "crash"
    text = f"FINDING: x\nEvidence: {EV}\n## NON-BLOCKING\n- note\nVERDICT: PASS"
    found = _findings.parse_findings(text)
    (blocking,) = _findings.select_blocking(found)
    assert "note" not in blocking.body


@pytest.mark.parametrize(
    ("evidence", "blocks"),
    [
        ("missing:", False),
        ("command: pytest", False),
        ("command: pytest\n```\nFAILED\n```", True),
        ("ratio was 1.5:2 which looked wrong", False),
        ("file: src/a.py:12 -- returns None", True),
    ],
)
def test_evidence_needs_content_not_just_a_label(evidence, blocks):
    found = _findings.parse_findings(f"FINDING: bug\nEvidence: {evidence}")
    assert bool(_findings.select_blocking(found)) is blocks


@pytest.mark.parametrize(
    ("evidence", "kind"),
    [
        ("command: app | output: x", "command"),
        ("test: t | output: x", "test"),
        ("file: src/a.py:1 -- x", "file"),
        ("missing: a.py", "missing"),
        ("clause: x | deliverable: y", "clause"),
        ("reproduction: a -> b", "reproduction"),
        ("command (run from x): app | output: y", "command"),
        ("command, from x: app | output: y", "command"),
        ("reproduction (from x): a -> b", "reproduction"),
        ("```\n$ app\n```", "fence"),
        ("see src/a.py:3", "path"),
        ("it looked wrong", "none"),
    ],
)
def test_evidence_kind_is_always_one_of_the_fixed_labels(evidence, kind):
    finding = _findings.Finding("t", "", "", evidence, False)
    assert _findings.classify_evidence_kind(finding) == kind


@pytest.mark.parametrize("heading", ["## Summary", "#### Other"])
def test_a_heading_of_any_level_does_not_end_a_finding(heading):
    text = f"FINDING: crash\n{heading}\nEvidence: command: a | output: b\nVERDICT: FAIL"
    (finding,) = _findings.parse_findings(text)
    assert heading in finding.body and not finding.advisory
    assert _findings.decide(text, None).verdict == "FAIL"


@pytest.mark.parametrize(
    "evidence",
    [
        "- **reproduction:** run `app serve` and send `HEAD /`; it answers Content-Length: 0",
        "- **test:** ran `pytest tests/test_a.py`\n  - Output: `AssertionError: True is not False`",
        "* _file_: `src/a.py` -- returns None",
        "**missing:** `docs/usage.md`",
    ],
)
def test_a_label_written_as_markdown_still_counts(evidence):
    text = f"FINDING: crash\nEvidence:\n{evidence}\nVERDICT: FAIL"
    assert _findings.decide(text, None).verdict == "FAIL"


@pytest.mark.parametrize(
    "evidence",
    ["- **missing:**", "- **test:** ran `pytest`", "**reproduction:** **", "- Output: ``"],
)
def test_a_markdown_label_still_needs_its_content(evidence):
    text = f"FINDING: crash\nEvidence:\n{evidence}\nVERDICT: FAIL"
    assert _findings.parse_findings(text)[0].advisory is False
    assert _findings.select_blocking(_findings.parse_findings(text)) == []


def test_evidence_kind_reads_a_markdown_label():
    text = "FINDING: crash\nEvidence: - **test:** ran `pytest` | output: `1 failed`\nVERDICT: FAIL"
    assert _findings.classify_evidence_kind(_findings.parse_findings(text)[0]) == "test"


def test_decide_carries_the_kept_finding_objects_and_claims():
    d = _findings.decide(TWO, "CHECK 1: REPRODUCED -- saw it\nCHECK 2: NOT_REPRODUCED -- no")
    assert d.verdict == "FAIL" and [f.title for f in d.kept_findings] == ["one"]
    assert [f.render() for f in d.kept_findings] == d.kept and d.kept_claims == []

    bare = "Handling is poor.\nBLOCKING: 1\nVERDICT: FAIL"
    d = _findings.decide(bare, "CLAIM 1: poor handling\nCHECK 1: REPRODUCED -- crashed")
    assert d.verdict == "FAIL" and d.kept == ["poor handling -- crashed"]
    assert d.kept_claims == d.kept and d.kept_findings == []

    d = _findings.decide("fine\nBLOCKING: 0\nVERDICT: PASS", None)
    assert d.kept_findings == [] and d.kept_claims == []


@pytest.mark.parametrize(
    ("header", "advisory"),
    [
        ("FINDING (blocking, not a nit): x", False),
        ("FINDING (major; non-minor): x", False),
        ("FINDING (minor): x", True),
        ("FINDING (non-blocking): x", True),
        ("FINDING (nit): x", True),
    ],
)
def test_a_qualifier_that_says_blocking_is_never_advisory(header, advisory):
    (finding,) = _findings.parse_findings(f"{header}\nEvidence: file: src/a.py:3 -- x\nVERDICT: FAIL")
    assert finding.advisory is advisory
