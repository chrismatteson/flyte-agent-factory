"""The bootstrap's Flyte side: `flyte.remote` for the project, secrets and tasks, and the deploy scripts.

Nothing here prints or logs a secret value: values go from the caller's hands into the SDK call and
no further.
"""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from ._bootstrap_base import (
    EXPECTED_ERRORS,
    AccessDenied,
    BootstrapError,
    IssuedKey,
    KeyExists,
    NotSignedIn,
    Settings,
)
from ._deploy import strip_hand_deploy_keys

ENGINE_ROOT = Path(__file__).resolve().parents[2]
_MCP_URL_LINE = re.compile(r"^factory-mcp public URL:\s*(\S+)\s*$")
_CONFIG_SECONDS = 120
#: A deploy builds images remotely, which takes minutes; an hour is far past any healthy one, and a script
#: that hangs (a lost connection mid-upload) must not hold the person's terminal for ever.
_DEPLOY_SECONDS = 3600


def _is_not_found(exc: ConnectError) -> bool:
    """Whether `exc` is the control plane saying "no such thing" (as opposed to a network or auth failure)."""
    return exc.code == Code.NOT_FOUND


def _union_plugin_present() -> bool:
    """Return whether the Union plugin that makes API keys is installed."""
    try:
        return importlib.util.find_spec("flyteplugins.union.remote") is not None
    except ImportError:  # find_spec imports the parent package, which raises when it is absent
        return False


def _load_union_remote() -> Any:
    """Return `flyteplugins.union.remote` (ApiKey, Policy, Assignment), imported on use: Linux-only plugin."""
    # flyteplugins ships no type information (no py.typed); import_module keeps the module Any for mypy, as
    # tests/test_guided_setup_docs.py does with the same package.
    return importlib.import_module("flyteplugins.union.remote")


def _read_binding(binding: Any) -> dict[str, str | None]:
    """Read a `PolicyBinding` as its role and, for a project resource, its project and domain names."""
    resource = binding.resource
    project = resource.project if resource.WhichOneof("resource") == "project" else None
    return {
        "role": binding.role_id.name,
        "project": project.name if project is not None else None,
        "domain": project.domain.name if project is not None else None,
    }


class FlyteSdk:
    """The real `FlyteOps`, over `flyte.remote` and the `flyte create config` command."""

    def __init__(self, settings: Settings, config_path: Path | None = None) -> None:
        self._settings = settings
        self.config_path = config_path or Path("~/.flyte/config.yaml").expanduser()
        self._ready = False

    def config_endpoint(self) -> str | None:
        try:
            text = self.config_path.read_text()
        except OSError:
            return None
        match = re.search(r"^\s*endpoint:\s*(\S+)", text, re.MULTILINE)
        return match[1].strip("\"'") if match else ""

    def create_config(self) -> None:
        s = self._settings
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # The CLI is run through the interpreter that holds the SDK, so no `flyte` need be on PATH.
        args = ["--endpoint", s.endpoint, "--project", s.project, "--domain", s.domain]
        if s.endpoint.startswith(("localhost", "127.0.0.1")):
            args.append("--insecure")  # a local open-source cluster has no TLS
        try:
            proc = subprocess.run(
                [sys.executable, "-c", "from flyte.cli.main import main; main()", "create", "config", *args]
                + ["--output", str(self.config_path)],
                capture_output=True,
                text=True,
                timeout=_CONFIG_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise BootstrapError(f"flyte create config did not finish within {_CONFIG_SECONDS}s") from None
        if proc.returncode != 0:
            raise BootstrapError(f"flyte create config failed: {proc.stderr.strip()[-500:]}")

    def _can_sign_in_without_a_browser(self) -> bool:
        """Whether a call now would not open the sign-in browser: a cached login, or a non-interactive type.

        Reads the SDK's own keyring entry (the SDK has no public "is there a login" call): `Pkce` and
        `DeviceFlow`, the interactive types and the default, need one; a client secret or an external command
        does not.
        """
        from flyte.remote._client.auth._keyring import KeyringStore

        try:
            text = self.config_path.read_text()
        except OSError:
            return False
        auth_type = re.search(r"^\s*authType:\s*(\S+)", text, re.MULTILINE)
        if auth_type and auth_type[1].strip("\"'") not in ("Pkce", "DeviceFlow"):
            return True
        return KeyringStore.retrieve(self.config_endpoint() or "") is not None

    def _init(self) -> None:
        if not self._ready:
            import flyte
            from flyte.errors import InitializationError

            if self._settings.read_only and not self._can_sign_in_without_a_browser():
                # `--status` and `--dry-run` change nothing, and a first sign-in is a change of this machine.
                raise NotSignedIn("not signed in, and a read-only run does not open the browser")

            try:
                flyte.init_from_config(
                    self.config_path, project=self._settings.project, domain=self._settings.domain
                )
            except InitializationError as exc:  # no config yet, or one the SDK cannot use
                raise NotSignedIn(f"no usable Flyte config at {self.config_path}: sign in first") from exc
            self._ready = True

    def sign_in(self) -> None:
        """Make one authenticated call; with PKCE and no cached token it opens the browser sign-in."""
        from flyte import remote

        self._init()
        # `Project.listall` takes no `limit` (the SDK's signature): one page is enough to prove the sign-in.
        next(iter(remote.Project.listall()), None)

    def project_exists(self) -> bool:
        from flyte import remote

        self._init()
        try:
            remote.Project.get(self._settings.project)
        except ConnectError as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    def create_project(self) -> None:
        from flyte import remote

        self._init()
        remote.Project.create(id=self._settings.project, name=self._settings.project)

    def secret_exists(self, name: str) -> bool:
        from flyte import remote

        self._init()
        try:
            remote.Secret.get(name)
        except ConnectError as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    def create_secret(self, name: str, value: str) -> None:
        from flyte import remote

        self._init()
        try:
            remote.Secret.create(name=name, value=value)
        except EXPECTED_ERRORS as exc:
            # The class only: the SDK's message may quote the request.
            raise BootstrapError(f"creating secret {name} failed: {type(exc).__name__}") from None

    def factory_registered(self) -> bool:
        from flyte import remote

        self._init()
        return next(iter(remote.Task.listall(by_task_name="factory.launch", limit=1)), None) is not None

    def factory_mcp_url(self) -> str | None:
        from flyte import remote

        self._init()
        try:
            return remote.App.get("factory-mcp").endpoint or None
        except ConnectError as exc:
            if _is_not_found(exc):
                return None
            raise

    def can_manage_keys(self) -> bool:
        return _union_plugin_present()

    def create_api_key(self, name: str) -> IssuedKey:
        self._init()
        try:
            key = _load_union_remote().ApiKey.create(name, skip_default_policy_assignments=True)
        except ConnectError as exc:
            if exc.code == Code.ALREADY_EXISTS:
                raise KeyExists(name) from None
            raise BootstrapError(f"creating API key {name!r} failed: {type(exc).__name__}") from None
        return IssuedKey(client_id=key.client_id, encoded=key.encoded_credentials)

    def delete_api_key(self, client_id: str) -> None:
        self._init()
        _load_union_remote().ApiKey.delete(client_id=client_id)

    def ensure_policy(self, name: str) -> None:
        try:
            self._ensure_policy(name)
        except ConnectError as exc:
            if exc.code == Code.PERMISSION_DENIED:
                raise AccessDenied(f"Union refused to make or read policy {name!r}") from None
            raise

    def _ensure_policy(self, name: str) -> None:
        remote = _load_union_remote()
        self._init()
        wanted = {"role": "contributor", "project": self._settings.project, "domain": self._settings.domain}
        binding = {
            "role": "contributor",
            "resource": {"project": wanted["project"], "domain": wanted["domain"]},
        }
        try:
            existing = remote.Policy.get(name)
        except ConnectError as exc:
            if not _is_not_found(exc):
                raise
            remote.Policy.create(name, bindings=[binding])
            return
        # The organization is the policy's own, so only the role, project and domain are compared. Read from
        # the protobuf: `Policy.bindings` flattens a project resource to its name and drops the domain.
        held = [_read_binding(b) for b in existing.pb2.bindings]
        if not held:  # made by an earlier run that stopped before adding the binding
            remote.Policy.update(name, [], [binding])
        elif held != [wanted]:
            raise BootstrapError(
                f"policy {name!r} exists but does not bind exactly contributor on project "
                f"{wanted['project']} and domain {wanted['domain']}; ask your Union administrator to fix "
                "it (docs/factory-mcp-setup.md, Grant the key a role)"
            )

    def assign_policy(self, client_id: str, policy: str) -> None:
        remote = _load_union_remote()
        self._init()
        try:
            remote.Assignment.create(creds_subject=client_id, policy=policy)
        except ConnectError as exc:
            if exc.code == Code.PERMISSION_DENIED:
                raise AccessDenied(f"Union refused to assign policy {policy!r}") from None
            # An admin who runs the printed `--grant` command twice finds the assignment already made.
            if exc.code != Code.ALREADY_EXISTS:
                raise


class DeployScripts:
    """The real `Deployer`: `ops/deploy_work.py` then `ops/deploy_factory.py`, as a person runs them."""

    def __init__(
        self,
        say: Callable[[str], None] = print,
        env: Mapping[str, str] | None = None,
        engine_root: Path = ENGINE_ROOT,
    ) -> None:
        self._say = say
        self._env = dict(os.environ if env is None else env)
        self._root = engine_root

    def deploy(self, profile: Path, *, work: bool = True) -> str | None:
        # The scripts act as the person's own CLI login (`init_for_script`), never a key in the environment.
        env = strip_hand_deploy_keys(self._env)
        env["PYTHONPATH"] = str(self._root / "src")
        url = None
        for script in ("deploy_work.py", "deploy_factory.py") if work else ("deploy_factory.py",):
            url = self._run_script(script, profile, env) or url
        return url

    def _run_script(self, script: str, profile: Path, env: dict[str, str]) -> str | None:
        """Run one deploy script, echoing its output; return the factory-mcp URL it printed, if any."""
        proc = subprocess.Popen(
            [sys.executable, str(self._root / "ops" / script), "--profile", str(profile)],
            cwd=self._root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Its own process group, so the watchdog also stops what the script started (a hung `git fetch`
            # keeping the output pipe open would otherwise hold this read loop past the bound).
            start_new_session=True,
        )
        assert proc.stdout is not None
        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        watchdog = threading.Timer(_DEPLOY_SECONDS, stop)
        watchdog.start()
        url = None
        try:
            for line in proc.stdout:
                self._say(line.rstrip())
                if match := _MCP_URL_LINE.match(line):
                    url = match[1]
            code = proc.wait()
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            raise BootstrapError(f"ops/{script} was stopped after {_DEPLOY_SECONDS}s; its output is above")
        if code != 0:
            raise BootstrapError(f"ops/{script} failed ({code}); its output is above")
        return url
