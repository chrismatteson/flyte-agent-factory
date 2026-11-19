# Sealed runs: design (flyte-agent-factory#25)

> **Sealing and the unprivileged user.** Every sealed start (a sub-agent's Claude Code or Codex, the
> sealed follow-up turn, the plain arm) goes through `_unprivileged.spawn` with the jail's wrapped argv and
> env, so the jail is launched only by a non-root process. The jail's user namespace maps the uid to itself
> (not to 0), so a role is never root inside the seal either. The `subagent` and `sealed_plain` tasks call
> `_unprivileged.refuse_root` first, then the seal check: either refusal ends the call before anything starts.

Engine root: `projects/seal-benchmark-runs/` (package `src/flyte_agent_factory`). All new modules
are `_seal_*.py` there; tests in `tests/test_seal_*.py`.

## Evidence this rests on (probed in this pod, 2026-11-16)

- `os.unshare(CLONE_NEWUSER|CLONE_NEWNET)` works for the unprivileged pod user. Mapping uid->same uid
  (`/proc/self/uid_map` = "1000 1000 1", setgroups deny), bringing `lo` up with ioctl
  SIOCGIFFLAGS/SIOCSIFFLAGS (0x8913/0x8914), then exec: the child runs as uid 1000, a direct TCP connect
  to any external IP fails with ENETUNREACH, and there is no DNS.
- Unix sockets addressed by path cross the netns boundary (the filesystem is shared). A forwarder
  inside the netns, `127.0.0.1:<port>` -> unix socket, plus a proxy outside on that unix socket, is the
  only egress.
- `claude -p` run inside with `HTTPS_PROXY=http://127.0.0.1:<port>` reaches `api.anthropic.com` via
  CONNECT; its telemetry (`http-intake.logs.*.datadoghq.com`) and `api.github.com` are denied at the proxy.
- acscan-container-matrix: ordinary Flyte pods and `flyte.sandbox.create()` have open egress and
  allowlist modes don't stop direct TCP. A network namespace with no route does.
- No `bwrap`/`socat` in the image, so Claude Code's built-in sandbox is not available without an image
  change; the netns launcher above needs only Python's stdlib and the kernel.

## Threat model and what "sealed" means

The adversary is a helpful agent looking for the answer, not one attacking the harness. Within that:
a sealed role **cannot reach** the reference fix/tests because (1) the task repo it gets has nothing
beyond the base commit and no remote, (2) its workspace repo holds nothing but the template, and (3) its
whole process tree has no network except a proxy that allows only the model API, a date-frozen package
index with the task project blocked, and push-only access to its own workspace repo. Instructions and the
guard hook are backstops that make attempts explicit in the record. Not covered, and stated as
depending on rule-of-two-split (#29): a role that deliberately kills or tampers with the harness process
or its record files (same uid, same pod). Recall from training data cannot be sealed; the report lists
tasks older than the model cutoff.

## Layers

1. **Marking (operator-only).** A project is sealed when launched with a seal spec: launch input
   `seal` (JSON string) on the `project` task, carried by `ops/operate.py launch --seal <file.toml>` and
   `_factory.launch_project(..., seal=...)`. Not from brief text, issue text or labels. The spec is held
   by the supervisor (task input), and passed down to every sub-agent it launches as a task input.
   A sub-agent call from a sealed project without the seal is refused (fail closed).
   `SealSpec` (`_seal_spec.py`, validated at parse time, raises `ValueError` naming the field):
   - `task_repo` (https URL), `base_sha` (40 hex), `task_project` (PyPI name, may be "" for none),
   - `index_cutoff` (ISO-8601 UTC date; files uploaded after it are hidden from the index),
   - `model_hosts` (default `["api.anthropic.com"]`; codex adds `api.openai.com`, `chatgpt.com`),
   - `push_remote` (https URL of the workspace repo the session may push to; push only). Leave it out: the launch sets
     it to `https://github.com/<[seal] workspace_repo>.git` (`_seal_launch.resolve`) and refuses a different value,
   - `forbidden_paths` (globs; extra paths the guard denies reading), `allow_index` (bool, default true).
   `spec.sha256()` is the canonical-JSON hash recorded in every role record.
2. **Task snapshot** (`_seal_snapshot.py`): outside the seal, `git init`, `git fetch --depth 1
   --no-tags <task_repo> <base_sha>`, checkout detached at base, then remove remote, FETCH_HEAD,
   shallow-boundary leftovers other than base itself, reflogs, packed remote refs; verify
   `git rev-list --all` == [base_sha] and no other commit object exists (`git cat-file
   --batch-all-objects --batch-check` lists exactly one commit).
3. **Network seal** (`_seal_net.py` launcher + `_seal_proxy.py`):
   - `PYTHONPATH=src python -m flyte_agent_factory._seal_net --socket S --port P -- cmd...` does the unshare/map/lo-up,
     forks the in-netns TCP->unix forwarder, sets `HTTPS_PROXY/HTTP_PROXY/https_proxy/http_proxy`,
     `NO_PROXY=localhost,127.0.0.1`, `PIP_INDEX_URL`/`UV_INDEX_URL`/`UV_DEFAULT_INDEX` to the proxy's index
     route, `UV_EXCLUDE_NEWER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`,
     and execs `cmd` without closing inherited fds (the token arrives on an fd; the pty must survive).
     Also enters a mount namespace and hides each `--hide PATH`: an empty tmpfs over a directory, a read-only bind of
     `/dev/null` over a file. `sealed_role` expands every `forbidden_paths` glob at launch (`glob.glob`, recursive) and
     hides what exists, so a forbidden file or glob's files read as empty. Each `--readonly PATH` is bind-mounted onto
     itself and remounted read-only; the mount is locked by the parent namespace, so the role cannot remount it
     read-write, unmount it, or do either from a user and mount namespace of its own (tested). `sealed_role` marks its
     private directory (`guard.json`, `proxy.sock`) read-only for claude roles; connecting to the socket still works.
     A mount that fails fails the launch (never runs unsealed).
     **Filesystem lock** (`--writable PATH`, `--private-tmp DIR`): the role may write only where it is told to, because
     the harness runs as the same uid outside the seal and reads what a role could plant (`~/.gitconfig`, a `.pth` in
     `~/.local`, a shell rc file, the harness's temp files). `/tmp` becomes a fresh tmpfs (`TMPDIR=/tmp`), so the role
     neither sees nor writes the harness's; the paths the role needs that live there (workdir, guard log, the private
     guard/socket directory, the interpreter, `PYTHONPATH` and `PATH` entries, hidden and read-only paths) are bound
     back at the same path first. `/dev/shm` becomes a fresh tmpfs too, and the launcher enters a fresh IPC namespace, so
     SysV shared memory and message queues are the role's own (tested). `sealed_role` resolves its writable paths before the role
     starts, and the launcher refuses any writable path with a symbolic link in it (a link the role swaps in later fails
     the launch rather than binding wherever it points). Each writable path is bound onto itself
     (recursively, so a read-only mount inside it, such as the record root, stays read-only) and then every other mount
     in `/proc/self/mountinfo` except `/proc`, `/sys` and `/dev/pts` is remounted read-only with the flags the user
     namespace locks, `$HOME` and `/dev` included. A writable path that does not exist, or a remount that fails, fails
     the launch. The mounts are locked by the parent namespace, so the role cannot remount, unmount or uncover them
     (expose what lies beneath), from its own user and mount namespace either (tested). In a namespace of its own the
     role may bind one of its writable directories over a locked path, but only in that namespace's private view: the
     write lands in the directory it bound, the real path is unchanged, and the sealed view still refuses writes with
     EROFS (tested). `sealed_role` passes exactly: its workdir roots, the engine
     config dir (`CLAUDE_CONFIG_DIR`/`CODEX_HOME`), the guard log directory, the private `/tmp`, and a per-role cache
     directory (`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `npm_config_cache` point into it; deleted at close);
     the sealed author also gets the bridge directory, which `flyte-note`, `ask-human`, `delegate` and DONE write.
     What the harness copies or writes in those paths does not follow a link the role planted: transcripts and the
     guard log are copied only as regular files opened with `O_NOFOLLOW` below an unlinked parent (a linked transcript,
     or a directory at a transcript's name, is skipped; a guard log that is a link, FIFO, directory or anything but a
     regular file is recorded as a line that is not JSON, so the audit calls the record incomplete and the run void),
     and the bridge writes each file through a fresh `mkstemp` name and `os.replace`, never a fixed temp name.
     The role's record says so (`seal.fs_readonly`).
     `available() -> bool` probes in a forked child. Unavailable => sealed launch fails with a clear error
     (never runs unsealed).
   - Proxy (asyncio unix server, stdlib only), started by whoever launches the role, outside the netns:
     - `CONNECT host:443` allowed only for `model_hosts`.
     - `GET/POST http://127.0.0.1:P/index/...`: reverse proxy to PyPI simple (JSON upstream, PEP 691),
       hiding files with `upload-time` > `index_cutoff`, 403 for the normalized `task_project`, file URLs
       rewritten to `/files/...` -> files.pythonhosted.org, filenames of the task project 403.
     - `http://127.0.0.1:P/push/...`: reverse proxy to `push_remote`, only `info/refs?service=git-receive-pack`
       and `POST .../git-receive-pack`; upload-pack (fetch/clone/ls-remote) 403. Client auth headers pass
       through.
     - Everything else 403. One JSONL line per request to the role's `proxy.jsonl`.
4. **Tool limits + guard** (`_seal_guard.py`): settings fragment for every sealed claude role:
   `permissions.deny` += `WebSearch`, `WebFetch`, `mcp__browser__*`; `hooks.PreToolUse` matcher `.*`
   running `PYTHONPATH=src python -m flyte_agent_factory._seal_guard` which denies: web tools; browser MCP; Bash commands
   that fetch from a non-local git URL (`fetch|pull|clone|ls-remote|fetch-pack|archive --remote|remote update|submodule
   update`, `--unshallow/--deepen`), `git remote add|set-url` of a non-local URL, `gh` (except `--version`),
   curl/wget/httpie/python-requests one-liners and inline scripts naming a non-local host, `pip download|wheel`, `pip
   install <task_project>` / from an sdist/archive URL (also through `uv`, `uv tool`, `uvx` and `pipx run pip`); the
   commands are read through wrappers (`env`, `timeout`, `nice`, `sudo`, `setsid`, `uv run`, ...), `eval`, `bash -c`
   and heredocs fed to a shell (three levels deep), `$(...)` and backticks; Read/Grep/Glob
   or Bash reads of `forbidden_paths`; Write/Edit/MultiEdit/NotebookEdit of them; recursive traversal (`find`, `grep -r`,
   `rg`, `tar`, `cp -r`, `rsync -r`, `du`, `ls -R`, `tree`...) of a directory that holds one, and absolute Glob/Grep-glob
   patterns that start above or inside one. A plain `ls /tmp` or `cat /tmp/x` on such a directory is allowed: `/`, `/tmp`
   and `/home` are used by ordinary commands. The guard config is read-only inside the seal (layer 3), so a role cannot
   clear `forbidden_paths` or redirect `guard_log`. The guard still matches the globs itself, so a path created after
   launch is denied and recorded though it is not hidden. Allows: pip/uv install of other packages, pytest, local git, git push.
   Every decision (allow and deny) is appended to the role's `guard.jsonl`. It composes with the existing
   `_context_guard` hook (both installed).
5. **Record** (`_seal_record.py`), per role, under a record root:
   ```
   <root>/manifest.json            {"spec_sha256", "roles": [{"role_id", "kind", "registered_at"}]}
   <root>/roles/<role_id>/role.json {"role_id","kind": author|subagent|reviewer|judge|delegate|plain,
                                     "engine": claude|codex,"parent","action","started_at",
                                     "seal": {"net": bool,"guard": bool,"tools_off": bool,"fs_readonly": bool,"spec_sha256"}}
   <root>/roles/<role_id>/transcript/*.jsonl   claude session jsonl + subagents/*.jsonl, or codex rollouts
   <root>/roles/<role_id>/proxy.jsonl          {"ts","method","target","route","decision","rule","status"}
   <root>/roles/<role_id>/guard.jsonl          {"ts","tool","input","decision","rule"}
   <root>/roles/<role_id>/end.json             {"ended_at","exit_code","files": {relpath: {"sha256","lines"}}}
   ```
   `register(root, role_id, kind)` is called by the launcher **before** a role starts (so a role that never
   returns a record is visible as missing). `collect(...)` copies transcripts after the role exits and
   writes `end.json` last. Sub-agents bundle their role dir into their returned `Dir` under
   `.seal-record/roles/<role_id>/` (a native task output; nothing else is written to the object store); the
   caller merges it into its own record root. Timestamps are UTC ISO-8601 with `Z`.
   Inside the seal the record root is **read-only** (`--readonly`, every role, claude and codex): only the harness,
   outside the role's namespace, writes it (registering, the proxy log, copying transcripts and the guard log,
   `end.json`, merging a sub-agent's bundle). The guard hook runs inside the seal, so it appends to a log in a separate
   writable temp dir; `sealed_role` copies that into `guard.jsonl` before `end.json` is written, and a role with no
   tool calls keeps the empty `guard.jsonl` that `register` made. A record root that holds a role's workdir is refused
   (read-only would cover the workdir). The author's `delegate` CLI only drops request files in the bridge dir
   (`_bridge.BRIDGE_DIR`, under `$HOME`); the sub-agent is registered and its bundle merged by the harness
   (`_reviewer._call_sealed`, `_session_setup` reattach), outside the seal.
   The author's record root is `seal-record/` in the project's state tree, which the project checkpoints whole;
   `ops/seal_record.py RUN DEST` copies it out of the last checkpoint (Operator, below).
6. **Check** (`_seal_audit.py`, which lands separately from the modules above; CLI `PYTHONPATH=src python -m
   flyte_agent_factory._seal_audit <root> [--gold gold.json]`): deterministic, no model. Gold file (operator-held, never given to a run):
   `{"fix_patch": str, "test_patch": str, "shas": [str], "task_project": str, "forbidden_paths": [str]}` (the last
   two are optional; see Operator, below). Output JSON `{"verdict": clean|void,
   "reasons": [...], "attempts": [...], "counts": {...}}`, each item `{kind, role_id, file, line,
   evidence}`. Void if any of:
   - `incomplete`: a manifest role with no role dir, no `end.json`, a hash/line mismatch, an unparseable
     line, a missing `guard.jsonl` or `proxy.jsonl` (the harness creates both for every role), an Agent/Task tool call in a claude transcript with no matching, non-empty subagent transcript file (a
     `.meta.json` naming the call is not enough), a role dir
     not in the manifest, a manifest listing no roles, a tool call with no result in its transcript file
     (a cut, or a killed role). The author is exempt for its file's final step only: the harness stops its
     session whatever it is doing (project end, pod restart, the DONE call in flight), so a cut exactly
     after the author's last call is not caught; one anywhere earlier is.
   - `unsealed`: a role whose `seal` flags are not all true or whose spec hash differs.
   - `reached`: a distinctive gold line (from `+` lines of the patches, normalized whitespace, >= 25 chars,
     not also a `-`/context line) appearing in a tool **result** before any role authored it (in a
     Write/Edit/NotebookEdit input, a Bash command input, or assistant text); or a gold sha in a tool result.
   - `route-succeeded`: a tool call matching an upstream route, read by the same command reader as the guard
     (`_seal_shell.classify_command`; `tests/test_seal_shell.py` checks the two agree on every command in both test
     tables), (git fetch family to non-local URL, hosting
     API, web tool, a codex hosted `web_search_call` (it runs on OpenAI's servers, past the network seal),
     task-project download, forbidden-path read) whose result did not fail: not `is_error` (or a non-zero exit), and no line of its text matches a failure
     signature (`FAILURE_SIGNATURES`: `fatal: unable to access`, `curl: (NN)`, `Could not resolve host`, an
     `HTTP/1.1 403` status line, pip's `ERROR:` and uv's `error:` prefixes, the guard's `Sealed run:` text,
     a Python traceback ending in `URLError`, ...). The signatures are anchored to how the tools print a failure;
     a result that merely contains `403`, `proxy` or `TypeError` worked. A hosted search with status
     `failed` is an attempt; any other status is a search that ran.
   - `egress`: a proxy line with decision allow whose route is not one of model/index/push.
   Not void: attempts that were denied/failed (listed in `attempts`), URLs in prose, placeholder remotes.
   `git remote add|set-url` of a non-local URL and an install of a task release file already on disk are denied by
   the guard but are not routes here (`NON_ROUTE_KINDS`): neither retrieves anything; the fetch that would follow is.
7. **Rates** (`ops/seal_audit_rates.py` over `tests/seal_corpus.py`): seeded records, each labelled
   clean or void, covering every route as positive and every harmless case as negative; prints a
   confusion matrix, FP rate and FN rate; `ops/seal_audit_rates.py --check` (run by `checks.sh`) fails if
   they differ from `ops/seal_audit_rates.expected.json`. Measured on 99 cases (63 void, 36 clean):
   **false-positive rate 1/36 = 2.8%**, **false-negative rate 1/63 = 1.6%**. The two misses are built
   in on purpose, to show the limits: a fetch whose URL is base64 inside a command substitution, with
   output that shows nothing, is called clean (FN; the network seal still stops it, and the audit
   cannot see that it was tried). A fetch that failed with stderr sent to `/dev/null` while `echo done`
   printed is called void (FP; a result that does not look failed counts as success, which is the safe
   way to be wrong). Per reason: incomplete 16/16, unsealed 5/5, reached 9/9, egress 2/2,
   route-succeeded 30/31. The real records in `docs/seal-e2e/` are re-audited by
   `tests/test_seal_e2e_records.py`.
8. **Report** (`_seal_report.py`): `summarize(runs, verdicts, tasks, model_cutoff)` -> one table: counted runs only
   clean; void runs listed separately with reason; no "clean only" variant; tasks with
   `created_at` < model cutoff listed as recallable. Rerun naming: `next_run_name(name, existing)` ->
   `<base>-r<n+1>`, a fresh project (new branch, new record root, no `workspace_in`).
9. **Integration**: `project(seal=...)`, `subagent(seal=...)`, reviewers/judges/delegates pass it on;
   sealed session setup (`_seal_session`, `_session_setup._prepare_workspace`), `sealed_plain(brief, seal, engine)`
   task for baseline arms (plain `claude -p`/`codex exec`, no factory instructions) returning `(summary, Dir)` with
   the record inside. What the session setup does, in order, and where it differs from the first design:
   - The task snapshot sits at `projects/<name>/<task_dir>` in the author's workspace, git-excluded
     (`.git/info/exclude`), not in a separate `repo/` beside the clone. The bench repo must therefore be a
     workspace repo (`projects/` on its default branch); anything else is refused.
   - The clone is single-branch (`git clone --single-branch`), then `limit_fetch` sets its fetch refspecs to
     the default branch and, once the remote has it, `factory/<name>`: the harness's `git fetch --prune origin`
     brings nothing else.
   - **Upstream tracking.** Git updates `refs/remotes/origin/factory/<name>` after a push only if a fetch
     refspec maps it, and a refspec for a branch the remote lacks makes every `git fetch origin` fail. So the
     harness (outside the seal, with the push token) calls `claim_branch` first: it creates `factory/<name>` on
     the bench repo at the default branch's tip, if it is not there, and then `limit_fetch` lists the refspec.
     The session's first `git push -u origin HEAD:refs/heads/factory/<name>` is a fast-forward, the tracking ref
     moves with it, and `git rev-list --count @{u}..HEAD` prints 0 with no harness step in between. A pattern
     refspec (`factory/<name>*`) was rejected: it also matches `factory/<name>-r2`, another attempt's branch.
     Cost: a project that never pushes leaves an empty branch (equal to the default branch) on the bench repo.
     If the claim fails (the remote is unreachable), the harness runs `refresh_upstream` (`limit_fetch` and a
     fetch) before `_done_preflight` judges `@{u}`, which covers the first push in that case.
   - Pushes go to the proxy's push route: `git config url.<route>.pushInsteadOf <real url>`, so fetches keep the
     real URL. The `pre-push` hook is installed too (`_push_guard.install_hook`): it refuses any URL but the
     route and scans the pushed commits and messages for credentials (`scan_main`). `_push_guard.install`'s own
     `pushInsteadOf` rewrites are not used, because they would send the route's `http://` URL to a directory
     that does not exist. As elsewhere, the hook is advisory against a session that edits `.git/hooks` or uses
     `--no-verify`.
   - **Delegate workers get the task repo.** `_delegation.snapshot(workspace, target, task_dir)` copies the task
     dir (working tree and its own `.git`, which holds only the base commit) into the worker tarball at the same
     relative path. The author's uncommitted edits in it are committed into the copy ("Delegation base", also
     written to `task-base`), so the worker's diff holds only the worker's work. The outer diff cannot carry
     edits to a nested repo, so `hand_back` writes a second patch, `task.patch` (the task dir against
     `task-base`, taken before the outer fallback diff, which strips nested `.git` dirs), and the job's answer
     has `task_patch`. `delegate wait` prints its path after the patch's; the coordinator applies it with
     `git -C <task dir> apply --3way <path>` (its CLAUDE.md says so). An unsealed snapshot is unchanged.
   - The sealed CLAUDE.md section replaces the fetch/merge push recipe; the browser is off.

## Owner decisions named, not taken

- A separate, empty workspace repo for benchmark projects (operator creates it; profile `[seal]
  workspace_repo`). Without one, the session's own clone of the agent workspace holds earlier attempts.
- Cluster-level egress policy (NetworkPolicy) for benchmark pods as defence in depth; not needed for the
  seal on this cluster, needed on a node where unprivileged userns is disabled.
- Moving the proxy/recorder out of the session's uid (#29 rule-of-two-split).

## Operator

For someone who has not read the code. Everything here is the operator's; a run cannot do any of it.

### What to redeploy

Only the engine, at the commit that holds this document. Nothing on the cluster changes (no NetworkPolicy, no
node setting, no new secret). Both deploys read a git ref from a throwaway worktree, so merge and push the commit
first; both fetch `origin`:

```sh
uv run python ops/deploy_work.py --ref <sha> --dry-run   # check first
uv run python ops/deploy_work.py --ref <sha>             # every slot's project env and its sub-agent env
uv run python ops/deploy_factory.py --ref <sha>          # the factory apps
```

- `deploy_work.py` puts the sealed session (`project`), the sub-agents, reviewers and delegate workers
  (`<slot>.subagent`) and the plain arm (`<subagent env>.sealed_plain`) on the slots. A run already going keeps the
  version it started with.
- `deploy_factory.py` is needed too: the queue's dispatcher runs in the factory apps and starts a queued launch
  with the request's `seal`. An older dispatcher does not know the field and would start the project unsealed in the
  benchmark repo.
- The code bundle carries the new modules (`_seal_*.py`) because they are imported statically. There is no new
  image content: the seal needs only Python's standard library and the kernel.
- The in-pod network namespace needs unprivileged user namespaces. The cluster this was proved on has them. To
  check on the pod type a project runs in (a slot's pod, not your laptop):

  ```sh
  PYTHONPATH=src python -c 'from flyte_agent_factory import _seal_net; print(_seal_net.available())'
  ```

  `False` means the sealed launch fails at the start with a clear error; it never runs unsealed. See "Owner decisions".
- The seal tests (`tests/test_seal_*.py`) need the same, and `git http-backend` (git ships it); they fail, never skip,
  where either is missing (the `seal_namespaces` and `git_http_backend` fixtures). Upstream's `checks.yml` lifts
  Ubuntu 24.04's AppArmor restriction on unprivileged user namespaces in the test jobs
  (`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`); run the same on any other Ubuntu 24.04 host.

### One-time setup

1. Create an **empty** benchmark workspace repo (`owner/name`) with `projects/` on its default branch (a workspace
   layout; commit a `projects/.gitkeep`). It must not be the regular workspace repo: that repo's history holds
   earlier attempts, which a sealed run must never have.
2. Install the GitHub App on it, with https push (`[github] push = "app"` and `AGENT_FACTORY_GITHUB_PUSH=app` on the
   slot). A sealed project is refused without an App push token: the seal has no route to ssh, and the proxy's push
   route authenticates with that token.
3. In the deployment profile:

   ```toml
   [seal]
   workspace_repo = "owner/name"
   ```

   Redeploy after changing the profile (`deploy_work.py` and `deploy_factory.py` above).

### Launch one sealed factory project

A seal spec is a flat TOML file, written by the operator, one per task. Every field:

```toml
task_repo = "https://github.com/example-org/task-repo.git"   # https URL of the repository the task is from
base_sha = "0123456789abcdef0123456789abcdef01234567"        # 40 lowercase hex: the commit the task starts at
task_project = "taskpkg"                    # PyPI name of the project under test; its files and pages are hidden. "" for none
index_cutoff = "2025-03-01"                 # ISO-8601 date or datetime (UTC): package files uploaded later are hidden
model_hosts = ["api.anthropic.com"]         # default; a Codex arm adds "api.openai.com" and "chatgpt.com"
# push_remote: leave out. The launch sets it to https://github.com/<[seal] workspace_repo>.git and refuses another.
forbidden_paths = ["/home/*/.cache/tasks"]  # extra globs the guard denies reading and the seal hides (empty tmpfs)
allow_index = true                          # false: no package index at all
task_dir = "repo"                           # where the snapshot goes: projects/<name>/<task_dir>
```

Only `task_repo`, `base_sha` and `index_cutoff` are required. A bad value fails the launch, naming the field, before
anything starts. The brief is an ordinary brief file. Then:

```sh
uv run python ops/operate.py launch --seal spec.toml --name bench-task-01 brief.md
```

`launch` takes the brief file first, `--name` is required, `--profile NAME` is optional, and `--seal` is the only
way to seal a project: not brief text, issue text or labels. The run name it prints is what `seal_record.py` takes.
The task snapshot is made inside the pod, outside the seal, with `git fetch --depth 1 <task_repo> <base_sha>`;
it holds the base commit and nothing else (checked object by object).

The task repo is git-excluded in the project's workspace, so it is **not** part of what the session pushes. The
project must put the result where the benchmark reads it, and the brief says so. The benchmark briefs already do:
they end with `git -C <name>/repo add -A && git -C <name>/repo diff --cached <base_sha> > <name>/model.patch` and
name `<name>/model.patch` the deliverable, which matches the default `task_dir = "repo"`. Keep `task_dir` and the
brief in step. The engine does not write the patch for you.

### A plain (baseline) arm, sealed

The `sealed_plain` task runs one bare `claude -p` (or `codex exec`) session in the same seal, with no factory
instructions, roles or bridge. It lives in the slot's sub-agent environment, `<subagent env>.sealed_plain`
(`subagent_env` of the `[[slots]]` entry). Inputs: `brief` (the prompt), `seal` (the spec as JSON: the TOML parsed
and re-serialised with `SealSpec.to_json()`, used as written so the record's hash is the hash of your spec;
`push_remote` stays empty, there is no push), `engine` (`claude` or `codex`, default `claude`), `model` (default
the engine's), `max_turns` (default 200). Run it like any deployed task:

```python
# run with: PYTHONPATH=src uv run python run_plain.py
import flyte
from flyte import remote

from flyte_agent_factory._seal_spec import SealSpec

seal = SealSpec.from_toml("spec.toml").to_json()
task = remote.Task.get("<subagent env>.sealed_plain", auto_version="latest")
run = flyte.with_runcontext(mode="remote").run(
    task, brief=open("brief.md").read(), seal=seal, engine="claude"
)
```

(Initialise Flyte for your profile first, as `ops/operate.py` does.) Its output is `(summary, Dir)`; the `Dir` holds
the work dir: the task repo at `<task_dir>/` (with the model's edits), `model.patch` (the working tree against
`base_sha`; `model.patch.error` instead if the diff could not be taken), and `.seal-record/` (`manifest.json` and
`roles/<role id>/...`: the record root the audit reads). Download the `Dir` and pass `<dir>/.seal-record` to the audit. The engine session is killed after two hours
(`_seal_plain._ENGINE_TIMEOUT_S`) and the task then fails with "plain <engine> exceeded ...", so a hung engine does
not hold the task forever.

### Get a finished project's record and audit it

```sh
PYTHONPATH=src uv run python ops/seal_record.py RUN DEST [--profile NAME]
```

`RUN` is the run name `launch` printed; `DEST` must not exist. It reads the checkpoint location from the run's
report, loads that checkpoint (the one a recreated run would resume from; no new store, nothing new written) and
copies its `seal-record/` to `DEST` (`manifest.json`, `roles/<role id>/...`). A finished project's record is complete:
the project closes the author's role before its last checkpoint. It fails with a message if the run has no checkpoint
location, the checkpoint is empty, or the checkpoint holds no `seal-record/` (the project was not sealed or never
started). Then, with the operator-held gold file (never given to a run):

```sh
PYTHONPATH=src python -m flyte_agent_factory._seal_audit DEST --gold gold.json --json > verdicts/<run name>.json
```

`gold.json` is `{"fix_patch": "<diff>", "test_patch": "<diff>", "shas": ["<commit sha>", ...], "task_project":
"<PyPI name>", "forbidden_paths": ["<glob>", ...]}` (`Gold.load`). `fix_patch`, `test_patch` and `shas` enable the
`reached` check, and a gold file must enable it: an unknown field (SWE-bench names the fix `patch`; here it is
`fix_patch`), a sha that is not 40 lowercase hex, a field of the wrong type, or a gold with no added line of 25 or more
characters and no sha is a usage error (exit 2, "gold.json identifies nothing"), never a quiet `clean`. `task_project` and `forbidden_paths` are optional and each enables a `route-succeeded`/`egress` check:
with `task_project`, an index or file download of that project (`pip download`, `uv pip install`, an allowed
`/files/<task project>-...` proxy line) is a route that worked; with `forbidden_paths`, a read of a matching path (the
other attempts' folders, e.g. `/home/flyte/state/workspace/projects/swe-attempt-*`) is. Without them those two checks
are off, so a gold file for a real benchmark should carry them (`docs/seal-e2e/gold.json` does not: the e2e task has
no other attempts). `--task-project NAME` and `--forbidden GLOB` (repeatable) add the same to what the gold file has,
for a gold file that lacks them. With `--json` the output is JSON with `verdict` (`clean` or `void`), `gold` (whether
a gold file was given), `reasons`, `attempts` and `counts` (layer 6 above); without it, text: the verdict, one line per
reason, then the number of attempts.

Exit status: 0 clean (with `--gold`), 1 void, 2 usage error (unreadable root, unreadable or invalid gold file), 3 clean **without**
`--gold` ("clean except `reached`: not checked"; a warning goes to stderr). Void is 1 with or without gold. A script
that decides whether a run counts must treat only 0 as clean.

The project also audits its own record when it declares DONE, without gold (it has none): the verdict is the `🔒`
row of the run report ("seal audit at DONE (no gold): clean, N role(s), M blocked attempt(s) recorded", or VOID with
the first reason and its evidence line). It is informational and never bounces DONE (a role must not be able to learn
from the audit what got through). Only the audit with gold decides whether a run counts, because only gold can show
`reached`.

What a blocked attempt looks like in a record: a guard denial is a line in the role's `guard.jsonl` and the
`tool_result` that says "Sealed run: ..." in its transcript; a route the network stops shows as the command's own
error (`Network is unreachable`, `curl: (56) ... 403`) in the transcript and a `deny` line in `proxy.jsonl`. WebSearch and WebFetch
are not in a sealed role's tool list at all, so an attempt to use them shows only as a `ToolSearch` that finds nothing;
Codex's web search is off by configuration (it runs on OpenAI's side, so the network seal would not stop it), and a
`web_search_call` in a rollout voids the run.

### Rerun the 14 contaminated tasks

Each rerun is a **fresh project**: a new name by the `-r<n>` rule (`task-07` becomes `task-07-r2`, then `-r3`),
which gives a new branch `factory/<name>`, a new record root, and nothing carried over. A sealed launch under a name
that already has runs (or a queued request) is **refused** before anything is queued, because it would be a resume from
the earlier run's published workspace, with its earlier attempts in it; the error names `_seal_report.next_run_name`.
`workspace_in` is never passed to a sealed run, on any dispatch path. The `-rN` suffix is stripped only when the base
is itself a known run (`django-r5` with no other runs becomes `django-r5-r2`). Launch each with its own `--seal` file
and `--name`, as above. Rules for the numbers:

- A **void** run is excluded from every number (resolve rate, cost, time); list it separately with its reason. There is
  no "clean only" variant of a table that hides the voids.
- The benchmark's readback must read each project's patch from the **bench workspace repo** (branch `factory/<name>`
  of `[seal] workspace_repo`), not from the regular workspace repo or a Flyte output. `factory/<name>-r2` is another
  attempt's branch: match the branch name exactly.
- A task whose repository is older than the model's training cutoff is reported as recallable (below).

The numbers come from one command, over every run of every arm, with one audit verdict per run:

```sh
PYTHONPATH=src python -m flyte_agent_factory._seal_report --runs runs.json --verdicts verdicts/ \
  --tasks tasks.json --model-cutoff 2025-01-31
```

`runs.json` is a list of `{"name", "task", "arm", "resolved"}`, one per run: `resolved` is the benchmark's own score
of that run's patch, and must be a JSON `true` or `false` (anything else is an error naming the run). `verdicts/` holds
`<run name>.json`, the audit's `--json` output. A run counts only with `"verdict": "clean"` **and** `"gold": true`: a
clean verdict from an audit without the gold patch never checked what the run reached, so it is void ("no gold"). A run
with no verdict file is void ("no audit"). Two counted runs of the same task in the same arm are an error naming both:
a task counts once per arm, and a rerun only follows a void run. A run name listed twice is an error too.
`tasks.json` is `{"<task>": {"created_at": "<date>"}}`, with an entry for every run's task (a run whose task is
missing is an error: it could never be listed as recallable), and the model's cutoff is the published one. It prints
one results table over clean runs only, the void runs with each one's first reason, one rerun name per task and arm
that still needs one (for its latest void run; none if a counted run follows: `a-t2` void then `a-t2-r2` clean needs
none, and `t1`, `t1-r2` both void needs only `t1-r2 -> t1-r3`), and the recallable tasks. Nothing else reports
numbers. A bad input (a duplicate run, a task missing from `tasks.json`, a malformed file, a verdict that is not JSON) ends with one `error: ...` line on stderr and exit status 1, no table.

### Proved end to end

`docs/seal-e2e/` holds records from real sealed runs on the cluster: a plain arm and an engine sub-agent
(reviewer) each try every route, with a Claude sub-agent too. They are called clean, with each blocked
attempt in the record. A run that read another attempt's patch is void with the evidence line. Damaged
copies are void as `incomplete`. The README there has the table and the command to rerun it.

### Limits, stated plainly

- **Recall.** A model may recall a fix from its training data; no seal prevents it. The report lists tasks older than the
  model's cutoff.
- **Same uid.** The harness, the proxy, the recorder and the session run as the same user in the same pod. A role that
  deliberately kills or alters the harness or its record files is not covered; that depends on the rule-of-two split
  (#29). The guard config and `forbidden_paths` are outside that: the config is read-only inside the seal and
  forbidden files are hidden, and the record root is a read-only mount in every role's namespace (a write, delete or
  rename under it fails with EROFS; the role cannot remount it). What remains is what the role must be able to write:
  its engine's transcripts in its config dir until they are collected, and the guard hook's log in its own writable
  directory until the harness copies it into the record (at each close and at the role's end, before `end.json` hashes
  it). Tampering with those, or with the harness, depends on #29. Planting code the harness later runs is closed off
  outside the role's writable paths (the filesystem lock, layer 3); inside them, what the harness reads is the workdir's
  repository (covered by harness git below), the engine config dir (read as transcripts, data only) and the author's
  bridge directory (read as JSON requests, the author's intended channel). Processes the role starts can still signal
  other processes of the same uid (no PID namespace); that too depends on #29.
- **Codex roles have no guard hook.** They have the network seal and web search off, and nothing that records a denied
  attempt in `guard.jsonl`.
- **Codex roles hold a live model token.** A Codex role's `CODEX_HOME/auth.json` holds the access token the engine
  uses, and the proxy admits `CONNECT` to the model hosts by host only (it cannot see inside TLS). A script in the role
  could call the model API itself with a hosted web-search tool, which `web_search="disabled"` does not stop. The audit
  catches that only as a recognised HTTP-client command or as a gold line in a result. Closing it needs the token held
  outside the role's uid (#29) or a TLS-terminating proxy; neither is built.
- **Same-uid process memory.** `ptrace_scope` is 0 in the pod, so a process the role starts can read the engine's memory,
  its OAuth token included, and use the provider's server-side tools through the allowed model host. That is an attack
  on the engine, outside the threat model above; it depends on #29.
- **Package files are filtered by project, not by date.** The index hides files uploaded after `index_cutoff` and every file
  of `task_project`, but `/files/...` is filtered by task project only; a later file is reachable only by a URL the filtered
  index never lists.
- **Credential helper.** The session can read its clone's credential helper config. It has no route to use it except the
  proxy's push route, which adds the token itself and admits only `git-receive-pack`.
- **Harness git on the role's repository.** The clone and the nested task repo (`.git/` included) are writable by the
  role, and the harness runs git there outside the seal, with network. Every harness git call on them (`_seal_git.py`)
  runs with hooks and fsmonitor off, no system or global config, and the push token supplied through the environment,
  and just before it the repo's `.git/config` is rewritten to an allow-list (`core.*` basics, `user.name/email`,
  `branch.*.remote/merge`, the push route, `remote.origin.url` as the harness set it, `remote.origin.fetch` as
  `limit_fetch` set it); `include.*`, `filter.*`, `credential.*`, other `url.*`, other remotes and everything else are
  dropped, so a role that repoints `origin` or plants a hook or driver gets nothing run and no fetch from another URL.
  The role's own `pre-push` hook stays and still runs for its pushes; harness git never runs hooks. The delegation
  snapshot sanitises the copied `.git` too and commits with `--no-verify`. Also covered: the "nothing changed" shortcut
  compares `ctime` and a hash of the config as well as mtime/size/inode, so a config rewritten in place with a restored
  mtime is sanitised again; the config is replaced through an `O_EXCL` temp file and a rename, so a planted
  `config.harness` symlink, FIFO or directory is never written through or waited on; an armed repo whose `.git` is
  missing or not a git directory (no `HEAD`/`objects`/`refs`, a symlink, `commondir`) raises `UnsafeRepo` instead of
  being skipped, and calls that name the repo run with `GIT_DIR`/`GIT_WORK_TREE` and `safe.bareRepository=explicit`, so
  a bare repository the role builds at the worktree root is not used; `objects/info/(http-)alternates` are deleted;
  `status` and `diff` get `--ignore-submodules=all` (and submodule recursion is off in config), so a nested repository
  the role adds is not entered and its config is not read; the delegation snapshot copies no symlink out of `.git`;
  and `.git/info/exclude` and the `pre-push` hook are written by replacing the file, never through a link.
  Residual: the role runs concurrently, so a rewrite between the sanitise and the git call that follows (milliseconds)
  is not excluded; only `status` and `diff` are given `--ignore-submodules=all`, other porcelain that recurses into a
  submodule (`checkout`, `stash`) relies on the config alone, which a `.gitmodules` `ignore` can outrank; harness git
  that does not name its repository (`git_argv()` without a `-C`) relies on the sanitiser's `.git` check and
  `safe.bareRepository=explicit` rather than `GIT_DIR`; `checks.sh` runs the role's own code and is not covered.
- The pre-push credential scan is advisory (`--no-verify` or editing `.git/hooks` skips it).

### Owner decisions not taken

- A cluster egress NetworkPolicy for benchmark pods. It is defence in depth here, and needed on any node where
  unprivileged user namespaces are off (the in-pod seal is then unavailable).
- Moving the proxy and recorder out of the session's uid (#29).
- The benchmark workspace repo itself: who owns it, its name, and who may read it.
