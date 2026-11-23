# DECISION: the deploy lock lives on the state branch (#83)

**Problem.** On a bootstrapped install (OSS backend, `[direction]` = the workspace, `[deploy] auto = true`) every
`deploy_pair` failed in ~15 s: the lock was a git ref written with an App token scoped `contents: write`, and the
bootstrap registers the App with `contents: read`. GitHub answers that token request with HTTP 422. The tick only
logged "submitted", so the failure showed up only in the child run.

**Decision.** The lease is a file, `deployer-lock/<sha256(project/domain/profile)>` (`{"run","profile","scope"}`),
on the factory's git state branch (`[state] branch`, else `factory-state/<project>-<domain>`) of `[repo] project`,
taken with `StateStore.transact` (a non-forced push; the loser re-reads the winner's record). It is written with the
workspace deploy key, which every profile has (`[secrets] deploy_key`, created write-enabled by the bootstrap) and
which the deployer now mounts (`_factory_base.state_secrets`). Semantics are those of the old ref: same run
re-acquires; a live holder (Flyte phase not terminal) stands the new pair down; a terminal holder is replaced, not
deleted; an unreadable holder fails closed; another profile's or scope's record needs the operator.

**Why not the alternatives.**
- *Widen the App to `contents: write`*: the owner's constraint; also the isolation canary exists because an App
  that can write code must not be reachable from a session.
- *A Flyte-native lock* (run listing): `RUN_LISTING_CONSISTENT` is False until `ops/probes/run_listing_order.py`
  passes on the tenant, and OSS Flyte rejects the label filters; the listing is not a compare-and-swap.
- *The object store / a volume*: the owner's constraint is no new object-store state, and the lease must work on
  both Union and OSS. The lease is a git store even if `[state] backend` is `bucket` or `volume`.
- *The deploy key in the deploy's children*: its variables are not passed on: `_child_env` hides `GIT_DEPLOY_KEY*`,
  Flyte's prefixed copy and `GIT_SSH_COMMAND`. The key file `_git.install_deploy_key` writes under `$HOME/.ssh` stays
  readable to them; that is no new exposure, since the code they run (the engine being deployed) is the code that
  mounts the same key in the factory's own environments.

**Visible failures.** The tick now refuses, naming the fix, when the deploy key is not mounted or the state
branch cannot be read (`_preflight_lease`), and its message names the most recent failed `deploy_pair` run and
its redacted error. A credential or permission failure (token mint 401/403/422, git auth refused) is not
retried: the tick says what to fix and does not submit until a head moves. A rate limit, which GitHub answers
with 403 too, is retried like any other failure. `deploy_if_changed(force=True)` retries a credential failure
only while the heads' failed attempts are under `MAX_ATTEMPTS`; once they reach it, force does not submit
either and only a moved head deploys again, so the tick then names the fix and does not offer force.

**Migration of existing installs.** An older deployer left a ref `heads/factory-deployer-lock/<key>` in the deploy
repo (where the App is installed) or in `[direction]`. Before taking the lease a new run reads it with
`contents: read` only (404, or the App not installed, is skipped) and stands down while it names another live run;
it never writes it. A `deploy_pair` of the old code that is still running at upgrade therefore keeps exclusion. The
legacy read (`_find_legacy_holder`) can be removed once no install has a live legacy holder. A ref older than twice
`MAX_RUNTIME` is not looked up (no pair can still hold it), so a holder Flyte has since forgotten does not fail
every later deploy closed; a ref whose commit message is not a lock record needs the operator, as a foreign one does.

**Behaviour changes worth a reviewer's eye.** A deploy repo the App is not installed on, with no `[direction]`,
used to be refused ("the App must be installed on a repo it can write"); it now deploys, since the lease no longer
needs such a repo. The old test asserting that refusal is replaced by one asserting the deploy and the lease.
