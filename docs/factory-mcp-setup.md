# Factory MCP

The reference for the server the chat operator connects to. The step-by-step setup of the operator (this server,
the skill and the GitHub connector) is [chat-operator.md](chat-operator.md#setup).

**Backend requirement.** The engine currently depends on Union services. These instructions require a Union
backend; OSS Flyte cannot run it unchanged ([compatibility audit](oss-flyte-compat.md)).

The factory MCP uses Flyte's stock `FlyteMCPAppEnvironment`, deployed as the app
`factory-mcp` with `requires_auth=True`. Union authenticates every request at
the app ingress and Flyte runs each call as the caller, so Union RBAC applies.
Which wrappers stock settings replaced and which remain, with evidence, is in
[mcp-stock-move.md](mcp-stock-move.md). The server exposes every stock tool, as it always has (`TOOLS`, listed in
[architecture.md](architecture.md#the-controls-on-the-mcp)); a tool a newer flyte adds is served and scoped too.
Chat starts projects with `run_task` on `factory.launch` and follows them through the queue; the instructions
steer it to `factory.launch`, `factory.status`, `factory.queue_list`, `factory.queue_cancel`,
`factory.set_reserve`, `factory.get_reserve`, `factory.clear_limit`, `factory.rotate_token`, `factory.browser_link`, `factory.credential_link`,
`factory.credentials` and `factory-bench.benchmark`. Other tasks, such as `factory-deployer.deploy_if_changed`, can be run too. Running a slot
task directly is unsupported (it skips validation and the queue) but not blocked.

**Scope.** Every tool is limited to the factory's project and domain. The server checks that itself: a call that
names another project or domain is refused, `list_projects` shows only the factory project, and `get_project`
refuses any other name. Inside that scope, what the caller may do is bounded by the caller's Union auth (the RBAC of
the bearer token or API key), so give an API key only the role it needs. `list_secrets` shows names and types,
and no tool returns a secret value. `create_secret` and `delete_secret` exist (`create_secret` returns a one-time link on
this server where the owner types the value), but engine credentials are stored with local commands
([quickstart §4](quickstart.md#4-store-engine-credentials-locally)), never sent through chat.

**The app tools and long calls.** Besides the stock tools the server has `get_app_status` (an app's status history and
why it last failed), `get_app_logs` (its newest log lines, including replicas that have died), `restart_app`
(deactivate then activate, never delete), `factory_mcp_health` (this server's uptime, restarts and errors; its call and error counts cover the last hour and
reset on a restart, so read a whole day from `get_app_logs` with `since=24h` and its `mcp_call` lines, or from the health
tool once an hour) and
`get_operation`; `get_app`, `activate_app` and `deactivate_app` list environment and secret names, never values.
`restart_app` and `deactivate_app` refuse the server's own app (`AGENT_FACTORY_MCP_APP`, set at deploy), since stopping it
would end the call that restarts it. The chat client cuts a call at about 230 s, and a cut call reaches it as a 502 with
nothing to read. So every call has a 200 s budget: past it the tool returns
`{"status": "still_working", "operation_id": ...}`, the work goes on in the server, and `get_operation` gives the
result (or the steps done so far). A call that changes state (`ACTING` in `_factory_mcp.py`), repeated with the same
arguments before its result was delivered, joins the first instead of acting twice. A request the client drops is
cancelled on the server (`CancelOnDisconnect` watches the transport's disconnect); the work goes on and an identical
repeat joins it. `wait_for_run`'s `timeout_s` is
capped at 170 s (the result says `timeout_capped_to_s`); a run still going returns `timed_out: true`, so call it again.
The operations live in the server's memory: after a restart an id is `unknown_operation`, and the effect should be read
directly (`get_run`, `get_app_status`) before acting again. Every result is masked for secrets before it leaves the
server, and each call writes one log line (tool, duration, outcome, never the arguments). The server asks Kubernetes for
100m CPU and 256Mi memory (limit 512Mi); its measured RSS is 114-126 MiB ([small-nodes.md](small-nodes.md)).

The search tools read the Flyte SDK examples, the Union examples and the Union docs from `/opt/flyte-mcp-docs`,
fetched into the app image when it is built (the SDK at tag `v2.10.7`).

Set `[mcp] public_url` in your profile and deploy with
`python ops/deploy_factory.py --ref origin/main`. `[mcp] repos` lists the
`[repo]` keys whose URLs the server's instructions offer for `repo`.

## Connecting a client

Use the HTTPS URL `<public_url>/flyte-mcp/mcp` (`Connector URL:` in the deployment output).

On a Linux x86_64 machine, one bootstrap command does the key, the grant and the connector for the chat key; the
bootstrap's `connect` step prints it with your values filled in (here the example values, and `--union-keys` in its
place makes the factory's project and deployer keys, stored as Flyte secrets):

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --chat-key
```

It makes the `factory-chat` key, a `factory-chat` policy binding `contributor` to the factory's project and domain
(an existing policy of that name must bind exactly that), and the assignment; then it adds the connector to Claude
Code, or shows the key once on your terminal for claude.ai, and never prints it to stdout. The rest of this section
is the manual route, for someone running with another administrator or another machine.
Send the header `Authorization: Bearer <credential>`, where the credential is either:

- a Union API key: `flyte create api-key --name <name> --no-default-policies` (from
  `flyteplugins-union`) prints it as the value of `export FLYTE_API_KEY="..."`, and the subject for the grant
  below as `Client ID:`. It does not expire. A key created this way can do nothing until a policy is assigned.
  A key created without that flag gets Union's org-wide `contributor` policy; remove it with
  `flyte delete assignment --creds-subject <name> --policy contributor` before granting the scoped one.
- a Union sign-in access token.

The bootstrap says: making a Union API key from the command line needs flyteplugins-union, which installs on Linux x86_64 only. The key and policy commands come from `flyteplugins-union`, which this project installs only on Linux; its
pinned release (0.15.5) ships a Linux x86_64 wheel and nothing for macOS or Linux on ARM. On macOS, run
`flyte create api-key`, the policy grant below and `flyte delete assignment` on a Linux x86_64 machine or VM
with the engine checked out and the same Flyte config; every other setup step runs on macOS.

### Grant the key a role

When `--chat-key` or `--union-keys` is refused the policy or the assignment (you may not manage access in the
Union organization), it still hands over the key, ends `todo`, and prints one command for a Union admin, filled in
with your values and the key's client ID. The admin runs it on a Linux x86_64 machine, signed in to the same
endpoint (if that machine has no Flyte config for `--endpoint`, the command writes one and opens the browser sign-in first; a config for another endpoint stops it before any change); it makes or checks the policy (`contributor` on the project and domain only) and assigns it to that
client ID, and makes no key. For the deployment key, here with the example values and a client ID that is the key's
name:

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --grant factory-deployer factory-deployer
```

The rest of this section is the same grant by hand, for a key made with `flyte create api-key`.

Union only. A policy binds a role to a project and domain, and an assignment gives the policy to the key's
client ID (the name, lowercased with other characters as hyphens, except on Union Serverless). Run this
locally with the profile's `FLYTE_CONFIG`, `FLYTE_PROJECT` and `FLYTE_DOMAIN` set
([quickstart.md](quickstart.md#3-profile-and-secret-names)), and fill the `client_id=` line with the
printed client ID. Creating policies and assignments needs permission to manage access in the Union organization;
if you do not have it, send the block to whoever does. For the chat key the policy is `factory-chat`; for the
deployment key, change the `policy=` line to `factory-deployer` (or `factory-project`) and use that key's client ID. Pasted with
`client_id=` empty, the block stops before it creates anything.

```bash
(
set -euo pipefail
: "${FLYTE_CONFIG:?}" "${FLYTE_PROJECT:?}" "${FLYTE_DOMAIN:?}"
policy=factory-chat
client_id=
: "${client_id:?put the Client ID the key creation printed after client_id=}"
policy_file="$(mktemp)"
trap 'rm -f "$policy_file"' EXIT
cat > "$policy_file" <<YAML
name: $policy
bindings:
  - role: contributor
    resource:
      project: $FLYTE_PROJECT
      domain: $FLYTE_DOMAIN
YAML
uv run flyte --config "$FLYTE_CONFIG" get policy "$policy" >/dev/null 2>&1 ||
  uv run flyte --config "$FLYTE_CONFIG" create policy "$policy" --file "$policy_file"
NO_COLOR=1 TERM=dumb uv run flyte --config "$FLYTE_CONFIG" get policy "$policy" > "$policy_file"
uv run python - "$policy_file" "$policy" "$FLYTE_PROJECT" "$FLYTE_DOMAIN" <<'PYTHON'
import ast
import sys
from pathlib import Path

from flyteplugins.union.internal.common.policy_pb2 import Policy as PolicyPb2
from flyteplugins.union.remote._policy import _dict_to_binding
from google.protobuf.json_format import ParseDict
from rich.text import Text

path, name, project, domain = sys.argv[1:]
# NO_COLOR can leave bold escapes; use Rich's decoder rather than a partial ANSI regex.
records = ast.literal_eval(Text.from_ansi(Path(path).read_text()).plain)
if not isinstance(records, list) or len(records) != 1:
    sys.exit("Cannot inspect policy; no assignment made. Ask your Union administrator.")
existing = ParseDict(records[0], PolicyPb2())
expected = _dict_to_binding(
    {"role": "contributor", "resource": {"project": project, "domain": domain}},
    existing.id.organization,
)
if (
    existing.id.name != name
    or not existing.id.organization
    or list(existing.bindings) != [expected]
):
    sys.exit("Policy scope mismatch; no assignment made. Ask your Union administrator.")
PYTHON
uv run flyte --config "$FLYTE_CONFIG" create assignment --creds-subject "$client_id" --policy "$policy"
)
```

`contributor` is the built-in Union role for deploying tasks and apps and managing secrets inside one project
and domain ([Union RBAC](https://www.union.ai/docs/v2/union/security/identity-and-access/rbac/)); whether it
suffices for every call the factory makes has not been run. A narrower custom role is optional. The `--file`
format is a `name`, an optional `description` and a list of `bindings`, each a `role` with a `resource` of
`project` and `domain` (both optional); the policy takes its name from the file's `name`. On every run, the
block reads back the policy and requires exactly one `contributor` binding to the profile's project and
domain, including the organization on the role and resource. An existing policy skips
creation, but a broader, differently scoped or unreadable policy stops before assignment. Ask the Union
administrator to inspect it and supply a matching policy; changing only the name does not check its scope.

The pinned Union CLI's named `get policy` renders a Python literal list of protobuf dictionaries (`roleId`
and nested resource fields), even with a JSON output option. The block requests unstyled output and strips
any remaining ANSI styling with Rich before parsing that literal with `ast.literal_eval`. It uses Union's
own binding converter to compare the complete binding.
Unexpected output fails closed. The block stops at the first failed command or unset variable and deletes its
temporary file. This checks a snapshot; an administrator who can edit the policy can change its grants later.
If `ops/deploy_work.py` later fails with a permission error,
widen the policy's bindings in an editor (the block's commands are the `flyte create policy` and
`flyte create assignment --creds-subject` subcommands, run through `uv run` with the profile's config):

```bash
uv run flyte --config "$FLYTE_CONFIG" update policy factory-deployer
```

### claude.ai

Add a custom connector through Customize > Connectors > + Add > Add custom connector; on Team or
Enterprise, an authorized owner uses Organization settings > Connectors > Add > Custom > Web
([Anthropic setup guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).
Enter the server URL above, click Continue, and review authentication. Choose **No sign-in**, then add a
required `authorization` request header with `Bearer ` followed by the Union API key. Enter the credential
only in connector settings; never send it in chat. Save with Add and enable it from **+ > Connectors**
in the conversation.

[Anthropic's request-header documentation](https://claude.com/docs/connectors/custom/add-unlisted#authenticate-with-request-headers)
calls this a beta for a limited set of organizations. If the dialog has no Request headers section,
use Claude Code below. Team/Enterprise header credentials are shared by everyone using that connector,
so its Union role must fit everyone with access. The stock factory app serves no RFC 9728
protected-resource metadata or OAuth sign-in flow; selecting OAuth discovery cannot connect it.

### Claude Code

Run the Claude Code block in [chat-operator.md](chat-operator.md#1-connect-the-factory-mcp). It prompts for the
key without echo, refuses an empty entry and unsets the variable on exit. Ask the connected client to call
`whoami` to check authentication.

For GitHub issues and the Projects (v2) board from chat, add GitHub's official MCP server as a
second connector: see [github-connector.md](github-connector.md).

## Chat skill

The `agent-factory` and `since` skills are public and need no profile: download the committed zips from
[`skills/dist/`](../skills/dist) and install them as in
[chat-operator.md](chat-operator.md#install-and-update). `ops/package_skill.py` builds them from `skills/`, and
`ops/package_skill.py --check <zip>` says whether an installed copy is current.

## Operating and measuring

These scripts run from `projects/mcp-apps-reliability/` with `PYTHONPATH=src uv run python <script>`; each takes
`--profile`, and `--help` lists the rest. None prints a token.

- `ops/live_mcp_test_app.py` deploys a throwaway copy of the factory MCP app named
  `factory-mcp-test-<short sha>` (it refuses `factory-mcp`). Run bare, it deploys, prints the endpoint and the
  connector URL `<endpoint>/flyte-mcp/mcp`, then holds until Ctrl-C, SIGTERM or end of stdin and deletes the
  app on the way out (a failed deploy deletes it too). `--keep` deploys, prints and exits, leaving the app
  running; `--delete --name <name>` removes it later. `--dry-run` prints the plan.
- `ops/mcp_soak.py --endpoint <connector URL> --app <test app> --status-name <project> [--rounds 10]`
  makes rounds of calls in separate MCP sessions (tool list, four parallel `run_task factory.status` with
  `rows` 20 to 23, waiting for the run, run output, logs, app logs and status, health) and writes
  latencies and errors by kind to `evidence/mcp-soak.json`. Each call has distinct inputs because identical
  parallel calls join into one run, by design.
- `ops/mcp_burst.py --endpoint <connector URL> --app <test app> --status-name <project>` replays the parallel
  bursts that timed out (8 `whoami`, 8 `tools/list`, 4 `get_app`, 4 `factory.status`), reads
  `factory_mcp_health` before and after, and writes `evidence/mcp-burst.json`.
- `ops/mcp_chat_check.py --endpoint <connector URL> --test-app <name>` does what the operator does from chat
  (app logs, app status, health, a refused self-restart, then a restart of the test app) and keeps the
  evidence. Production is only read.
- `ops/app_logs_probe.py [--app <name>] [--out <file>]` checks the app tools against a live app, read-only,
  with this pod's token as the bearer header, and writes redacted evidence.
- `ops/devbox_mcp/` is the one-shot devbox harness: `python -m devbox_mcp.run --out evidence/devbox-<label>.json`
  with `PYTHONPATH=ops:...` as its docstring in `ops/devbox_mcp/run.py` gives. It submits one privileged child
  action, takes about 12 minutes once a node is placed, has no retries, and never overwrites `--out`.
