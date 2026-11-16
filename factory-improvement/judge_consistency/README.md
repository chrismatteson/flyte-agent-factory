# Judge consistency: the changed judge, measured on the study's decisions

The engine's review judge now writes findings under a stricter format, a cheaper second call (the finding
check) re-runs each finding's evidence in a fresh copy, and the verdict comes from `_findings.decide` rather
than the `VERDICT:` line. This harness re-runs the **same 45 decisions** the self-agreement study
(`projects/judge-self-agreement/`) re-judged twice, with the same stored prompts (only the finding-instruction
block is swapped) and the same workspaces, then compares the old and new self-agreement.

Nothing here changes `judge-self-agreement/`; it is only read and imported.

## Commands, in order

Run from `projects/judge-consistency/`.

1. `python factory-improvement/judge_consistency/run.py estimate` -- offline. Writes
   `results/cost_estimate.json` (aggregate, committed).
2. `python factory-improvement/judge_consistency/run.py --dry-run --limit 1` -- prepares one workspace and
   prints the calls it would make. Needs the `factory/*` refs of the reviewed commits in the checkout.
3. `python factory-improvement/judge_consistency/run.py --cap 150` -- the paid run, inside an attached
   Flyte session. Resumable: a rep whose file exists is skipped and its cost counted. `--limit K`,
   `--only ID...`, `--parallel N` (default 4), `--out DIR`, `--study DIR` narrow or move it.
4. `python factory-improvement/judge_consistency/run.py charge-interrupted --calls N` -- offline. Charges
   `N` calls that were cut short at the worst measured round and records the basis in
   `results/spend_ledger.json`; the committed total is only ever raised.
5. `python factory-improvement/judge_consistency/analyze_after.py` -- offline. Writes `results/summary.json`
   (it copies the ledger in) and the private `moves_private.json`. If the private reps are absent, it
   writes nothing and exits non-zero.

## Where things go

- **Private** (raw judge and check replies, per-rep records, spend, the label-to-decision table):
  `.claude-flyte/judge-consistency/rejudge-after/` at the workspace root, which is git-ignored. They are
  decision data and must never be written under `projects/judge-consistency/`.
- **`results/` holds aggregates only**: numbers, opaque labels (`sha256(decision id)[:10]`) and mechanism
  categories. No decision ids, run names, reply text or paths from replies; a test fails if one appears.

## Modules

- `study.py` loads the study's modules and the changed engine's `_findings.py` (standalone, never as the
  `flyte_agent_factory` package) and reads the finding check's model and effort from `_policy.py`.
- `prompts.py` builds the after-prompt and the finding check's brief and acceptance text.
- `run.py` is the paid run; `analyze_after.py` the comparison.
