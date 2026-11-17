# Factory compatibility with OSS Flyte 2

The factory deploys and runs on open-source Flyte 2 when the profile says `[backend] kind = "oss"`
([the tested path](#running-the-factory-on-oss-flyte)): deployment takes its image builder from the profile
(the local one on OSS), pods are told the in-cluster endpoint, no Union key is mounted or required, and the MCP
app trusts an authenticating proxy in front of it. The audit below still holds for what OSS lacks: artifact
discovery and artifact-driven automation need replacements, and condition webhook notifications do not exist.
A first launch from GitHub is feasible without migrating Union run history.

The audit sections are a source audit, dated 2026-10-28, for QA to validate against the deployment. The factory
changes described in the next section are covered by offline tests (`tests/test_oss_backend.py`,
`tests/test_run_labels.py`); none of it is a successful factory migration or a live compatibility test.

## Running the factory on OSS Flyte

What the factory does on OSS is chosen by the profile alone. Union behaviour is unchanged when `kind = "union"`.
This path is tested offline (profile, pod environment, builder choice, MCP app); it has **not** been run against a
live OSS cluster.

### 1. The profile

`profiles/oss-example.toml` is the working example. Its `[backend]` and `[mcp]` tables:

```toml
[backend]
kind = "oss"
endpoint = "flyte.example.com"
project = "my-agents"
domain = "development"
internal_endpoint = "http://flyte-http.flyte.svc.cluster.local:8090"
insecure = true
image_builder = "local"
config = "~/.flyte/example-oss-config.yaml"

[mcp]
public_url = "https://factory-mcp.example.com"
auth = "proxy"
```

| Key | Meaning | Default on `oss` | Default on `union` |
|---|---|---|---|
| `[backend] kind` | `"union"` or `"oss"`; selects every default below | none (required) | none (required) |
| `[backend] endpoint`, `project`, `domain` | Where an operator's CLI reaches Flyte, and the project and domain the factory lives in | as on union | as on oss |
| `[backend] internal_endpoint` | The URL a *pod* reaches Flyte at. Set on every deployed environment as `_U_EP_OVERRIDE`; Union injects its own, OSS does not | **required** (see below) | unused |
| `[backend] insecure` | The internal endpoint speaks plain HTTP: sets `_U_INSECURE=true` on the pods and `insecure=` on the deployer's SDK init | `false` | unused |
| `[backend] image_builder` | `"local"` (docker, pushes to the registry your Flyte config names), `"local-podman"` or `"remote"` | `"local"` | `"remote"` |
| `[backend] config` | Flyte config file the CLI and the hand-deploy scripts use | none | none |
| `[mcp] auth` | `"proxy"`: an authenticating proxy in front of the MCP app authenticates callers and the app does not. `"union"`: the app checks the caller's Union token | `"proxy"` | `"union"` |
| `[union]` table (tier keys) | Union API keys the pods act as | not needed; leave it out | required |

`internal_endpoint` is required on `oss`, but it is **not** checked when the profile is parsed, so profiles that
predate the key still load. It is enforced where it matters, in `_profile_oss.endpoint_for_pods`: the deploy
(`_union_identity.init_for_script`) and a pod's start (`_union_identity.controller_kwargs`) raise `ProfileError`
without it. Without the check a pod would fall back to the SDK's laptop default (`host.docker.internal:8090`) and
fail at its first Flyte call, long after the deploy had succeeded.

Keep `[deploy] auto = false`, as the example does: the in-pod self-deployer would need docker in the pod to use the
local builder.

### 2. Secrets on the OSS cluster

The names are the profile's `[secrets]` values (the example's are `claude_code_oauth_token` and `my_deploy_key`); the
full list is [secrets.md](secrets.md). Create each in the project and domain of `[backend]` before the deploy, from
a file so the value is never on a command line. The values below are placeholders:

```bash
export FLYTE_CONFIG=~/.flyte/example-oss-config.yaml   # your OSS config file
# The token printed by `claude setup-token`, mounted in the pods as CLAUDE_CODE_OAUTH_TOKEN:
uv run flyte --config "$FLYTE_CONFIG" create secret claude_code_oauth_token --from-file <file holding the token> -p my-agents -d development
# The private half of the workspace repository's write deploy key, mounted as GIT_DEPLOY_KEY:
uv run flyte --config "$FLYTE_CONFIG" create secret my_deploy_key --from-file <private key file> -p my-agents -d development
```

How to obtain the two values is in [quickstart.md §4](quickstart.md#4-store-engine-credentials-locally). There
are no `[union]` key secrets on OSS.

### 3. Deploy

The deploy is the hand deploy of [quickstart.md §5](quickstart.md#5-deploy-and-connect-the-factory), pointed at the
OSS profile through `AGENT_FACTORY_PROFILE` (a profile name under `profiles/` or a path):

```bash
export AGENT_FACTORY_PROFILE=oss-example
export AGENT_FACTORY_SECRETS="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile agent_secrets)"
PYTHONPATH=src uv run python ops/deploy_work.py
PYTHONPATH=src uv run python ops/deploy_factory.py
```

The deploy children build with the profile's builder, `local` on OSS, so the machine running the deploy needs
**docker** (or podman, with `image_builder = "local-podman"`) and **push access to the registry** your Flyte config
names. `--dry-run` still initializes the backend and fetches code.

### 4. Trust model

- **There are no API keys on OSS.** The Flyte API has no authorization: whoever can reach it can act as the factory
  (`_union_identity.OSS_IDENTITY`: "the network is the credential"). Protect it with the network, a tailnet or a
  Kubernetes network policy, and keep it unreachable from anywhere else.
- **The MCP app with `auth = "proxy"` does no authentication of its own** (`requires_auth=False`).
  **It MUST sit behind an authenticating (OAuth) proxy and must never be exposed on its own:** anyone who can reach
  the app directly can launch, stop and signal factory projects.
- On `kind = "union"` nothing changes: every pod acts as its tier's `[union]` key, and the factory still never
  falls back to the key Union injects into every pod (#183; `_union_identity.establish`).
- An `oss`-kind profile that still lists `[union]` keys (a backend that issues them) keeps acting as them
  (`_profile_oss.keyless` is false), as it did before.

**Minimum node:** one 4 CPU / 16 GiB node (a GKE `e2-standard-4` with its default 100 GB boot disk) runs the factory and
one live project with the default resources ([small-nodes.md](small-nodes.md)). `ops/deploy_factory.py` deploys `--ref`
(default `origin/main`) from a clean worktree of that commit, not the working tree.

## Evidence and scope

- Factory baseline: `ee05242229a61972d4f15ae156085d6e522bae8a`.
- `SDK:` paths below are relative to `/opt/venv/lib/python3.12/site-packages/flyte/`, the installed SDK
  **2.10.7** (`_version.py:21`). SDK presence alone does not establish backend support.
- `OSS:` paths refer to [flyteorg/flyte v2.0.51](https://github.com/flyteorg/flyte/tree/v2.0.51), commit
  `5986692`, the release pinned by flyte-gke. Network access to GitHub worked: the `v2` branch was cloned
  and the release tag fetched and inspected. The branch exists; no `flyte-2` branch was returned by
  `git ls-remote --heads`. Use the release links rather than assuming current `v2` matches the deployment.
- `flyte-gke/` paths refer to the read-only snapshot in
  the owner's workspace, under `history/briefs/factory/flyte-gke-src/`. The source repository name requested as a
  citation prefix is omitted because the existing shareable-file guard forbids that owner-specific literal. Its `flyte-gke/docs/preflight.md:64` describes the installation and
  `flyte-gke/docs/preflight.md:72` starts its feature findings; these were checked against source where possible.
  Its statement that no identity service exists is stale: the release registers one, but does not issue Union API keys.
  That project used SDK **2.12.0**; this factory pins **2.13.x** (`pyproject.toml`).
- The supplied live observation is HTTP **401**, OAuth required, at
  `https://<flyte-gke MCP host>/flyte-mcp/mcp`. No credentials are available here, so no authenticated
  live probe of that backend was possible. A 401 establishes an authentication boundary, not tool support.

Verdicts describe OSS v2.0.51 with this factory's usage: **yes** means source implements the primitive;
**no** means a required service/behavior is absent; **partial** means only some usage is supported or a
factory/deployment change is required. None implies a live factory pass.

## SDK gates and proprietary paths

The relevant explicit SDK gate is the remote builder: missing builder task/ImageService becomes a
Union-support error (SDK `_internal/imagebuild/remote_builder.py:164`). It is not fixed by installing
an optional package. Trigger input offloading is different: SDK `_internal/runtime/trigger_serde.py:302`
catches an Unimplemented response and falls back to inline inputs, so an absent zero-trust upload route
alone does not make scheduled triggers unsupported.

The SDK also labels code-changing `flyte fork` as reserved for the Union plugin
(SDK `cli/_rerun.py:19`, SDK `_run.py:1567`); no factory fork/debug invocation was found in src/ops/scripts.
The installed project dependency confirms that `flyte create api-key` is a plugin command:
`.venv/lib/python3.12/site-packages/flyteplugins_union-0.15.5.dist-info/entry_points.txt:6` maps it to
`flyteplugins.union.cli.api_key`, whose `remote/_api_key.py:23` imports proprietary `AppsServiceClient`
(path relative to `.venv/lib/python3.12/site-packages/flyteplugins/union/`). This service is absent from
OSS registration. This inspection did not issue or print an API key. No `union` CLI command or direct
Union Python import was found in the audited factory runtime; the dependency and key-based initialization
are still real Union coupling. SDK `_internal` imports in attachment are a separate private-API risk.

## Feature matrix

| Feature | Where the factory uses it (file:line) | OSS Flyte 2: yes / no / partial | Evidence | Replacement if not supported |
|---|---|---|---|---|
| Conditions: `ask-`, `direct-`, `gcp-login-` | `src/flyte_agent_factory/_conditions.py:44`; asks: `src/flyte_agent_factory/_bridge.py:706`; direction: `_inbox._watch_app_messages`; login: `agent.gcp_bootstrap` | **yes** for create/wait/signal/timeouts | SDK `_condition.py:187` registers through the controller. OSS `actions/k8s/client.go:171` creates condition actions; `runs/service/run_service.go:1153` signals them; `executor/pkg/controller/taskaction_condition.go:47` handles signal and timeout. | Keep attempt-scoped names and existing bridge. Answer through `remote.Condition.signal` or MCP. Union UI/phone delivery is not implied. |
| Condition webhook notifications | `src/flyte_agent_factory/_bridge.py:706` forwards condition options; shared construction: `src/flyte_agent_factory/_conditions.py:44` | **partial** | SDK `_condition.py:69` defines the webhook and `_condition.py:175` passes it. OSS condition creation/reconciliation above stores the condition but has no webhook delivery path; `flyte-gke/docs/preflight.md:80` explicitly records webhooks ignored. No active factory webhook argument was found at these call sites. | Poll/list conditions with the existing operator/MCP tools; do not depend on condition webhook notifications. |
| Apps: browser, personal browser, GitHub webhook | `src/flyte_agent_factory/browser.py:64`, `src/flyte_agent_factory/browser.py:101`; `src/flyte_agent_factory/_webhook.py:246` | **partial** | OSS `manager/cmd/components.go:268` enables internal apps; `app/internal/k8s/app_client.go:612` injects secret metadata and `app/internal/k8s/app_client.go:700` maps container resources. `flyte-gke/k8s/flyte/values.yaml:44` enables internal apps; Knative MCP is deployed at `flyte-gke/k8s/mcp/app/app.py:75`. `requires_auth` is not enforced by the OSS app path; preflight reports GPU/cluster-pool limitations. | Reuse Knative/Kourier and the stack's ingress/proxy pattern. Provision service accounts, routes and browser storage; protect browser and webhook ingress explicitly. CPU/memory support does not establish GPU app support. |
| `flyte.sandbox.create` | `src/sandbox_probe.py:62`; `src/native_child_probe.py:78`; instructions: `_instructions._project_memory` | **partial** | SDK `sandbox/_code_sandbox.py:111` calls `flyte.build`; `sandbox/_code_sandbox.py:312` skips it when `image` is supplied. SDK `_initialize.py:762` forces remote building inside task initialization. OSS has no remote builder (next row). | Supply an existing, locally built `image=` or `.run(image=...)`; change the generated instructions that recommend runtime `packages=` builds. |
| Remote image builder | `src/flyte_agent_factory/_deploy.py:_INIT`; sandbox instructions above | **no** (OSS has none); the factory does not ask for it on OSS | SDK `_internal/imagebuild/remote_builder.py:60` targets the `system` project, `_internal/imagebuild/remote_builder.py:61` production domain; `_internal/imagebuild/remote_builder.py:131` calls ImageService; `_internal/imagebuild/remote_builder.py:164` raises “remote image builder is not enabled”. OSS `manager/cmd/components.go:216` setup and `runs/setup.go:147` service mounting register no ImageService or build-image task. | Done: `_deploy._INIT` passes `image_builder=_profile_oss.builder(backend)`: `remote` on Union, `local` on OSS unless `[backend] image_builder` says otherwise. Sandbox instructions still need prebuilt images. |
| Local builder (`local`, `local-podman`) | Factory image recipes: `src/flyte_agent_factory/_images.py:301`; builder selection: `src/flyte_agent_factory/_profile_oss.py:builder`, used by `_deploy._INIT` | **yes** outside task pods | `flyte-gke/k8s/flyte/config.yaml.tmpl:6` selects `local-podman`; `flyte-gke/scripts/up.sh:352` builds the MCP image with it. SDK `_initialize.py:456` honors configured builder. | Done: `local` is the OSS default; `local-podman` is `[backend] image_builder = "local-podman"`. The deploy host needs the container tool and push access to the registry. Keep `[deploy] auto = false`: the in-pod self-deployer has no docker. |
| `flyte.Secret` injection | `agent._credentials_secret`, `agent._secrets`, the `agent.env` task environment; `src/flyte_agent_factory/_webhook.py:253` | **partial** for this deployment | OSS `executor/pkg/webhook/secrets.go:11` creates the secret mutator; apps propagate requests at `app/internal/k8s/app_client.go:612`. flyte-gke's agent instead mounts Secret Manager CSI files (`flyte-gke/k8s/agent/chart/templates/secrets.yaml:9`). That CSI configuration alone does not satisfy arbitrary factory Flyte secret keys. | Populate matching project/domain Flyte secrets or explicitly mount the required CSI files and translate to existing environment bindings. Provision task/app identities and admit the resulting pods. |
| Secret creation | `agent.gcp_bootstrap` (its `store` step); `scripts/export_local_credentials.sh:99` | **yes** in OSS source; deployment untested | SDK `remote/_secret.py:70` initializes the service client. OSS `manager/cmd/components.go:274` registers SecretService; `secret/service/secret_service.go:60` implements it using Kubernetes Secrets. This is distinct from GCP Secret Manager CSI. | Keep the SDK calls if using OSS-managed secrets; check namespace/scoping, webhook admission and refresh visibility. If choosing CSI instead, write-back requires an explicit Secret Manager integration. |
| Scheduled `Trigger` (`FixedRate`/Cron) | `src/flyte_agent_factory/_factory.py:1132`; `src/flyte_agent_factory/_deployer.py:72`; `src/flyte_agent_factory/_reconcile.py:277`; `src/flyte_agent_factory/_loop_flow.py:484`; `src/flyte_agent_factory/_bench_flow.py:1270` | **yes** | SDK `_internal/runtime/trigger_serde.py:20` serializes both schedule types. OSS `runs/setup.go:176` mounts TriggerService, `runs/setup.go:202` starts the scheduler when enabled (default true: `runs/config/config.go:35`); `runs/scheduler/core/schedule_time.go:39` handles fixed rate and `runs/schedule/cron.go:12` parses cron. | Keep scheduled reconciliation and dispatch; check scheduler configuration and a real firing before cutover. Disable auto-deploy until image deployment is ported. |
| Artifact-driven `Trigger` | `src/flyte_agent_factory/voice.py:62`, `src/flyte_agent_factory/voice.py:68` | **no** | OSS `runs/repository/transformers/trigger.go:48` assigns schedule automation only when a schedule exists, otherwise TYPE_NONE. The source has no mounted ArtifactService, and the scheduler is schedule-based. | Leave optional voice retraining undeployed for the first factory launch; invoke its task directly with a `Dir` input if needed. Artifact events require a separate implementation. |
| `remote.Artifact` catalog | Demo publishing: `_demo_checks._publish_demo_artifact`; voice model lookup: `_demo_checks._fetch_voice_model`, `voice.train`; generated instructions: `_instructions._project_memory` | **no** | SDK `remote/_artifact.py:610`, `remote/_artifact.py:683`, `remote/_artifact.py:799` require ArtifactService. OSS `manager/cmd/components.go:216` and `runs/setup.go:147` mount no ArtifactService; generated artifact protobufs are not a serving implementation. | Use existing task `Dir`/`File` outputs, GitHub demo packages and reports with explicit uploaded URIs. Change the generated discovery instructions; disable optional voice catalog lookup. Demo publication currently catches errors (`_demo_checks._publish_demo_artifact`), so core work can continue, but catalog access will fail. |
| `remote.upload_dir` / `upload_file` | `_demo_checks._publish_demo_artifact`; `voice.train`; both APIs taught in `_instructions._project_memory`; `_slow_lane._upload_lane` uploads its tree with `upload_file` | **yes** | SDK `remote/_data.py:401` requests a signed upload location; `remote/_data.py:476`, `remote/_data.py:500` expose the two helpers. OSS `dataproxy/service/dataproxy_service.go:65` implements CreateUploadLocation. flyte-gke grants signing permission (`flyte-gke/README.md:187`). | Keep uploads independently of artifact registration. Check backend signed URLs and task access to the resulting GCS URI. |
| `flyte.Resources` with a request and a limit | `_slow_lane.make_lane_resources`: `cpu=(request, limit)` and `memory=(request, limit)` for each lane child (three quarters of the CPUs as the request, all as the limit) | **partial**: SDK serialization only, not checked against OSS source | SDK `_resources.py:450` defines `Resources`, which takes a `(request, limit)` tuple for `cpu` and `memory`. Whether the OSS executor applies both to the pod was not inspected here. | If limits are ignored, a shard that exceeds its memory is not killed (no `OOMError`) and the lane takes the node's pressure instead; set the request alone and size `AGENT_FACTORY_LANE_CPUS` to the node. |
| `flyte.Timeout(max_queued_time, max_runtime)` and `MaxQueuedTimeExceededError` / `MaxRuntimeExceededError` | `_slow_lane.make_lane_task` sets both on every attempt, and `_slow_lane._run_shard` reads the two errors: each attempt is bounded, and the two errors tell capacity (queue bound) from the change (runtime bound) | **partial**: SDK only, not checked against OSS source | SDK `_timeout.py:31` defines `Timeout`; `errors.py:236` and `errors.py:246` define the two errors, and `_internal/runtime/convert.py:639` maps the server's `QUEUED_TIMEOUT_EXCEEDED` / `MAX_RUNTIME_EXCEEDED` codes to them. Whether the OSS executor enforces both bounds and reports those codes was not inspected here. | Without the queue bound a lane waits forever for room, and without the runtime bound it runs past the caller's limit; bound them in the launcher with its own timers. |
| `flyte.errors.OOMError` | `_slow_lane._run_shard`: a shard killed for its memory limit is the change (exit 1, record `out of memory`), never capacity; `LaneEnded.as_watch_error` gives the same for a lost watch | **partial**: SDK only, not checked against OSS source | SDK `errors.py:97` defines it; `_internal/runtime/convert.py:716` raises it for a user error whose code contains `OOM`. Whether OSS reports an OOM kill with such a code was not inspected here. | A kill reported under another code is retried as a child that did not report, then reported as capacity: read the child's termination reason instead. |
| Child actions via `child.attach()` | `src/flyte_agent_factory/child.py:60`, `src/flyte_agent_factory/child.py:95`, `src/flyte_agent_factory/child.py:107`; the slow lane attaches through `_slow_lane._attach` | **partial**: the endpoint is passed on OSS; child placement under the parent is untested | SDK `_interactive_run_context.py:101` restores the context; `_internal/controllers/__init__.py:194` creates the Rust controller. Factory calls `create_controller("rust")` without endpoint; SDK `_internal/controllers/remote/_r_controller.py:142` explicitly requires an API-key environment when endpoint is absent. OSS does not supply Union API keys. | Done: `_union_identity.controller_kwargs` passes `flyte.init_in_cluster(endpoint=internal_endpoint, insecure=...)` on OSS, and `_profile.pod_env` sets `_U_EP_OVERRIDE`/`_U_INSECURE` on every environment (`_profile_oss.endpoint_env`). Probe child placement and outputs under the same parent run, including retry. |
| Run reports / `flyte-note` | `src/flyte_agent_factory/_bridge.py:445`; `_session_setup._install_bridge_commands`; status reader: `src/flyte_agent_factory/_loop_flow.py:99` | **yes** | SDK `report/_report.py:174`, `report/_report.py:180` uploads report.html; OSS `executor/pkg/controller/taskaction_controller.go:1213` records its URI; `dataproxy/service/dataproxy_service.go:413` resolves reports for signed reads. | Keep the bridge and dashboard. Validate incremental writes plus operator/MCP report retrieval; signed GCS URLs need backend signing permission. |
| Factory MCP server and caller identity | `src/flyte_agent_factory/_factory_mcp.py:90`, `src/flyte_agent_factory/_factory_mcp.py:93`; `whoami` tool: `src/flyte_agent_factory/_factory_mcp.py:33` | **partial**: behind a proxy, auth is the proxy's | SDK `ai/mcp/_flyte_mcp_app.py:477`, `ai/mcp/_flyte_mcp_app.py:492` selects bearer passthrough with requires_auth. flyte-gke uses requires_auth=False and explicit internal initialization (`flyte-gke/k8s/mcp/app/flyte_mcp_server.py:52`, `flyte-gke/k8s/mcp/app/flyte_mcp_server.py:67`) behind OAuth proxy; README `flyte-gke/README.md:166` describes the chain. OSS `runs/service/identity_service.go:59` rejects whoami when forwarded identity is not trusted; stack `flyte-gke/k8s/flyte/values.yaml:67` disables that trust. | Done: `_factory_mcp.app` has `requires_auth=False` and an empty `flyte.Resources()` when `_profile_oss.mcp_auth` is `proxy` (the OSS default); the tools and the project/domain scope check are unchanged. The app does no authentication: it MUST sit behind an OAuth proxy. `whoami` is not meaningful without a trusted forwarded identity. |
| Union API keys and endpoint selection | Every one through `_union_identity.init_for_script`: deployment children in `_deploy._INIT` (an API key only inside the deployer, which sets `_deploy.DEPLOY_KEY_ENV`; a hand deploy strips it, `_deploy.strip_hand_deploy_keys`); operators, as their own CLI login, in `ops/operate.py`, `ops/wait_deploy.py`, `ops/report_tail.py`, `ops/wait_event.py`, `ops/plan_dry_run.py`, `ops/meter_logins.py`, `ops/model_eval/remote.py` (`Backend`), `ops/probes/planner_smoke.py`, `ops/probes/podman_rootless.py` | **no** for Union key authentication; the factory needs none on OSS | SDK `_initialize.py:518` decodes a Union API key to configure the client; `_initialize.py:715` still falls back to injected Union keys in pods. OSS service registration has no API-key issuing service. IdentityService **does exist** (`runs/setup.go:159`), but does not provide Union key issuance/authorization. `flyte-gke/README.md:172` explicitly says the API has no authorization. | Done: with no `[union]` keys on an OSS profile (`_profile_oss.keyless`) `_union_identity.pod_spec` mounts nothing, `establish` returns `OSS_IDENTITY`, `init_for_script` uses the profile's config or `flyte.init(endpoint=internal_endpoint, insecure=...)`, and `controller_kwargs` passes the endpoint. Keep the API private behind tailnet/network policies. The public MCP OAuth bearer is not a Union API key. |
| Label-filtered run listing | `src/flyte_agent_factory/_factory_base.py:list_runs`, `list_runs_aio`, `iter_runs_aio` | **partial**: OSS rejects server-side label filters | OSS answers `Run.listall(with_labels=...)` and `with_label_keys=...` with `failed to convert filters: invalid filter field: labels...`; Union filters on the server. | Done: the factory asks with the server filter first and, on that exact `ConnectError` only (`_factory_base._rejects_labels`), repeats the listing unfiltered and matches labels and label keys client-side (`_has_labels`), keeping `limit` (the SDK's 100 when none is given). Every label listing goes through it, including `_reconcile.project_runs` and `_loop_flow._spend`. Every other error propagates. Cost: the whole listing is read on OSS. |
| Union console URLs, browser bearer injection and hosted endpoints | Profile: `src/flyte_agent_factory/_profile.py:61`, `src/flyte_agent_factory/_profile.py:262`; browser keys: `src/flyte_agent_factory/_local_browser.py:236`; token in the browser MCP config: `_session_setup._write_mcp_config`; instructions: `_instructions._console_auth_note`, `_instructions._recording_note`; probes: `src/browser_probe.py:48`, `src/demo_chain_probe.py:48`, `src/egress_probe.py:43` | **partial** | Console address is configurable, but `src/flyte_agent_factory/_profile.py:267` constructs a particular /v2 route; browser flow expects hosted bearer credentials. `flyte-gke/README.md:172` uses private network access instead. Search of shareable factory src/ops/scripts found no literal `*.unionai.cloud` host; private profiles and historical briefs are separate deployment inputs. | Set existing endpoint/console/signin/MCP profile fields to OSS addresses, or leave optional console fields empty. Remove Union console instructions/token requirements for OSS; use tailnet browser access. Check the OSS console route before advertising run links. |
| Union plugin, private SDK imports and CLI | `pyproject.toml:24`; stale volume comments: `src/flyte_agent_factory/_images.py:188`; private imports: `src/flyte_agent_factory/child.py:60`, `src/flyte_agent_factory/child.py:95`, `src/native_child_probe.py:62`; credential CLI: `scripts/export_local_credentials.sh:99` | **partial** | The plugin is installed, but no active `union` Python import or `union` CLI invocation was found in shareable src/ops/scripts. Factory state uses native primitives (`src/flyte_agent_factory/_state.py:14`). SDK private controller APIs exist as cited above; their names do not prove a proprietary dependency. `flyte create secret` has an OSS SecretService implementation; `flyte create api-key` does not gain OSS support from installing the Union plugin. | Do not attempt to port obsolete Volume/FUSE paths. Check whether the plugin's installed CLI extensions are needed, then remove the unused dependency in a separate implementation change if appropriate. Keep the SDK pinned while fixing/testing attachment. |
| Workspace/checkpoint and queue storage | `src/flyte_agent_factory/_state.py:66`, `src/flyte_agent_factory/_state.py:71`; filesystem dependencies: `src/flyte_agent_factory/_images.py:301` | **partial** for migration | Native Checkpoint/Dir state has no Union Volume requirement. flyte-gke configures GCS run/upload storage (`flyte-gke/k8s/flyte/values.yaml.gotmpl:37`); its IAM table `flyte-gke/README.md:187` distinguishes backend and task identities. The factory's own queue and state are on a git branch of the workspace repository ([state-store.md](state-store.md)), so they need no bucket access. | Nothing to configure for factory state. Launch fresh from GitHub state; Union run output URIs/history/queue are not automatically discoverable by the new backend. |

## Minimum cutover plan

Estimates are engineering time, assuming an operator has tailnet access, registry write access and the necessary GCP
permissions. They exclude waiting for credentials or cluster capacity. Reuse existing profile fields and Flyte APIs.

1. **Prepare the OSS deployment profile and credentials (0.5–1 day).** Set existing backend config, project/domain,
   image registry, repositories, slots and secret bindings. Begin with `[deploy] auto = false`; leave optional
   voice/browser/webhook components undeployed unless needed. Provision factory task/app accounts, secret injection,
   network admission. Closes secret injection/write-back, workspace and endpoint rows.
2. **Initialization and builds (done in code; 0.5 day to configure).** The API-key initializers and the remote builder
   are gone on OSS: `[backend] kind = "oss"` with `internal_endpoint` and the local builder
   ([Running the factory on OSS Flyte](#running-the-factory-on-oss-flyte)). Deploy from a build host with docker and
   registry push access; do not run the in-pod self-deployer (`[deploy] auto = false`). Still open: update sandbox
   instructions to require prebuilt images. Supporting automatic in-pod image builds is additional work, not
   necessary for the first relaunch.
3. **Remove catalog assumptions (0.5–1 day).** Child attachment already passes the explicit OSS endpoint
   (`_union_identity.controller_kwargs`); probe it on the cluster. Keep native task outputs and GitHub demo packages; report uploaded URIs without artifact
   registration and replace artifact-discovery instructions. Keep voice artifact automation off or launch its task
   directly. Closes child, artifact catalog and artifact-driven trigger rows.
4. **Deploy the factory MCP through the existing OAuth proxy (0.5–1 day).** The app already runs with
   `auth = "proxy"` (no authentication of its own); put the OAuth proxy in front of it. Preserve the project/domain scope check,
   host/origin checks; allow its required write/condition tools through the protected route. Remove unsupported whoami
   expectations. Configure existing public URL fields; check console links. Add browser/webhook ingress only if required.
   Closes MCP, app authentication, console and condition-notification delivery rows.
5. **QA the cutover and transfer ownership (0.5–1 day).** On the target SDK pin, deploy a bounded run that exercises a
   child under its parent, all three condition types, secret reads/write-back, report updates/reads, upload helpers,
   checkpoint retry, state-branch dispatch (deploy-key push to `factory-state/*`) and one scheduled reconciliation. Exercise MCP OAuth rejection and authenticated
   launch/status/signal. QA should also check the unsupported artifact behavior is explicit. Stop the Union project with
   its existing `/stop` path, confirm it saved/pushed and ended, then launch factory-improvement from the GitHub branch on
   OSS. Do not copy active queue requests or overlap supervisors. This validates all source-only verdicts before the move.

The minimum first migration is roughly **3–6 engineering days**, with optional voice/catalog, full browser integration
and automatic in-pod rebuilding outside that estimate. This document changes no deployment or running project.

## Not verified

- OSS enforcement of `max_queued_time` is unverified, so scheduled ticks guard themselves through the run listing
  and skip while an earlier run of the same task is live (`_tick_guard`; docs/triggers.md, "Ticks do not pile up").
- No authenticated live backend/MCP probe was possible: only the supplied 401 observation is available. No factory task,
  app, condition, trigger, report or upload was executed on flyte-gke during this audit.
- The running backend digest/configuration was not independently read from the cluster. Release source and the supplied
  snapshot may differ from what is currently running. The available `v2` branch is not evidence of a deployed upgrade.
- Factory SDK 2.10.7 interoperability with the stack used with SDK 2.12.0, including image/secret cluster routing and
  Rust controller endpoint handling, needs a target-cluster test. Passing the endpoint is a source-based proposed fix.
- Secret key scoping, webhook mutation, CSI versus managed-secret choice, task/app service accounts, admission policies,
  refresh visibility and deploy-key push rights to the state branch have not been tested for factory pods.
- Condition webhook absence is supported by supplied project experience and source inspection, not an observed delivery
  attempt. Union console/phone notification behavior is not provided by basic OSS condition support.
- Knative browser/webhook startup, internal endpoint resolution, persistent browser recordings, public routing, ignored
  GPU/cluster-pool app fields, per-caller authorization, console /v2 links and proxy token handling remain unprobed.
- Scheduler firing/catch-up timing, retry/checkpoint restoration, queue concurrency and cross-run workspace resume remain
  untested on GKE. Native primitives in source do not establish the factory's operational semantics there.
- Catalog and ImageService absence are findings from complete component/service registration and implementation searches;
  generated protocol definitions exist. No request was sent to confirm an Unimplemented response from the deployed binary.
- Private deployment profiles, their actual hosted `*.unionai.cloud` addresses, DNS/tailnet reachability and availability
  of GitHub/model credentials were not inspected. The shareable-code search is not a claim about private configuration.
- Union run history/checkpoints/artifacts cannot be assumed accessible from OSS; no export/import was attempted. A new run
  from GitHub preserves committed work, not unpushed workspace files, session transcripts or historical run identity.
- The installed Union plugin's incidental CLI behavior without Union credentials and removing its dependency were not
  tested. No runtime Volume dependency was found in the audited factory paths.

QA owns acceptance of these findings and the migration. Repository lint/tests check the documentation change's regression
surface; they cannot establish target-backend compatibility.
