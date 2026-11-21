# Setup

From nothing to a first project. The deploy and the key modes require Union: with `--backend oss` the bootstrap
runs every step before the deploy, and OSS Flyte needs the adaptations in
[the compatibility audit](oss-flyte-compat.md). The factory also needs a Claude
`setup-token` (and optionally a Codex access token), one GitHub repository of your own for the workspace, and a
deployment profile that names them. How the pieces fit together is in [architecture.md](architecture.md).
Start with [guided setup](quickstart.md): install the skills, run `/agent-factory`, and it checks existing resources
and takes you from GitHub access to a first launch one step at a time; every secret is in
[secrets.md](secrets.md). This page is the detail behind them.

**Minimum cluster:** one node with 4 CPU and 16 GiB of memory runs the factory and one live project with the default
resources; see [small-nodes.md](small-nodes.md) for the arithmetic and for raising it on a bigger cluster.

## The short path

1. **Create the workspace repository.** It is the repository projects are pushed to (`[repo] project`). Create a
   private GitHub repository for it, then fill a clone of it from the engine's bundled template:

   ```bash
   uv run python ops/new_workspace.py ../agent-workspace
   ```

   Copy the workspace's `profile.example.toml` to `profile.toml` at its root and fill it in
   ([quickstart.md](quickstart.md), step 3), replace `OWNER` in `.github/CODEOWNERS`, and turn on branch
   protection ([branch-protection.md](branch-protection.md)). `ops/bootstrap.py` does all of this, and more, in
   one resumable command. `standards/` and `waivers.toml` are yours; the
   factory cannot change them. So is `gardening.toml`; without it there are no gardening targets
   ([architecture.md](architecture.md#gardening-targets)).
2. **Store one `claude setup-token`** as a Flyte secret and name it in the profile's `[secrets] claude_token`
   (section 1 below). Every Claude session runs on it.
3. **Add a deploy key** for the workspace repository, store it as a secret and name it in
   `[secrets] deploy_key` (section 3). To launch projects into another repository, give it a
   deploy key too and list it under `[repo_keys]`. The GitHub App is the guided-setup default (read-only on code; pushes stay on deploy keys until the isolation canary reports `isolated`), yours to register
   ([github-app.md](github-app.md)); without it the workspace deploy key is the only GitHub credential setup requires,
   and there is no CI visibility. A launch repository where the App is installed and no deploy key reaches is cloned
   with a contents-read token for that one repository, so reading it needs no `[repo_keys]` entry; pushing to it still
   needs its deploy key (or `push = "app"`).
4. **Select the profile and deploy** with `--profile <workspace clone>/profile.toml` (or `AGENT_FACTORY_PROFILE` set to
   that path). The first install is `ops/deploy_work.py` and `ops/deploy_factory.py` ("Deploying", below): the only
   hand step besides creating secrets. After that a merge to the workspace deploys itself when the profile has
   `[direction]` (below). A merge to the public engine deploys once its CI is green: the App is installed on the
   workspace only, so the deployer reads the engine anonymously, polls (`[deploy] webhook = false`) and keeps its lock
   in `[direction] repo`. To deploy sooner, run the
   bootstrap with `--redeploy` ([quickstart.md](quickstart.md#what-agent-factory-walks-you-through)). The personal browser app
   follows the same rule ([personal-browser.md](personal-browser.md)); a merge to the workspace deploys
   only if the profile has `[direction]` pointing at it (`repo`, and `profile = "profile.toml"`), which the workspace's
   own `profile.toml` carries. The engine does not look for a `profile.toml` in the workspace by itself: the first
   deploy names it, and moving an existing install onto `[direction]` later needs one run of the bootstrap with
   `--redeploy` ([quickstart.md](quickstart.md)), which deploys with that `--profile`.
5. **Link a backlog board.** A GitHub Projects board owned by the same account, linked to the workspace with it as
   the board's default repository, is the standard backlog: unrelated projects, your own items, and copies of
   issues pulled from tracked repositories. The bootstrap creates and links it (`gh project create`,
   `gh project link`; skip with `--no-board`) and prints the clicks GitHub has no command for.
6. **Connect chat** to the factory MCP ([chat-operator.md](chat-operator.md#setup),
   [factory-mcp-setup.md](factory-mcp-setup.md)) and launch a project
   ([operating.md](operating.md)).

The sections below are the detail behind each step.

## One-time setup

### 0. A deployment profile

Everything specific to your deployment (Flyte project and domain, console host, project
repo, project slots, secret names, role policy, MCP settings) lives in one TOML
profile: `profile.toml` at the root of your workspace. Fill in the workspace's `profile.example.toml`, and select it
for every command and ops tool, either with the environment variable or with `--profile <path>` on `ops/*.py`:

```bash
export AGENT_FACTORY_PROFILE=../agent-workspace/profile.toml
```

See [profiles/README.md](../profiles/README.md) for the schema. The names below in angle brackets are the profile's.

### 1. A Claude setup-token

Every Claude session, the benchmark's included, runs on one `claude setup-token` credential.
Run `claude setup-token`, store the token as a Flyte secret (`flyte create secret <name>`) and name
that secret in `[secrets] claude_token` in the deployment profile; never commit the token.
The pod mounts it as `CLAUDE_CODE_OAUTH_TOKEN`, without an OAuth login file. A restored login file
is deleted before a session starts. Direction and questions use Flyte conditions.

The coordinator runs `ops/probes/token_meter.py` with the mounted token and a fresh nonce to
record whether the usage endpoint returns both 5-hour and weekly utilization. Until that evidence
is reviewed, `SETUP_TOKEN_USAGE_SUPPORTED` stays false. A setup-token's windows come from the
`rate_limit_event`s in the author session's and sub-agents' stream-json output (`cli-event`), and from the
status line of a coordinator on the TUI fallback (`statusline`). `factory.usage` reports them per credential with `kind` and `measured`, and always adds an `estimate`, labelled
as such, from the factory's own hourly records; with no current reading the estimate is the answer. While
`SETUP_TOKEN_USAGE_SUPPORTED` is false, `factory.read_login_meter` answers the credential's newest measured
reading, else `unsupported-for-setup-token`. Refusals block until the
reset, with bounded backoff when no reset is available. See
[`docs/operating.md`](operating.md#usage-meters).

For direct Flyte commands, resolve the selected profile in your shell first:

```bash
export FLYTE_CONFIG="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile config)"
export FLYTE_PROJECT="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile project)"
export FLYTE_DOMAIN="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile domain)"
export AGENT_FACTORY_SECRETS="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile agent_secrets)"
```

### Where the state lives

The bootstrap keeps the factory's state on a git branch unless it finds a bucket of the factory's own.
It looks, in order, at `--state-bucket s3://bucket/prefix` (or `gs://`; `--state-region` and
`--state-endpoint` go with it), the `AGENT_FACTORY_STATE_BUCKET` environment variable, and, only when
`--terraform-dir DIR` is given, `terraform output -json` in that directory: its `factory_bucket` output
(and `factory_bucket_region`, `factory_bucket_endpoint`, and the names in `factory_service_accounts`, which
go to `[state] factory_service_account` and `browser_service_account`; `--factory-service-account` and
`--browser-service-account` override them). Every pod that touches state then runs as the factory account and the
personal browser app as the browser account. It never runs `terraform apply`. A bucket found
is written to `[state]` in the profile and the bootstrap says so; one that is Flyte's own data bucket is
refused (`--flyte-bucket URI` names it when the Flyte config does not; it is recorded as `[state]
flyte_bucket`). Without one, it prints a line saying a bucket is recommended for production and how to add
it. Turn object versioning on for the bucket. An existing state moves with `ops/move_state.py`
([state-store.md](state-store.md#moving-the-state-between-backends)).

For a project that needs GCP, `PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py gcp_bootstrap --project_id <id>` signs in to
Google through a Flyte condition and leaves only a service-account key behind; see
[docs/gcp-bootstrap.md](gcp-bootstrap.md).

Then deploy with the profile-selected credential mounted. The slots, `factory`, `factory-mcp`,
`factory-deployer` and, with `[browser] personal`, the personal browser app are deployed by the factory itself: with `[deploy] auto = true` a merge to the default branch of the repository
named by `[deploy] repo` (default `[repo] factory`) ships on the next deployer tick once its head's CI passed;
`ops/deploy_work.py` and `ops/deploy_factory.py` cover the first install. Engine improvement is a workspace project,
imported and promoted like any other ([architecture-repos.md](architecture-repos.md#promotion)); nothing is
deployed by hand. The voice app is
deployed directly:

```bash
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" deploy --all --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/voice.py
```

### 2. A project of its own

Secrets are scoped per project and domain, so the agent runs in its own —
holding the Claude token and the deploy keys of the repositories it may push to. Its identity then
cannot reach anything else you keep in your main project. Use the profile's `[backend] project`:

```bash
flyte create project --id <project> --name "Agents"
```

### 3. The repositories it can reach

A GitHub **deploy key** scoped to one repository, stored as a secret. This one is the workspace's. Each further
repository is another deploy key under `[repo_keys]`. A slot mounts all of them, but a session installs only the key of
the repository it was launched on, so other repositories are unreachable because the session holds no credential
for them, not because a rule forbids it. The secret name is the profile's `[secrets] deploy_key`:

Generate and store it with the block in [quickstart.md §4](quickstart.md#4-store-engine-credentials-locally),
which makes the key in a `mktemp` directory and removes it afterwards.

The agent writes it 0600 and pins `GIT_SSH_COMMAND` with `IdentitiesOnly=yes`,
so ssh cannot offer another key, plus `StrictHostKeyChecking=accept-new` and
`GIT_TERMINAL_PROMPT=0` so a first clone can neither hang nor prompt.

Optionally, a **GitHub App** (the profile's `[github]` table, see
[profiles/README.md](../profiles/README.md)) lets the supervisor open pull
requests. A project then works on `factory/<name>`, the supervisor opens a
draft PR (a ready one where the repository has no drafts) when the session
declares DONE and marks it ready, with a verdict
comment and a `factory/verifier` commit status, when review passes. The App's
private key is held by the supervisor only. No session or delegate worker has it in its environment or files, but while Union injects its own key into every pod a session can launch a task that mounts the App key secret; that is what the isolation canary's `not isolated` reports ([github-app.md](github-app.md)). Every installation token
is limited to one repository and named permissions. Until the deployer's isolation canary reports `isolated`, the
session still pushes with the deploy key ([github-app.md](github-app.md)). Union injects `_UNION_EAGER_API_KEY` into
every pod; the factory ignores and removes it, and each tier acts as its own scoped key
([Union keys](#union-keys)), so a session reaches only what its tier key may. Without `[github]`,
projects push to the default branch and open no pull request. Merging is not gated on the verdict until
branch protection requires it ([branch protection](branch-protection.md)).

### 4. The browser app (optional)

```bash
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" serve --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/browser.py browser_env
```

Access control is the platform's. The app's public URL is behind Union's own
auth, and the agent reaches it on the cluster-internal Service address. The `/mcp` endpoint has no token of
its own: a shared token would only have blocked the agent it was meant to serve.

To run without the browser entirely, deploy with `AGENT_FACTORY_BROWSER=0`. The
browser is a per-run choice: `PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py project --browser`, or `--browser` /
`--no-browser` directly.

### 5. Extra secrets (optional)

A secret that doesn't exist is a hard pod mount failure in Flyte, so optional
secrets are opted into at deploy time:

```bash
AGENT_FACTORY_SECRETS=github_token,npm_token=NPM_TOKEN PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" \
  deploy --all --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py
```

### 6. Where the factory keeps its state (optional)

The factory's own state (the launch queue, leases, reserves) needs no setup to start: by default it is a branch of
the workspace repository. Choose before the first run, because the backends do not share state
([state-store.md](state-store.md)):

- **A dedicated bucket is the recommended production default.** Provision it with the Terraform additions in
  [terraform-factory-bucket.md](terraform-factory-bucket.md) and set `[state] bucket`.
- **git** is the zero-setup trial: nothing to provision, slower and more contended under many slots.
- **volume** is for open-source, single-node use.

[state-escalation.md](state-escalation.md) has the measurements behind the choice.

## Deploying

Flyte's CLI loads a task file two different ways: `flyte deploy --all` imports it package-aware as a dotted path
relative to `--root-dir`, while `flyte run <file>` imports it as a top-level module via `spec_from_file_location`.
Either way `--root-dir src` is what makes the package layout work: it makes the in-container module name
`flyte_agent_factory.agent`, and the CLI appends it to `sys.path`, which resolves the absolute intra-package imports
locally. Without it both fail with `ModuleNotFoundError: No module named 'flyte_agent_factory'`; with it,
`PYTHONPATH=src` changes nothing, and these docs carry it on `flyte` commands only so that every command running the
package reads the same way. Use the direct commands below. In order, they:

1. store a `claude setup-token` token as the Flyte secret named in `[secrets] claude_token`;
2. redeploy only the work slots from `origin/main` (safe from inside the agent);
3. deploy the factory, factory-mcp and factory-deployer from `origin/main` (the first install of the auto-deploy
   trigger);
4. verify the sandbox child-action contract on the cluster;
5. start a project.

```bash
bash scripts/export_local_credentials.sh
PYTHONPATH=src uv run python ops/deploy_work.py
PYTHONPATH=src uv run python ops/deploy_factory.py
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/sandbox_probe.py probe
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py project
```

A hand deploy acts as your own `flyte` login (the profile's `[backend] config`) and needs no API key: the scripts
leave `AGENT_FACTORY_DEPLOY_KEY_ENV` and the key Union injects into pods (`_UNION_EAGER_API_KEY`, `EAGER_API_KEY`)
out of what they run, even when your shell has them set. Every `ops/` script puts `src` on the path itself:
`PYTHONPATH=src` is needed only where a command imports the package itself (`python -m` on one of its modules, a
`python -c` or a heredoc); `flyte run` and `flyte deploy` find `src` through `--root-dir src`, and carry it anyway.
`ops/deploy_factory.py` deploys `--ref` (default `origin/main`) from a clean worktree of that commit, not your working
tree, so a change must be pushed before it ships.

Merged changes deploy themselves: with `[deploy] auto = true` in the profile, the `factory-deployer` task polls the
default branch of `[deploy] repo` (default `[repo] factory`) every 5 minutes and ships a head whose CI passed, so a
merge ships on the next tick plus build time. For a user on the public `unionai-oss/flyte-agent-factory`, where the
App is not installed, the deployer reads that repository anonymously and keeps its deploy lock in `[direction] repo`;
to deploy the engine's `main` at once, run the bootstrap with `--redeploy`
([quickstart.md](quickstart.md#what-agent-factory-walks-you-through)). With
`[github] webhook_secret`, a signed push starts it instead and the schedule is an hourly reconcile
([webhook setup](deploy-webhook.md)). It deploys the
slots, factory, factory-mcp, itself and the personal browser (when the profile enables it, replaced
stop-then-start), and skips a commit while its CI is failing or still pending. The profile and
standards it deploys with are the merged tree's `profiles/<name>.toml` when that exists (else the profile the
deployer pod started with), so a merged change to either ships too. With `auto = false` the trigger still ticks,
and does nothing. If a Flyte secret that a planned environment mounts does not exist, it deploys nothing and
its run report and tick message name the secret; create it and the next tick deploys (where the deployer works at all; see above). The `ops/deploy_*.py`
scripts above remain for the first install and for emergencies; the personal browser has no `ops/` path and
is deployed only by the deployer.

With `[board]`, GitHub branch and PR events advance issue cards. Completion requires a non-forced push
whose head is still on the current default branch, or a PR merged into that branch. Commit references
count only on the first line; revert subjects and quoted lines are ignored. Status changes reread the
card and only advance it, except an unmerged PR closure can return In review to In flight. Done stays
Done. Without a GitHub App, completion still moves the card and logs that the issue could not be closed.
The hourly board drift scan checks at most the latest 100 default-branch commits. It heals closed
issues' cards to Done and flags open issues for review; historical text never closes a reopened issue.

To roll back, run `ops/deploy_factory.py --ref <commit older than the deployer>`. It needs that commit's own ops
scripts, so run it from a checkout of that commit rather than the current `ops/`. The next merge deploys the default
branch again.

A `float` task input needs a float literal default (`4.0`, not `4`). Flyte serializes every task default at deploy
time (`convert_upload_default_inputs`) and rejects an `int` there; a dry run (`--dry-run`) does not run that step.
`tests/test_deploy_factory.py::test_deployed_task_defaults_serialize` runs it for every task a factory or slot
deploy sends. When a deploy reports `cannot schedule new futures after shutdown` for one task, the cause is the first
`Failed to deploy task` line in the log: the SDK keeps deploying the other tasks while the process exits, and one of
them then finds the event loop's thread pool closed.

## Union keys

Union injects `_UNION_EAGER_API_KEY` into every task pod. The factory never reads it: `_union_identity.establish()` removes
it at startup. Each tier instead acts as an owner-chosen scoped API key stored as a named Flyte secret, which the
deployer mounts as `AGENT_FACTORY_UNION_KEY` (with `FLYTE_AUTH_TYPE=ExternalCommand`, whose command runs
the module `flyte_agent_factory._union_token`). There is no fallback: a tier whose key is missing fails at start with a
message naming the tier, the secret and `python ops/bootstrap.py --union-keys`. A permission-denied error names the tier,
the key's client id and the secret.

The profile's `[union]` table ([profiles/README.md](../profiles/README.md#union)) names the secrets:

| Key | Required | Acts as | Role |
|---|---|---|---|
| `project_key` | yes, for a Union backend | slot supervisors and setup tasks; the default for `session_key` and `queue_key` | `contributor` on the one project and domain; nothing org-wide |
| `deployer_key` | yes, for a Union backend | the deployer and its deploy children | `contributor` on the same project and domain; it may be the same key as `project_key`, but that is your choice |
| `session_key` | no | every Claude process, sub-agents, delegates and reviewers | optional separate key; use one whose role cannot mount secrets to isolate the GitHub App ([github-app.md](github-app.md)) |
| `queue_key` | no | the factory, gardening, loop, issues, webhook, factory-mcp, bench, browser and voice | defaults to `project_key` |

**Create one in the console.** In the Union console, create an API key with no default policies, bind it to a policy with the
role `contributor` on the work project and domain only, copy the credential it shows once, and store it as a secret in
that project and domain (the secret names are placeholders such as `factory_project_key`). The easiest way is
`python ops/bootstrap.py --union-keys`: it prints the console steps, reads the key at a hidden prompt, stores it as a
secret and never echoes it. It works on a Mac; nobody needs `flyte create api-key` locally. If you may not manage access in
the organization, ask an admin for the `--grant` command it prints.

**What the `deployer_key` must be able to do.** The deploy children act as it: build images with the remote image builder,
register tasks, deploy, update, stop and delete apps and read their status, create, update and delete triggers, and list
secrets (create and delete them too when the owner manages them through the factory). The project-scoped role
(`contributor` on the one project and domain) is enough. The deploy waits for apps by polling their status and never
opens the organization-wide app watch stream (measured: that stream is refused for a project-scoped key).

**What a project tier cannot do** (measured live on 2026-11-12 with a key that was `contributor` on one project and
domain): the pod authenticated with the scoped key and listed runs in its own project and domain; a child launched by that
identity ran; listing projects org-wide was refused (unauthorized); and a launch into the staging domain was refused
(permission denied, naming the key's identity). Union **still injects** `_UNION_EAGER_API_KEY` into pods, including
those launched by the scoped identity; the factory ignores and removes it, but it is present in the pod environment.

Hand deploy scripts and `ops/*.py` use your CLI login (`flyte.init_from_config`) by default, or a named key variable
(`_union_identity.init_for_script`). The default-lane test `tests/test_union_identity_guard.py` fails on any engine code that
names the injected key, builds the rust controller or calls `init_in_cluster` outside `_union_identity`.

## Migrating an existing install

For an owner whose factory already runs on the old credential.

1. **Before merging**, create (or reuse) a key with `contributor` on the work project and domain for `project_key`, and a
   deployer key. Store both as secrets in that project and domain ([Union keys](#union-keys)), and add `[union]` to the
   direction repository's profile. The running deployer tolerates the unknown table.
2. **Deploy once as the deployer's key.** An install whose running deployer predates `[union]` cannot deploy the
   current engine itself: the deployer starts, but every deploy child it launches has no key and, in a pod, no CLI
   config, so the new engine stops it at its check with a message naming `ops/deploy_as_key.py`. After adding `[union]`
   and the secrets, merge, then deploy once from a checkout of the engine at the release and a checkout of the
   direction repository, on any machine or pod that holds the deployer key in `AGENT_FACTORY_UNION_KEY`:

   ```
   python ops/deploy_as_key.py --direction <direction checkout> --check   # plans; changes nothing
   python ops/deploy_as_key.py --direction <direction checkout>           # deploys slots, factory and apps
   ```

   The last lines say what was deployed and what was not. Or hand deploy with a CLI login, which needs no key but does
   not deploy the browser apps (the next auto-deploy does):

   ```
   python ops/deploy_work.py --ref <release>
   python ops/deploy_factory.py --ref <release>
   ```

   Then the new deployer takes over: its first tick deploys the same commit again, this time with the tier
   identities and the push mode its canary chooses (the one-off sets neither, and is not a deployer run). After
   that, check that `factory.status` lists the tiers and their client ids. Run a project, a
   review round, a queue dispatch and a deploy. To run the script in a pod, see
   [Running the one-off deploy in a pod](#running-the-one-off-deploy-in-a-pod).
3. **To move direction**, use the two-sided commit handoff ([profiles/README.md](../profiles/README.md)):
   commit in the new repository a profile whose `[direction]` names the new repository itself, then commit in the old
   repository `[direction]` naming the new one. No hand deploy is needed. Then archive the old repository.
4. **GitHub isolation is optional and gated.** Create a session key whose role cannot mount secrets and set
   `session_key`. Set `[github] push = "app"` (and `workflows = true` if you push `.github/workflows/`), accept the App's
   new permissions, and confirm the deploy report says `isolated` and `factory.status` says `app`. Only then delete the
   deploy-key secrets and `[repo_keys]`. Until then two GitHub credentials remain ([github-app.md](github-app.md)).
   While Union injects its own key into every pod (measured above), the canary reports not isolated whatever the
   session key's role, because a pod holding that key can launch a task that mounts the App key: this step waits on
   Union, and the deploy report names the reason.
5. **Rollback.**
   - Revert the engine commit; the deployer redeploys the previous engine.
   - Revoke keys individually.
   - Remove `push = "app"` to return to deploy keys, and keep those keys until a week of green.

### Running the one-off deploy in a pod

The script removes the key Union injects into every pod from its children's environment and never uses it; it acts
only as the key in `AGENT_FACTORY_UNION_KEY`. The least a task needs:

- **Image:** one with git, `uv` and Python 3.12. The factory's own agent image does (it is what the deployer runs on).
- **Secrets:** the profile's `deployer_key` secret, mounted as `AGENT_FACTORY_UNION_KEY`. For a private direction
  repository also its deploy-key secret, mounted as a file: `[secrets] deploy_key` when the direction repository is the
  workspace repository, otherwise its `[repo_keys]` entry. Clone with it through
  `GIT_SSH_COMMAND="ssh -i <file> -o IdentitiesOnly=yes"`.
- **Environment:** the only variable to set is the secret mounted as `AGENT_FACTORY_UNION_KEY` (or the name you pass
  to `--key-env`); the script sets the rest.
- **Runtime:** not interruptible, and about an hour (the deploy itself is bounded at 50 minutes, image builds included).

```
git clone <engine repository> engine && git -C engine checkout <release>
GIT_SSH_COMMAND="ssh -i <deploy key file> -o IdentitiesOnly=yes" git clone <direction repository> direction
cd engine && uv sync --python 3.12
uv run python ops/deploy_as_key.py --direction ../direction --check
uv run python ops/deploy_as_key.py --direction ../direction
```
