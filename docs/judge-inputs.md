# What each review judge was shown

A judge's verdict can only be explained against what it read. Each review round records that, per judge, in
the round's row: the commit and diff it was shown, a hash of its prompt, and the engine, model and effort that
answered. The diff text is not stored, only its hash, plus the command to recreate it.

The row is the dict appended to `timeline.verdicts`. It already reaches the run snapshot (`review.verdicts`),
the metrics (`metrics.rounds`), the local metrics file and the pass record, so the new `judges` key goes
wherever those go. A row from before this existed has no `judges` key and loads as before.

## The entry

`judges` maps each judge (`correctness`, `completeness`, `craft`) to one entry:

```json
{
  "head": "9f2c1e0a5b7d4c3e8a6f1b2d0c9e8f7a6b5c4d3e",
  "base": "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d",
  "diff_spec": "git-diff/1",
  "diff_argv": ["git", "-c", "core.fsmonitor=false", "...", "diff", "--binary", "--full-index", "--no-color",
                "--no-renames", "--no-ext-diff", "--no-textconv", "{base}", "{head}", "--"],
  "diff_sha256": "5d41402abc4b2a76b9719d911017c592...",
  "round_base": "4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5",
  "round_diff_sha256": "7d793037a0760186574b0282f2f435e7...",
  "worktree_clean": true,
  "engine": "claude",
  "model": "opus",
  "effort": "high",
  "provider": "anthropic",
  "prompt_sha256": "2c26b46b68ffc68ff99b453c1d304134...",
  "outcome": "PASS",
  "attempts": 1,
  "carried": false
}
```

| Field | Meaning |
|---|---|
| `head` | The commit the round reviewed (`HEAD`, read before the workspace was uploaded) |
| `base` | The project's base commit, the one the gates use |
| `diff_spec`, `diff_argv` | The version and exact command of the canonical diff, with `{base}` and `{head}` as placeholders |
| `diff_sha256` | Hex SHA-256 of the raw bytes of that diff from `base` to `head`; `null` when git failed |
| `diff_error` | Why `diff_sha256` is `null`, a short string. Present only when it is |
| `round_base` | Where the previous round was judged (the project base in round one). Empty when there is none |
| `round_diff_sha256`, `round_diff_error` | The same for `round_base` to `head` (hex SHA-256 of the raw bytes): what this round changed |
| `worktree_clean` | Tri-state: `true` when `git status --porcelain -z --untracked-files=all` printed nothing, `false` when it printed anything (a modified tracked file or an untracked file that is not ignored), `null` when it could not run |
| `engine`, `model`, `effort` | What the call that produced the verdict ran on. A retry on the other engine records the second one |
| `provider` | `anthropic` for `claude`, `openai` for `codex`, otherwise empty |
| `prompt_sha256` | Hex SHA-256 of the UTF-8 encoding of the brief the supervisor built for the judge on that call (the varying part of its prompt; see below). Empty if the judge failed before one was built |
| `outcome`, `attempts`, `carried` | How the judge ended; `attempts` is a count of the calls made for this judge in the round it ran (1 for a single call); and whether this round reused its verdict from an undecided round on the same commit |

The row's existing keys are unchanged: `at` remains epoch seconds (a float), and `round`, `verdict`,
`blocking`, `changed_files` and `changed_files_count` keep their meaning.

A carried judge keeps the engine, model, effort and prompt hash and attempt count of the round where it really ran. A carried
entry restored from a metrics file written before these were kept has them empty (attempts 1). A judge that raised after its call keeps that call's engine, model and prompt hash.

Recording never fails a review. If git fails the hash is `null` and the error is a short string. The diff is
computed once per round; every judge's entry repeats it.

## The canonical diff

`git-diff/1` pins everything that changes diff text: no user or system git config is read, and the options
that depend on config are set on the command line (prefixes, renames, algorithm, context, colour, quoting,
no external diff, no textconv). That makes the bytes the same for any user, with a hostile `.git/config`, and
with `diff.external` or `color.ui=always` set. Changing the argv means a new spec name; `recreate_diff` refuses
an entry whose argv is not the one its `diff_spec` stands for, since a report is a file anyone may have edited.

`git diff A B` in a clone also reads `.gitattributes` from the working tree and `.git/info/attributes`, which
are not part of either commit: a `binary` or `-diff` line turns text hunks into a binary patch. So `git-diff/1`
runs the argv in an empty, throwaway bare repository that borrows the clone's object store as an alternate:
no working tree, no index, no attributes and no repository config, only the two commits. The same commits
give the same bytes in any clone.

## Recreating a diff

```
PYTHONPATH=src python -m flyte_agent_factory._judge_inputs recreate --report snapshot.json --round 2 --judge craft \
    --repo path/to/clone --out round2.patch [--which project|round]
```

`--report` is a run snapshot or a metrics file; the round row is found under `review.verdicts`,
`metrics.rounds`, or top-level `verdicts` or `rounds`. `--which round` recreates what the round changed
instead of the whole project diff. Without `--out` the diff goes to stdout. The clone must hold both commits
with their blobs (not a `--filter=blob:none` clone).

| Exit | Meaning |
|---|---|
| 0 | The diff was recreated and its hash matches |
| 2 | The round, judge or hash is not in the report, the report cannot be read, `--repo` is not a git repository, or `--out` cannot be written |
| 3 | A commit is not in the clone (a rebase, a squash, or a different repository) |
| 4 | The recreated diff hashes to something else |

From Python, `recreate_diff(repo, entry, which="project")` raises `CommitsUnreachable` or `DiffMismatch`, both
subclasses of `RecreateError`. `judge_inputs_of(row)` reads a row into typed `JudgeInputs` and returns `{}` for
an old or malformed row.

## `worktree_clean`

The judges read an upload of the whole workspace, not of the commit. When `worktree_clean` is `false` the
tree held uncommitted or untracked changes, so the judge saw files the recorded diff does not contain. Treat
such a row as not fully described by its hashes. It is not a statement about ignored files, and harness paths
are left out of `git status` by the clone's `info/exclude`. It is read just after the upload, so a file
written in between can be missed.

## Against the proposed corpus format

`DESIGN-CORPUS.md` (in the workspace, `projects/decision-corpus-design/`) section 3 proposes `run` and `annotation`
records, and its prototype `corpus/capture.py` builds them from a run snapshot. Today that capture writes the
round's `round_verdict` and `blocking_findings` as one annotation from a judge named `review-panel`, with an
identity guessed from the round's usage rows (models and engines comma-joined, `prompt_sha256: null`). Per-judge
verdicts reach it only as `metrics.labels` rows (`kind: judge`). The `judges` entries let a capture name each
judge instead of guessing:

| Engine row | Corpus |
|---|---|
| `model`, `effort`, `provider`, `prompt_sha256` of `judges[<name>]` | `produced_by.judge{model, provider, effort, prompt_sha256}` on that judge's own annotations (the `metrics.labels` rows with `kind: judge`, `name: <name>`, same round) |
| the key of `judges` (`correctness`, `completeness`, `craft`) | `produced_by.name` on those annotations |
| the round row's `verdict`, `blocking` | unchanged: the panel's `round_verdict` and `blocking_findings`, still from `review-panel`. Its identity can now be the list of the judges' entries rather than a guess from usage |
| round, `head`, `base`, diff hashes | `run.rounds[]`, which has no field for them (below) |

Where they differ:

- **Judge outcomes are not verdict labels.** `outcome` is how a judge ended: `PASS`, `FAIL`, `ADVISORY`, or
  `NOT_RUN`, `NO_VERDICT`, `DEFERRED` (no verdict at all). The corpus has no label for a judge that did not
  answer. A capture should write a per-judge verdict annotation only for `PASS`/`FAIL`/`ADVISORY`, and keep the
  others as run facts.
- **The inputs are per round, the corpus has no place for them.** `run.rounds[]` has no `head`, `base` or diff
  hash. Propose `rounds[].inputs` holding `head`, `base`, `round_base`, `diff_spec`, `diff_argv`, `diff_sha256`,
  `round_diff_sha256` and `worktree_clean`, once per round. The engine repeats them in each judge's entry so an
  entry stands alone.
- **No framework version or engine commit per judge.** The corpus requires `produced_by.framework{name, version}`
  and `factory_sha`. The row has neither; `factory_sha` comes from the snapshot's `engine_sha`, and the framework
  version is still not recorded anywhere (its B3 item). `engine` is the command that ran the judge (`claude` or
  `codex`), not a versioned tool name.
- **Empty values the corpus schema rejects.** The schema requires a non-empty `model` and `provider`, and a
  `prompt_sha256` that is a sha256 or null. Here a Codex call has `model: ""` (Codex's default model is not
  named by the engine), an unknown engine has `provider: ""`, and a judge that failed before its prompt was
  built has `prompt_sha256: ""`. A capture should map `""` to null for `prompt_sha256` and refuse (as the corpus
  design already does) rather than invent a model or provider.
- **What `prompt_sha256` covers.** It is the sha256 of the brief string the supervisor built for the judge (the
  `brief` argument of the call), before the sub-agent strips surrounding whitespace and writes it to its file.
  The judge also receives the role definition (`--agent verify`), its `CLAUDE.md` memory and the fixed `-p`
  instruction, which are not in the hash; those come from the engine at `engine_sha`. The corpus `prompt_sha256`
  is described as the judge's prompt; treat this as the varying part of it.
