# Factory state: a git branch, not a bucket

The factory keeps its own state -- the launch queue and its leases, usage
reserves, admission wait reasons, gardening counts and intake de-duplication -- on one orphan branch
of the `[repo] project` repository. No factory component reads or writes object storage directly;
only Flyte's native I/O (task inputs and outputs, `flyte.Checkpoint`, code bundles) touches the bucket.
The one object-storage URI outside `[state]` is the personal browser's credential archive,
`[browser] profile_uri` (`docs/personal-browser.md`).
`[state] uri` is no longer read (a profile that still sets it loads, with a deprecation note).

## Where

- Repository: `[repo] project`, reached with `[secrets] deploy_key`, the deploy key every profile
  already has and every project pod already installs. An operator's own machine uses the operator's
  git credentials.
- Branch: `factory-state/<backend project>-<backend domain>`, or `[state] branch` (any name
  `git check-ref-format --branch` accepts) when two factories share one repository and one Flyte project
  and domain.
- Pods find the store through the profile (`[repo] project`, and `[state] branch` or the default).
  `AGENT_FACTORY_STATE_REMOTE` and `AGENT_FACTORY_STATE_BRANCH` override it in the current process only
  (operator tools, tests); they are never forwarded to a pod.
- In a project pod the project's clone installs the deploy key first; the store then finds a key installed
  and leaves it alone, so a project on a `[repo_keys]` repository keeps the key it needs.
- The branch is an orphan: its tree holds only state files, so no workflow, deploy or review sees it,
  and pushes to it never start CI (GitHub runs the workflows in the pushed commit's tree; there are none).

Tree layout (the same keys the bucket used; migration is a three-way merge per key, see Cutover):

| Path | What |
|---|---|
| `queue/<name>/NNNN.json` | a launch request |
| `queue/<name>/NNNN.{started,cancelled,failed,attempt-N,yield-<run>}` | its markers |
| `queue/.dispatch-lease.json` | the dispatch lease |
| `reserve.json` | usage reserves per account |
| `waits.json` | why each waiting request waited at the last admission pass |
| `gardening/counts.json` | weekly gardening counts |
| `limits/<sha256[:16]>.json`, `limits/engine-<engine>.json` | remembered usage limits and their clears, shared by every pod (`_limit_share`; advisory: a failed read or write is logged and never fails a call) |
| `github/intake/...`, `loop/backlog/...` | intake de-duplication markers and leases |
| `checkpoints/<run>.json` | checkpoint notes of runs started before the cutover, copied by the migration (read-only) |
| `migration.json` | what the cutover migration has carried over, for its next pass, and the bucket's highest generation per project |

## How concurrency is made safe

Every write is a commit whose parent is the branch tip the writer read, pushed without force. Git
(and GitHub) accept a non-forced push only as a fast-forward, so a push whose parent is no longer the
tip is rejected: **a ref update is a compare-and-swap on the tip commit.** On a rejection the writer
fetches the new tip, re-reads, re-decides and tries again (bounded; then it fails loudly).

- Plain writes (a marker, a request) re-apply on the new tip, so two writers of different
  files never lose each other's write, which an object store's last-writer-wins could not promise
  across a read-modify-write.
- The dispatch lease is a conditional update: "take it if absent, expired or mine" is decided against
  the tip and committed in one CAS. Two dispatchers or sweeps racing cannot both hold it: the second
  push is rejected, and the re-read shows the first holder.
- Behind the lease the dispatcher keeps its existing checks: it re-reads the cancel marker before and
  after creating a run, and a request that still gets two runs is de-duplicated on the server by labels
  (`_factory`). One live project per slot follows from one dispatcher at a time plus that dedupe.
- Nothing is written when nothing changes (an identical blob makes no commit), and a sweep with no
  pending request takes no lease, so the branch grows only with real events.

## Resume

A project pod puts two fields, `checkpoint` (the location `flyte.Checkpoint` saves to) and `app` (the
client id of its API key), into the status JSON of its run's report, so every report flush carries them.
Nothing is written to the store or to storage for this. The checkpoint itself stays where Flyte put it.
When a run is aborted by its own key, the factory reads both fields from that run's report and recreates
it from that location; a report that cannot be read, or lacks the fields, falls back to a migrated
pre-cutover note (see the cutover below), and with neither the abort is final. A run that ended normally resumes from its published outputs, as before.

## Cutover from the bucket

For the live work factory. `migrate_state.py` is the only code that still reads the old `[state] uri` prefix, read-only,
and only for this move. Each pass is a three-way merge against what the previous pass carried (recorded on
the branch in `migration.json`), so it can be repeated as often as needed and never replaces what the new
code has written. Use `--dry-run` first on each pass to see the counts.

1. Before the deploy, run `python ops/migrate_state.py --from <old [state] uri>` from the operator machine
   (it needs read access to the bucket, and git push to the workspace repository). The branch then holds
   every request, marker, reserve and checkpoint note, so from its first sweep the new code sees the
   generations already used and numbers new launches after them. A pass that exits non-zero with
   "no factory state under ..." means `--from` is wrong (empty, missing, or too deep a prefix): nothing
   has been migrated, so stop and fix the prefix.
2. Deploy this release. Runs already live are found on the server by their labels, so nothing is
   dispatched twice. Runs are matched to requests by their (project, generation) labels, and pods still
   on the old image keep numbering their own requests from the bucket, so once `migration.json` exists
   the new code numbers each project's launches at least 100 (`_queue.CUTOVER_GAP`) above the highest
   generation the bucket had, and never below a generation a run on the server already carries: the
   two codes never label a run alike, and neither can mistake the other's run for its own.
3. Immediately run the migration again, and again until it reports "nothing to migrate" with no
   `renumbered` line. This carries over what the old factory and old pods wrote between step 1 and the
   deploy: new requests and markers, hand-overs, cancels, and checkpoint notes. "Nothing to migrate" is
   a stop condition only on a pass that exited 0; "no factory state under ..." is never done. A request the old code
   queued under a generation the new code has meanwhile used for another launch of the same name is
   moved, with its markers, to the next free generation (`renumbered: ...`), so neither request is lost
   and the new one is not mistaken for started. A key the new code has changed since it was copied is
   reported as a conflict and the branch's (newer) version is kept; `--overwrite` takes the bucket's.
   A `started` marker the old code removed (it reopened the request) is removed on the branch as long
   as the branch still has the copy, and each one is printed as `deleted ...`. Always point `--from` at
   the live prefix, never at a copy of it: an older copy lacks markers written since, which reads as
   reopens. Check the dry run's `deleted` lines before the real pass.
4. Keep re-running it (step 3) while any run started before the deploy is still live: those pods are on
   the old image and hand over, and note their checkpoints, to the bucket until they end. Avoid cancelling
   a request while pods on the old image are still running, or re-check its `started` markers afterwards:
   a cancel made on either side during the two-image window is only seen by the other side after a pass.
5. When no such run is left and a final pass exits 0 and reports "nothing to migrate" (not "no factory
   state under ..."), remove `[state] uri` from the profile.
6. The owner deletes the bucket prefix.

A run started before the deploy never put its checkpoint pointer or key id in its report, so when a
report lacks them the factory reads `checkpoints/<run>.json`, which the migration copied, from the
branch instead; such a run is still recreated from its checkpoint after an abort from inside.

Not migrated: `waits.json` (each admission pass rewrites it whole), the retired `board/`, `github/outbox*`
and `deployer/`, and the old dispatch lease.
