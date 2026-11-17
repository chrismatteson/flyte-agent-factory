# Requirements for flyte-sdk from the factory MCP server

What the factory's MCP server (`src/flyte_agent_factory/_factory_mcp.py`, the app `factory-mcp`) still has to build
on top of Flyte's stock `FlyteMCPAppEnvironment`, written so the owner can file each gap upstream at
[flyteorg/flyte-sdk](https://github.com/flyteorg/flyte-sdk). Nothing here is filed yet. For the server itself see
[factory-mcp-setup.md](factory-mcp-setup.md).

Evidence is from `flyte[mcp]==2.13.5` (the version the server now runs, up from 2.10.7; see
[mcp-stock-move.md](mcp-stock-move.md)), read from an installed copy. Paths are relative to `flyte/ai/mcp/` in that package. Each section is one gap, and they are ranked by what
removing it is worth, most first:

| # | Gap | Why it ranks here |
|---|---|---|
| 1 | No way to limit tool calls to one project and domain | A security boundary the factory enforces by wrapping every tool, by parameter name; a new kind of parameter has to be caught by a test |
| 2 | No wait for a SUCCEEDED run's outputs to become readable | Hit on every chat launch (`run_task`, `wait_for_run`, `get_run_io`); the failure is silent |
| 3 | `signal_condition` is strict about the answer and fails on an ended condition | The owner's answer is lost to the project |
| 4 | No public way to read, wrap or replace a registered tool | Forces the one private read that is left, in the wrappers above |
| 5 | Custom HTTP routes only through an underscore-named hook | One static page (the credential grant page) |

The factory rebuilds a tool by removing it and adding a wrapper (`_use_the_factorys_tools`), and each wrapper
copies a stock tool's signature by hand or through `functools.wraps`. A stock tool that gains a parameter is
therefore only reachable through a wrapper that has it too; see
[what 2.13.5 already changed](#what-2135-already-changed-under-the-factorys-wrappers). The factory imports no
private module of flyte any more (`test_no_module_imports_a_private_module_of_flyte_ai_mcp`); what is left is one
read of a private attribute of the `mcp` python-sdk (gap 4).

## 1. Restrict tool calls to one project and domain

### Problem

A server deployed for one project (the factory's) hands every tool the caller's whole Union or Flyte scope. The
caller can name any project and domain, so a chat connected to `factory-mcp` could list, run, abort and read logs
in every project its credentials reach. The stock server has no option to say "this server serves project X
only"; the only server-wide setting is a default for when the argument is omitted.

### Evidence

At 2.13.5:

- `_flyte_mcp_app.py:559-585`, `resolve_scope`: explicit argument first, then `FLYTE_MCP_PROJECT` /
  `FLYTE_MCP_DOMAIN` (`_flyte_mcp_app.py:83-84`), then the init config. The environment variables are defaults, not
  limits: a caller's explicit `project="other"` wins.
- `_flyte_mcp_app.py:587`, `_scoped`: installs whatever `resolve_scope` returned and runs the call there as the
  caller.
- `_tools.py:523-524` and `_tools.py:541-542`, `run_task`: besides `project` and `domain` (where the task is
  registered) it takes `run_project` and `run_domain`, applied by `_operate_tools.py:81-82` through
  `flyte.with_runcontext(project=..., domain=...)`. These bypass `resolve_scope` entirely, so no scope check
  placed inside `resolve_scope` would see them.
- `_tools.py:1342-1362`, `list_projects`: lists every project on the control plane, with no scope at all.
  `_tools.py:1364`, `get_project`: takes any name.
- `_flyte_mcp_app.py:498`, `task_allowlist`: limits which tasks `run_task` may start (`_tools.py:108`,
  `_tools.py:543`), not where the run goes and not any other tool.

What the factory builds to cover it, in `_factory_mcp.py`:

- `_scoped_to_the_factory` wraps every registered tool that has a scoped parameter and raises
  `ToolError("... is not allowed; this server is limited to ...")` for a different non-empty value. A parameter
  is scoped when `_scoped_arguments` finds its name ending in `project` or `domain`, so `project`, `domain`,
  `run_project` and `run_domain` are all checked, and so is any parameter a later SDK release names the same way.
- `_list_only_the_factory_project` and `_get_only_the_factory_project` replace `list_projects` and `get_project`.
  The first asks the stock tool for `10**6` rows and filters, because the stock `limit` counts rows before any
  filter.
- `_use_the_factorys_tools` applies the wrappers to every tool the server registered, reading the stock functions
  through `_registered_tool_functions` (gap 4).
- The name rule is a convention, not a guarantee: a parameter that picks a project under another name would not
  be scoped. `test_every_tool_that_takes_a_project_or_domain_is_covered_by_the_scope_tests` and
  `test_a_tool_refuses_another_project_or_domain_before_any_remote_call` read every parameter of every served
  tool off the live server, so a release that adds one fails the tests instead of shipping. `run_project` and
  `run_domain` were added at 2.13.5 and are refused (`test_stock_run_task_runs_in_any_project_or_domain` shows the
  stock tool would run there). The live check is in
  [mcp-operator-smoke.md](mcp-operator-smoke.md#4-a-run_task-for-another-run_project-is-refused).

### Requirement

A server-level limit, enforced inside the SDK where scope is resolved, so that no tool can be added later that
forgets it:

```python
FlyteMCPAppEnvironment(
    ...,
    allowed_projects=["my-agents"],  # None (default): no limit, today's behaviour
    allowed_domains=["development"],  # None (default): no limit
)
```

- Enforced on every tool parameter that names a project or a domain: `project`, `domain`, `run_project`,
  `run_domain`, and any added later. The cleanest way is one check that every such parameter passes through, not
  one per tool.
- A refusal is a `ToolError` that names the parameter, the value and the allowed values.
- `list_projects` returns only allowed projects, applying `limit` after the filter. `get_project` refuses a project
  that is not allowed. Domains in a `get_project` result are filtered the same way.
- An empty or omitted argument still resolves as today (explicit, then `FLYTE_MCP_PROJECT` / `FLYTE_MCP_DOMAIN`,
  then the init config), and the resolved value is checked too. A default outside the allowed set is a startup
  error.
- With exactly one allowed project and one allowed domain, those are the defaults, so `FLYTE_MCP_PROJECT` and
  `FLYTE_MCP_DOMAIN` need not be set.
- `task_allowlist` keeps its meaning and composes with this: a task has to be in both.

### What the factory removes once it lands

In `_factory_mcp.py`: `_scoped_to_the_factory`, `_scoped_arguments`, `_list_only_the_factory_project` and
`_get_only_the_factory_project`, the scoping half of `_use_the_factorys_tools`, the `task_allowlist=None` comment
that points at `_scoped_to_the_factory`, and the `FLYTE_MCP_PROJECT` / `FLYTE_MCP_DOMAIN` entries of `env_vars` if
the defaults come from the allowed lists. The tests for the wrappers become one test that the constructor
arguments are passed. A parameter the factory's name rule would miss is then covered, because the SDK checks
where scope is resolved.

## 2. Wait until a SUCCEEDED run's outputs are readable

### Problem

`wait_for_run` returns when the run reaches a terminal phase, and `get_run_io` then reads its outputs. The two read
different models. Right after SUCCEEDED, `get_run_io` can return `outputs: null` with no error, which a client
reads as "the run produced nothing". The factory sees this for 10 to 60 seconds after a launch run succeeds, and
every chat flow is `run_task`, `wait_for_run`, `get_run_io`.

### Evidence

At 2.13.5:

- `_tools.py:685-722`, `wait_for_run`: waits on `run.wait.aio(quiet=True)` (`_tools.py:703`), the watch stream, then
  reports `phase`, `url` and `done`. It says nothing about outputs.
- `_tools.py:724-775`, `get_run_io`: reads outputs only `if not inputs_only and target.done()`
  (`_tools.py:750`), where `target` comes from `Run.get` (`_diagnose.py:179`, `io_target`). When `Run.get` still
  says the run is not done, `outputs` stays `None` and `outputs_error` is never set (it is set only when decoding
  raises, `_tools.py:749-754` and `:768-769`). The docstring says `outputs` is null "while the run is still going", which is not
  distinguishable from this case.

The factory code this forces, in `_factory_mcp.py`:

- `_wrap_tools_to_wait_for_outputs` builds `wait_for_run` and `get_run_io` wrappers: its inner `poll_outputs` polls
  the stock `get_run_io` (`outputs_only=True`) with a doubling delay (`_POLL_FIRST_S`, `_POLL_MAX_S`) up to
  `_OUTPUTS_WAIT_S` (90 s), the wrapped `wait_for_run` adds `outputs_ready` to a succeeded run's result, and the
  wrapped `get_run_io` calls the stock `get_run` to tell a failed run (null outputs are right) from a stale one.
  Both call the stock tools by keyword, so a parameter the wrapper does not know cannot shift into another's place.
- `_use_the_factorys_tools` swaps them in.
- `_SUCCESS_PHASES`, `_FAILURE_PHASES` and `_phase_name`, which exist only to read the phase the stock tools
  return as a string.
- A paragraph of `INSTRUCTIONS` that tells the model to retry `get_run_io` when `outputs_error` says the outputs are
  not yet readable.

### Requirement

Do it in the SDK, where both models are known:

```python
FlyteMCPAppEnvironment(..., outputs_ready_timeout_s=90.0)  # 0 disables the wait
```

- `wait_for_run` on a SUCCEEDED run waits, up to the timeout, until `get_run_io` would return the outputs, and
  adds `outputs_ready: bool` to its result.
- `get_run_io` on a run whose phase is SUCCEEDED but whose outputs cannot be read yet waits the same way. If the
  wait runs out it sets `outputs_error` ("not yet readable after Ns") rather than returning `outputs: null` alone.
- A FAILED, ABORTED or TIMED_OUT run returns immediately with null outputs, as now.
- A per-call `outputs_ready_timeout_s` parameter on `get_run_io` would let a client choose; the environment value
  is the default.

Alternatively, fix the read: if `get_run_io` took the terminal phase from the same stream as `wait_for_run`, the
wait would not be needed. That is the better fix, and the one the factory would prefer, but it is a backend change.

### What the factory removes once it lands

`_wrap_tools_to_wait_for_outputs` (with its `poll_outputs`), its two entries in `_use_the_factorys_tools`,
`_OUTPUTS_WAIT_S`, `_POLL_FIRST_S`, `_POLL_MAX_S`, `_SUCCESS_PHASES`, `_FAILURE_PHASES`, `_phase_name`, the `asyncio` and `time`
imports they need, and the `INSTRUCTIONS` paragraph about retrying. That also ends the hand copy of the stock tools'
signatures (see the end of this page).

## 3. Lenient `signal_condition`, and a hook for a signal that fails

### Problem

The factory's chat relays the owner's words as they are. A project's `ask-` condition may be typed `bool`, and the
owner answers "yes please". The stock tool raises. A condition that has already ended (timed out, or replaced by a
newer ask) also just errors. In both cases the owner's decision is lost, and the project, which is waiting on a
human, never hears it.

### Evidence

At 2.13.5:

- `_tools.py:1490-1522`, `signal_condition`: `coerce_condition_payload(value, expected)` at `_tools.py:1511`,
  then `condition.signal.aio(payload)` at `_tools.py:1512`, with no handling of either failure.
- `remote/_condition.py:344-345`: `_BOOL_TRUE = {"true", "1", "yes", "y", "t"}` and `_BOOL_FALSE`. `:348-360`,
  `coerce_condition_payload` raises `ValueError("could not parse 'yes please' as bool")` for anything else. The
  tool's docstring promises the coercion ("the string "true" is accepted"), but not any sentence.
- `_tools.py:1507-1508`: a condition that cannot be found raises `ToolError`; one that has ended fails inside
  `condition.signal.aio`.

The factory code this forces, `_signal_condition_that_reaches_the_project` in `_factory_mcp.py`. It wraps the
stock `signal_condition` and calls the stock `list_conditions`, so it imports nothing private and does not touch
`_scoped`: the stock tools take `project` and `domain` and set the scope for each call, which the
`flyte.remote.Condition` calls under them need (they read the scope off the init config). For an `ask-` condition
it tries the answer as given. If the stock tool raises `ValueError` or `TypeError`, or the server says the
condition had ended (`_bridge._ended_on_server` on `_errors.REMOTE_ERRORS`), it retries a bool ask with the answer
parsed by `_bridge_common.parse_approval`. If that fails too it lists the run's conditions through the stock
`list_conditions` for an open `direct-` one and signals `/late-answer <condition> <value>` to it, which the
project's session hands to `Bridge.late_answer`; with no open inbox, or an empty answer, it raises "NOT
delivered". The `INSTRUCTIONS` text about `rerouted: true` and "NOT delivered" describes it. The stock tool itself
is still behind the two private names (`_tools.py:1504`, `from flyte.remote._condition import
coerce_condition_payload, ...`, and `env._scoped` at `_tools.py:1506`), so a client of the SDK cannot rebuild the
coercion without importing them.

### Requirement

Two small, general pieces. Neither is factory-specific:

```python
FlyteMCPAppEnvironment(
    ...,
    condition_coercers={bool: parse_free_text_bool},  # type -> callable(value) -> payload, raise ValueError
    on_signal_failure=reroute,  # async callable(ctx: SignalFailure) -> dict | None
)
```

- `condition_coercers`: consulted before `coerce_condition_payload` for the condition's expected type. The SDK
  could also ship a `lenient_bool` (yes/no with trailing words) for those who want it, off by default so the
  documented behaviour does not change.
- `on_signal_failure`: called when coercion raises or `signal` fails, with the condition, the run name, the
  original value, the exception and whether the condition had ended. It may return a result dict (used as the
  tool's result) or `None` (the original error is raised). It runs inside the same scope as the tool.
- Both are public and typed; `coerce_condition_payload` and `resolve_condition_expected_type` become importable
  from `flyte.remote` so a hook needs no private module.

### What the factory removes once it lands

The body of `_signal_condition_that_reaches_the_project` shrinks to the reroute itself (a coercer for `bool`, which
replaces the `parse_approval` retry and its try-twice flow, and an `on_signal_failure` that finds the `direct-`
inbox and signals `/late-answer ...`). Gone: the wrapper's `try_signal` retry loop and its replacement of the stock
tool in `_use_the_factorys_tools`. Nothing private is imported today, so this gap is about how much the wrapper
has to guess (the error types it catches, the ended condition from the server's message), not about reaching in.

## 4. A public way to read, wrap or replace a registered tool

### Problem

Gaps 1 to 3 are all "run my code around a stock tool". The only way is to take the tool's function out of the
server, build a wrapper, remove the stock tool and add the wrapper. `remove_tool` and `add_tool` are public on
FastMCP; getting the function out is not, so the factory reads one private attribute of the `mcp` python-sdk and
every `mcp` upgrade has to be read for whether it moved.

### Evidence

At 2.13.5:

- `_flyte_mcp_app.py:840-875`, `_create_mcp_server`: tools are registered with `mcp.add_tool(fn, name=name,
  annotations=info.annotations())`. The comment at `:858-860` itself calls `_tool_manager._tools` private.
- FastMCP (`mcp==1.30.0`, `mcp/server/fastmcp/server.py:413` and `:451`) has public `add_tool` and `remove_tool`
  but no public way to read a registered tool's function: `list_tools()` (`:331`) returns the wire descriptions,
  not the callables. The only access is `server._tool_manager` (set at `:230`), whose `ToolManager.list_tools()`
  (`mcp/server/fastmcp/tools/tool_manager.py:41`) returns the `Tool` objects with `fn`, `title` and `annotations`.
- The SDK's `TOOL_REGISTRY` and `build_tool_functions` (`_tools.py:490`) are not used by the factory any more.
  Both are reachable only through underscore-named modules (`TOOL_REGISTRY` is in `__all__` of
  `_flyte_mcp_app.py`, `:51-61`), and the factory now takes each stock function and its annotations from the tool
  the stock server registered, so it neither rebuilds the stock tools nor keeps a copy of the annotations table.

The factory code this forces, all in `_factory_mcp.py`: `_registered_tool_functions`, the one helper that reads
`server._tool_manager` (a test holds it to one place, `test_the_factory_mcp_server_uses_no_private_name_of_the_flyte_environment`),
and `_use_the_factorys_tools`, which builds each replacement from those functions, then calls `server.remove_tool`
and `server.add_tool` with the registered tool's `title` and `annotations`.

### Requirement

Either of these, whichever fits the SDK better. Both make the wrapper a supported thing:

```python
# A: middleware around every tool call
FlyteMCPAppEnvironment(..., tool_middleware=[scope_check, outputs_wait])
#   async def middleware(call: ToolCall, next: Callable[[ToolCall], Awaitable[Any]]) -> Any
#   ToolCall has .name, .arguments (the bound parameters, mutable) and .ctx

# B: wrap or replace one tool by name
app.wrap_tool("get_run_io", lambda stock: patient_get_run_io(stock))
```

- The wrapped tool keeps the stock tool's signature, docstring and annotations, so the schema the client sees is the
  stock one, and a wrapper does not have to restate parameters. (Today a wrapper that restates them silently drops
  any the SDK adds: see the end of this page.)
- Public, documented, and covered by the SDK's tests. In the SDK `resolve_scope` is public already
  (`_flyte_mcp_app.py:559`); `_scoped` (`:587`) is not, and a wrapper that has to run code in a tool's scope should
  not need it.
- A is preferred: gap 1 becomes a one-line middleware if it is not built in, and it composes.

### What the factory removes once it lands

`_registered_tool_functions` (the `server._tool_manager` read, the factory's last reach into a private name) and
the `remove_tool` / `add_tool` swap in `_use_the_factorys_tools`. Together with gaps 1 to 3 this is most of the
module's code outside `INSTRUCTIONS`, and the `mcp` private attribute goes with it.

## 5. Public custom HTTP routes

### Problem

The factory serves one static HTML page next to the MCP endpoint: the credential grant page
(`_grant_page.py`, at `_grant_seal.PAGE_PATH`, `/flyte-mcp/grant`), which seals a short-lived credential in the
owner's browser so it never passes through chat ([credential-grants.md](credential-grants.md)). The environment
has no public way to add a route.

### Evidence

At 2.13.5:

- `_mcp_app.py:147-153`, `_starlette_routes`: "Subclasses may override to add endpoints", but the name is
  underscore-prefixed, and the Flyte subclass already overrides it (`_flyte_mcp_app.py:734-736`) with the
  protected-resource and secret-input routes, so a second override has to call `super()` and keep up with what
  the SDK adds there.
- `_mcp_app.py:139-145`, `_starlette_middleware`: "Subclasses may override to inject middleware", also
  underscore-prefixed, and also overridden by the Flyte subclass (`_flyte_mcp_app.py:754`), which returns the auth
  middleware. `_create_starlette_app` (`_mcp_app.py:163-201`) passes the list to `Starlette(middleware=...)`, so
  what a subclass appends after `super()`'s list sits inside the auth middleware.
- `_mcp_app.py:71-92`, `__post_init__`: the Starlette app is built once, there (`:92`), from the routes at that
  moment. A route registered later is not served.
- `_mcp_app.py:163-196`, `_create_starlette_app`: mounts FastMCP's ASGI app at `mcp_mount_path`, then the extra
  routes. Starlette gives a request under a mount to the mount alone, so an extra route at `/flyte-mcp/grant` is
  never reached: the page cannot be one of these routes.
- FastMCP's public `custom_route` (`mcp/server/fastmcp/server.py:721`) puts a route inside the MCP ASGI app, which
  is how the page used to be served under `/flyte-mcp`. But it has to run before `_create_starlette_app` takes the
  app, and the environment builds that in `__post_init__`; the only way to run it after was to rebuild the
  Starlette app through two private names, which the factory does not do.

The factory code this forces, in `_factory_mcp.py`: the `_GrantPage` ASGI middleware, which matches the request to
`_grant_seal.PAGE_PATH` (`/flyte-mcp/grant`) with a Starlette `Route` of `_grant_page.serve_grant_page` and passes
every other request on, and `FactoryMCPAppEnvironment._starlette_middleware`, which returns
`[*super()._starlette_middleware(), Middleware(_GrantPage)]`. That puts the page inside the stock auth middleware at
the same path as before (Union sign-in in union mode, the proxy in proxy mode, as
`test_the_grant_page_is_behind_the_stock_auth_of_each_mode` checks) with no Starlette rebuild. What it still
depends on is an underscore-named hook, which the SDK documents for subclasses but may rename.

### Requirement

```python
FlyteMCPAppEnvironment(
    ...,
    extra_routes=[Route("/grant", serve_grant_page, methods=["GET"])],  # starlette BaseRoute list
)
```

- Routes are added beside `/health`, outside the MCP mount, and are covered by the same auth middleware as the
  MCP endpoint (`FastAPIPassthroughAuthMiddleware`, `_flyte_mcp_app.py:754-783`, which excludes only `/health` and
  the metadata paths). A route can ask to be excluded with `public=True`.
- A path that clashes with the SDK's (`/health`, `/secret-input/{token}`, the metadata paths) is a startup error.
- A method `app.add_route(path, endpoint, methods=...)` that works after construction, by rebuilding the app, would
  also do.

### What the factory removes once it lands

`_GrantPage`, the `FactoryMCPAppEnvironment` subclass and its `_starlette_middleware` override (the page is passed
in the constructor instead, and the stock environment is used as it is). The `_grant_page.serve_grant_page`
handler stays.

## What 2.13.5 already changed under the factory's wrappers

These are not gaps, but they show what gap 4 costs, and each is a fix the factory needs in the same upgrade:

- `get_run_io` gained `action_name`, `inputs_only` and `outputs_only` (`_tools.py:724-731`). The factory's
  patient `get_run_io` (`_wrap_tools_to_wait_for_outputs`) was written for the old signature and called the stock
  tool positionally, which would have dropped the new parameters from the schema and passed `project` where the
  stock tool now expects `action_name`. Fixed in this move: it mirrors the new signature and calls the stock tools
  by keyword, and `test_the_wrappers_keep_the_stock_input_schema_and_only_the_patient_ones_change_the_text` and
  `test_get_run_io_with_an_action_or_inputs_only_does_not_wait` hold it. Gap 2 would still end the wrapper, and
  with it the hand copy.
- `run_task` gained `run_project` and `run_domain` (`_tools.py:523-524`). The scope wrapper now covers every
  parameter ending in `project` or `domain` (`_scoped_arguments`), so they are refused (gap 1).
- `create_secret` and `update_secret` return a one-time `/secret-input/{token}` URL instead of taking a value
  (`_secret_tools.py:199-233`, routes at `_secret_input.py:264`). That is what the factory's instructions already
  assume ("no tool returns a secret value"), and the route is on the same ingress as the MCP endpoint.
- `get_logs` returns a dict with paging fields (`first_line`, `end_line`, `has_more_before`, `next_before_line`,
  `stopped`, `available`), not a list of lines (`_tools.py:1012-1060`).
- `signal_condition`'s argument list is unchanged, but the module it imports from is private, and still the only
  place `coerce_condition_payload` lives. The factory's wrapper does not need it: it calls the stock tool.

Run [the live smoke test](mcp-operator-smoke.md) after the upgrade; it checks each of these from chat.
