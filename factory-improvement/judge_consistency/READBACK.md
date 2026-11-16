# Readback: did the changed judge agree with itself more often?

**No. The 90% target was NOT met.** On the 43 of the study's 45 decisions that fit under the $150 cap, the
changed judge agreed with itself **77%** of the time (95% Wilson interval 62–87%), against **81%** (67–90%) for
the old judge on the same 43 decisions. The paired difference is −5 points, with a by-PR bootstrap interval of
−17 to +8. In the high-confidence subset agreement fell from 90% to 76%, with an interval (−26 to −3) that
excludes zero.

Requirements, as the brief states them:

1. Self-agreement of at least 90% on the study's decisions: **not met** (77%, interval 62–87%). The owner
   ruled on 2026-11-16 to accept this as a negative result (option a, below).
2. Cost per round at most 1.15× before: **met** (0.96×).
3. PASS rate before and after, with every FAIL→PASS move explained: **met** (table below, both moves explained).
4. No check waived or weakened, and what blocks for the acceptance criteria unchanged: **met**.
5. Spend within the $150 cap, reporting the completed subset if the cap binds: **met** ($136.05 of $150; the
   cap bound, and the last two decisions in run order are omitted).

**Owner ruling (2026-11-16, 11:21Z): option (a).** The owner accepts the negative result on requirement 1,
and the engine change lands on the rest of the evidence (requirements 2 to 5). The operator recommended "option
(a) for the current PR, plus a follow-up issue on #10", and the owner answered, verbatim: "Sure. Do it and send
the follow up." This answers `ask-human` 1794802810530-7b6a8577. Requirement 1 and its acceptance criterion
(C12) stay reported as **NOT MET** above, with no number changed. They no longer block this PR. No other
requirement is relaxed and no check is waived. Work on reaching consistent verdicts through more structure
moves to a follow-up issue that the operator is filing against unionai-oss/flyte-agent-factory. That work was
not started here, and nothing more was spent.

For the record, this was the question put to the owner. The target was unmet and the cap was spent ($13.95
remains under it, `spend.remaining_under_cap_usd` in `results/summary.json`, which cannot complete a decision
pair). The options:

- (a) Accept the negative result on requirement 1 and rule on whether the engine change lands. **Chosen.**
- (b) Fund about the measured spend ($115.55, plus reservation margin) for a second intervention, measured on
  the same 43 decisions. The odds of reaching 90% are rated low, because PASS reps already walk every
  criterion.
- (c) Amend the target or the metric (for example kappa, or "not below before"), shown beside the raw table
  below.
- (d) A small third data point: apply the new rule plus the finding check to the study's existing replies.

The other requirements were met without any check being waived or weakened.

Most of the remaining disagreement is about recall. In **9 of the 10 splits**, one rep wrote a blocking
finding with evidence that the finding check re-ran and kept, and the other rep wrote no finding. The finding
check caused the tenth split, `40d350e037`. Rep 1's finding quoted a line that the engine had redacted
(`<redacted>`), so the checker could not see what the judge saw. It answered `NOT REPRODUCED`, the finding was
dropped, and rep 1 passed while rep 2 failed on the same defect. That drop was wrong. The engine now keeps such
a finding as CANNOT CHECK. Re-deciding the same stored replies under the rule that lands gives **79%**
agreement (**65–89%**) with **9** splits, and that **1** rep is the only one whose verdict changes
(`after_under_current_rule` in `results/summary.json`). The measured 77% stands as the result. The 79% is still
below both the 81% before and the 90% target. Apart from that one case, what is left is **which real defects one pass of the judge happens to find**, not how a verdict is
read from the reply. A rule for reading verdicts cannot fix that without throwing away findings that were
checked and kept, and requirement 4 forbids that.

## Self-agreement, before and after (same decisions, two reps each)

Before = the study's two old-judge reps, with the verdicts the study recorded. After = two reps of the
changed judge, with verdicts from `_findings.decide`. Intervals are 95%.

| Subset | Decisions | Before | Wilson | After | Wilson | After − before (by-PR bootstrap) |
|---|---|---|---|---|---|---|
| All completed | 43 | 81% | 67–90% | 77% | 62–87% | −5 pts (−17 to +8) |
| High confidence | 29 | 90% | 74–96% | 76% | 58–88% | −14 pts (−26 to −3) |
| Medium or high | 41 | 83% | 69–91% | 76% | 61–86% | −7 pts (−19 to +5) |
| First rounds | 20 | 80% | 58–92% | 75% | 53–89% | −5 pts (−28 to +16) |
| Later rounds | 23 | 83% | 63–93% | 78% | 58–90% | −4 pts (−25 to +19) |
| Correctness | 23 | 83% | 63–93% | 70% | 49–84% | −13 pts (−32 to +5) |
| Completeness | 20 | 80% | 58–92% | 85% | 64–95% | +5 pts (−11 to +23) |
| SWE-bench | 28 | 86% | 69–94% | 82% | 64–92% | −4 pts (−17 to +8) |
| Other | 15 | 73% | 48–89% | 67% | 42–85% | −7 pts (−29 to +27) |

By-PR cluster bootstrap for all 43 (24 PRs): before 66–94%, after 65–88%. Cohen's kappa: before 0.39, after
0.49. Kappa is higher after because the after verdicts are less one-sided, so chance agreement is lower.
Splits: 8 before, 10 after.

The study's 80% was on all 45 decisions. On the 43 measured here the old judge scores 81%, and that is the
"before" used above, so both sides cover exactly the same decisions.

## Cost per round

| | Per round | Notes |
|---|---|---|
| Before (one old-judge pass) | $1.405 | the study's recorded cost on these 43 decisions |
| After (judge + finding check) | $1.344 | judge $1.313 + check $0.030 averaged over all rounds |
| Ratio | **0.96×** | limit 1.15× |

The finding check ran in 36% of rounds, only when the judge wrote a blocking finding, and cost $0.084 each
time it ran (Sonnet, low effort). The judge pass itself cost a little less than the old one. This was not
investigated; it may just be run-to-run variation.

## PASS rate

| | Rep 1 | Rep 2 | Both reps |
|---|---|---|---|
| Before | 77% | 86% | 81% |
| After | 65% | 65% | 65% |

Over 86 rounds: 54 stayed PASS, 14 stayed FAIL, **16 moved PASS→FAIL**, **2 moved FAIL→PASS**.

### Every FAIL→PASS move, explained

Decisions are named by opaque label (`sha256(decision id)[:10]`); the table that maps labels to decisions is
private.

1. **`2d6aa60b66`, rep 2.** The new rep wrote no finding and `VERDICT: PASS`
   (`judge_raw_pass_no_findings`). The old judge's rep 2 FAIL carried evidence (`evidenced_fail`), so the old
   rule did not downgrade it. The old judge's other rep passed, and both new reps passed, so the new judge
   agrees with itself here where the old one split. The check dropped nothing.
2. **`3526d72c24`, rep 2.** The new rep wrote no finding and `VERDICT: PASS` (`judge_raw_pass_no_findings`).
   The same decision's rep 1 wrote one blocking finding, which the check reproduced and kept. The old judge's
   rep 2 FAIL carried evidence (`evidenced_fail`), so the old rule did not downgrade it. The check dropped
   nothing. This is the recall variance described above, not a check being relaxed.

Neither move came from the finding check dropping a finding. The check dropped a finding in 1 of the 86
rounds (`check_drops` in `results/summary.json`), and that round is not among the moves listed in
`moves`. It is `40d350e037` rep 1. Its old-judge rep was also PASS, so the drop hid a PASS→FAIL move rather than
creating a FAIL→PASS one. It caused one of the 10 splits (see above).

### The PASS→FAIL moves

All 16 came from a blocking finding that the check re-ran and kept. In 12 of them the old judge's raw verdict
was PASS. In the other 4 the old judge wrote FAIL, but the old rule turned it into PASS because no finding
carried evidence. The old judge's wrote-nothing passes are the same recall variance seen from the other side.

## Spend

| | |
|---|---|
| Up-front estimate, all 45 decisions | $176 (expected 43 within the cap, in run order) |
| Measured spend (rep records) | $115.55 |
| Charged for four interrupted calls | $20.50 (`spend.interrupted_charge_usd`) |
| **Counted against the cap** | **$136.05 of $150** (the ledger's earlier recorded figure stands: four calls at $5.1247 is $20.4988, below the $20.4991 already recorded, `spend.unrecorded_usd`) |
| Decisions completed | 43 of 45; the cap stopped the last two |

The first run process hit a two-hour time limit with four calls in flight. Those calls were aborted, and their
cost is unknown, so each was charged at the most expensive round seen ($5.12; `run.py charge-interrupted --calls 4` writes
this into `results/spend_ledger.json`). That is the worst observed round, not a proven upper bound. It cannot
be settled afterwards: the study's `harvest_orphans.py` reads usage from a call's reply, and aborted calls left
none. The cap then refused to start a
decision whose reserved estimate would cross $150. All prices are API prices as reported by the calls.

## What would be needed to reach 90%

The remaining disagreement is recall: one pass finds a real defect, the next does not. A single judge pass at
roughly the same cost cannot fix that by reading its reply differently. Options that could, each needing a new
paid measurement (about the measured spend, $115.55, plus reservation margin):

- **A cheap second, targeted pass** on rounds the judge passes. For example, a Sonnet call that only runs the
  reporter's exact case and the upstream regression test. The check alone costs $0.08, so there is room under
  the 1.15× limit.
- **Making the judge always run named probes first**: the reporter's case, upstream tests, and each
  criterion's stated command. This turns "did this pass think to look?" into a fixed step.

Neither was tried, because the cap leaves $13.95 (`spend.remaining_under_cap_usd`).

## What was changed in the engine

- The review judge's verdict is now computed from its findings (`_findings.decide`), not read from its
  `VERDICT:` line. A blocking finding must state what breaks and how to see it, and a no-evidence FAIL is no
  longer quietly turned into a PASS: it goes to the check, which restates and re-runs its claims. Evidence has
  to be in a form someone else can re-run (a command and output, a test, a missing path, a clause, a
  reproduction, a `path:line` or a fenced block); prose is "no evidence", as before.
- A cheaper **finding check** (Sonnet, low effort) re-runs the evidence of each blocking finding in a fresh
  copy and drops findings it cannot reproduce. A finding that carries the engine's redaction marker
  (`<redacted>`) is kept as CANNOT CHECK, because the checker saw that line masked. If the check itself fails,
  every finding is kept.
- Tests: `tests/test_findings.py`, `tests/test_finding_check.py`, plus the harness tests. The full
  `checks.sh` passes.

## Caveats

- Two reps per decision is the study's design, and it gives wide intervals. A 43-decision sample cannot
  separate 77% from 81%.
- "Before" uses the verdicts the study recorded, which include the old no-evidence rule. Applying the new
  verdict rule to the old replies (offline, at no cost) gives 87% (95% Wilson interval 74–94%) on all 45 decisions
  (`offline_new_rule_on_study_replies` in `results/summary.json`). That is the old judge's raw agreement
  under the new rule, and it is still below 90%.
- The committed after-figures are the verdicts recorded when the run happened. One check answer in that run
  was wrong: the redacted finding in `40d350e037` rep 1, described above. Under the rule that lands, the same
  stored replies give 79% (65–89%) with 9 splits, and only that rep changes (`after_under_current_rule`).
- The two decisions the cap left out are the last two in the study's run order (hash order). They were not
  chosen by result.

## How to reproduce

From `projects/judge-consistency/`, inside an attached Flyte session for the paid step:

```sh
python factory-improvement/judge_consistency/run.py estimate         # offline: cost_estimate.json
python factory-improvement/judge_consistency/run.py --parallel 4     # paid, resumable, $150 cap
python factory-improvement/judge_consistency/run.py charge-interrupted --calls 4   # offline: spend_ledger.json
python factory-improvement/judge_consistency/analyze_after.py        # offline: results/summary.json
```

Every number above comes from `results/summary.json`, `results/cost_estimate.json` and
`results/spend_ledger.json`.
