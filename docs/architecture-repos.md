# Repositories: the engine and the workspace

Two kinds of repository. This replaces the three-repo split (engine, sandbox, direction) of the earlier design note.

| Repository | Holds | Who writes it |
|---|---|---|
| Engine, [unionai-oss/flyte-agent-factory](https://github.com/unionai-oss/flyte-agent-factory) | the factory's code. Deployments run its default-branch head once its CI passes | pull requests only: maintainers', and yours, promoted from a workspace project like any other target ([Promotion](#promotion)) |
| Workspace template, bundled in the engine | the starting layout; a separate `unionai-oss/flyte-agent-workspace` repository is not published yet (optional, later) | maintainers; shipped in the engine at `src/flyte_agent_factory/_assets/workspace_template/` |
| Workspace, `<user>/agent-workspace` | one user's standards, profile, projects and backlog | the user, and the factory in a project's own directory under `projects/` |

## The workspace

- `standards/` is seeded from the template's example set and edited by hand ([standards.md](standards.md) is the format).
  It is read-only to the factory: the gate rejects a patch that touches it, a session's May-edit scope excludes it, and
  the template ships CODEOWNERS for it and `docs/branch-protection.md`. The template's copy is the source of the example
  set; `tests/test_workspace_template.py` checks that it parses. The engine's own `pyproject.toml` ruff and mypy settings
  are compared with the template's `standards/languages/configs/` by the `configs` gardening check
  (`_gardening.config_drift`).
- `profile.toml` is the user's Union project and domain, engines and limits; it is the workspace's only profile
  name. The template ships only `profile.example.toml`, and the bootstrap (or you) copies it to `profile.toml`. The
  engine does not look for a `profile.toml` in the workspace by itself: the first deploy passes it with
  `--profile <clone>/profile.toml`. Only when the App exists, the file carries `[direction] repo` (the
  workspace) and `profile = "profile.toml"`, so a merge to the workspace deploys itself from then on.
- `waivers.toml` is owner-only, like `standards/`. `gardening.toml` is too: the engine reads it from the workspace's default branch, and
  without it gardening has no targets (see
  [architecture.md](architecture.md#gardening-targets)).
- `projects/<name>/` is a project's directory, on branch `factory/<name>`, with one pull request per project. A
  repository whose root has a `projects/` directory is a *workspace*, and a project there works in
  `projects/<name>/`.
- Issues and a Projects v2 board are the backlog.

### Where a project works

The layout is chosen per launch repository, and the gates, the scope check and the session's instructions all
follow it:

| Layout | When | The project is |
|---|---|---|
| `workspace` | the repository's root has a `projects/` directory | `projects/<name>/` |
| `sandbox` | the `[repo] project` repository, shared by several projects, without `projects/` | `<name>/` |
| `root` | any other repository | the whole repository |

The scope check judges writes against the project's directory. The code and the example profile still call
`[repo] project` the sandbox; with the workspace template it is the workspace.

#### A launch that works in another project's folder

A launch's name is its identity (harness directory, branch `factory/<name>`, base commit, commit identity). In a
workspace the gates judge `projects/<name>/` by default. A launch that edits an imported folder under another
name, such as `fix-x` working in `projects/engine/`, says so with a line in the brief it is launched with
(a list item or bold key works too; only the first word after the key is read):

    Project folder: projects/engine/

Every launch carries its brief, so the line works for direct, factory and queued launches alike. The value
must be one directory name under `projects/` (no `/`, `.`, `..` or absolute path), or the launch is refused
naming it. It comes from the launch only: a `Project folder:` line
added to `.claude-flyte/BRIEF.md` in the working tree changes nothing. Scope, fail-to-pass, `checks.sh`, the
uncommitted-work check and `promote.toml` protection then all use `projects/engine/`; everything keyed by the
name stays on the name.

Without a `Project folder:` line, a launch is refused when `projects/<name>/` does not exist and the brief's `May edit:` paths name
another existing `projects/<x>/`. The error names both folders: add `Project folder: projects/<x>/` to the
brief, or `Project folder: projects/<name>/` if this really is a new project.
A routed folder that does not exist in the workspace is refused too, unless it is the launch's own name.

A workspace project is judged against its start tree: the newest default-branch (`origin/HEAD`) commit its
branch holds (`git merge-base HEAD origin/HEAD`), narrowed to its folder, so debt it inherited passes and what it
adds or grows fails. Everything not on the default branch is the project's, a merge commit it authored included,
so a merge cannot carry new code into the start, and `scope` judges every path that differs from it. (Without
`origin/HEAD`, the start is HEAD with the project's own commits reverse-applied, as in the root layout.)

When the operator's `promote.py import` commit (`promote.toml`'s first commit) is only on the project's branch,
the start's project folder takes that import's files, written as stored (its `.gitattributes` does not filter
them), beside what the default branch's copy of that folder already holds, which they never replace; so
upstream's files are not the project's new code. The import is taken whole only if the base the supervisor
recorded at creation, in its own process, contains it (never a commit made later) and no agent identity authored
it; after a supervisor restart only the session-writable `base-commit` file is left, so nothing is taken whole
and the import is verified as below.

Any other import, a session's own included (the first commit of `promote.toml`, or of a `vendored.toml` whose
tree is the whole folder, `.` or `projects/<name>`), gives the start only its files byte-identical to the
upstream commit that file names, read as a vendored tree's upstream is
([standards](standards.md#vendored-upstream-trees)): fetched over `https` and in the history of that
repository's default branch. The import tool's own files at the folder's top (`promote.toml`, `checks.sh`,
`SUMMARY.md`) come in with it only when no agent identity authored the import and, while the supervisor's recorded
base is intact, only if that base contains it, which is the whole-import case above. So with the record intact a
verified import never brings them, and a `promote.toml` committed after the base stays the project's whatever mail
it carries: `scope` blocks it. After a supervisor restart the record is gone, so an operator's branch-only import
verified against its upstream keeps them (`promote.toml` must still match the import). In that state a commit
under a faked operator mail together with a rewritten `base-commit` file can bring its own `promote.toml` in: that
case is as trustworthy as that file, the known limit `_waivers.py` already documents. A whole-folder
`vendored.toml` tree is read by this start rule only, to name the upstream commit to verify against; the
vendored-tree rule still reports it as not a sub-tree.

The `scope` record names the commits measured from, or why an import was not used. A non-merge commit since
the base that is neither attributed to the launch nor on the default branch (`origin/HEAD`) fails
`attribution`, so a commit made under another `--author` cannot pass the project's additions off as debt.

Lanes started from a project folder attach to the run through `AGENT_FACTORY_RUN_CONTEXT`, which the engine sets
to the workspace root's `.flyte` (the directory or its `run_context.json`), so a project needs no `.flyte` of its
own. When the variable is unset, or names no context, `attach` looks in the working directory and then its
ancestors. Finding none, it fails naming the variable's state and every path it searched, and the lane runs
locally with that message.

### Credentials

`[secrets] deploy_key` is the deploy key of the `[repo] project` repository. `[repo_keys]` adds one deploy key per
further `[repo]` entry where the GitHub App is not installed. A repository the App is installed on is read with a
contents-read installation token the supervisor mints, so no key is made by hand. Every slot mounts all of them, but
a session installs only the key of the repository it was launched on and the rest are dropped from its environment,
so a session holds one key and can push only to that repository.

A session's checkout also carries a `pre-push` hook (`_push_guard.install`) that refuses a push to any URL other than
the launch repository's. It checks the URL only, not the branch. The key is the limit; the hook catches mistakes
before they reach it.

A repository delivered to by promotion needs no key: the session works on the imported copy in the workspace, and
the operator pushes ([Promotion](#promotion)). That includes the engine.

The factory does not merge its own work. The one exception is opt-in: with `[loop] merge = "on"` the loop task merges
its own pull request on its `[loop] repo` repository once review has passed and its checks hold
([profiles/README.md](../profiles/README.md#loop)). It acts through the GitHub App, so it needs the App installed on
that repository and does not apply to a public engine repository the owner cannot install an App on; there the
owner merges the promotion pull request.

## Promotion

Another repository is a project like any other. Nothing in the factory pushes to it: the chat operator does, on your
word, from a shell with your git and `gh` credentials, by running `ops/promote.py` (the factory MCP has no promote
task). A shell user runs the same commands. They run from an engine checkout (`PYTHONPATH=src`); `projects/<name>`
stands for the project's path in your workspace checkout, such as `../agent-workspace/projects/<name>`.

1. **Import.** `python ops/promote.py import <target-url> projects/<name> [--ref BRANCH] [--path DIR] [--new]` writes
   the target's files into the project, records the repository and the commit they came from in
   `projects/<name>/promote.toml`, and makes one commit in the workspace. `--new` is for a repository that does not
   exist yet. The operator owns `promote.toml`; the gate rejects a project change to it.
2. **Work.** A project whose brief has a `Delivers to:` line edits that tree and commits it like any other project.
   The pre-review gate runs the target's own CI commands and the changed-line coverage check on it
   (`factory:target-ci`, `factory:target-coverage`). A changed Python line the tests do not cover passes when it is
   listed in the project's `SUMMARY.md`.
3. **Review.** As for every project. Only committed, reviewed content is delivered.
4. **Deliver.** `python ops/promote.py deliver projects/<name> <branch> [--to URL] [--fork URL] [--title TEXT]
   [--base SHA] [--dco | --no-dco] [--fixes OWNER/REPO#N] [--dry-run] [--replace]
   [--checks off|advisory|required]` replays the project's changes onto the
   target's current default branch as one commit by the operator's git identity (`user.name` and `user.email`
   must be set), scans what the commit adds (`ops/scan_public.py`), and pushes the branch and opens or updates a
   pull request with `SUMMARY.md` as its body. Changes made upstream since the import are kept. A conflict, an
   empty result or a scan finding stops it before anything is pushed: a push to a public repository cannot be
   undone, so the scan always blocks. The target's CI and changed-line coverage already ran in the pre-review gate
   and run again as the pull request's CI, so `deliver` does not rerun them by default (`--checks off`).
   `--checks advisory` reruns them and writes what failed or could not run into the pull request body;
   `--checks required` refuses on anything but a pass. A rerun measures a target with no coverage command of its
   own with pytest-cov (a uv project in its own environment), and skips the workflow's machine-setup steps
   (`sudo`, `apt`, `npm install -g` and the like), so what they install must already be on your machine. Without
   `gh` the report prints the pull request body to open it by hand. `--fork` pushes to your fork instead,
   `--fixes` adds a `Fixes OWNER/REPO#N` line to the body for each public issue the change resolves (the
   workspace item records that link), and `--dry-run` does everything but push. A branch that already exists
   on the target (or the fork) is replaced only when its tip is your earlier promotion; otherwise `deliver`
   refuses, and `--replace` overwrites it. A `--new` project is delivered once: after that, re-import it.
5. **Merge.** The owner merges, or tells the operator to. `deliver` never merges. After the merge the operator
   confirms each fixed public issue closed and marks the workspace item Done with a link to the merged pull request.

If the target asks for a DCO (a `.github/dco.yml`, a `DCO` file, or CONTRIBUTING or a workflow mentioning it),
`deliver` adds `Signed-off-by` with the operator's identity at promotion; `--dco` and `--no-dco` override the
detection, and `--no-dco` on such a target prints a warning that its DCO check will fail. The sign-off is the human's personal certification, made at promotion with the operator's (the owner's)
git identity; the commits a session made in the sandbox stay attributed to the factory, and factory sessions never
sign off. A CLA or a commit format in the target's rules is printed as a notice.

### Deploying the factory

Deploying is a setting, the same for every owner: with `[deploy] auto`, the deployer ships the default branch of
`[deploy] repo` (default `[repo] factory`) once its CI has passed
([profiles/README.md](../profiles/README.md#schema)). Developing the engine is a workspace project like any
other, imported with `ops/promote.py import` and promoted as above; it needs no App, deploy key or webhook on the
repository that is deployed. A modified engine copy in the workspace is deployed only for testing and benchmarking;
production, the owner's own factory included, runs what was merged. Without the App on the deployed repository the
deployer reads it anonymously. Its deploy lock is a lease on the state branch of the workspace
([state-store.md](state-store.md)), written with the workspace deploy key: neither `[direction]` nor a repository
the App can write is needed for it.

The owner's own factory deploys from `unionai-oss/flyte-agent-factory` main, as every other owner's does from the
public repository or a fork. The alternative, deploying from a private engine repository, would let a change reach
production before it is public, with the factory's App on the deployed repository and no public CI. The cost of the
public repository is latency: a change reaches production only after its promotion pull request passes the
public CI and is merged, and the deployer reads that repository without the App, so it polls (`[deploy] webhook`
off) and keeps its lock on the state branch. In exchange the owner runs exactly the documented setup, and every
deployed commit is public, reviewed and green.

## The public template

The template is public, so everything in it, history included, passes `ops/scan_public.py`: gitleaks over the
contents and the history, and the personal-literal guard of `tests/test_profile.py`. A real profile never enters it.

## No standards in the engine

The engine has no standards of its own and no fallback copy of them. The gate and the session read the workspace's
`standards/` first, then the deployment profile's `[standards] dir`; with neither there are no rules, and both the
deploy and the gate report say so. The template's `standards/` is the worked example ([standards.md](standards.md)).

## Migrating a sandbox

`ops/migrate_workspace.py --sandbox <path> --direction <path> --out <path>` builds a workspace from the template, the
sandbox's projects, the direction repository's profile, briefs and history, and writes `MIGRATION.md` with the manual
steps. The rename to `agent-workspace` is last, together with the profile's allowed-repo string.

## What lives in the workspace, not the engine

Owner content is kept out of the engine repository. In the workspace:

- `history/operations-log.md`: the runs launched and nursed, with their run ids.
- `briefs/`: the owner's project briefs. Every brief that is not `briefs/demo-*.md`,
  `briefs/smoke-wordstats.md` or `briefs/factory-gardening.md` lives in the owner's workspace.

The engine keeps only those product briefs. `ops/migrate_workspace.py` carries every other brief from the
direction checkout into a new workspace.
