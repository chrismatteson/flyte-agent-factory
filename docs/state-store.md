# Factory state: a git branch, a bucket or a volume

The factory keeps its own state -- the launch queue and its leases, usage
reserves, admission wait reasons, gardening counts and intake de-duplication -- behind one interface
(`_state_store.Store`), with three backends. No factory component reads or writes object storage except
through it; only Flyte's native I/O (task inputs and outputs, `flyte.Checkpoint`, code bundles) touches
Flyte's own bucket.
`[state] uri` is no longer read (a profile that still sets it loads, with a deprecation note).

## Backends

Choose with `[state] backend` (`git`, `bucket`, `volume`). Without it the backend follows the fields set: `bucket`
selects `bucket`, `volume` selects `volume`, neither is `git`.

| Backend | Use it for | Needs |
|---|---|---|
| `bucket` | **Production default.** A bucket the factory owns, on obstore: S3, GCS or an S3-compatible server | a bucket, and the pod's own cloud identity |
| `git` | Zero-setup trial: nothing to provision | the `[repo] project` repository and its deploy key |
| `volume` | Open-source, single node: no bucket, no git remote | a persistent directory every factory process on the machine can reach |

```toml
[state]
bucket = "s3://acme-factory-state/prod"   # or gs://bucket/prefix
region = "eu-west-1"                      # optional
endpoint = "http://localhost:9000"        # optional: S3-compatible server (MinIO); http only for localhost
retention_days = 30                       # how long audit records are kept (default 30)
# volume = "/factory-state"               # instead of bucket: where the claim is mounted in every pod
# volume_claim = "factory-state"          # the PersistentVolumeClaim to mount there (required with volume)
# factory_service_account = "flyte-factory"   # the pods that touch state run as this Kubernetes account
# browser_service_account = "flyte-browser"   # the personal browser apps run as this one
# flyte_bucket = "s3://flyte-data/raw"    # optional: Flyte's own data bucket, see below
```

- A profile with an unknown scheme, a field that does not belong to the backend (`endpoint` with a
  volume, `branch` with a bucket), a relative `volume`, credentials inside a URI or an `http` endpoint that is
  not localhost does not load; the error names the field.
- **Credentials never go in the profile.** A bucket is reached with what the environment gives obstore:
  IRSA or `AWS_*` for S3, workload identity or `GOOGLE_APPLICATION_CREDENTIALS` for GCS. Nothing logs or
  prints one.
- **Never Flyte's own bucket.** The state bucket and every browser `profile_uri` are refused when they are in the
  Flyte/Union data bucket (same bucket name, whatever the prefix; `s3`/`s3a`/`s3n` and `gs`/`gcs` count as the
  same). The engine learns that bucket from `[state] flyte_bucket` (checked at profile load) and from the raw-data
  path of the Flyte task it runs in (checked when the store is first used). The bootstrap writes `flyte_bucket`
  when it can find the bucket (`--flyte-bucket URI`, repeatable, a `raw_data_path` in the Flyte config, or the
  task it runs in) and refuses a `--state-bucket` that is inside it. Flyte's local config does not otherwise
  name its data bucket, so pass `--flyte-bucket` (or set `flyte_bucket` yourself) when you know it. Each entry must be
  `scheme://bucket` (`s3://`, `gs://`) or `file:///path`; a bare bucket name would match nothing and is refused.
- A personal browser identity with no `profile_uri` gets `<bucket>/browser/<identity>/profile.tar.gz`
  (volume: `file://<volume>/browser/<identity>/profile.tar.gz`). The archive code itself is `docs/personal-browser.md`.
- The backends do not share state, but `ops/move_state.py` copies it from one to another
  ([Moving the state](#moving-the-state-between-backends)).
- **Turn object versioning on** for an S3 or GCS state bucket: it is what lets you undo a bad write or a
  deleted key. The Terraform module for the factory bucket enables it; the bootstrap reminds you when it
  writes a bucket into the profile, and nothing checks it by calling the cloud.

### The object backends (`bucket`, `volume`)

Layout under the bucket prefix (or volume directory): state files under `state/` (the keys of the table
below) and audit records beside them under `audit/`; `list("")` shows state keys only.

- **Reads.** A snapshot is a listing of `state/` with each key's etag (and GCS generation); the bodies are
  read on first use and cached by etag. Its `tip` is a digest of the listing.
- **Compare-and-swap.** `transact` runs `decide(snapshot)`, then writes each changed key under the tag it was read with:
  a key that was absent is written create-only (`If-None-Match`), one that was present is written
  `If-Match` its etag/generation. A refused write means another writer got there first: the store lists again,
  runs `decide` again after a full-jitter pause, and gives up with `StateError` after a bounded number of
  rounds. S3 needs obstore's `conditional_put = "etag"` for `If-Match`. obstore 0.11.1 defaults to it, and the store sets it explicitly so an
  `AWS_CONDITIONAL_PUT` in the environment cannot turn it off.
- **Plain writes** (`write`, a marker or request) are unconditional and skip a key whose bytes are unchanged;
  `write` returns False when nothing changed. Writers of different keys never touch each other.
- **Delete is not conditional.** Object stores have none. No transaction deletes a key writers contend
  for: `release_lease` writes an expired lease (`{"holder": "", "expires": 0}`) under the same
  condition instead of deleting it, and `take_lease` treats that as free. The remaining deletes are markers
  and requests their owner removes.
- **Several keys in one transaction are not atomic.** They are written one by one in sorted order, each
  conditionally; a refusal part-way leaves the earlier keys written and `decide` runs again on the new state. Only the
  migration (`migrate_state.py`, `move_state.py`) writes several keys at once, and it is an idempotent merge.
- **Audit.** Every write that changed something also writes `audit/<UTC date>/<ns timestamp>-<random>.json`
  (message, keys, action, bytes, writer host). `recent_changes(limit)` returns the newest. A failed audit
  write is logged and never fails the state write. A process deletes audit days older than `retention_days`
  once, at its first audit write; a bucket lifecycle rule can do the same without it.
- **Stats.** `store.stats` counts `writes`, `lost_races` (conditional writes refused) and `objects_written`.
- **Volume.** Every write takes an exclusive `flock` on `<volume>/.lock`, checks the object's tag (the SHA-256 of its
  content) and replaces the file through a temporary file and `os.replace`, so it is safe across threads and
  processes of one machine. Do not put it on NFS or another network file system: use a bucket.

`recent_changes` on the git backend is the branch's `git log`.

### Mounting the volume in the pods

A directory is only shared if every pod sees the same one. With `backend = "volume"` the engine puts a pod template
on every environment that touches the state (the slots, `factory`, `gardening-scan`, `factory-mcp`,
`factory-issues`, `factory-reconcile`, `factory-loop`, the webhook when the planner runs in it) and on the personal
browser apps. It mounts the PersistentVolumeClaim `[state] volume_claim` at the path `[state] volume`. The operator
creates the claim once. On a single-node install with the local-path or hostpath provisioner:

```sh
kubectl apply -n <the namespace the tasks run in> -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: factory-state
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
EOF
```

- **Why a claim and not a `hostPath`.** Containers run as an unprivileged user. A `hostPath` of type
  `DirectoryOrCreate` makes a root-owned `0755` directory the pod cannot write. A claim from the provisioner is
  scheduler-aware and the operator makes it once.
- **Writability.** The mount must be writable by the pods' user. If the provisioner makes it root-owned, change
  the directory's mode on the node (local-path: `chmod` it, or edit the `setup` script in its ConfigMap); a pod
  that cannot write fails at its first state write.
- **One node.** `ReadWriteOnce` on a local provisioner binds every pod to the node that holds the volume, which is
  why this backend is for a single-node install. Do not use a network file system: use a bucket.
- **Deploying by hand needs `pip install kubernetes`.** The pod template's Kubernetes objects are built when Flyte
  serializes the environment. The deployer image has the package; a git or bucket profile needs none.
- `volume` is the path inside the pods. Pick one the unprivileged user can mount at (e.g. `/factory-state`),
  not a path under `/var/lib`.

### Which Kubernetes service account each pod runs as

`[state] factory_service_account` is set on every environment that touches state, and `browser_service_account` on
each personal browser app, as `service_account=`. A bucket is reached with the pod's own identity (IRSA, Workload
Identity), so the two names are what let the bucket policy give the browser app `browser/*` alone. Both are
optional and allowed with any backend; the bootstrap fills them from the `factory_service_accounts` Terraform
output ([terraform-factory-bucket.md](terraform-factory-bucket.md)). Create the Kubernetes accounts first.

## Where (git backend)

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

Tree layout (the same keys the old Flyte-bucket layout used; migration is a three-way merge per key, see Cutover):

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
| `deployer-lock/<sha256(project/domain/profile)>` | the deployer's lease: `{"run", "profile", "scope"}` of the `deploy_pair` run that may deploy (below) |
| `checkpoints/<run>.json` | checkpoint notes of runs started before the cutover, copied by the migration (read-only) |
| `migration.json` | what the cutover migration has carried over, for its next pass, and the old Flyte-bucket layout's highest generation per project |

## How concurrency is made safe (git backend)

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
- The deployer's lease (`factory-deployer`, `_deployer._acquire_lease`) is the same kind of conditional update. It
  is always in a git store on `[repo] project`, even when `[state] backend` is `bucket` or `volume`: it needs
  the compare-and-swap, and the owner's rule is that it adds nothing to an object store. A `deploy_pair` run
  takes it if it is absent, already its own, or held by a run whose Flyte phase is terminal; a run that
  finds a live holder stands down, and one that cannot look the holder up fails (it never expires on a guess).
  The record is replaced when its holder is finished, never deleted. The deploy key is mounted in the deployer
  for it (`_factory_base.state_secrets`), and `_child_env` keeps its variables from the deploy's child processes
  (the key file `_git.install_deploy_key` writes stays readable to them; see `deploy-lock-decision.md`). The tick
  refuses, naming the fix, when the key is missing or the branch cannot be read. Installs from before it kept the
  lock as a git ref `factory-deployer-lock/<key>` written by the App: a new run still reads that ref (contents
  read only, never written) and stands down while its holder is live.
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

## Moving the state between backends

`ops/move_state.py` copies every state key from one backend to another in one command, and back again.
Both ends are named as URIs, or by the profile:

| Name | Meaning |
|---|---|
| `git` | the profile's `[repo] project` and state branch, or `--git-remote` and `--git-branch` |
| `profile` | whatever the profile's `[state]` selects now |
| `s3://bucket/prefix`, `gs://bucket/prefix` | a bucket (`--endpoint`, `--region` for the destination; `--from-endpoint`, `--from-region` for the source; credentials from the environment) |
| `file:///path` | a directory on a volume |

```sh
# git -> bucket: look first, then copy
PYTHONPATH=src uv run python ops/move_state.py --from git --to s3://acme-factory-state/prod --region eu-west-1 --dry-run
PYTHONPATH=src uv run python ops/move_state.py --from git --to s3://acme-factory-state/prod --region eu-west-1
# and back: --mirror onto the original branch deletes what the factory deleted in the meantime, so the
# branch ends with exactly the bucket's keys (byte-identical); without it the command stops and lists them
PYTHONPATH=src uv run python ops/move_state.py --from s3://acme-factory-state/prod --region eu-west-1 --to git --mirror
# the old Flyte-bucket layout (the old [state] uri): the same merge as `migrate_state.py`, which the
# cutover below uses
PYTHONPATH=src uv run python ops/move_state.py --from-legacy s3://old-bucket/prefix --to profile
```

- `--dry-run` prints each key it would copy and the counts, and writes nothing.
- It is idempotent: a second run copies nothing (`copied 0, same N`).
- A key that exists at the destination with other content is a **conflict**: reported and kept, unless
  `--overwrite`.
- A key only the destination has is listed as `only at destination` and kept, nothing at all is written (the
  keys that would have been copied are listed as `held`), and the command exits 1 with no switch-over line:
  switching to a store that still holds a request the factory already consumed would dispatch it again. `--mirror` deletes those keys in the same transaction (`--dry-run --mirror` prints
  `would delete`). Nothing is ever deleted at the source; the command says so.
- A source with no state keys is refused (exit 1, nothing written): an empty or mistyped prefix or branch looks
  exactly like a finished move. `--allow-empty-source` proceeds anyway (a fresh install switching to an empty
  bucket); `--mirror` never does, since it would delete every key at the destination.
- The bucket's `audit/` records are not state and are not copied (the destination writes its own).
- Everything goes in one transaction on the destination, so a writer there is not lost; a key written
  there meanwhile makes the copy decide again.
- The last line names the `[state]` lines that switch the profile over: `bucket = ... region = ...` (and
  unset `volume`), `volume = ...` with `volume_claim`, or for git unset `bucket`, `endpoint`, `region`, `volume`
  and `volume_claim`. Stop
  the factory's writers (or run it twice, see the cutover below) around the switch, so nothing is written to
  the old place after the last copy. URLs with a user name or token are shown with it masked.
- The destination is refused when it is inside Flyte's data bucket (`[state] flyte_bucket`, or the raw-data path).
- `--from-legacy` keeps `migrate_state.py`'s behaviour (three-way merge, renumbering, `migration.json`), which
  the cutover below describes; it works for any destination, not only git.

### A browser identity's archive

```sh
PYTHONPATH=src uv run python ops/move_state.py --browser github \
    --from-profile-uri s3://flyte-data/browser-github/profile.tar.gz --to s3://acme-factory-state/prod
```

copies the identity's archive to `<to>/browser/<identity>/profile.tar.gz` (`--to file:///dir` for a volume). The
newest archive is the one the old `.lease` lists, and the one behind it becomes `.prev`. The lease is not
copied (it names the old objects; the app makes its own). Bytes are copied as they are: a sealed archive stays
sealed, and a plaintext archive from before encryption is copied as-is and the app re-seals it on its next
snapshot (the command says so). **The old objects are never deleted**: once the identity runs from the new
place, the owner deletes them by the steps the command prints. Stop the identity's app before copying: a
running one keeps publishing to the old place. `--from-endpoint` and `--from-region` name the old place,
`--endpoint` and `--region` the new, as for the state. The archive is read and written through obstore, the
state store's library, so the command needs only the engine's own dependencies (no `s3fs` or `gcsfs`).

## Cutover from the old Flyte-bucket layout

For the live work factory. `migrate_state.py` is the only code that still reads the old `[state] uri` prefix, read-only,
and only for this move. Each pass is a three-way merge against what the previous pass carried (recorded on
the branch in `migration.json`), so it can be repeated as often as needed and never replaces what the new
code has written. Use `--dry-run` first on each pass to see the counts.

1. Before the deploy, run `python ops/migrate_state.py --from <old [state] uri>` from the operator machine
   (it needs read access to the old Flyte bucket, and git push to the workspace repository). The branch then holds
   every request, marker, reserve and checkpoint note, so from its first sweep the new code sees the
   generations already used and numbers new launches after them. A pass that exits non-zero with
   "no factory state under ..." means `--from` is wrong (empty, missing, or too deep a prefix): nothing
   has been migrated, so stop and fix the prefix.
2. Deploy this release. Runs already live are found on the server by their labels, so nothing is
   dispatched twice. Runs are matched to requests by their (project, generation) labels, and pods still
   on the old image keep numbering their own requests from the old Flyte bucket, so once `migration.json` exists
   the new code numbers each project's launches at least 100 (`_queue.CUTOVER_GAP`) above the highest
   generation the old Flyte bucket had, and never below a generation a run on the server already carries: the
   two codes never label a run alike, and neither can mistake the other's run for its own.
3. Immediately run the migration again, and again until it reports "nothing to migrate" with no
   `renumbered` line. This carries over what the old factory and old pods wrote between step 1 and the
   deploy: new requests and markers, hand-overs, cancels, and checkpoint notes. "Nothing to migrate" is
   a stop condition only on a pass that exited 0; "no factory state under ..." is never done. A request the old code
   queued under a generation the new code has meanwhile used for another launch of the same name is
   moved, with its markers, to the next free generation (`renumbered: ...`), so neither request is lost
   and the new one is not mistaken for started. A key the new code has changed since it was copied is
   reported as a conflict and the branch's (newer) version is kept; `--overwrite` takes the old Flyte bucket's.
   A `started` marker the old code removed (it reopened the request) is removed on the branch as long
   as the branch still has the copy, and each one is printed as `deleted ...`. Always point `--from` at
   the live prefix, never at a copy of it: an older copy lacks markers written since, which reads as
   reopens. Check the dry run's `deleted` lines before the real pass.
4. Keep re-running it (step 3) while any run started before the deploy is still live: those pods are on
   the old image and hand over, and note their checkpoints, to the old Flyte bucket until they end. Avoid cancelling
   a request while pods on the old image are still running, or re-check its `started` markers afterwards:
   a cancel made on either side during the two-image window is only seen by the other side after a pass.
5. When no such run is left and a final pass exits 0 and reports "nothing to migrate" (not "no factory
   state under ..."), remove `[state] uri` from the profile.
6. The owner deletes the old bucket prefix.

A run started before the deploy never put its checkpoint pointer or key id in its report, so when a
report lacks them the factory reads `checkpoints/<run>.json`, which the migration copied, from the
branch instead; such a run is still recreated from its checkpoint after an abort from inside.

Not migrated: `waits.json` (each admission pass rewrites it whole), the retired `board/`, `github/outbox*`
and `deployer/`, and the old dispatch lease.
