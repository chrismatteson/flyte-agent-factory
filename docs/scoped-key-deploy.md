# Deploying as a project-scoped key

Design note for unionai-oss/flyte-agent-factory#40: a deploy that acts as the profile's `deployer_key`, holding only
`contributor` on one project and domain, completes every stage, and an install whose deployer predates `[union]` has a
supported way across. Setup steps are in [setup.md](setup.md#union-keys) and
[setup.md](setup.md#migrating-an-existing-install).

## What a project-scoped key is refused

Measured from a pod on flyte 2.13.5, acting only as such a key with the injected key taken out of its environment: remote image build, task
registration, app deploy, reading app status and polling `App.get`, `deactivate(wait=False)`, app delete, trigger create,
deactivate and delete, and secret create and delete all worked. Refused: the app watch stream (`App.watch`, and
`activate(wait=True)` / `deactivate(wait=True)`, which open it), because it needs view-executions on the organization.
The run watch stream (`watch_action_details`) is not refused: the remote image build waits on it and passed.

## Decisions

**Every wait in the deploy polls.** The factory stage's wait for `factory-mcp` was the deploy's only use of the app
watch stream. It now polls `App.get` through `_deploy_apps`, the same polling the browser apps use, and fails after the
apps' activation timeout with a message naming the app and its own conditions. Answer to "should every wait poll?": yes;
a deploy that depends on the stream needs an organization-wide permission for no gain, since an app's status is a cheap
unary read. `App.replace`, `App.listall` and `activate`/`deactivate` with the SDK's default `wait=False` open no stream.
The rollback (`_deploy_apps._roll_back`: `App.replace`, then `activate(wait=False)`), the replace path's
`deactivate(wait=False)` and the dropped-app scan (`App.listall`) are unary calls on apps in the project and domain;
`delete_retired_triggers` only calls `Trigger.delete`. `tests/test_deploy_scoped_key.py` scans the deploy's modules and child scripts for `watch` calls and `wait=True`.

**The deployer and its trigger are deployed last.** A factory-stage failure that happens after the deployer is replaced leaves a new `factory-deployer`
and its `deploy-on-merge` trigger live, retrying the same failure every tick. The factory stage therefore deploys every
other factory environment, waits for `factory-mcp`, and only then deploys the deployer. Chosen over a pre-check because the
failures that strand a deployer (an app that never activates, a refused call) only show up by doing them; a pre-check
would have to repeat the deploy to be sure. With the order changed, a failure leaves the deployer that was already
running, which still works, and the report records what the stage deployed before it failed.

**One message when a deploy cannot authenticate.** `_union_identity.check_script_identity` runs once in
`_deploy_child.main`, before any environment is imported, for the check and the deploy steps. On a Union backend in a
pod with no `AGENT_FACTORY_DEPLOY_KEY_ENV` and no CLI config it says the deployer that started it is too old to deploy
this engine and names `ops/deploy_as_key.py`; a key variable that is named but empty is named. OSS is unchanged. The
engine still never acts as the key Union injects into a pod.

**A one-off command.** `ops/deploy_as_key.py` calls `_deploy.check_in_tree` then `_deploy.deploy_in_tree`, as the
deployer does, acting only as the key in `AGENT_FACTORY_UNION_KEY`. `--check` runs the check only. Both stop with exit 1 when the check could not list the secrets (the key may
not work): the deployer goes on, because it retries every tick, but this command is run once by a person, who should rerun it once the key
works. A key variable whose value is not a Union API key is refused by the same preflight. It passes on none of the values only the deployer sets (`DEPLOYER_ONLY_ENV`: the canary's push mode, the tier
identities), nor other tiers' keys or the GitHub App's credentials; the new deployer's first tick redeploys the
same commit with them. How to run it in a
pod: [setup.md](setup.md#running-the-one-off-deploy-in-a-pod).

## Run-time calls (outside the deploy)

No run-time tier calls `App.watch`, `activate`/`deactivate(wait=True)`, or anything org-wide. Streams that remain, all
on the run watch stream scoped to an action in the project and domain (not refused):

| Where | Call | Tier |
|---|---|---|
| `src/flyte_agent_factory/_loop_flow.py:146` (benchmark run) | `run.wait.aio()` | queue (loop) |
| `src/flyte_agent_factory/_session_setup.py:772` (delegate reattach) | `action.wait.aio()` | session |
| `src/flyte_agent_factory/_deployer.py:825` (canary launch script) | `run.wait()` | queue (deployer) |
| `src/flyte_agent_factory/_factory_mcp.py:372` (`wait_for_run`, over `stock_wait`) | run wait | project |
| `src/flyte_agent_factory/agent.py:1391`, `_bridge.py:1071`, `_inbox.py:148`, `_grants_supervisor.py:164` | `condition.wait.aio()` (in-pod controller, the pod's own identity) | project, session |

For a follow-up, not a deploy path: `ops/probes/planner_smoke.py:137-138` calls `deactivate(wait=True)` and
`activate(wait=True)`, so it fails under a project-scoped key; `_bootstrap_flyte.py:150-168` lists and creates projects,
which is org-wide by design and runs as a person's CLI login.
