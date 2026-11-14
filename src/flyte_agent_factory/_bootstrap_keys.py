"""The keys of the bootstrap: `--chat-key`, and the `union-keys` step (`--union-keys`).

Each makes a Union API key with no default policies, a policy of the same name (`contributor` on the
factory's project and domain) and its assignment, then hands the key over without it reaching stdout, stderr
or a chat: to `claude mcp add` as a subprocess argument, once to the controlling terminal, or straight into a
Flyte secret (`union-keys`, which stores the factory's project and deployer keys and prints only their client
ids). When Union refuses the person the policy or the assignment, the key is still handed over and the line
ends with one filled-in `--grant POLICY CLIENT_ID` command for an admin to run (docs/factory-mcp-setup.md,
"Grant the key a role"); that command makes the same policy and assignment, and no key.
"""

from __future__ import annotations

import shlex
import subprocess
import tomllib
from typing import Protocol

from . import _bootstrap_profile as profile_file
from . import _union_identity
from ._bootstrap_base import (
    BLOCKED,
    DEFAULT_DOMAIN,
    DONE,
    EXPECTED_ERRORS,
    TODO,
    AccessDenied,
    BootstrapError,
    Check,
    Context,
    IssuedKey,
    KeyExists,
    Outcome,
    Settings,
)
from ._bootstrap_flyte import ENGINE_ROOT
from ._bootstrap_profile import ENGINE_REPO_URL, SECRET_UNION_DEPLOYER, SECRET_UNION_PROJECT

CHAT_KEY, PROJECT_KEY, DEPLOYER_KEY = "factory-chat", "factory-project", "factory-deployer"
#: The only policies `--grant` gives: an admin's run must not become a way to assign anything else.
GRANTABLE = (CHAT_KEY, PROJECT_KEY, DEPLOYER_KEY)
#: The connector names that count as "already connected": the one this adds, and the one the hidden-prompt
#: block in docs/chat-operator.md adds.
CONNECTOR_NAMES = ("agent-factory", "factory")
_CLONE_AND_SYNC = (
    'd=~/flyte-agent-factory; u={url}; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && '
    'case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; '
    '*) echo "$d is not a clone of $u" >&2; false ;; esac && '
    'git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && '
    "uv sync -q --python 3.12 && uv run python ops/bootstrap.py"
)


def build_command(settings: Settings, flag: str = "") -> str:
    """Return the one copy-paste command: clone or update the engine, then run the bootstrap with `flag`.

    The prefix is the one skills/agent-factory/references/setup.md hands over; tests hold the two together.
    """
    parts = [
        _CLONE_AND_SYNC.replace("{url}", ENGINE_REPO_URL),
        f"--endpoint {shlex.quote(settings.endpoint)}",
        f"--project {shlex.quote(settings.project)}",
        f"--workspace {shlex.quote(settings.workspace)}",
        f"--state-uri {shlex.quote(settings.state_uri)}",
    ]
    if settings.domain != DEFAULT_DOMAIN:
        parts.append(f"--domain {shlex.quote(settings.domain)}")
    if flag:
        parts.append(flag)
    return " ".join(parts)


def build_delete_secret_command(settings: Settings, name: str) -> str:
    """Return the command that deletes Flyte secret `name` in the run's project and domain."""
    return (
        f"cd {shlex.quote(str(ENGINE_ROOT))} && uv run flyte delete secret {shlex.quote(name)} "
        f"-p {shlex.quote(settings.project)} -d {shlex.quote(settings.domain)}"
    )


def find_connector_url(ctx: Context) -> str:
    """Return the factory-mcp connector URL from the profile, else from the deployed app; "" when unknown."""
    try:
        url = tomllib.loads(ctx.profile_path.read_text())["mcp"]["public_url"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        url = ""
    url = url or ctx.flyte.factory_mcp_url() or ""
    return f"{url.rstrip('/')}/flyte-mcp/mcp" if url else ""


def _make_key(ctx: Context, name: str) -> tuple[IssuedKey, bool] | Outcome:
    """Make policy `name`, the key `name` and the assignment; undo the key when a later call fails.

    Returns the key and whether it was granted. A key Union refused this person the right to grant is kept:
    its secret goes to the person as usual and an admin grants it by client ID (`_admin_must_grant`).
    """
    # The policy first: one that exists with other bindings stops the run before a key is made, which would
    # otherwise be lost with its secret. A refusal for want of access-management rights does not stop it.
    try:
        ctx.flyte.ensure_policy(name)
        granted = True
    except AccessDenied:
        granted = False
    try:
        key = ctx.flyte.create_api_key(name)
    except KeyExists:
        return Outcome(
            BLOCKED,
            f"a Union API key named {name!r} already exists and its secret cannot be read back; delete it "
            "(`flyte get api-key` lists its client ID, `flyte delete api-key <client ID>` removes it) "
            "and run this again, or reuse the key you stored when you made it "
            "(docs/factory-mcp-setup.md, Connecting a client)",
        )
    if not granted:
        return key, False
    try:
        ctx.flyte.assign_policy(key.client_id, name)
    except AccessDenied:
        return key, False
    except EXPECTED_ERRORS as exc:
        _discard(ctx, key)
        raise BootstrapError(
            f"assigning policy {name!r} to the new key failed ({type(exc).__name__}); the key was deleted"
        ) from None
    except Exception:  # an unexpected failure: its secret never reached the person either, so the key goes
        _discard(ctx, key)
        raise
    return key, True


def _admin_must_grant(ctx: Context, key: IssuedKey, name: str, handed_over: str) -> Outcome:
    """Return the `todo` for a key the person holds but may not grant: one filled-in command for an admin."""
    command = build_command(ctx.settings, f"--grant {name} {shlex.quote(key.client_id)}")
    return Outcome(
        TODO,
        f"key {name} made and {handed_over}, but Union refused you the right to give it policy {name}, so "
        f"it can do nothing yet. Send this one command to a Union admin; it gives policy {name} "
        f"(contributor on {ctx.settings.project}/{ctx.settings.domain}) to the key's client ID "
        f"{key.client_id} and makes no key: {command}",
    )


def grant_key(ctx: Context, policy: str, client_id: str) -> Outcome:
    """`--grant POLICY CLIENT_ID`: the admin's half, for a key whose maker may not grant it."""
    ctx.flyte.ensure_policy(policy)
    ctx.flyte.assign_policy(client_id, policy)
    return Outcome(
        DONE,
        f"policy {policy} (contributor on {ctx.settings.project}/{ctx.settings.domain}) assigned to the key "
        f"with client ID {client_id}",
    )


def _discard(ctx: Context, key: IssuedKey) -> None:
    """Delete a key that was made but never reached the person: its secret is lost, so it is of no use."""
    try:
        ctx.flyte.delete_api_key(key.client_id)
    except EXPECTED_ERRORS as exc:
        ctx.say(
            f"could not delete the unused API key {key.client_id!r} ({type(exc).__name__}); delete it by hand"
        )


def _show_once(ctx: Context, key: IssuedKey, lines: str) -> Outcome | None:
    """Write `lines` and then the key to the terminal. Returns an outcome only when that is impossible."""
    try:
        ctx.write_tty(f"\n{lines}\n{key.encoded}\n\n")
    except OSError:
        _discard(ctx, key)
        return Outcome(
            BLOCKED,
            "there is no terminal to show the new key on, so it was deleted; run this command in a terminal "
            "window, not through a tool that captures output",
        )
    return None


def _connector_present(ctx: Context) -> bool:
    proc = ctx.run_command(["claude", "mcp", "list"])
    listing = proc.stdout.lower() if proc.returncode == 0 else ""
    return any(f"{name}:" in listing for name in CONNECTOR_NAMES)


def make_chat_key(ctx: Context) -> Outcome:
    """`--chat-key`: make the chat key and connector credentials, then hand the key over."""
    connector = find_connector_url(ctx)
    if not connector:
        return Outcome(TODO, "factory-mcp has no public URL yet; run the bootstrap without --chat-key first")
    have_claude = ctx.which("claude") is not None
    if have_claude and _connector_present(ctx):
        return Outcome(DONE, "the agent-factory connector is already in Claude Code; no new key made")
    made = _make_key(ctx, CHAT_KEY)
    if isinstance(made, Outcome):
        return made
    key, granted = made
    if have_claude:
        try:
            proc = ctx.run_command(
                [
                    "claude", "mcp", "add", "-s", "user", "--transport", "http", "agent-factory", connector,
                    "--header", f"Authorization: Bearer {key.encoded}",
                ]
            )  # fmt: skip
        except (subprocess.SubprocessError, OSError) as exc:  # its text holds the argv, so the key: unprinted
            _discard(ctx, key)
            return Outcome(
                BLOCKED,
                f"claude mcp add failed ({type(exc).__name__}); the new key was deleted, run this again",
            )
        if proc.returncode != 0:
            # Neither stream is printed: claude may echo the header it was given.
            _discard(ctx, key)
            return Outcome(
                BLOCKED, f"claude mcp add failed ({proc.returncode}); the new key was deleted, run this again"
            )
        if not granted:
            return _admin_must_grant(ctx, key, CHAT_KEY, "the agent-factory connector added to Claude Code")
        return Outcome(
            DONE,
            f"key {CHAT_KEY} made, policy {CHAT_KEY} assigned (contributor on {ctx.settings.project}/"
            f"{ctx.settings.domain}); the agent-factory connector was added to Claude Code",
        )
    shown = _show_once(
        ctx,
        key,
        f"Your {CHAT_KEY} key is below, shown once, on this terminal only. In claude.ai add a custom "
        f"connector for {connector} with a request header Authorization: Bearer <the key>; paste it into "
        "that header and never into a chat.",
    )
    if shown:
        return shown
    if not granted:
        return _admin_must_grant(ctx, key, CHAT_KEY, "shown once on your terminal for the connector's header")
    return Outcome(
        DONE,
        f"key {CHAT_KEY} made, policy {CHAT_KEY} assigned (contributor on {ctx.settings.project}/"
        f"{ctx.settings.domain}); the key is shown once on your terminal only, to paste into the "
        "connector's header",
    )


class KeyMaker(Protocol):
    """Where a `union-keys` key comes from. `make` returns the key and whether its policy was granted."""

    def make(self, ctx: Context, policy: str) -> tuple[IssuedKey, bool] | Outcome: ...

    def discard(self, ctx: Context, key: IssuedKey) -> None:
        """Undo `make` for a key that could not be stored."""
        ...


class PluginKeyMaker:
    """Linux x86_64: `flyteplugins-union` makes the key and its grant."""

    def make(self, ctx: Context, policy: str) -> tuple[IssuedKey, bool] | Outcome:
        return _make_key(ctx, policy)

    def discard(self, ctx: Context, key: IssuedKey) -> None:
        _discard(ctx, key)


class PastedKeyMaker:
    """macOS, or no plugin: the person makes the key in the console and pastes it at a hidden prompt.

    The key is read with `ctx.ask_secret` (`getpass`): never echoed, never written to disk, and handed on
    only to the secret store. The key is the person's own, made outside this tool, so `discard` leaves it.
    """

    def make(self, ctx: Context, policy: str) -> tuple[IssuedKey, bool] | Outcome:
        # The steps are the prompt, not a `say` line: a step's own status line stays the first line it prints.
        pasted = ctx.ask_secret(
            f"make the Union API key for policy {policy} in the console: open the API keys page of "
            f"https://{ctx.settings.endpoint}, create a key named {policy} with no default policies, and "
            f"copy its value (it is shown once).\nPaste API key for {policy} (hidden): "
        ).strip()
        try:
            client_id = _union_identity.client_id_of(pasted)
        except ValueError:  # its text never quotes the key
            return Outcome(BLOCKED, f"what you pasted for {policy} is not a Union API key; run this again")
        return IssuedKey(client_id=client_id, encoded=pasted), self._grant_policy(ctx, policy, client_id)

    def _grant_policy(self, ctx: Context, policy: str, client_id: str) -> bool:
        if not ctx.flyte.can_manage_keys():  # no plugin, so no policy call: an admin on Linux grants
            return False
        try:
            ctx.flyte.ensure_policy(policy)
            ctx.flyte.assign_policy(client_id, policy)
        except AccessDenied:
            return False
        return True

    def discard(self, ctx: Context, key: IssuedKey) -> None:
        pass


class UnionKeysStep:
    """Make the factory's project and deployer keys and store each as a Flyte secret.

    Stored with `create_secret` over the person's CLI login, which works on a Mac. Only client ids are
    printed. The secret names are the profile's `[union]` ones (`ProfileStep` writes them).
    """

    id = "union-keys"
    informational = False
    advisory = False

    def _list_wanted(self, ctx: Context) -> list[tuple[str, str]]:
        """(secret, policy) for each distinct key: one key serves both tiers when the names are equal."""
        try:
            table = tomllib.loads(ctx.profile_path.read_text()).get("union", {})
        except (OSError, tomllib.TOMLDecodeError):  # `ProfileStep` reports a profile that does not parse
            table = {}
        project = table.get("project_key") or SECRET_UNION_PROJECT
        deployer = table.get("deployer_key") or SECRET_UNION_DEPLOYER
        wanted = [(project, PROJECT_KEY)]
        if deployer != project:
            wanted.append((deployer, DEPLOYER_KEY))
        return wanted

    def _find_missing(self, ctx: Context) -> list[tuple[str, str]]:
        return [
            (secret, policy)
            for secret, policy in self._list_wanted(ctx)
            if not ctx.flyte.secret_exists(secret)
        ]

    def check(self, ctx: Context) -> Check:
        if ctx.settings.backend == "oss":
            return Check(True, "open-source Flyte has no Union API keys")
        missing = self._find_missing(ctx)
        if missing:
            return Check(False, f"no secret {', '.join(secret for secret, _ in missing)}")
        return Check(True, f"secrets {', '.join(secret for secret, _ in self._list_wanted(ctx))} exist")

    def plan(self, ctx: Context) -> str:
        names = ", ".join(secret for secret, _ in self._list_wanted(ctx))
        return f"make the factory's Union API keys and store them as secrets {names} (only client ids print)"

    def _make_key_maker(self, ctx: Context) -> KeyMaker:
        if ctx.platform == "darwin" or not ctx.flyte.can_manage_keys():
            return PastedKeyMaker()
        return PluginKeyMaker()

    def do(self, ctx: Context) -> Outcome:
        maker = self._make_key_maker(ctx)
        stored, ungranted = [], []
        for secret, policy in self._find_missing(ctx):
            made = maker.make(ctx, policy)
            if isinstance(made, Outcome):
                return made
            key, granted = made
            kept = False
            try:
                ctx.flyte.create_secret(secret, key.encoded)
                kept = True
            finally:
                if not kept:
                    maker.discard(ctx, key)
            stored.append(f"{secret} (client {key.client_id}, policy {policy})")
            if not granted:
                ungranted.append((policy, key.client_id))
        added = (
            profile_file.fill_missing(ctx.profile_path, ctx.settings, None)
            if ctx.profile_path.is_file()
            else []
        )
        note = f"; added {', '.join(added)} to {ctx.profile_path.name}: commit and push it" if added else ""
        done = f"stored {'; '.join(stored)}{note}"
        if not ungranted:
            return Outcome(DONE, done)
        commands = " ; ".join(
            build_command(ctx.settings, f"--grant {p} {shlex.quote(c)}") for p, c in ungranted
        )
        return Outcome(
            TODO,
            f"{done}, but Union refused you the right to give them their policies "
            f"(contributor on {ctx.settings.project}/{ctx.settings.domain}), or this machine cannot. Send "
            f"this to a Union admin on a Linux x86_64 machine; it makes no key: {commands}",
        )
