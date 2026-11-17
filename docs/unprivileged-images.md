# Factory images run as an unprivileged user (issue #30)

## Cause

With a released `flyte` (2.10.7, the pinned line), `flyte.Image.from_debian_base(...)` does not build a Debian
base: it returns the prebuilt `ghcr.io/flyteorg/flyte:py3.12-v2.10.7` through `Image._new(base_image=...)`, with
none of the layers the dev path adds, in particular no `_CREATE_FLYTE_USER_CMD` layer. The SDK's local builder
(`flyte/_internal/imagebuild/docker_builder.py`) starts every Dockerfile with `USER root` after `FROM` and appends
`USER flyte` / `WORKDIR /home/flyte` only when `_image_creates_flyte_user(image)` finds that layer. So every factory
image built locally ends `USER root`, even though the ghcr base itself ends `USER flyte` (the Docker Hub listing's
`USER flyte` at layer 31 is the base's; the `USER root` at layer 35 is the template's). Union's remote builder does
not add the `USER root`, which is why the same definition runs as uid 1000 there. It is the image definition plus
the SDK, not the machine that built it: any 2.10.x release reproduces it.

### Reproduction (offline, no docker, no registry) — for the upstream SDK report

```python
import asyncio, contextlib, io

import flyte
from flyte._internal.imagebuild.docker_builder import DockerImageBuilder


class DryRun(DockerImageBuilder):
    @classmethod
    async def _build_command_prefix(cls):
        return ["docker", "buildx", "build"]


image = flyte.Image.from_debian_base(name="repro", python_version=(3, 12)).with_commands(["true"])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    asyncio.run(DryRun()._build_image(image, push=False, dry_run=True))
print([line for line in out.getvalue().splitlines() if line.startswith(("FROM", "USER"))])
# flyte 2.10.7: [... 'FROM ghcr.io/flyteorg/flyte:py3.12-v2.10.7', 'USER root']  -> runs as root
```

Expected: the final user of an image from `from_debian_base` is `flyte` with either builder. Suggested fix upstream:
restore the base image's user (or always append the flyte-user footer) for images whose base is the default image.

## The fix here

- Every factory image ends with the SDK's own create-user layer (`flyte._image._CREATE_FLYTE_USER_CMD`), which is
  what makes the local builder append `USER flyte`; on the remote builder it is an idempotent `RUN`. One definition
  for Union and OSS. The deploy renders each environment's Dockerfile with the SDK's local builder (dry run) and
  refuses an image whose final user is root before `flyte.deploy`.
- The guarantee is in the image, not a pod security context: `runAsNonRoot` cannot verify the non-numeric `USER
  flyte` and would need a hard-coded uid on both backends.
- At run time a factory task that finds itself root raises `NonRecoverableError` before starting a model session,
  so Flyte makes one attempt, not 21. A session that exits before ever reaching its prompt (refused flag, wrong
  user), or a missing binary, is likewise not retried.

## The other open questions

- Other deterministic startup failures were retried too: any non-zero exit of the author session raised a plain
  `RuntimeError` (a refused or unknown flag, a broken install, a login that is gone), as did a resumed start
  that exited at once, a missing `claude`/`codex` binary, and a `claude -p` sub-agent that exited without a
  result. Now an exit with a plain failure status (1-127) before the session ever drew its input prompt, a
  missing or non-executable binary, and a sub-agent exit with no result JSON are `NonRecoverableError`, with the
  real output first. A signal (a kill, OOM) or a crash after the prompt is still retried from the checkpoint.
  Codex runs one process per turn and has no prompt to have reached, so its exits are still retried.
- The sub-agent, setup and lane images: the slot, sub-agent and setup tasks share `agent_image(...)`, and the
  factory, MCP, deployer, browser and bench-check images are built the same way, so all ended as root; all now end
  `USER flyte`, and `tests/test_image_user.py` walks every environment the deploy children deploy. The test lanes
  (`_slow_lane`) run on the image of the action that launches them (`FLYTE_INTERNAL_IMAGE`), the slot's.

## Rebuild and redeploy on the personal OSS cluster

From an engine checkout, with the OSS profile selected (`[backend] kind = "oss"`, `image_builder = "local"` or
`"local-podman"`), docker or podman available and push access to the registry. Both scripts deploy `--ref`
(default `origin/main`) from a throwaway worktree, so pass the merged commit if it is not yet on `origin/main`:

```sh
export AGENT_FACTORY_PROFILE=<path to the personal profile.toml>
PYTHONPATH=src uv run python ops/deploy_work.py --ref <merged sha>      # slots, sub-agent and setup images
PYTHONPATH=src uv run python ops/deploy_factory.py --ref <merged sha>   # factory, factory-mcp, deployer (+ extras)
```

The personal browser apps (one per `[browser]` identity, e.g. `claude-browser-personal`) are deployed by the in-cluster
`factory-deployer` (`_deploy_child.py`: slots, factory, then apps), not by these scripts; they get the new image
when the deployer releases that commit. The image tags change (the definition changed), so the new images
are built and pushed; a deploy that would ship a root image now stops with "refusing to deploy '<env>': its image
'<name>' would run as root under the local image builder". Then launch one project.

What to look for in the first log lines of the slot (`<run>.project`): `Starting the project session as uid 1000,
HOME=/home/flyte` (any non-zero uid; never `HOME=/root`), the workspace under `/home/flyte/state/workspace` (not
`/root/state/workspace`), and no `cannot be used with root/sudo privileges` line. If the pod is still root the run
fails once with "Refusing to start the project session as root (uid 0, HOME=/root)" and is not retried.

What only a real OSS deploy can confirm: that the pushed image's config reports `User: flyte` (check with
`docker image inspect <image> --format '{{.Config.User}}'`), that the cluster does not override the user, and that
the session then starts and takes its first turn.
