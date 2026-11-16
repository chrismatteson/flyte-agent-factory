"""Guided setup, the machine's half: `uv run python ops/bootstrap.py --endpoint ... --workspace ...`.

A claude.ai chat has no shell, so the chat asks the questions and hands over one command; this runs it.
Every step is `check()` then `do()`, the check reads live state (never a progress file), and a step whose
check passes is skipped, so a run after any interruption resumes. One line per step:
`[done|skip|todo|blocked] <step>: <detail>`. Exit 0 when every step is done or skipped, 3 when one needs the
person (the one next action is printed last).

Design: docs/quickstart.md. The steps are in `_bootstrap_steps.py` and `_bootstrap_finish.py`, the adapters
(`gh`, Flyte, deploy scripts) in `_bootstrap_github.py` and `_bootstrap_flyte.py`; tests run the same table
against fakes.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from . import _bootstrap_base as base
from . import _bootstrap_state as state_choice
from ._bootstrap_base import (
    BLOCKED,
    DEFAULT_DOMAIN,
    DONE,
    TODO,
    BootstrapError,
    Check,
    Context,
    Settings,
    Step,
)
from ._bootstrap_finish import ConnectStep, DeployStep, GithubAppStep, McpUrlStep, ProfileStep
from ._bootstrap_flyte import DeployScripts, FlyteSdk
from ._bootstrap_github import GhCli
from ._bootstrap_keys import GRANTABLE, UnionKeysStep, grant_key, make_chat_key
from ._bootstrap_steps import (
    BoardStep,
    DeployKeyStep,
    EngineTokenStep,
    ProjectStep,
    RulesetStep,
    SignInStep,
    TemplateStep,
    ToolsStep,
    WorkspaceStep,
)

EXIT_OK, EXIT_TODO = 0, 3
KEY_MODES = {"chat-key": "the chat key", "grant": "granting a key"}
_WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_T = TypeVar("_T")
# Every later step runs `gh`, `git` or `uv`; without them their checks end in a raw FileNotFoundError.
_GATE_STEP = "tools"
_WAITING = "the steps after tools wait on it"


def build_steps() -> list[Step]:
    """Return the table, in order."""
    return [
        ToolsStep(),
        WorkspaceStep(),
        TemplateStep(),
        ProfileStep(),
        # Sign-in and project come before the deploy key and the App, against the usual order of the
        # workspace's steps: both store their private key as a Flyte secret, which needs them.
        SignInStep(),
        ProjectStep(),
        # The factory's own Union keys, stored as secrets over the CLI login: the pods and the deployer act as
        # them. After the profile, which names the secrets.
        UnionKeysStep(),
        DeployKeyStep(),
        GithubAppStep(),
        # After the pushes of the template, the profile and the App's profile keys; the factory-URL push
        # that follows the deploy is the owner's, who bypasses the ruleset (`ruleset_body`).
        RulesetStep(),
        BoardStep(),
        EngineTokenStep(),
        DeployStep(),
        McpUrlStep(),
        ConnectStep(),
    ]


def _format_line(status: str, step: str, detail: str) -> str:
    return f"[{status}] {step}: {detail}"


def _guard(label: str, call: Callable[[], _T], *, read_only: bool = False) -> _T | base.Outcome:
    """Run one step method; an expected error becomes a `blocked` outcome naming it, never a traceback.

    Anything else propagates. `read_only` is whether the run was started with `--status` or `--dry-run`: only
    then does a message name them, since the key modes cannot be given either.

    Adapters keep secret values out of their exceptions (see `_bootstrap_base`), so the text is safe to print.
    """
    try:
        return call()
    except base.NotSignedIn:
        # Not a failure of this step: everything that asks Flyte waits for the sign-in step.
        again = "run it again without --status or --dry-run" if read_only else "run it again"
        return base.Outcome(TODO, f"sign in first: {again} and finish the browser sign-in")
    except subprocess.SubprocessError as exc:
        # Its text is the command line, which may carry a credential given as an argument: the class only.
        return base.Outcome(BLOCKED, f"{label} failed: {type(exc).__name__}")
    except base.EXPECTED_ERRORS as exc:
        return base.Outcome(BLOCKED, f"{label} failed: {type(exc).__name__}: {str(exc)[:300]}")


def run(ctx: Context, table: Sequence[Step]) -> int:
    """Run the table; return the exit code. `settings.status_only` runs the checks alone, `dry_run` plans."""
    status_only = ctx.settings.status_only
    read_only = ctx.settings.read_only
    pending: list[str] = []
    for step in table:
        checked = _guard("check", functools.partial(step.check, ctx), read_only=read_only)
        if isinstance(checked, base.Outcome):  # the check itself raised
            ctx.say(_format_line(checked.status, step.id, checked.detail))
            pending.append(checked.detail)
            # `--status` and `--dry-run` report every row: a step waiting for the sign-in does not end them.
            if not step.advisory and not (checked.status == TODO and read_only):
                break
            continue
        check: Check = checked
        if step.informational and not read_only:
            outcome = _guard("step", functools.partial(step.do, ctx), read_only=read_only)
            ctx.say(
                _format_line("done" if outcome.status == DONE else outcome.status, step.id, outcome.detail)
            )
            continue
        if check.ok:
            ctx.say(
                _format_line(
                    "done" if status_only and not step.informational else "skip", step.id, check.detail
                )
            )
            continue
        gate = step.id == _GATE_STEP
        if status_only:
            # `blocked` for the gate, as in a real run: the table stops there instead of going on to fail.
            ctx.say(_format_line(BLOCKED if gate else "todo", step.id, check.detail))
            pending.append(check.detail)
            if gate:
                ctx.say(_WAITING)
                break
            continue
        if ctx.settings.dry_run:
            planned = step.plan(ctx)
            ctx.say(_format_line("todo", step.id, f"would {planned}"))
            pending.append(planned)
            if gate:
                ctx.say(_WAITING)
                break
            continue
        outcome = _guard("step", functools.partial(step.do, ctx), read_only=read_only)
        ctx.say(_format_line(outcome.status, step.id, outcome.detail))
        if outcome.status != DONE:
            pending.append(outcome.detail)
            if not step.advisory:
                if gate:
                    ctx.say(_WAITING)
                break
    if pending:
        ctx.say(f"next: {pending[0]}")
        return EXIT_TODO
    return EXIT_OK


def _signed_in(ctx: Context, mode: str) -> bool:
    """Sign in if the config does not work yet; say why on the `mode` line and return False if that fails."""
    signin = SignInStep()
    checked = _guard("check", functools.partial(signin.check, ctx))
    if isinstance(checked, base.Outcome) or not checked.ok:
        outcome = _guard("step", functools.partial(signin.do, ctx))
        if outcome.status != DONE:
            ctx.say(_format_line(outcome.status, mode, outcome.detail))
            return False
    return True


def run_key_mode(ctx: Context, mode: str, make: Callable[[], base.Outcome] | None = None) -> int:
    """Run `--chat-key` or `--grant` (`make`); print its one line and return the exit code.

    The sign-in step runs first, as in the table: the keys are made in the org the Flyte config names, which
    must be `--endpoint`'s, and these commands are often run on a machine that has no config yet.
    """
    if make is None:
        make = functools.partial(make_chat_key, ctx)
    # Needs no Union call, so it comes before the sign-in opens a browser for nothing.
    if not ctx.flyte.can_manage_keys():
        what = KEY_MODES[mode]
        needs = f"{what}: {base.KEY_PLUGIN_REQUIREMENT}. Run this on a Linux x86_64 machine"
        ctx.say(_format_line(BLOCKED, mode, needs))
        return EXIT_TODO
    if not _signed_in(ctx, mode):
        return EXIT_TODO
    outcome = _guard("step", make)
    ctx.say(_format_line(outcome.status, mode, outcome.detail))
    return EXIT_OK if outcome.status == DONE else EXIT_TODO


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], prog="ops/bootstrap.py")
    ap.add_argument("--endpoint", required=True, help="the Flyte/Union host, e.g. flyte.example.com")
    ap.add_argument("--project", required=True, help="the Flyte project the factory runs in")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN)
    ap.add_argument("--workspace", required=True, help="<owner>/<repo>, the workspace repository")
    ap.add_argument(
        "--state-uri",
        help="deprecated and ignored: the old Flyte-bucket location of the state (ops/migrate_state.py moves "
        "it); to keep the state in a bucket of the factory's own, use --state-bucket",
    )
    ap.add_argument(
        "--state-bucket",
        metavar="URI",
        help="keep the factory's state in this bucket (s3://bucket/prefix or gs://bucket/prefix), never "
        "Flyte's own; without it AGENT_FACTORY_STATE_BUCKET, then --terraform-dir, else a git branch",
    )
    ap.add_argument("--state-region", default="", help="region of --state-bucket (S3)")
    ap.add_argument("--state-endpoint", default="", help="S3-compatible endpoint of --state-bucket (MinIO)")
    ap.add_argument(
        "--factory-service-account",
        default="",
        help="Kubernetes service account every pod that touches state runs as; else the "
        "`factory_service_accounts` Terraform output (with a state bucket)",
    )
    ap.add_argument(
        "--browser-service-account",
        default="",
        help="Kubernetes service account of the personal browser apps; else the Terraform output "
        "(with a state bucket)",
    )
    ap.add_argument(
        "--terraform-dir",
        type=Path,
        help="read `terraform output -json` here for factory_bucket (and _region, _endpoint); never applies",
    )
    ap.add_argument(
        "--flyte-bucket",
        action="append",
        default=[],
        metavar="URI",
        help="Flyte's own data bucket or prefix, which the state must stay out of (recorded as "
        "[state] flyte_bucket); repeat for more than one",
    )
    ap.add_argument("--backend", choices=("union", "oss"), default="union")
    ap.add_argument("--engine", choices=("claude", "codex"), default="claude")
    ap.add_argument("--no-app", action="store_true", help=f"skip the GitHub App; {base.APP_LOST}")
    ap.add_argument("--no-board", action="store_true", help="skip the Projects board")
    ap.add_argument("--dir", type=Path, help="the workspace clone (default: ~/<repo>)")
    ap.add_argument(
        "--redeploy",
        action="store_true",
        help="deploy the work slots and the factory again from the public engine's main, even if the last "
        "deploy matches the profile",
    )
    reading = ap.add_mutually_exclusive_group()
    reading.add_argument(
        "--dry-run", action="store_true", help="print what each step would do; change nothing"
    )
    reading.add_argument("--status", action="store_true", help="read-only: print each step's check and stop")
    keys = ap.add_mutually_exclusive_group()
    keys.add_argument(
        "--chat-key",
        action="store_true",
        help="instead of the steps: make the factory-chat Union API key and its grant, and connect Claude "
        "Code with it, or show it once on this terminal (Linux x86_64)",
    )
    keys.add_argument(
        "--union-keys",
        action="store_true",
        help="instead of the steps: sign in, then make the factory's project and deployer Union API keys and "
        "store each as a Flyte secret, printing only client ids (Linux x86_64 makes them; elsewhere you "
        "paste keys made in the console at a hidden prompt)",
    )
    keys.add_argument(
        "--grant",
        nargs=2,
        metavar=("POLICY", "CLIENT_ID"),
        help=f"instead of the steps, for a Union admin: give policy {' or '.join(GRANTABLE)} (made if "
        "missing) to the key with this client ID, which --chat-key or --union-keys printed when its maker "
        "could not (Linux x86_64)",
    )
    return ap


def _choose_state(args: argparse.Namespace, ctx: Context) -> None:
    """Set where the profile keeps the state: the factory's bucket if one is found, else the git branch."""
    settings = ctx.settings
    settings.flyte_buckets = state_choice.find_flyte_buckets(ctx.flyte.config_path, args.flyte_bucket)
    found, why = state_choice.find_state_bucket(
        explicit=args.state_bucket or "",
        region=args.state_region,
        endpoint=args.state_endpoint,
        factory_service_account=args.factory_service_account,
        browser_service_account=args.browser_service_account,
        environ=os.environ,
        terraform_dir=args.terraform_dir,
    )
    if found is None:
        ctx.say(f"state: {why + '; ' if why else ''}{state_choice.KEEP_GIT}")
        return
    state_choice.check_state_bucket(found, settings.flyte_buckets)
    settings.state_bucket, settings.state_region, settings.state_endpoint = (
        found.uri,
        found.region,
        found.endpoint,
    )
    settings.factory_service_account = found.factory_service_account
    settings.browser_service_account = found.browser_service_account
    # The bucket's own URI is not printed: a mistyped one could carry a credential, which
    # `check_state_bucket` would have refused.
    ctx.say(f"state: bucket from {found.source} goes into [state] of the profile (never Flyte's own bucket)")
    if found.uri.partition("://")[0] in ("s3", "gs"):
        ctx.say(f"state: {state_choice.VERSIONING}")


def main(
    argv: Sequence[str] | None = None, *, make_context: Callable[[Settings], Context] | None = None
) -> int:
    """Run the bootstrap. `make_context` lets tests supply fake adapters; the command line never does."""
    ap = _build_parser()
    args = ap.parse_args(argv)
    if (args.chat_key or args.grant) and (args.dry_run or args.status):
        ap.error("--chat-key and --grant change Union; they cannot be combined with --dry-run or --status")
    if (args.chat_key or args.union_keys or args.grant) and args.backend == "oss":
        ap.error(
            "--chat-key, --union-keys and --grant make Union API keys and policies; OSS Flyte has neither "
            "(see docs/oss-flyte-compat.md for MCP authentication there)"
        )
    if args.grant and args.grant[0] not in GRANTABLE:
        ap.error(f"--grant gives only {' or '.join(GRANTABLE)}")
    if not _WORKSPACE.fullmatch(args.workspace):
        ap.error("--workspace must be <owner>/<repo>")
    if args.state_uri is not None:
        # Old copy-paste commands carry it; they must keep running.
        print(
            "note: --state-uri is ignored; the old Flyte-bucket state moves with ops/migrate_state.py "
            "(docs/state-store.md)"
        )
    if not args.project.strip():
        ap.error("--project must not be empty")
    if not args.domain.strip():
        ap.error("--domain must not be empty")
    endpoint = base.normalise_endpoint(args.endpoint)
    if not endpoint:
        ap.error("--endpoint must be a host, e.g. flyte.example.com")
    settings = Settings(
        endpoint=endpoint,
        project=args.project,
        workspace=args.workspace,
        domain=args.domain,
        backend=args.backend,
        engine=args.engine,
        no_app=args.no_app,
        no_board=args.no_board,
        directory=args.dir.expanduser().resolve() if args.dir else None,
        dry_run=args.dry_run,
        redeploy=args.redeploy,
        status_only=args.status,
    )
    if make_context is None:
        context = Context(settings, GhCli(), FlyteSdk(settings), DeployScripts())
    else:
        context = make_context(settings)
    if not (args.chat_key or args.union_keys or args.grant):
        try:
            _choose_state(args, context)
        except BootstrapError as exc:
            ap.error(str(exc))
    if args.grant:
        return run_key_mode(context, "grant", functools.partial(grant_key, context, *args.grant))
    if args.chat_key:
        return run_key_mode(context, "chat-key")
    if args.union_keys:  # only this step, after the sign-in it needs: the keys are made in the config's org
        if not context.settings.read_only and not _signed_in(context, "union-keys"):
            return EXIT_TODO
        return run(context, [UnionKeysStep()])
    return run(context, build_steps())


if __name__ == "__main__":
    sys.exit(main())
