# Live smoke test of the factory MCP, from chat

Operator steps for checking `factory-mcp` from a chat after redeploying it, for example after a flyte SDK upgrade
(the move from 2.10.7 to 2.13.5 is the reason this page exists, see [mcp-stock-move.md](mcp-stock-move.md); what
the server still builds around the stock tools is in [mcp-sdk-requirements.md](mcp-sdk-requirements.md)). The unit tests cannot cover this: they never reach
a real Flyte control plane, a real ingress or a real browser.

It is for the owner, who runs the commands in a shell and the prompts in a chat connected to the server. Setup of
that connection is in [factory-mcp-setup.md](factory-mcp-setup.md) and [chat-operator.md](chat-operator.md).

Run the steps in order and stop at the first one that fails: later steps assume the earlier ones. Never paste a
credential, a token or a secret value into the chat while doing this; a step that needs one says where it goes.

## The two deployments

The steps are the same on both. Only the connection differs.

| | Work cluster | Personal cluster |
|---|---|---|
| Backend | Union (`[backend] kind = "union"`) | Open-source Flyte (`kind = "oss"`) |
| `[mcp] auth` | `"union"`: the app is deployed with `requires_auth=True`; Union authenticates every request at the ingress and each call runs as the caller | `"proxy"`: the app checks no token (`requires_auth=False`) and sits behind an OAuth proxy, which authenticates the caller |
| Connect with | `Authorization: Bearer <Union API key or access token>` ([connecting a client](factory-mcp-setup.md#connecting-a-client)) | The proxy's sign-in, then the URL the proxy publishes for the app |
| `whoami` | the caller's Union identity | not meaningful without a trusted forwarded identity ([oss-flyte-compat.md](oss-flyte-compat.md)); it must answer, and its identity can be empty |
| Grant page and `/secret-input/` links | behind the same Union sign-in as the MCP endpoint | behind the same proxy |

Everything below refers to `<project>` and `<domain>`: the factory's project and domain from the profile
(`[backend]`), which `INSTRUCTIONS` hands the chat as "Use Flyte project ... and domain ...". `other` stands for any
project that exists on the cluster and is not `<project>`; if you have none, create an empty one, because a
nonexistent project is refused for the wrong reason.

## 0. Redeploy

From a checkout of the commit to ship, with the profile's `FLYTE_CONFIG` set
([quickstart.md](quickstart.md#3-profile-and-secret-names)):

```bash
uv sync -q --python 3.12
PYTHONPATH=src uv run python ops/deploy_factory.py --profile <profile.toml> --ref <commit> --dry-run
PYTHONPATH=src uv run python ops/deploy_factory.py --profile <profile.toml> --ref <commit>
```

`--ref` defaults to `origin/main`, and the deploy is from a clean worktree of that commit, so the commit must be
pushed. The dry run goes first and builds nothing. The real run builds the `factory-mcp` image (which fetches the
search corpus into `/opt/flyte-mcp-docs`) and ends by printing `factory-mcp public URL:` and `Connector URL:`.
Write the Connector URL down: it is `<public_url>/flyte-mcp/mcp`. On the personal cluster the deploy prints
"Connect through your authenticating proxy"; use the URL your proxy gives the app, not the app's own.

Note the commit you replaced before you deploy ([rollback](#rollback) needs it):

```bash
PYTHONPATH=src uv run python ops/deploy_status.py
```

It lists the engine commit each deployed task runs. Run it before step 0 and keep the output.

With `[deploy] auto = true`, the `factory-deployer` will also redeploy whatever is on the default branch
([setup.md](setup.md#deploying)). If your test commit is not on that branch, the deployer replaces it with `main`
on its next tick, within five minutes. Set `auto = false` in the deployed profile for the length of the test, or test
a commit that is on `main`.

Then reconnect the chat (a new conversation, or remove and add the connector) so it reads the new tool list and
instructions. A conversation started before the deploy still holds the old ones.

## 1. Connect and `whoami`

Prompt:

```text
Call whoami and show me the whole result.
```

Must show: a result with `subject`, `name`, `org`, `endpoint`, `project`, `domain`. `project` and `domain` equal the
factory's. On the work cluster `subject` is your Union identity, or the API key's client ID. On the personal
cluster, behind the proxy, the identity fields can be empty; what matters is that the call returns.

Fails if: a 401 or 403 (the bearer, or the proxy sign-in), a connection error (the URL; the `allowed_hosts` check
refuses a host that is not `[mcp] public_url`), or the tool list does not include `whoami`.

Also ask `What tools do you have?` and check that the chat sees all 39 stock tools of flyte 2.13.5 (`TOOLS` in
`_factory_mcp.py` is `ALL_MCP_TOOLS`; [architecture.md](architecture.md#the-controls-on-the-mcp) lists them),
including the 7 new since 2.10.7: `delete_app`, `create_trigger`, `delete_trigger`, `get_secret`,
`update_secret`, `generate_secret` and `get_condition`. A list of 32 means the client cached the old one, or the old
engine is still deployed.

## 2. `list_projects` shows only the factory project

Prompt:

```text
Call list_projects with limit 100 and show me every id.
```

Must show: exactly one entry, whose `id` is `<project>`. Any other id means the filter did not run.

Then:

```text
Call get_project with name "other".
```

Must show: an error containing `is not allowed; this server is limited to`. And `get_project` with `<project>`
succeeds.

## 3. A foreign project is refused

Prompt:

```text
Call list_runs with project "other" and domain "<domain>", limit 5.
```

Must show: an error containing `list_runs: project 'other' is not allowed; this server is limited to project
'<project>'.` No runs. Repeat with `domain` set to a domain that is not the factory's; the message names `domain`.

Then the same call with no `project`, which must work and list only the factory's runs:

```text
Call list_runs with limit 5.
```

## 4. A run_task for another run_project is refused

This is the one that matters most. `run_task` has two more project parameters than the other tools: `project` and
`domain` say where the task is registered, and `run_project` and `run_domain` say where the run is created
([mcp-sdk-requirements.md](mcp-sdk-requirements.md#1-restrict-tool-calls-to-one-project-and-domain)).

Prompt:

```text
Call run_task with name "factory.queue_list", run_project "other". Do not pass project or domain.
```

Must show: an error containing `run_task: run_project 'other' is not allowed; this server is limited to project
'<project>'.`, and **no run created**. The chat cannot
check that (`list_runs` with `project "other"` is refused), so look from a shell:
`uv run flyte --config "$FLYTE_CONFIG" get run -p other -d <domain>` shows nothing new.

**If this step succeeds and a run appears in `other`, the server is not scoping `run_project`: stop, abort that run,
and do not leave the new version deployed.** The factory's `_scoped_to_the_factory` refuses every parameter whose
name ends in `project` or `domain` (`_scoped_arguments`), `run_project` and `run_domain` among them, so this
step must be refused. Also run it with `run_domain "<other domain>"`; the message names `run_domain`.

Then check the same tool on the factory's own scope, which must work: `Call run_task with name
"factory.queue_list", run_project "<project>", run_domain "<domain>"`.

## 5. Launch, wait, read the outputs

The launch task is `factory.launch`. This starts a real project, so give it a throwaway name and a brief that
asks for nothing:

```text
Launch a project with run_task name "factory.launch" and inputs: name "smoke-test", model "<the model INSTRUCTIONS names>",
brief "Smoke test of the MCP server. Do not change anything: write one line to README.md saying hello, then stop.",
priority "low". Then call wait_for_run with timeout_s 300 and then get_run_io, and show me each result.
```

Must show:

- `run_task` returns a run name and a URL.
- `wait_for_run` returns `phase` `succeeded` (the launch task returns once the project has started or queued, long
  before the project ends) with **`outputs_ready: true`**.
- `get_run_io` returns `outputs` that is not null, whose `status` is `started` (give the user its url) or
  `queued` (with a position), and no `outputs_error`.

Fails if `outputs` is `null` and `outputs_error` is empty, which is the 2.13.5 read-model lag with the wrapper not
working; if `get_run_io` errors with an unexpected argument, or answers with the wrong data, the wrapper's
signature does not match the stock tool's (the wrappers call the stock tools by keyword and mirror its
signature, fixed in this move:
[mcp-sdk-requirements.md](mcp-sdk-requirements.md#what-2135-already-changed-under-the-factorys-wrappers)). Call
`get_run_io` with `inputs_only` true and with `outputs_only` true as well; both must work.

## 6. Status and queue

```text
Call run_task name "factory.status" inputs {"name": "smoke-test"}, then get_run_io.
Then call run_task name "factory.queue_list", then get_run_io.
```

Must show: for `factory.status`, the current activity, dashboard phase, timeline rows and `snapshot_age_s`. For
`factory.queue_list`, the slots (or the shared environment) and, if the project is waiting, `smoke-test` with its
position. Each `get_run_io` has outputs on the first call.

## 7. Conditions: `list_conditions` and `signal_condition` with a sentence

```text
Call list_runs with task_name "factory.launch" and find the live smoke-test project's run. Then call list_conditions
for that run and show me every name and expected_type.
```

Must show: at least a `direct-` inbox, and, when the project has asked something, an `ask-` condition. An ask is
not guaranteed: the factory's judge answers engineering asks itself before they open. If there is none, use an
`ask-` condition of any other live project, or skip the two `ask-` calls below and say so in the result.

Answer an `ask-` condition with a sentence:

```text
Call signal_condition on <the ask- condition> in run <run name> with the value "yes please".
```

Must show: `signaled: true`. For a yes/no ask the value delivered is `true`, not an error ("could not parse 'yes
please' as bool" is the stock tool's message, and means the factory's `signal_condition` wrapper is not installed: it calls the stock tool, retries the bool
with the answer parsed as yes or no, and reroutes only if that fails too). Then answer the
same ask a second time, now that it has ended:

```text
Call signal_condition on <the same ask- condition> in run <run name> with the value "yes please, again".
```

Must show: `signaled: true`, `rerouted: true`, `delivered_to` a `direct-` condition. If it says the answer was
"NOT delivered", the run has no open `direct-` inbox (it ended): that is also correct, but it does not prove the
reroute; try again while the project is live.

Finally stop the smoke project, with `/stop` to its inbox (not `abort_run`, which loses unsaved work):

```text
Call signal_condition on <the direct- condition> in run <run name> with the value "/stop".
```

Must show: `signaled: true`, and `factory.status` for `smoke-test` shows it ending.

## 8. Logs

```text
Call get_logs for the run of the launch task you started in step 5, with max_lines 20, and show me the whole result.
```

Must show the newest lines of the log (its end, oldest first), as a dict (flyte 2.13.5), not a list: `available`, `logs` (the lines joined by newlines), `first_line`, `end_line`,
`lines_returned` (at most 20), `has_more_before`, `next_before_line`, `stopped`, `message`. Then call it again with `before_line` set to the
returned `next_before_line` and check that the lines before come back, without overlap.
`available: false` means the logs are not up yet; wait a minute and retry.

## 9. Secrets

```text
Call list_secrets and show me the names and types.
```

Must show: names and types only. No value anywhere. The factory's engine tokens are among the names.

```text
Call create_secret with name "smoke-test-secret". Do not ask me for the value; show me the result.
```

Must show: `status: awaiting_value` and a one-time URL ending `/secret-input/<token>`, valid for 10 minutes. Open it
**in your own browser**, never in the chat. The page asks for a value in a password field; type `smoke` and submit.
Then:

```text
Call get_secret with name "smoke-test-secret".
```

Must show: `exists: true` and, within a minute, `ready: true`, with no value. Open the same URL again: it is refused (one use). Delete
the test secret afterwards:

```text
Call delete_secret with name "smoke-test-secret".
```

The chat must never have shown the value `smoke`. Search the conversation for it. On the work cluster the URL
opens only for someone signed in to Union; on the personal cluster only through the proxy.

## 10. The grant page

The credential grant page is served by `factory-mcp` at `/flyte-mcp/grant` (`PAGE_PATH` in
`src/flyte_agent_factory/_grant_seal.py`). It is a static page on the app's public URL, behind the same ingress
as the MCP endpoint (on the work cluster, Union sign-in; on the personal one, the proxy): public in the sense of
having a public address, not open without sign-in. Open it in a browser:

```text
<public_url>/flyte-mcp/grant
```

The page is served by the factory's `_GrantPage` middleware, inside the stock auth middleware, so it needs the same
sign-in as the MCP endpoint. Must show: a page (HTTP 200) titled "Give a credential". With no fragment it says the link is damaged ("copy
the whole link again"), which is correct: a real link carries the project's key in its `#fragment`
([credential-grants.md](credential-grants.md)). To check a real link, run
`run_task name "factory.credential_link"` for the live smoke project (if it is still live) and open the `url` it
returns, but do not paste a real credential: the page should show the project, the credential name and the hours,
and a Seal button. Check from a shell that an unauthenticated request does not get it on the work cluster:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "<public_url>/flyte-mcp/grant"
```

Must show: a sign-in redirect or 401/403 on the work cluster, and the proxy's answer on the personal one, and 200
only with the credentials.

## 11. Search tools

```text
Call search_flyte_sdk_examples with the pattern "cache", then search_flyte_docs_examples with "secrets",
then search_full_docs with "trigger", and tell me how many results each returned.
```

The `pattern` argument is a regular expression matched against the corpus, so keep it to one plain word.

Must show: results for each, with file names and snippets, read from `/opt/flyte-mcp-docs`. No results at all, or an
error about a missing path, means the image build did not fetch the corpus (the `_FETCH_DOCS` step in
`_factory_mcp.py`), or the unprivileged user cannot read it.

## Result

The deploy is good when steps 1 to 11 each show what they must. Record the commit, the date and which cluster; the
steps that were skipped (step 7 without an ask) say so. A failure of step 4 blocks keeping the deploy on a shared
cluster.

## Rollback

If a step fails and the previous version should be back:

1. Pause the auto-deploy, or it puts `main` straight back: set `[deploy] auto = false` in the deployed profile
   (`profiles/<name>.toml` in the engine, or the workspace's `profile.toml`), and deploy that, or revert the change
   on `main` ([setup.md](setup.md#deploying)).
2. Find the commit you replaced: the `ops/deploy_status.py` output from before step 0, or the git history of the
   branch.
3. Check out that commit, so that its own `ops/` and `src/` run, and redeploy:

```bash
git fetch origin && git checkout <previous commit>
uv sync -q --python 3.12
PYTHONPATH=src uv run python ops/deploy_factory.py --profile <profile.toml> --ref <previous commit>
```

This rebuilds the `factory-mcp` image from that commit, with its flyte pin (`flyte[mcp]` in the app's
`with_pip_packages`) and the docs corpus tag, so the previous flyte comes back with it. Wait for the app to
activate, reconnect the chat, and run steps 1 and 5 to see the old behaviour is back. Then check
`ops/deploy_status.py` shows the previous commit for `factory-mcp`.

A project launched by the smoke test runs on the engine it started with and does not need rolling back; stop it as
in step 7.
