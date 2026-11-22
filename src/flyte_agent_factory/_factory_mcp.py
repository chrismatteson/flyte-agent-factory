"""The factory's MCP server: Flyte's stock MCP app, every tool, limited to the factory's project and domain.

With `[mcp] auth = "union"` (the default on a Union backend) Union authenticates every request at the app
ingress (`requires_auth=True`) and Flyte runs each call as the caller, so a Union user's sign-in token or a
Union API key both work as the bearer. What the caller may do inside the factory's project and domain is
bounded by their Union auth (RBAC); this module only keeps every tool from reaching outside it
(`_build_scope_check`).

With `auth = "proxy"` (the default on OSS) the app does no authentication of its own (`requires_auth=False`):
it trusts whoever reaches it. It must sit behind an authenticating proxy (an OAuth proxy) and never be exposed
on its own: anyone who can reach the app can launch and abort runs in the factory's project.

Every served tool goes through one layer (`_serve`): the scope check, then the call budget
(`_mcp_budget.run_within_budget`), then the OSS URL fix, then redaction, and one log line per call. See
`_serve`'s docstring.

SDK proposals: gaps 1 (scope), 4 (wrapping a registered tool) and 12 (list_projects) in
docs/mcp-sdk-requirements.md; the factory-side or SDK-side decision for each tool is the table there.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast
from urllib.parse import urlparse

import flyte
from flyte.ai.mcp import ALL_MCP_TOOLS, FlyteMCPAppEnvironment
from mcp.server.fastmcp import (
    Context as MCPContext,
)  # annotations below are strings; FastMCP resolves them here
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import (
    _app_tools,
    _factory,
    _git,
    _grant_page,
    _grant_seal,
    _mcp_budget,
    _mcp_redact,
    _oss_follow,
    _profile,
    _profile_oss,
    _resources,
    _state_pod,
    _union_identity,
)
from ._bridge import _ended_on_server
from ._bridge_common import parse_approval
from ._errors import REMOTE_ERRORS
from ._factory import DOMAIN, PROJECT
from ._factory_base import PROFILE, SLOTS, cap
from ._images import agent_image
from ._policy import LAUNCH_MODEL
from ._reserved import list_owner_reserved

#: The tools the factory serves: every stock tool, as it always has. `_build_scope_check` is what limits
#: them to the factory's project and domain, and it covers a tool a newer SDK adds without a change here.
TOOLS: tuple[str, ...] = tuple(ALL_MCP_TOOLS)
#: The tools the factory adds to the stock ones (`get_app`, `activate_app` and `deactivate_app` keep their
#: stock names and are served by `_app_tools`). Documented in docs/architecture.md, which a test holds equal.
FACTORY_TOOLS: tuple[str, ...] = (
    "get_app_status",
    "get_app_logs",
    "restart_app",
    "factory_mcp_health",
    "get_operation",
)
SERVED_TOOLS: tuple[str, ...] = TOOLS + FACTORY_TOOLS
#: The tools that change state. A call to one that was cut off is joined, never repeated: a second
#: `run_task` would start a second run. Every tool not named here only reads, and is simply run again.
ACTING: tuple[str, ...] = (
    "run_task",
    "rerun_run",
    "abort_run",
    "abort_action",
    "signal_condition",
    "activate_app",
    "deactivate_app",
    "restart_app",
    "delete_app",
    "create_secret",
    "update_secret",
    "delete_secret",
    "generate_secret",
    "create_trigger",
    "delete_trigger",
    "activate_trigger",
    "deactivate_trigger",
)

_mcp = PROFILE.mcp
if _mcp is None:
    raise _profile.ProfileError(f"profile {PROFILE.name!r}: missing table [mcp], needed by factory-mcp")

#: Union's read model lags the watch stream: for 10-60s after a run SUCCEEDED, `Run.get` can still say it
#: is not done, and the stock `get_run_io` then returns `outputs: null` with no error. These bound how long
#: the tools below wait for it to catch up, and how fast they poll (doubling up to the cap).
_OUTPUTS_WAIT_S = 90.0
_POLL_FIRST_S = 1.0
_POLL_MAX_S = 10.0
#: Compared lowercase: the stock tools report `ActionPhase.value` ('succeeded'), other callers use the name.
_SUCCESS_PHASES = ("succeeded", "recovered")
#: Phases after which a run has no outputs coming, so a null `outputs` is the right answer straight away.
_FAILURE_PHASES = ("failed", "aborted", "timed_out")
#: The share of the call budget `wait_for_run` and `get_operation` plan to be done in: the rest is the slack
#: that keeps the budget's own `still_working` answer from racing the call's.
_FOLLOW_UP_BUDGET_SHARE = 0.95


def _phase_name(phase: Any) -> str:
    """Lowercase name of a phase given as an enum, its value, or a string of either case."""
    return str(getattr(phase, "value", phase) or "").lower()


def list_write_targets(workspace: str, allowed: Iterable[str]) -> list[str]:
    """Return the operator's write targets as owner/name: the workspace (STATE.md), then the allowed repos.

    The workspace is always there, so an `[mcp] repos` that leaves out `project` still names STATE.md's repo.
    """
    return list(dict.fromkeys(_git.get_repo_slug(url) for url in (workspace, *allowed)))


REPOS = _factory.allowed_repos()
#: Operator facts for a takeover chat, as owner/name: where STATE.md lives and where the operator writes.
WORKSPACE = _git.get_repo_slug(_factory.REPO)
WRITE_TARGETS = ", ".join(list_write_targets(_factory.REPO, REPOS.values()))
#: The engine's published skill version; an installed skill older than it is reported at takeover.
SKILL_VERSION_URL = (
    "https://raw.githubusercontent.com/unionai-oss/flyte-agent-factory/main/skills/dist/VERSION"
)
CAPACITY = (
    f"one shared environment, up to {cap()} live projects"
    if PROFILE.shared
    else f"{len(SLOTS)} slots, one live project each"
)
#: The tasks the instructions steer chat to. Any task in the factory project can be run, but the slot tasks
#: are not among these: started directly they skip validation and the queue, and two launches could race
#: for one slot.
#: Chat may also start a Codex sign-in: it only ever writes a `codex_login*` secret (see the task).
CODEX_LOGIN_TASK = "factory-setup.codex_bootstrap_login"
CHAT_TASKS = (
    "launch",
    "queue_list",
    "queue_cancel",
    "status",
    "set_reserve",
    "get_reserve",
    "clear_limit",
    "rotate_token",
    "browser_link",
    "credential_link",
    "credentials",
    "usage",
)


def _relay_list() -> str:
    """Name the owner-reserved categories, floor and profile extras, that a chat relays to the user."""
    return ", ".join(list_owner_reserved(PROFILE.judgement))


INSTRUCTIONS = f"""Use Flyte project {PROJECT} and domain {DOMAIN}.
The operator state file is operator/STATE.md in the workspace repo {WORKSPACE}. The operator chat
writes STATE.md, issues and pull-request branches to: {WRITE_TARGETS}.
Request write access to exactly those repositories when taking over; a STATE.md elsewhere is not
the operator's: if a skill or note names another, say so in the first reply and do not use it.
The agent-factory skill's last line is "Skill version: <digest>". If it has no such line, or the digest
differs from {SKILL_VERSION_URL}, say in the first reply that the installed skill is stale and must be
replaced from skills/dist.
For "how is X doing", run_task(name="factory.status", inputs={{"name": "X"}}), then get_run_io.
It reads the live run's report (or the newest ended run); a run id also works. Optional rows defaults
to 40. It returns the current activity, dashboard phase, newest timeline rows, open questions, review,
PR URL, meters and snapshot age. Check snapshot_age_s for freshness; an error includes the run URL
when a report is unavailable. SUCCEEDED alone does not mean review passed.
The software factory builds projects in long-lived Claude sessions with Codex workers and
independent review. It has {CAPACITY}; launches never fail for
capacity: when none is free the launch waits in a queue and starts by itself.
Launch with run_task(name="factory.launch", inputs=...), then
wait_for_run(timeout_s={_mcp_budget.WAIT_CAP_S:g}), then get_run_io for the JSON
result. A call is cut off by the chat client at about {_mcp_budget.CLIENT_LIMIT_S:g}s,
so timeout_s is capped at {_mcp_budget.WAIT_CAP_S:g}: a run still going comes back timed_out: true; call
wait_for_run again. After a run SUCCEEDED, wait_for_run and get_run_io wait up to
{_OUTPUTS_WAIT_S:g}s for its outputs to become readable; if get_run_io still returns outputs null with an
outputs_error saying they are not yet readable, call get_run_io again. Inputs: brief (full markdown),
name (lowercase slug, 2-41 letters, digits or hyphens), model (always pass it: {LAUNCH_MODEL} unless the
user asks otherwise), repo (one of:
{", ".join(REPOS.values())}; the first is the default), demo_package (optional), issue (optional
owner/repo#number linking the project to a GitHub work item; status comments are posted on it
wherever the factory's GitHub App is configured), priority (optional: normal, the default; low, which
starts only when a slot is free and no other launch waits; or expedite, which goes first). The result's
status is
started (give the user its url), queued (tell the user the position) or updated (that name was already
waiting; its brief was replaced and it kept its place). A launch that is refused says why: a bad name, a
brief that is too long, a repo that is not allowed, or a name that is running (send direction instead).
When a launch run FAILS, get_run_io has no outputs: read the reason with get_logs (small max_lines) or
get_action's error_info.
run_task(name="factory.queue_list") shows the slots, the waiting launches in order and the recent
ones (and any usage reserve holding a launch back); run_task(name="factory.queue_cancel",
inputs={{"name": ...}}) removes a waiting launch (and aborts its run, if a dispatcher had just created
one). Read both results with get_run_io.
Pass browser (optional) to factory.launch only when the user asked for a signed-in browser identity for that
project: identity names, comma-separated, or "none" to revoke the grant a resume would inherit. Leave it out
otherwise; a resume keeps the project's grant.
run_task(name="factory.browser_link", inputs={{"identity": ...}})
returns JSON {{identity, url}}: a one-time sign-in/view link for the user (ten minutes, single use).
Credentials: a project asks for an AWS credential with an ask- question that holds a link. The user opens
the link, pastes a short-lived scoped credential, presses Seal and copies the sealed text (it starts with
fgc1.); relay exactly that text with signal_condition as the answer, or "no" to refuse. Never ask the user
to paste a raw credential into chat, and never repeat one. To give or refresh one the project did not ask
for, run_task(name="factory.credential_link", inputs={{"name": "<project>", "credential": "<name>",
"hours": 4}}) returns JSON {{project, credential, hours, url, paste_to}}: give the user the url, then
signal the sealed text they bring to the condition named paste_to (the project's direct- inbox). To
revoke, signal "/revoke-credential <name>" (or "/revoke-credential" for all) to that direct- condition.
run_task(name="factory.credentials") lists every live project's grants (names, expiry, state; never a
value); factory.status returns one project's as credentials.
Each account (claude, codex) can hold back k percent of every usage window per hour left until that
window resets, so a launch waits when its account has less than k x hours-to-reset left and reopens by
itself as the reset nears. run_task(name="factory.set_reserve", inputs={{"account": "claude", "k": 0.5}})
sets it (0 removes it) and the next admission uses it; run_task(name="factory.get_reserve") reads every k.
For "how much of the week is left", run_task(name="factory.usage") (no inputs), then get_run_io. It returns
JSON per Claude credential: observed (the newest five-hour and weekly percentages USED a session saw, with
*_left_pct for what remains, reset times, source and age_s; a window whose reset passed is expired, with no
percentage; limited_until when the credential is refused until then; login_rejected for a token to replace;
null when nothing was observed), provider_usage (Anthropic has no usage endpoint for setup-tokens: say so
plainly) and estimate (tokens and USD the factory itself spent on that credential this week). Always call the
estimate an estimate and give its basis; it misses use outside the factory.
A usage limit the owner has reset by hand (a weekly reset, say) is forgotten by every project's pod with
run_task(name="factory.clear_limit", inputs={{"engine": "claude"}}) (claude is the default); each pod
drops what it remembered within a minute (it says "not cleared" when the state branch cannot be written).
Send "/clear-limit [engine]" to one project's direct- condition to clear it for that pod at once, and for
the others too.
A project whose run has ended is resumed by launching the same name again with
the new work as the brief: it continues from the last published workspace.
Use factory.status for progress and review state (rounds, blocking findings, each judge's latest
verdict); get_logs returns the newest max_lines lines of a log, oldest first: page back with
before_line=next_before_line, or pass before_line=max_lines to read from the start.
list_conditions finds ask- questions and direct- inboxes (send direction; /stop saves and ends the
project). Decide an engineering-judgement ask- yourself (design, structure, how to satisfy a check, tests,
tooling): do what is the right thing to do, never a waiver, lowered threshold, removed rule or skipped test.
Signal a value that starts "Operator decision (engineering judgement): " with the reasoning, and tell the
user what you decided. Relay only owner-reserved asks ({_relay_list()}) to the user, one at a time, stated
plainly with your recommendation, and signal_condition their answer verbatim. Plain yes or no, or a sentence,
is fine for any ask, including a yes/no one: do not convert it to true or false. If the ask had already closed
(timed out, or replaced by a newer one), the answer is not lost: signal_condition sends it to the project's
inbox and returns rerouted: true; an error saying it was NOT delivered means the project has no open inbox,
so tell the user. The factory's own judge already
decides engineering asks before they open: factory.status returns those as decisions, with reasoning. The user
overrules one by sending direction that names its id, such as "overrule decision-003: ...".
Prefer /stop over abort_run; abort loses uncheckpointed work.
run_task(name="{CODEX_LOGIN_TASK}", inputs={{"secret_name": "codex_login_..."}}) signs Codex in to a
ChatGPT account: list_conditions on its run shows a codex-login prompt with a link and a code for the user to
open and enter; once they do, the login is stored as that secret.
run_task(name="factory.rotate_token") (no inputs) makes a replaced token secret reach running projects: the
owner updates the secret first; this then sends /stop to every live project and queues one resume of each
with the reason "token rotated", which starts on a new pod from the checkpoint once the old run has
ended. Calling it again while the resumes wait queues nothing more. Read the result with get_run_io;
factory.queue_list and factory.status show the waiting resumes and their reason.
When a project fails because its token was refused (its run says the Claude setup-token or the Codex
access token expired or was revoked), the owner replaces the token in the Flyte secret named by
[secrets] claude_token or codex_token; then launch the project again by name.
Launch benchmarks with run_task(name="factory-bench.benchmark", inputs=...), then get_run_io for
the summary JSON and cases JSONL. Inputs: candidate_sha (required full 40-character commit SHA or
factory repo ref), baseline_sha (optional SHA or ref; empty means candidate only), tasks (suite task
ids; empty means all), repeats (1-3, default 2), candidate_policy and baseline_policy (JSON object
strings, default "{{}}"), reuse_baseline_days (non-negative, default 7), max_usd (default 0).
First pass max_usd=0: it returns an estimate and spends nothing. Read estimate.ceiling_usd, including
both arms and stop margins; a paid launch requires the user's spend authorization and an explicit
max_usd covering that ceiling, within the profile's [bench] max_usd. Reuse the same inputs for that
launch. The task refuses bad SHAs or refs, unknown task ids, invalid policies, repeats outside 1-3,
and estimates above max_usd before deployment or case launches. A refused run has no outputs: use
get_logs (small max_lines) or get_action's error_info. Monitor with
list_runs(task_name="factory-bench.benchmark"), get_run_io and the run report.
Every tool is limited to this project and domain; a call naming another is refused, and list_projects shows
only this project. What you may do here is bounded by the caller's Union permissions.
Running a slot task directly is unsupported (it skips validation and the queue), though it is not blocked.
After a failed deploy, start run_task(name="factory-deployer.deploy_if_changed") (no inputs) instead of
waiting for the next tick; read its result with get_run_io.
list_secrets shows names only and no tool returns a secret value. create_secret returns a one-time link on
this server that the owner opens to type the value into a password field: give the owner the link, and never
ask for the value in chat; once they have used it, list_secrets shows the name. delete_secret removes one.
list_apps, get_app, activate_app and deactivate_app, and
list_triggers, get_trigger, activate_trigger and deactivate_trigger, manage this project's apps and triggers.
get_app lists an app's environment and secret names, never values. get_app_status gives an app's status
history and why it last failed (oom, crash, probe_failed, image_pull, secret_mount); get_app_logs reads its
newest log lines, including those of replicas that have died; restart_app deactivates then activates it
(never deletes), and refuses this server's own app; factory_mcp_health reports this server's uptime, restarts
and errors.
A call that takes longer than {_mcp_budget.TOOL_BUDGET_S:g}s returns
{{"status": "still_working", "operation_id": ...}} and goes on in the server: do not repeat it (an acting
call repeated with the same arguments joins the first), call get_operation(operation_id=...) for its result
or progress. An unknown_operation answer means the server restarted: look at the effect (get_run,
get_app_status) before acting again.
"""

_public_url = os.environ.get("FACTORY_MCP_PUBLIC_URL", _mcp.public_url).rstrip("/")

#: The docs the search tools read. The stock defaults are under /root, which the factory's `agent_image()`
#: does not have and its unprivileged user could not read; the image build puts them here instead.
DOCS_DIR = "/opt/flyte-mcp-docs"
#: The flyte the image installs, as `pyproject.toml` pins it for the engine (a test keeps the two equal).
FLYTE_PIN = "flyte[mcp]>=2.13.5,<2.14.0"
FLYTE_SDK_TAG = "v2.13.5"  # the installed flyte; its examples match the SDK the tools run on (a test checks)
_FETCH_DOCS = (
    # Shallow clones keep only the directory the search reads; the rest of each repo is dropped.
    f"mkdir -p {DOCS_DIR} "
    f"&& git clone --depth 1 --branch {FLYTE_SDK_TAG} https://github.com/flyteorg/flyte-sdk /tmp/flyte-sdk "
    f"&& mkdir -p {DOCS_DIR}/flyte-sdk && mv /tmp/flyte-sdk/examples {DOCS_DIR}/flyte-sdk/examples "
    "&& git clone --depth 1 https://github.com/unionai/unionai-examples /tmp/unionai-examples "
    f"&& mkdir -p {DOCS_DIR}/unionai-examples && mv /tmp/unionai-examples/v2 {DOCS_DIR}/unionai-examples/v2 "
    f"&& curl -fsSL -o {DOCS_DIR}/llms.txt https://www.union.ai/docs/v2/union/llms.txt "
    f"&& rm -rf /tmp/flyte-sdk /tmp/unionai-examples && chmod -R a+rX {DOCS_DIR}"
)


#: How late the event loop wakes from a 1 s sleep, for `factory_mcp_health`: a blocked loop shows as lag.
LOOP_LAG = _mcp_budget.LoopLag()
_loop_lag_task: asyncio.Task[None] | None = None


def _start_loop_lag() -> None:
    """Start the sampler on the server's own event loop, once.

    The task is kept in a global because the loop holds only a weak reference to it.
    """
    global _loop_lag_task
    if _loop_lag_task is None or _loop_lag_task.done():
        _loop_lag_task = asyncio.ensure_future(LOOP_LAG.run())


class _GrantPage:
    """ASGI middleware that serves the grant page at `PAGE_PATH`, ahead of the app's router.

    The page is the factory's one route of its own. The stock app mounts FastMCP's ASGI app at
    `/flyte-mcp`, and Starlette gives a request under a mount to the mount alone, so a route added beside it
    at `/flyte-mcp/grant` is never reached; and a FastMCP `custom_route` has to exist before the app is built,
    which the stock environment offers no public hook for. A middleware installed through the documented
    `_starlette_middleware` hook is inside the stock auth middleware, as the page was when it was a
    `custom_route`: Union mode asks for the caller's sign-in, proxy mode asks for nothing.

    Its first scope also starts the loop-lag sampler behind the health tool's `loop_lag_ms`: the stock
    environment has no start-up hook, and this is the first code of ours that runs on the server's loop.
    Removing this middleware would leave `loop_lag_ms` at zero.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, which gets every request but the page's."""
        self.app = app
        self.route = Route(_grant_seal.PAGE_PATH, _grant_page.serve_grant_page, methods=["GET"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the page for a request to its path, pass any other request on."""
        # The stock environment has no start-up hook of its own, but this runs on the server's loop from its
        # first scope: the lifespan start under uvicorn, else the first request.
        _start_loop_lag()
        if scope["type"] == "http":
            match, child_scope = self.route.matches(scope)
            if match is Match.FULL:
                scope.update(child_scope)
                await self.route.handle(scope, receive, send)
                return
        await self.app(scope, receive, send)


class FactoryMCPAppEnvironment(FlyteMCPAppEnvironment):
    """The stock Flyte MCP app, plus the factory's grant page."""

    def _starlette_middleware(self) -> list[Middleware]:
        # The disconnect watch first, so it is outermost: a dropped request cancels every layer under it.
        return [
            Middleware(_mcp_budget.CancelOnDisconnect),
            *super()._starlette_middleware(),
            Middleware(_GrantPage),
        ]


_QUEUE_SECRETS, _QUEUE_ENV = _union_identity.pod_for(PROFILE, "queue")  # none on OSS
_PROXY_AUTH = _profile_oss.mcp_auth(PROFILE) == "proxy"
app = FactoryMCPAppEnvironment(
    name=_app_tools.DEFAULT_OWN_APP,
    image=agent_image().with_pip_packages(FLYTE_PIN, "mcp>=1.30.0,<2").with_commands([_FETCH_DOCS]),
    requires_auth=not _PROXY_AUTH,
    tools=list(TOOLS),
    task_allowlist=None,  # any task in the factory project; `_build_scope_check` bounds it
    instructions=INSTRUCTIONS,
    allowed_hosts=[urlparse(_public_url).netloc] if _public_url else None,
    allowed_origins=[_public_url] if _public_url else None,
    **_state_pod.environment_kwargs(PROFILE, "factory", app=True),
    secrets=[*_QUEUE_SECRETS],
    env_vars={
        "FLYTE_MCP_PROJECT": PROJECT,
        "FLYTE_MCP_DOMAIN": DOMAIN,
        # So a separately named test app knows itself and `restart_app` can refuse to stop its own server.
        _app_tools.OWN_APP_ENV: _app_tools.DEFAULT_OWN_APP,
        **_profile.pod_env(),
        **_QUEUE_ENV,
    },
    # Spelled out: the app defaults to 1 CPU / 512Mi when it gets None.
    resources=_resources.for_component(_profile.current(), "mcp").to_flyte(),
    # One replica: the secret-input links `create_secret` mints live in the process's memory.
    scaling=flyte.app.Scaling(replicas=1),
    sdk_examples_path=f"{DOCS_DIR}/flyte-sdk/examples",
    docs_examples_path=f"{DOCS_DIR}/unionai-examples/v2",
    full_docs_path=f"{DOCS_DIR}/llms.txt",
)


ToolFn = Callable[..., Awaitable[dict[str, Any]]]


def _wrap_tools_to_wait_for_outputs(stock: dict[str, ToolFn]) -> dict[str, ToolFn]:
    """Return `wait_for_run` and `get_run_io` that wait out the read model's lag after SUCCEEDED.

    Wrappers over the stock tools rather than a replacement: the SDK owns how outputs are decoded, and
    this only adds the wait. Registered over the stock ones in `app.mcp`, because the chat flow is
    always `run_task`, `wait_for_run`, `get_run_io`, and the watch stream behind `wait_for_run` is
    ahead of the `Run.get` behind `get_run_io`.
    """
    stock_get_run: ToolFn = stock["get_run"]
    stock_wait: ToolFn = stock["wait_for_run"]
    stock_io: ToolFn = stock["get_run_io"]

    async def poll_outputs(
        name: str,
        project: str | None,
        domain: str | None,
        ctx: MCPContext | None,  # type: ignore[type-arg]  # bare on purpose, see `get_run_io`
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """Poll for the run's outputs until they are there, `outputs_error` is set or the wait is spent.

        `wait_s` is shorter than `_OUTPUTS_WAIT_S` when the caller has little of its call budget left.
        """
        wait_s = _OUTPUTS_WAIT_S if wait_s is None else min(wait_s, _OUTPUTS_WAIT_S)
        deadline = time.monotonic() + wait_s
        delay = _POLL_FIRST_S
        while True:
            # outputs_only: the inputs are already in hand, and each poll would fetch them again.
            io = await stock_io(name=name, outputs_only=True, project=project, domain=domain, ctx=ctx)
            if io.get("outputs") is not None or io.get("outputs_error"):
                return io
            if time.monotonic() + delay > deadline:
                io["outputs_error"] = (
                    f"outputs not yet readable: Flyte had not published the outputs of run {name!r} "
                    f"after {wait_s:g}s. Retry get_run_io; if the run may still be going, "
                    "wait_for_run tells the two apart."
                )
                return io
            await asyncio.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_S)

    async def wait_for_run(
        name: str,
        timeout_s: float | None = None,
        project: str | None = None,
        domain: str | None = None,
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # see `get_run_io`
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see `get_run_io`
        """Block until a run reaches a terminal phase, then report it.

        Prefer this over polling `get_run` in a loop. `timeout_s` is capped at WAIT_CAP_S (a call is cut off
        by the client at about 230 s); the result then says `timeout_capped_to_s`. On timeout this does
        *not* error: it returns the run's current, non-terminal status with `timed_out: true`, and the
        call can be repeated. A run that SUCCEEDED is reported only once its outputs are readable by
        `get_run_io` (`outputs_ready: true`), up to a bounded wait; if they are not, `outputs_ready` is
        false and `get_run_io` should be retried.
        """
        started = time.monotonic()
        # Read at call time, so a test can patch them. Unbounded or too long is clamped: past the client's
        # cut the call would only come back as a 502.
        capped = timeout_s is None or timeout_s > _mcp_budget.WAIT_CAP_S
        wait_s = _mcp_budget.WAIT_CAP_S if capped else timeout_s
        result = await stock_wait(name=name, timeout_s=wait_s, project=project, domain=domain, ctx=ctx)
        if _phase_name(result.get("phase")) in _SUCCESS_PHASES:
            # The outputs wait runs inside the same call, so it gets what the budget has left (never more than
            # `_OUTPUTS_WAIT_S`), and the call answers for itself instead of coming back as `still_working`.
            left = _FOLLOW_UP_BUDGET_SHARE * _mcp_budget.TOOL_BUDGET_S - (time.monotonic() - started)
            io = await poll_outputs(name, project, domain, ctx, wait_s=max(left, 0.0))
            result["outputs_ready"] = io.get("outputs") is not None
        if capped:
            result["timeout_capped_to_s"] = _mcp_budget.WAIT_CAP_S
        return result

    async def get_run_io(
        name: str,
        action_name: str | None = None,
        inputs_only: bool = False,
        outputs_only: bool = False,
        project: str | None = None,
        domain: str | None = None,
        # Unparametrised on purpose: FastMCP finds its Context argument by `issubclass`, which a
        # `Context[...]` alias fails. The tools' `-> dict` is bare for the same reason: `dict[str, Any]`
        # makes FastMCP add structured output, a second content block that clients do not expect.
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see above
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
        """Get a run's inputs, and its outputs once it has finished.

        `outputs` is null if the run failed. Otherwise this waits (bounded) for the outputs to become
        readable, since a run that has just SUCCEEDED can still look as if it is going; if they are
        not, `outputs_error` says so and the call can be repeated. If the outputs exist but cannot be
        decoded, `outputs_error` explains why instead of the outputs silently coming back null.

        Pass `action_name` (from `list_actions`) for a nested action's data instead of the root's, and
        `inputs_only` or `outputs_only` (not both) to leave the other out of the response when it is large.
        Those return at once: only the run's own outputs are waited for.
        """
        io = await stock_io(
            name=name,
            action_name=action_name,
            inputs_only=inputs_only,
            outputs_only=outputs_only,
            project=project,
            domain=domain,
            ctx=ctx,
        )
        if action_name is not None or inputs_only or io.get("outputs") is not None or io.get("outputs_error"):
            return io
        # Null outputs are right for a run that failed. Any other phase may be stale: the read model that
        # `get_run` reads lags the one that SUCCEEDED, so only the failure phases are trusted as they are.
        run = await stock_get_run(name=name, project=project, domain=domain, ctx=ctx)
        if _phase_name(run.get("phase")) in _FAILURE_PHASES:
            return io
        polled = await poll_outputs(name, project, domain, ctx)
        return {**io, **{k: v for k, v in polled.items() if k in ("outputs", "outputs_error")}}

    # The cap is the budget's constant, so the tool description cannot drift from the clamp above.
    wait_for_run.__doc__ = (wait_for_run.__doc__ or "").replace("WAIT_CAP_S", f"{_mcp_budget.WAIT_CAP_S:g}")
    return {"wait_for_run": wait_for_run, "get_run_io": get_run_io}


def _scoped_arguments(fn: ToolFn) -> dict[str, str]:
    """Return the factory's value for each parameter of `fn` that picks a project or a domain.

    By name, so an SDK release's new `run_project`-like parameter is covered the day it arrives; a test
    reads every parameter of every tool the live server lists to hold this to account.
    """
    factory_value = {"project": PROJECT, "domain": DOMAIN}
    return {
        parameter: factory_value[parameter.rsplit("_", 1)[-1]]
        for parameter in inspect.signature(fn).parameters
        if parameter.rsplit("_", 1)[-1] in factory_value
    }


def _build_scope_check(name: str, fn: ToolFn) -> Callable[..., None] | None:
    """Return a check that refuses a project or domain other than the factory's, or None if `fn` takes none.

    The stock tools take any `project` and `domain` (and `run_task`, a `run_project` and `run_domain` to
    create the run elsewhere) and run the call there as the caller, so without this chat could reach every
    project the caller's Union RBAC allows. Empty means "the server default", which is the factory's
    (`FLYTE_MCP_PROJECT`/`FLYTE_MCP_DOMAIN`). The check runs before the call is made into an operation.
    """
    signature = inspect.signature(fn)
    scoped_arguments = _scoped_arguments(fn)
    if not scoped_arguments:
        return None

    def check(*args: Any, **kwargs: Any) -> None:
        given = signature.bind(*args, **kwargs).arguments
        for argument, factory_value in scoped_arguments.items():
            value = given.get(argument)
            if value and value != factory_value:
                raise ToolError(
                    f"{name}: {argument} {value!r} is not allowed; this server is limited to "
                    f"{argument.rsplit('_', 1)[-1]} {factory_value!r}."
                )

    return check


def _list_only_the_factory_project(stock_list: ToolFn, stock_get_project: ToolFn) -> ToolFn:
    """`list_projects` that shows only the factory's project, fetched by name.

    The stock tool pages through the organisation's projects, which a project-scoped key (the server's own
    when it acts as the queue tier) is not allowed to list; one `get_project` is all it needs.
    """

    @functools.wraps(stock_list)
    async def list_projects(limit: int = 100, ctx: MCPContext | None = None) -> Any:  # type: ignore[type-arg]
        # `limit` is ignored: stock returns at least one project for any limit, and there is only one.
        return [await stock_get_project(name=PROJECT, ctx=ctx)]

    return list_projects


def _get_only_the_factory_project(stock: ToolFn) -> ToolFn:
    """`get_project` that refuses every project but the factory's."""

    @functools.wraps(stock)
    async def get_project(name: str, ctx: MCPContext | None = None) -> Any:  # type: ignore[type-arg]
        if name != PROJECT:
            raise ToolError(
                f"get_project: project {name!r} is not allowed; this server is limited to {PROJECT!r}."
            )
        return await stock(name=name, ctx=ctx)

    return get_project


def _signal_condition_that_reaches_the_project(stock: ToolFn, list_conditions: ToolFn) -> ToolFn:
    """`signal_condition` that takes any answer to an ask, and sends a late one to the project's inbox.

    A chat relays the owner's words as they are. The stock tool fails on a sentence for a bool condition
    and on a condition that has ended, and either way the owner's answer was lost to the project. Here an
    ask's answer that cannot be signalled to it goes to the run's open `direct-` condition instead, as
    `/late-answer <condition> <value>`, which the session's inbox hands to its bridge (`Bridge.late_answer`).

    Everything goes through the stock `signal_condition` and `list_conditions`, which take the scope: the
    `flyte.remote.Condition` calls under them read the project and domain off the init config, and only the
    stock tools can set that for one call.
    """

    @functools.wraps(stock)
    async def signal_condition(
        name: str,
        run_name: str,
        value: bool | int | float | str,
        project: str | None = None,
        domain: str | None = None,
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # bare on purpose, see `get_run_io`
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see `get_run_io`
        async def signal(condition: str, payload: bool | int | float | str) -> dict[str, Any]:
            return await stock(
                name=condition, run_name=run_name, value=payload, project=project, domain=domain, ctx=ctx
            )

        if not name.startswith("ask-"):
            return await signal(name, value)

        async def try_signal(payload: bool | int | float | str) -> tuple[dict[str, Any] | None, str]:
            """Return the stock call's result, or why an ask's answer could not be signalled to it."""
            try:
                return await signal(name, payload), ""
            except (ValueError, TypeError) as e:
                return None, f"{name} takes another type: {e}"
            except REMOTE_ERRORS as e:
                if not _ended_on_server(e):
                    raise
                return None, f"{name} had already ended"

        signalled, late = await try_signal(value)
        if signalled is not None:
            return signalled
        # An approve ask from before they took text: "yes" or a sentence for a bool condition.
        approval = parse_approval(value)
        if approval is not None:
            signalled, late = await try_signal(approval)
            if signalled is not None:
                return signalled

        if not str(value).strip():
            raise ToolError(
                f"The answer to {name} was NOT delivered: {late}, and an empty answer says nothing."
            )
        # The stock listing reads each condition's details, so it is the dearer way to find the inbox; it is
        # only asked for here, when the answer would otherwise be lost.
        conditions = cast(
            list[dict[str, Any]],
            await list_conditions(run_name=run_name, limit=500, project=project, domain=domain, ctx=ctx),
        )
        ended = next((c for c in conditions if c["name"] == name), None)
        if ended is not None and late.endswith("had already ended"):
            late += f" ({str(ended['phase']).removeprefix('ACTION_PHASE_')})"
        inbox = next(
            (
                c["name"]
                for c in conditions
                if c["name"].startswith("direct-") and "PAUSED" in str(c["phase"])
            ),
            None,
        )
        if inbox is None:
            raise ToolError(
                f"The answer to {name} was NOT delivered: {late}, and run '{run_name}' has no open "
                "direct- inbox right now. Tell the user it did not arrive and try again shortly."
            )
        await signal(inbox, f"/late-answer {name} {value}")
        return {
            "name": name,
            "run_name": run_name,
            "signaled": True,
            "rerouted": True,
            "delivered_to": inbox,
            "reason": f"{late}; sent to the project's inbox as a late answer",
        }

    return signal_condition


def _registered_tool_functions(server: Any) -> dict[str, Any]:
    """Return each registered tool of a FastMCP server: its name, function and annotations.

    FastMCP's public API (`list_tools`, `call_tool`) hands out schemas and results, never a tool's function,
    and the wrappers below need the stock function to call. `_tool_manager` is where FastMCP keeps its
    `Tool` objects; the python-sdk has no public getter, so this is the one place that reaches in. Upstream
    would need to add one (a `FastMCP.get_tool`) for it to go.
    """
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def _note_steps(fn: ToolFn) -> ToolFn:
    """Bind `on_step` of an app tool to `_mcp_budget.note_step` and hide it from the client's schema.

    A call that is cut off then shows in its `still_working` result how far it got.
    """
    signature = inspect.signature(fn)
    if "on_step" not in signature.parameters:
        return fn

    @functools.wraps(fn)
    async def with_steps(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await fn(*args, on_step=_mcp_budget.note_step, **kwargs)

    # A bare `dict`, as the tools declare it: FastMCP turns `dict[str, Any]` into structured output, a second
    # content block that clients do not expect. The string annotation is resolved here, not by FastMCP.
    with_steps.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[p for p in signature.parameters.values() if p.name != "on_step"],
        return_annotation=dict,
    )
    return with_steps


def _serve(name: str, fn: ToolFn) -> ToolFn:
    """Wrap `fn` in the one layer every served tool goes through, in this order.

    Scope check (a refusal is logged as `refused` and makes no operation), then the call budget
    (`still_working` past `TOOL_BUDGET_S`; an acting tool is joined, not repeated), then the OSS URL fix,
    then redaction. Redaction is outermost, so `still_working` and `get_operation` results are redacted too,
    and so is the message of an error the call raises.
    The per-call log line is the budget's. One place wraps every tool, so "every served tool" is true by
    construction; a test checks it against the live tool list.
    """
    budgeted = _mcp_budget.run_within_budget(
        name, fn, acting=name in ACTING, check=_build_scope_check(name, fn)
    )

    @functools.wraps(fn)
    async def served(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await budgeted(*args, **kwargs)
        except Exception as exc:
            # The client sees an error's message, so it is redacted like a result. The original is not
            # chained: FastMCP would not send it, but a log of the cause would carry the secret.
            message = await asyncio.to_thread(_mcp_redact.redact_text, str(exc))
            if message == str(exc):
                raise
            raise ToolError(message) from None
        # In a thread, always: a large result is a long regex scan, and it must not stall the other calls.
        return await asyncio.to_thread(
            lambda: _mcp_redact.redact_result(_oss_follow.fix_urls(result, PROFILE))
        )

    served.served_layer = True  # type: ignore[attr-defined]  # what the tests look for on every tool
    return cast(ToolFn, served)


def _get_operation_within_the_budget_share() -> ToolFn:
    """Return the budget's `get_operation`, its wait held to `_FOLLOW_UP_BUDGET_SHARE` of the call budget.

    The share is bound here and hidden from the client's schema, so the tool's name and parameters are the
    budget's own, and so is its description.
    """
    signature = inspect.signature(_mcp_budget.get_operation)
    bound = functools.partial(_mcp_budget.get_operation, budget_share=_FOLLOW_UP_BUDGET_SHARE)
    functools.update_wrapper(bound, _mcp_budget.get_operation)
    # A bare `dict`, as in `_note_steps`.
    bound.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[p for p in signature.parameters.values() if p.name != "budget_share"],
        return_annotation=dict,
    )
    return cast(ToolFn, bound)


def _summarise_calls() -> dict[str, int]:
    """Count the calls of the last hour in the budget's ring (which holds at most `CALL_LOG_MAX`)."""
    since = time.monotonic() - 3600
    log = [entry for entry in _mcp_budget.CALL_LOG if entry[3] >= since]
    durations = sorted(duration for _, _, duration, _ in log)
    return {
        "count": len(log),
        "still_working": sum(1 for _, outcome, _, _ in log if outcome == "still_working"),
        "dropped": sum(1 for _, outcome, _, _ in log if outcome == "dropped"),
        "tool_errors": sum(1 for _, outcome, _, _ in log if outcome.startswith("error:")),
        "p95_ms": _mcp_budget.pick_percentile(durations, 0.95) if durations else 0,
    }


def _read_loop_lag() -> dict[str, Any]:
    snapshot = LOOP_LAG.summarise_lag()
    return {"max_5m": snapshot["max_ms"], "p95_5m": snapshot["p95_ms"]}


_READS_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _use_the_factorys_tools(environment: FlyteMCPAppEnvironment) -> None:
    """Replace the stock tools in `environment.mcp` with the factory's and add the factory's own.

    The patient `wait_for_run` and `get_run_io`, `list_projects`, `get_project`, `signal_condition`, the app
    tools and (on OSS) `get_logs` come first; then every tool, those and the stock ones alike, is wrapped by
    `_serve`. The five `FACTORY_TOOLS` are added last.
    """
    server = environment.mcp
    assert server is not None  # built in the environment's __post_init__
    registered = _registered_tool_functions(server)
    stock = {name: cast(ToolFn, tool.fn) for name, tool in registered.items()}

    replacements: dict[str, ToolFn] = {}
    replacements.update(_wrap_tools_to_wait_for_outputs(stock))
    replacements["list_projects"] = _list_only_the_factory_project(
        stock["list_projects"], stock["get_project"]
    )
    replacements["get_project"] = _get_only_the_factory_project(stock["get_project"])
    replacements["signal_condition"] = _signal_condition_that_reaches_the_project(
        stock["signal_condition"], stock["list_conditions"]
    )
    replacements["get_logs"] = _oss_follow.wrap_get_logs(stock["get_logs"], PROFILE)
    # The stock app tools return `to_dict()`, env values included; these list names only.
    for app_tool in ("get_app", "activate_app", "deactivate_app"):
        replacements[app_tool] = cast(ToolFn, getattr(_app_tools, app_tool))
    for name, fn in stock.items():
        tool = registered[name]
        server.remove_tool(name)
        # Annotations are the registered tool's: the SDK's own table of them is private.
        server.add_tool(
            _serve(name, _note_steps(replacements.get(name, fn))),
            name=name,
            title=tool.title,
            annotations=tool.annotations,
        )

    health = _app_tools.make_factory_mcp_health(
        _app_tools.HealthInputs(
            calls=_summarise_calls,
            loop_lag=_read_loop_lag,
            operations_in_flight=lambda: _mcp_budget.OPERATIONS.count_in_flight(),
            budget_s=_mcp_budget.TOOL_BUDGET_S,
            started_at=_mcp_budget.SERVER_STARTED_AT,
        )
    )
    added: dict[str, tuple[ToolFn, ToolAnnotations]] = {
        "get_app_status": (cast(ToolFn, _app_tools.get_app_status), _READS_ONLY),
        "get_app_logs": (cast(ToolFn, _app_tools.get_app_logs), _READS_ONLY),
        "restart_app": (
            cast(ToolFn, _app_tools.restart_app),
            ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
            ),
        ),
        "factory_mcp_health": (health, _READS_ONLY),
        "get_operation": (_get_operation_within_the_budget_share(), _READS_ONLY),
    }
    assert tuple(added) == FACTORY_TOOLS
    for name, (fn, hints) in added.items():
        server.add_tool(_serve(name, _note_steps(fn)), name=name, annotations=hints)


_use_the_factorys_tools(app)
