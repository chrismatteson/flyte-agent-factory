# factory-mcp on the stock flyte 2.13.5 MCP server

The factory's MCP server (`src/flyte_agent_factory/_factory_mcp.py`, app `factory-mcp`) moved from flyte
2.10.7 to flyte 2.13.5. That release contains flyteorg/flyte-sdk#1736 (protected resource metadata), #1735
(MCP logging and human-in-the-loop fixes) and #1777 (CLI parity: secrets, diagnose-and-recover,
launch-and-operate). The engine as a whole moved too: `flyte[mcp]>=2.13.5,<2.14.0` and
`flyteplugins-union>=0.21.2,<0.22.0` (`pyproject.toml`, `uv.lock`). Line references to the SDK below are to
the installed 2.13.5, `flyte/ai/mcp/`.

Related: [mcp-sdk-requirements.md](mcp-sdk-requirements.md) (what is still missing upstream, ready to file),
[mcp-operator-smoke.md](mcp-operator-smoke.md) (the live smoke from chat after redeploy).

## Volumes did not hold the pin

The old pin's stated reason was the Volume API in `flyteplugins-union` 0.15.x. The engine uses no Volume API
from `flyteplugins-union`: its only use is `flyteplugins.union.remote` (API keys, policies, assignments) in
`_bootstrap_flyte.py`. The Volume methods named there (`from_locator`, `fork`, `RWVolume.finalize`) exist with
the same signatures in 0.21.2 anyway. So the MCP app and the slots stay on a single SDK. No split SDK version
is needed.

## Today's wrappers

At 7c60bfc the factory layer was a stock `FlyteMCPAppEnvironment` plus the items below. "Kept" means the stock
server at 2.13.5 still does not do it. The `test_stock_*` tests run the stock 2.13.5 tool to show the gap
each kept wrapper fills. Tests are in `tests/test_factory_mcp.py`.

| Wrapper at 7c60bfc | Now | Evidence | Why it is not stock configuration |
|---|---|---|---|
| Private imports: `ALL_MCP_TOOLS`, `TOOL_REGISTRY` from `_flyte_mcp_app`; `build_tool_functions` from `_tools` | **Removed.** `TOOLS` is the public `ALL_MCP_TOOLS`. Annotations and functions are read from the tools the stock server registered. | `test_no_module_imports_a_private_module_of_flyte_ai_mcp` (AST scan of `src`) | n/a |
| Starlette app rebuilt by hand to add the grant page (`app._starlette_app = app._create_starlette_app()`) | **Removed.** The page is served by a small ASGI middleware installed through `_starlette_middleware`. The SDK documents that hook as "Subclasses may override" (`_mcp_app.py:139`). The page keeps the same path and sits behind the same stock auth. | `test_the_grant_page_is_served_by_the_factory_app_at_its_public_path`, `test_the_grant_page_is_behind_the_stock_auth_of_each_mode`, `test_the_factory_mcp_server_uses_no_private_name_of_the_flyte_environment` | The stock app has no public extra-routes setting (requirement 5 in the SDK doc). |
| `signal_condition` reimplemented on `environment._scoped` and the private `flyte.remote._condition` helpers | **Removed as a reimplementation; the behaviour is kept** (see the `signal_condition` row). The wrapper now calls the stock `signal_condition` and `list_conditions`. | `test_the_reroute_is_made_in_the_factorys_project_through_the_stock_tools` | n/a |
| `instructions` | **Stock setting** (already was). | `test_the_stock_instructions_setting_is_what_the_server_tells_a_client` | n/a |
| Docs corpus moved out of `/root` | **Stock setting**: `sdk_examples_path`, `docs_examples_path`, `full_docs_path` (`_flyte_mcp_app.py:510-512`). The defaults are still under `/root`, which the unprivileged image user cannot read, so the factory sets them to `/opt/flyte-mcp-docs`. | `test_the_stock_docs_paths_are_what_the_search_tools_read`, `test_the_image_pins_the_same_flyte_as_the_project_and_its_tag_is_the_installed_one` (docs tag `v2.13.5` equals the installed flyte) | Setting only; the factory just supplies a path its image can read. |
| Allowed hosts and origins | **Stock setting** (`allowed_hosts`/`allowed_origins`, `_flyte_mcp_app.py:503`). | `test_the_stock_allowed_hosts_and_origins_settings_refuse_another_host_and_origin` | n/a |
| Secrets | **Stock behaviour**: `create_secret` returns a one-time `/secret-input/` link and the value never goes through chat (`_secret_tools.py:199`). `allow_inline_secret_values` stays False. One replica, because the links live in process memory. | `test_create_secret_does_not_echo_the_value`, `test_the_secret_input_route_is_served_by_the_factory_app`, `test_the_secret_value_is_not_an_argument_of_any_served_tool`, `test_the_stock_scaling_setting_is_one_replica_for_the_secret_input_links` | n/a |
| Scope: every tool limited to the factory's project and domain (`_build_scope_check`) | **Kept and widened.** It now covers any parameter that ends in `project` or `domain`. That includes `run_task`'s new `run_project`/`run_domain` (`_tools.py:523`), which would otherwise create runs in another project. | `test_a_tool_refuses_another_project_or_domain_before_any_remote_call` (parametrised over every served tool's scoped parameters, read from the live server), `test_stock_run_task_runs_in_any_project_or_domain`, `test_stock_list_secrets_reads_any_project` | `resolve_scope` (`_flyte_mcp_app.py:559`) only supplies a default. No stock option restricts calls to one project. `task_allowlist` and the other allowlists filter by name, not by project. |
| `list_projects` shows only the factory project | **Kept.** | `test_stock_list_projects_lists_every_project` | Stock lists every active project (`_tools.py:1342`). |
| `get_project` refuses other projects | **Kept.** | `test_stock_get_project_gives_any_project` | Stock returns any project (`_tools.py:1364`). |
| Patient `wait_for_run` / `get_run_io` (wait until a SUCCEEDED run's outputs are readable) | **Kept.** It calls the stock tools by keyword and mirrors the new signature (`action_name`, `inputs_only`, `outputs_only`). The positional call would have mis-bound on 2.13.5. | `test_stock_get_run_io_gives_null_outputs_on_a_succeeded_run_the_read_model_still_shows_going`, `test_stock_wait_for_run_returns_before_the_outputs_are_readable`, `test_the_wrappers_keep_the_stock_input_schema_and_only_the_patient_ones_change_the_text` | Stock `wait_for_run` returns when the watch stream ends (`_tools.py:685`). Stock `get_run_io` reads `Run.get` once and returns `outputs: null` without an error while the read model lags (`_tools.py:724`). Neither #1735 nor #1777 changed this. |
| `signal_condition` takes any answer to an `ask-` and reroutes a late one | **Kept**, on the stock tools. If an ask's answer cannot be signalled, the wrapper retries a bool ask with `parse_approval`. If that fails too, it sends `/late-answer <ask> <value>` to the run's open `direct-` condition. | `test_stock_signal_condition_raises_on_a_sentence_for_a_bool_ask`, `test_stock_signal_condition_raises_on_an_ask_that_has_ended` | Stock coerces strictly and raises on "yes please" or on an ended condition (`_tools.py:1490`). The inbox convention is the factory's own. |
| Grant page | **Kept** (factory content). | as above | Factory-only page. |
| Tool set: every stock tool | **Stock setting**, unchanged policy: `TOOLS` is the public `ALL_MCP_TOOLS`, now 39 (2.13.5 added `delete_app`, `create_trigger`, `delete_trigger`, `get_secret`, `update_secret`, `generate_secret`, `get_condition`). Each new tool takes `project`/`domain` and is limited by the scope wrapper like the rest; none returns a secret value (`update_secret` uses the same one-time link as `create_secret`). | `test_enables_every_stock_tool` (unchanged, except its comparison with the private `TOOL_REGISTRY` is now against what a plain stock environment registers), `test_a_tool_refuses_another_project_or_domain_before_any_remote_call` (parametrised over all 39) | n/a |
| (added by #49) One layer over every served tool: `_serve` in `_factory_mcp.py` (scope check, time budget with a resumable `still_working`, the OSS URL fix, redaction of the result and of an error's message) | **Added.** Every tool, stock or factory, is registered through it. | `test_every_served_tool_is_wrapped_by_the_one_layer` | The stock server has no per-call deadline, no operation to resume and no result redaction (gaps in docs/mcp-sdk-requirements.md). |
| (added by #49) `get_app`, `activate_app`, `deactivate_app` replaced by `_app_tools` | **Added.** Same names; they return env and secret *names* only. | `tests/test_app_tools.py::test_no_tool_result_carries_an_environment_value` | The stock app tools return the app's `to_dict()`, environment values included. |
| (added by #49) `get_logs` on OSS (`_oss_follow.wrap_get_logs`) | **Added, OSS only.** On Union the stock tool is served untouched. | `tests/test_oss_follow.py` (`test_oss_logs_use_the_real_attempt_when_the_details_say_zero`, `test_a_missing_log_stream_gives_the_gap_and_its_alternatives`, `test_union_logs_are_the_stock_tool_untouched`) | OSS resets an action's attempt to 0 once it starts a child, so the stock tool asks for a stream that does not exist (gap 9 in docs/mcp-sdk-requirements.md). |

One private read remains, and it is in the `mcp` python-sdk, not flyte. `_registered_tool_functions` reads
FastMCP's `_tool_manager` to get each stock tool's function, because FastMCP has no public getter. It is one
helper, and requirement 4 in the SDK doc is what would remove it.

## What else changed between flyte 2.10.x and 2.13.x

| Area | Outcome |
|---|---|
| Image builder, unprivileged final image (#30/#32) | Unchanged in what the factory relies on. `_CREATE_FLYTE_USER_CMD` is still there, and only the uv builder image moved (0.8.13 to 0.13.4). `tests/test_image_user.py` and `tests/test_images.py` pass unchanged. |
| Reusable containers | `_reusable_environment.py` is byte-identical. |
| `flyte.sandbox` | Same modules and API. Its users' tests pass. |
| Conditions | `flyte.remote.Condition` gains fields. The helpers still exist; the factory imports none of them. |
| `Action.wait`/`details`, `ActionDetails.inputs`/`outputs`/`output_literals` | Now `syncify_compat` objects. Awaiting the call directly still works inside a loop, with a DeprecationWarning, but mypy rejects it. The six flagged call sites now use `.aio()` (`_loop_flow.py`, `_session_setup.py`, `_slow_lane.py`, `factory-improvement/context_guard_bench.py`), and the test fakes follow the real shape. |
| `flyte._deploy` | Passes `queue=` to the image build. One test stub was updated to match. |
| `get_logs` | Returns a dict and is a tail: the last `max_lines` lines, oldest first; `before_line=next_before_line` pages back and `before_line=max_lines` reads from the start (`_tools.py:1026-1033`). The instructions and the skill describe it that way, and their advice to use a small `max_lines` holds. |

## For the owner: what changes on redeploy

Both clusters:

- factory-mcp runs flyte 2.13.5. The image installs `flyte[mcp]>=2.13.5,<2.14.0`, and the docs corpus is the
  `v2.13.5` examples.
- **Secrets.** `create_secret` no longer takes a value in chat. It answers with a one-time link (valid 10
  minutes) that you open in a browser to type the value. `list_secrets` shows the name once it is saved.
- **Project limit.** Still enforced, and it now also covers `run_task(run_project=..., run_domain=...)`, which
  2.13.5 added.
- **Tools.** Every stock tool, as before: 39 now. New: `delete_app` (prefer `deactivate_app`; a deleted app
  returns only by redeploy), `create_trigger`/`delete_trigger`, `get_secret` (exists/ready, never the value),
  `update_secret` (one-time link, like `create_secret`), `generate_secret` (SSH key pair; only the public key comes
  back) and `get_condition`. All are limited to the factory's project and domain.
- **Grant page.** Same path and same auth as before.
- The slots, deployer and every other image also build on flyte 2.13.5, since the engine has one pin.
- **Benchmark.** The ml-pipeline brief names the Flyte 2 SDK without a version (the check image ships the
  engine's flyte). The suite digest changes with it, so bench results from before this change are not compared
  with later ones.

Work cluster (Union, `[mcp] auth = "union"`): Union still authenticates at the ingress (`requires_auth=True`).
Protected resource metadata (#1736) stays unset, so the auth flow is unchanged.

Personal cluster (open-source Flyte, `auth = "proxy"`): still `requires_auth=False` behind the OAuth proxy,
and the app does no auth of its own. Leaving protected resource metadata unset matters here: setting it would
switch the app to requiring a bearer.

Rollback is to redeploy the previous engine commit (see [mcp-operator-smoke.md](mcp-operator-smoke.md)).
