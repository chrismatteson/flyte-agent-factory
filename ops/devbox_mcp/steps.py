"""The harness steps, inside the privileged pod: a fresh devbox, this engine on it, a live run, the MCP calls.

The devbox install is the oss-setup-selftest's (projects/oss-setup-selftest/selftest/install.py), copied
rather than imported because that is another project's tree; each copied step says so. What differs:
- the engine is this working tree, shipped as a tarball (uncommitted changes included), not a clone of a ref;
- `ops/deploy_work.py` is not run: no agent slot is needed, and `ops/deploy_factory.py` builds the agent image
  and deploys every factory task (`factory.status` among them) before its MCP app;
- deploy_factory.py failing is recorded, not fatal: on the base engine Knative rejects the MCP app (the
  selftest's gap 1) after the tasks are deployed, and the MCP calls here are made in-process anyway;
- the Claude token secret is a placeholder: nothing here runs an agent;
- instead of a project launch, `probe_run.py` starts a stand-in project run, and `mcp_calls.py` calls the
  factory MCP tools about it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from . import profile as profile_mod
from .pod import DOCKER_HOST, WORK
from .runner import Redactor, Runner, StepFailed, wrap_in_isolated_env

DOCKER_CLI_URL = "https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz"
KUBECTL_URL = "https://dl.k8s.io/release/v1.31.2/bin/linux/amd64/kubectl"
BUILDX_URL = "https://github.com/docker/buildx/releases/download/v0.19.3/buildx-v0.19.3.linux-amd64"
BINFMT_IMAGE = "tonistiigi/binfmt:qemu-v8.1.5"
BUILDX_BUILDER = "flytex"  # flyte docker_builder.DockerImageBuilder._builder_name
BINFMT_KEEPER = "devbox-mcp-binfmt-keeper"
KEEPER_IMAGE = "alpine:3.20"

PROJECT = "my-agents"  # the example profile's project, kept
DOMAIN = "development"
CONFIG_NAME = "devbox-oss-config.yaml"
PROFILE_NAME = "devbox-mcp"
DEPLOY_KEY_SECRET = "devbox_mcp_deploy_key"
GIT_NS = "devbox-mcp-git"
GIT_URL_IN_CLUSTER = f"ssh://git@git.{GIT_NS}.svc.cluster.local/srv/git/workspace.git"
GIT_LOCAL_PORT = 2222
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "devbox-mcp",
    "GIT_AUTHOR_EMAIL": "devbox-mcp@example.invalid",
    "GIT_COMMITTER_NAME": "devbox-mcp",
    "GIT_COMMITTER_EMAIL": "devbox-mcp@example.invalid",
}
PROBE_MINUTES = 40
# Waits inside mcp_calls.py: up to 10 min for the question to open, 10 min for factory.status, 5 min for the
# probe run to end, plus the calls themselves.
MCP_CALLS_TIMEOUT_S = 40 * 60

STEPS = [
    "pod-tools",
    "engine-unpack",
    "engine-sync",
    "devbox-start",
    "devbox-ready",
    "flyte-config",
    "flyte-project",
    "deploy-key",
    "workspace-repo",
    "profile",
    "secrets",
    "deploy-factory",
    "probe-run",
    "mcp-calls",
]
TEARDOWN = "teardown"


@dataclass
class Ctx:
    """State one run's steps share: the engine tarball and the facts gathered so far."""

    tarball: Path
    work: Path = Path(WORK)
    # Non-secret facts gathered on the way, for the evidence file.
    facts: dict[str, Any] = field(default_factory=lambda: {"engine_sha": ""})

    @property
    def home(self) -> Path:
        """HOME of every inner command, inside the pod's emptyDir."""
        return self.work / "home"

    @property
    def engine(self) -> Path:
        """The engine tree, unpacked from the tarball."""
        return self.home / "flyte-agent-factory"

    @property
    def config(self) -> Path:
        """The inner Flyte config file."""
        return self.home / ".flyte" / CONFIG_NAME

    @property
    def keydir(self) -> Path:
        """Directory of the fresh deploy key and the placeholder token (0700, in the emptyDir only)."""
        return self.work / "keys"

    def env(self, **extra: str) -> dict[str, str]:
        """Return the whole environment of every inner command (run under `env -i`)."""
        e = {
            "HOME": str(self.home),
            "TMPDIR": str(self.work / "tmp"),
            "PATH": f"{self.work}/bin:{self.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "DOCKER_HOST": DOCKER_HOST,
            "LANG": "C.UTF-8",
            "UV_CACHE_DIR": str(self.work / "uv-cache"),
            "UV_PYTHON_PREFERENCE": "only-system",
            "KUBECONFIG": str(self.home / ".kube" / "config"),
        }
        e.update(extra)
        return e

    def factory_env(self, **extra: str) -> dict[str, str]:
        """Return `env` plus the factory's profile, Flyte config and source path."""
        return self.env(
            AGENT_FACTORY_PROFILE=str(self.engine / "profiles" / f"{PROFILE_NAME}.toml"),
            FLYTE_CONFIG=str(self.config),
            PYTHONPATH="src",
            **extra,
        )


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# --- steps ----------------------------------------------------------------------------------------


def pod_tools(r: Runner, c: Ctx) -> str:
    """Install the docker CLI, buildx and kubectl; wait for the dind sidecar's dockerd (selftest's step)."""
    for d in ("bin", "tmp", "home", "uv-cache"):
        (c.work / d).mkdir(parents=True, exist_ok=True)
    c.keydir.mkdir(mode=0o700, exist_ok=True)
    tgz = c.work / "tmp" / "docker.tgz"
    _download(DOCKER_CLI_URL, tgz)
    with tarfile.open(tgz) as t:
        member = t.getmember("docker/docker")
        member.name = "docker"
        t.extract(member, c.work / "bin")
    _download(KUBECTL_URL, c.work / "bin" / "kubectl")
    (c.work / "bin" / "kubectl").chmod(0o755)
    # The local image builder shells out to `docker buildx`, which the static CLI does not ship.
    plugin = c.home / ".docker" / "cli-plugins" / "docker-buildx"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    _download(BUILDX_URL, plugin)
    plugin.chmod(0o755)
    # uv comes from pip in the task image, outside the minimal PATH.
    uv = shutil.which("uv")
    if uv and not (c.work / "bin" / "uv").exists():
        (c.work / "bin" / "uv").symlink_to(uv)
    missing = [t for t in ("git", "ssh", "ssh-keygen", "uv") if not shutil.which(t, path=c.env()["PATH"])]
    if missing:
        raise StepFailed(f"task image lacks {', '.join(missing)}")
    deadline = time.monotonic() + 180
    while True:
        # `docker info` exits 0 with the daemon down; `docker version` with a server field does not.
        p = r.sh(
            ["docker", "version", "--format", "{{.Server.Version}}"], env=c.env(), check=False, timeout=30
        )
        if p.returncode == 0 and p.stdout.strip():
            break
        if time.monotonic() > deadline:
            log = c.work / "dind.log"
            tail = log.read_text(errors="replace")[-2500:] if log.exists() else "(no dind.log)"
            raise StepFailed(
                f"dockerd in the dind sidecar did not answer within 180s; dind.log tail:\n{tail}"
            )
        time.sleep(3)
    return f"dockerd {p.stdout.strip()}"


def engine_unpack(r: Runner, c: Ctx) -> str:
    """Unpack the shipped engine tree and commit it, so the deploy scripts find the ref they deploy.

    `ops/deploy_factory.py` runs `git fetch origin`, resolves `--ref` to a commit and deploys from a worktree
    of it, so the tree needs a repository with that commit and an `origin`; the repository is its own origin.
    """
    c.engine.mkdir(parents=True)
    with tarfile.open(c.tarball) as t:
        t.extractall(c.engine, filter="data")
    git_env = c.env(**GIT_IDENTITY)
    git = ["git", "-C", str(c.engine)]
    r.sh([*git, "init", "-q", "-b", "main"], env=git_env)
    r.sh([*git, "add", "-A"], env=git_env)
    r.sh([*git, "commit", "-q", "-m", "devbox-mcp: the submitting working tree"], env=git_env)
    r.sh([*git, "remote", "add", "origin", str(c.engine)], env=git_env)
    r.sh([*git, "fetch", "-q", "origin"], env=git_env)
    sha = r.sh([*git, "rev-parse", "HEAD"], env=git_env).stdout.strip()
    c.facts["engine_sha"] = sha
    files = r.sh([*git, "ls-files"], env=git_env).stdout.count("\n")
    return f"engine tree committed as {sha} ({files} files)"


def engine_sync(r: Runner, c: Ctx) -> str:
    """Install the engine's locked environment with `uv sync` (selftest's step)."""
    r.sh(["uv", "sync", "--python", "3.12"], env=c.env(), cwd=str(c.engine), timeout=900)
    v = r.sh(["uv", "run", "flyte", "--version"], env=c.env(), cwd=str(c.engine), timeout=120).stdout.strip()
    return v.splitlines()[-1] if v else ""


def devbox_start(r: Runner, c: Ctx) -> str:
    """Start a fresh devbox with `flyte start devbox` (selftest's step and its recorded workaround)."""
    # `flyte start devbox` bind-mounts ~/.flyte/devbox without creating it, and a rootful dockerd then
    # creates ~/.flyte as root (selftest gap, run 20261116T025849Z).
    (c.home / ".flyte" / "devbox").mkdir(parents=True, exist_ok=True)
    r.sh(["uv", "run", "flyte", "start", "devbox"], env=c.env(), cwd=str(c.engine), timeout=1800)
    return "devbox container started"


def devbox_ready(r: Runner, c: Ctx) -> str:
    """Wait for every pod in the flyte namespace (selftest's step): the console answering is not enough."""
    deadline = time.monotonic() + 300
    while r.sh(["kubectl", "get", "ns", "flyte"], env=c.env(), check=False, timeout=30).returncode != 0:
        if time.monotonic() > deadline:
            raise StepFailed("namespace flyte never appeared")
        time.sleep(5)
    r.sh(
        ["kubectl", "wait", "--for=condition=Ready", "pod", "--all", "-n", "flyte", "--timeout=900s"],
        env=c.env(),
        timeout=960,
    )
    pods = r.sh(["kubectl", "get", "pods", "-A", "-o", "wide"], env=c.env(), check=False).stdout
    svcs = r.sh(["kubectl", "get", "svc", "-A"], env=c.env(), check=False).stdout
    c.facts["devbox_services"] = svcs[-3000:]
    with urllib.request.urlopen("http://localhost:30080/healthz", timeout=10) as resp:
        health = resp.status
    return f"healthz {health}; {len(pods.splitlines()) - 1} pods"


def flyte_config(r: Runner, c: Ctx) -> str:
    """Write the Flyte config for the devbox with `flyte create config --devbox` (selftest's step)."""
    c.config.parent.mkdir(parents=True, exist_ok=True)
    r.sh(
        ["uv", "run", "flyte", "create", "config", "--devbox", "--project", PROJECT, "--domain", DOMAIN]
        + ["--output", str(c.config), "--force"],
        env=c.env(),
        cwd=str(c.engine),
    )
    text = c.config.read_text()
    c.facts["flyte_config"] = text
    return text


def flyte_project(r: Runner, c: Ctx) -> str:
    """Create the factory's Flyte project if it is absent (selftest's step)."""
    flyte = ["uv", "run", "flyte", "--config", str(c.config)]
    p = r.sh([*flyte, "get", "project"], env=c.env(), cwd=str(c.engine), check=False)
    if PROJECT in p.stdout:
        return f"project {PROJECT} already exists"
    r.sh([*flyte, "create", "project", "--id", PROJECT, "--name", "Agents"], env=c.env(), cwd=str(c.engine))
    return f"created project {PROJECT}"


def deploy_key(r: Runner, c: Ctx) -> str:
    """Generate a fresh throwaway deploy key here; the private half never leaves the pod (selftest's step)."""
    key = c.keydir / "deploy_key"
    r.sh(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "devbox-mcp-deploy-key", "-f", str(key)],
        env=c.env(),
    )
    fp = r.sh(["ssh-keygen", "-lf", f"{key}.pub"], env=c.env()).stdout.split()[1]
    return f"fresh ed25519 deploy key, fingerprint {fp}"


GIT_SERVER = """
apiVersion: v1
kind: Namespace
metadata: {{name: {ns}}}
---
apiVersion: v1
kind: ConfigMap
metadata: {{name: git-keys, namespace: {ns}}}
data:
  authorized_keys: |
    {pub}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: git, namespace: {ns}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: git}}}}
  template:
    metadata: {{labels: {{app: git}}}}
    spec:
      containers:
      - name: git
        image: alpine:3.20
        command: ["/bin/sh", "-c"]
        args:
        - |
          set -e
          apk add --no-cache openssh git >/dev/null
          ssh-keygen -A
          adduser -D -s /usr/bin/git-shell git
          echo 'git:*' | chpasswd -e
          mkdir -p /home/git/.ssh /srv/git
          cp /keys/authorized_keys /home/git/.ssh/authorized_keys
          git init -q --bare -b main /srv/git/workspace.git
          chown -R git:git /home/git /srv/git
          chmod 700 /home/git/.ssh; chmod 600 /home/git/.ssh/authorized_keys
          exec /usr/sbin/sshd -D -e
        ports: [{{containerPort: 22}}]
        readinessProbe: {{tcpSocket: {{port: 22}}, periodSeconds: 2}}
        volumeMounts: [{{name: keys, mountPath: /keys}}]
      volumes: [{{name: keys, configMap: {{name: git-keys}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: git, namespace: {ns}}}
spec:
  selector: {{app: git}}
  ports: [{{port: 22, targetPort: 22}}]
"""


def workspace_repo(r: Runner, c: Ctx) -> str:
    """Create a disposable workspace repository behind sshd inside the devbox (selftest's step).

    The profile must name a writable git remote its pods reach with the deploy key; nothing here pushes to it
    after this step, but the factory's tasks are deployed with it configured.
    """
    pub = (c.keydir / "deploy_key.pub").read_text().strip()
    manifest = c.work / "tmp" / "git-server.yaml"
    manifest.write_text(GIT_SERVER.format(ns=GIT_NS, pub=pub))
    r.sh(["kubectl", "apply", "-f", str(manifest)], env=c.env())
    r.sh(
        ["kubectl", "-n", GIT_NS, "rollout", "status", "deploy/git", "--timeout=300s"],
        env=c.env(),
        timeout=320,
    )
    git_env = c.env(
        **GIT_IDENTITY,
        GIT_SSH_COMMAND=f"ssh -i {c.keydir / 'deploy_key'} -o IdentitiesOnly=yes"
        f" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={c.keydir / 'known_hosts'}",
    )
    ws = c.home / "workspace"
    r.sh(
        ["uv", "run", "python", "ops/new_workspace.py", str(ws)], env=git_env, cwd=str(c.engine), timeout=300
    )
    if not (ws / ".git").exists():
        r.sh(["git", "init", "-q", "-b", "main", str(ws)], env=c.env())
    r.sh(["git", "-C", str(ws), "add", "-A"], env=git_env)
    r.sh(
        ["git", "-C", str(ws), "commit", "-q", "--allow-empty", "-m", "workspace from ops/new_workspace.py"],
        env=git_env,
        check=False,
    )
    with _port_forward(c, "svc/git", GIT_LOCAL_PORT, 22, GIT_NS):
        url = f"ssh://git@127.0.0.1:{GIT_LOCAL_PORT}/srv/git/workspace.git"
        r.sh(["git", "-C", str(ws), "push", "-q", url, "HEAD:main"], env=git_env, timeout=120)
        heads = r.sh(["git", "ls-remote", url], env=git_env, timeout=60).stdout
    return f"workspace pushed to {GIT_URL_IN_CLUSTER}: {heads.strip()}"


def _wait_for_port(port: int, attempts: int, sleep: Callable[[float], None]) -> None:
    """Block until 127.0.0.1:`port` accepts a connection, or raise StepFailed."""
    for _ in range(attempts):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            sleep(0.5)
    raise StepFailed(f"nothing listened on 127.0.0.1:{port}")


@contextlib.contextmanager
def _port_forward(c: Ctx, target: str, local: int, remote: int, ns: str) -> Iterator[None]:
    """Run `kubectl port-forward` for the block; always kill and reap it, even on Ctrl-C."""
    argv = wrap_in_isolated_env(["kubectl", "-n", ns, "port-forward", target, f"{local}:{remote}"], c.env())
    with subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
        try:
            _wait_for_port(local, attempts=60, sleep=time.sleep)
            yield
        finally:
            proc.kill()
            proc.wait(timeout=30)


def _read_internal_endpoint(c: Ctx) -> str:
    """Return the in-cluster URL of the devbox's Flyte HTTP API (the `flyte` service on 8090)."""
    svcs = c.facts.get("devbox_services", "")
    for line in svcs.splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[0] == "flyte" and re.search(r"(^|,)8090(:\d+)?/TCP", cols[5]):
            return f"http://{cols[1]}.flyte.svc.cluster.local:8090"
    raise StepFailed("no service on 8090 in the devbox's flyte namespace; services:\n" + svcs[-1500:])


def profile(r: Runner, c: Ctx) -> str:
    """Derive the devbox profile from `profiles/oss-example.toml`, with the selftest's edits."""
    example = (c.engine / "profiles" / "oss-example.toml").read_text()
    edits = [
        ("", "name", PROFILE_NAME, "a name for this profile"),
        (
            "backend",
            "endpoint",
            "localhost:30080",
            "the devbox API, as `flyte create config --devbox` writes it",
        ),
        ("backend", "internal_endpoint", _read_internal_endpoint(c), "the devbox's in-cluster API service"),
        (
            "backend",
            "config",
            f"~/.flyte/{CONFIG_NAME}",
            "the config written by `flyte create config --devbox`",
        ),
        ("repo", "project", GIT_URL_IN_CLUSTER, "the disposable workspace repository inside the devbox"),
        ("secrets", "deploy_key", DEPLOY_KEY_SECRET, "the secret holding the fresh deploy key"),
        ("mcp", "public_url", "", "no public host: the MCP tools are called in-process"),
    ]
    text, changes = profile_mod.derive_profile(example, edits)
    (c.engine / "profiles" / f"{PROFILE_NAME}.toml").write_text(text)
    c.facts["profile"] = text
    return "\n".join(f"[{ch.table or 'top'}] {ch.key}: {ch.old!r} -> {ch.new!r} ({ch.why})" for ch in changes)


def secrets(r: Runner, c: Ctx) -> str:
    """Create the two secrets the factory's tasks mount: a placeholder token and the fresh deploy key."""
    # Nothing here runs an agent, so the token is a placeholder; the deployed tasks only need it to exist.
    token = c.keydir / "claude_code_oauth_token"
    token.write_text("placeholder: the devbox MCP harness runs no agent\n")
    base = ["uv", "run", "flyte", "--config", str(c.config), "create", "secret"]
    scope = ["-p", PROJECT, "-d", DOMAIN]
    r.sh(
        [*base, "claude_code_oauth_token", "--from-file", str(token), *scope], env=c.env(), cwd=str(c.engine)
    )
    r.sh(
        [*base, DEPLOY_KEY_SECRET, "--from-file", str(c.keydir / "deploy_key"), *scope],
        env=c.env(),
        cwd=str(c.engine),
    )
    return r.sh(
        ["uv", "run", "flyte", "--config", str(c.config), "get", "secret", *scope],
        env=c.env(),
        cwd=str(c.engine),
        check=False,
    ).stdout


def _read_secrets_env(r: Runner, c: Ctx) -> dict[str, str]:
    out = r.sh(
        ["uv", "run", "python", "-m", "flyte_agent_factory._profile", "agent_secrets"],
        env=c.factory_env(),
        cwd=str(c.engine),
    ).stdout
    return {"AGENT_FACTORY_SECRETS": out.strip().splitlines()[-1] if out.strip() else ""}


def _list_binfmt_emulators(r: Runner, c: Ctx) -> list[str]:
    p = r.sh(["docker", "run", "--privileged", "--rm", BINFMT_IMAGE], env=c.env(), timeout=600)
    try:  # stdout is followed by stderr (docker pull progress)
        status, _ = json.JSONDecoder().raw_decode(p.stdout, p.stdout.index("{"))
    except ValueError:
        raise StepFailed(f"no binfmt status JSON from {BINFMT_IMAGE}") from None
    return status.get("emulators") or []


def _prepare_builder(r: Runner, c: Ctx) -> None:
    """Apply the selftest's two image-build workarounds (its gaps 2 and 3), copied from its deploy-work step.

    The agent image is built for linux/amd64 and linux/arm64 and its arm64 stage runs an amd64 terraform, so
    the aarch64 binfmt handler is registered with the fix-binary flag; this pod's binfmt_misc lives only while
    mounted, so a keeper container holds a mount for the whole build. The buildx builder is created first
    because two concurrent builds race to create it.
    """
    if "qemu-aarch64" not in _list_binfmt_emulators(r, c):
        r.sh(
            ["docker", "run", "-d", "--privileged", "--name", BINFMT_KEEPER, KEEPER_IMAGE, "sh", "-c"]
            + [
                "mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null;"
                " cat /proc/self/uid_map; exec sleep 86400"
            ],
            env=c.env(),
            timeout=300,
        )
        time.sleep(2)
        c.facts["binfmt_uid_map"] = r.sh(
            ["docker", "logs", BINFMT_KEEPER], env=c.env(), check=False
        ).stdout.strip()
        p = r.sh(
            ["docker", "run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "arm64"],
            env=c.env(),
            timeout=600,
        )
        c.facts["binfmt_installed"] = "qemu-aarch64"
        # binfmt exits 0 even when a registration does not last, so check it from a fresh container.
        if "qemu-aarch64" not in _list_binfmt_emulators(r, c):
            raise StepFailed(
                f"qemu-aarch64 binfmt handler not registered after --install:\n{p.stdout[-1500:]}"
            )
    r.sh(
        ["docker", "buildx", "create", "--name", BUILDX_BUILDER, "--platform", "linux/amd64,linux/arm64"]
        + ["--driver-opt", "network=host"],
        env=c.env(),
        timeout=120,
    )


def deploy_factory(r: Runner, c: Ctx) -> str:
    """Build the agent image and deploy the factory's tasks with `ops/deploy_factory.py`.

    Its exit status is recorded, not enforced: what the later steps need is `factory.status` deployed, which
    the deploy does before the MCP app that this engine cannot deploy on a devbox.
    """
    _prepare_builder(r, c)
    p = r.sh(
        ["uv", "run", "python", "ops/deploy_factory.py", "--ref", c.facts["engine_sha"]],
        env=c.factory_env(**_read_secrets_env(r, c)),
        cwd=str(c.engine),
        timeout=3600,
        check=False,
    )
    c.facts["deploy_factory_exit"] = p.returncode
    if p.returncode != 0:
        errors = [line for line in p.stdout.splitlines() if "ERROR" in line or "Error" in line]
        c.facts["deploy_factory_errors"] = errors[-10:]
        r.gap(
            f"ops/deploy_factory.py exited {p.returncode}; the later steps go on. Last error: {errors[-1:]}"
        )
    return p.stdout[-1500:]


LAUNCHED = re.compile(r"^(\S+) (https?://\S+)$", re.M)  # probe_run.py's `{run} {url}`


def probe_run(r: Runner, c: Ctx) -> str:
    """Start the stand-in project run (probe_run.py), which logs and waits on a question."""
    p = r.sh(
        ["uv", "run", "python", "ops/devbox_mcp/probe_run.py", "--minutes", str(PROBE_MINUTES)],
        env=c.factory_env(),
        cwd=str(c.engine),
        timeout=1800,
    )
    found = LAUNCHED.findall(p.stdout)
    if not found:
        raise StepFailed("probe_run.py printed no `<run> <url>` line")
    c.facts["probe_run"], c.facts["probe_run_url"] = found[-1]
    return f"{found[-1][0]} {found[-1][1]}"


def mcp_calls(r: Runner, c: Ctx) -> str:
    """Call the factory MCP tools in-process about the probe run (mcp_calls.py); keep what they returned."""
    out = c.work / "tmp" / "mcp-calls.json"
    p = r.sh(
        [
            "uv",
            "run",
            "python",
            "ops/devbox_mcp/mcp_calls.py",
            "--run",
            c.facts["probe_run"],
            "--out",
            str(out),
        ],
        env=c.factory_env(),
        cwd=str(c.engine),
        timeout=MCP_CALLS_TIMEOUT_S,
        check=False,
    )
    if out.exists():
        c.facts["mcp"] = json.loads(out.read_text())
    if p.returncode != 0:
        raise StepFailed(f"mcp_calls.py exited {p.returncode}:\n{p.stdout[-2000:]}")
    failed = [call["label"] for call in c.facts["mcp"]["calls"] if call.get("ok") is False]
    return f"{len(c.facts['mcp']['calls'])} records; calls that raised: {failed or 'none'}"


def teardown(r: Runner, c: Ctx) -> str:
    """Delete the devbox and its volume and undo the binfmt handler (selftest's step)."""
    if not (c.work / "bin" / "docker").exists():
        return "nothing to tear down (docker CLI never installed)"
    up = r.sh(["docker", "version", "--format", "{{.Server.Version}}"], env=c.env(), check=False, timeout=30)
    if up.returncode != 0:
        # The devbox lives in the sidecar's emptyDir and dies with the pod.
        raise StepFailed(
            "dockerd unreachable, so teardown could not run or be verified; inner state dies with the pod"
        )
    if c.engine.exists():
        r.sh(
            ["uv", "run", "flyte", "delete", "devbox", "--volume"],
            env=c.env(),
            cwd=str(c.engine),
            check=False,
            timeout=300,
        )
    r.sh(["docker", "rm", "-f", "flyte-devbox"], env=c.env(), check=False)
    r.sh(["docker", "volume", "rm", "-f", "flyte-devbox"], env=c.env(), check=False)
    builders = r.sh(
        ["docker", "ps", "-aq", "--filter", "name=buildx_buildkit_"], env=c.env(), check=False
    ).stdout.split()
    if builders:
        r.sh(["docker", "rm", "-f", *builders], env=c.env(), check=False)
    if c.facts.get("binfmt_installed"):
        undo = r.sh(
            [
                "docker",
                "run",
                "--privileged",
                "--rm",
                BINFMT_IMAGE,
                "--uninstall",
                c.facts["binfmt_installed"],
            ],
            env=c.env(),
            check=False,
            timeout=300,
        )
        if undo.returncode != 0:
            raise StepFailed(
                f"could not remove the {c.facts['binfmt_installed']} binfmt handler this run registered"
            )
        c.facts["binfmt_installed"] = ""
    r.sh(["docker", "rm", "-f", BINFMT_KEEPER], env=c.env(), check=False)
    left = r.sh(["docker", "ps", "-aq"], env=c.env(), check=False).stdout.strip()
    if left:
        raise StepFailed(f"containers still present after teardown: {left}")
    return "devbox container and volume removed; no containers left"


#: Step name to function. The names are the recipe's own and are looked up by them, so they stay as the recipe
#: writes them; the functions are named for what they do.
FUNCS: dict[str, Callable[[Runner, Ctx], str]] = {
    "pod-tools": pod_tools,
    "engine-unpack": engine_unpack,
    "engine-sync": engine_sync,
    "devbox-start": devbox_start,
    "devbox-ready": devbox_ready,
    "flyte-config": flyte_config,
    "flyte-project": flyte_project,
    "deploy-key": deploy_key,
    "workspace-repo": workspace_repo,
    "profile": profile,
    "secrets": secrets,
    "deploy-factory": deploy_factory,
    "probe-run": probe_run,
    "mcp-calls": mcp_calls,
}
assert list(FUNCS) == STEPS


def run_all(ctx: Ctx, redact: Redactor) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every step (teardown always) and return the step records and redacted facts."""
    r = Runner(redact=redact)
    plan = [(name, partial(FUNCS[name], r, ctx), "") for name in STEPS]
    r.run(plan, (TEARDOWN, partial(teardown, r, ctx), ""))
    facts = json.loads(redact(json.dumps(ctx.facts, default=str)))
    return r.results(), facts


def list_secret_env_names() -> list[str]:
    """Names of environment variables that may hold secrets, for the redactor."""
    return sorted(k for k in os.environ if "SECRET" in k.upper() or "TOKEN" in k.upper())
