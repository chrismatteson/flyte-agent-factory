"""The privileged pod: a docker:dind native sidecar plus the primary (harness) container.

Copied from projects/oss-setup-selftest/selftest/pod.py (not imported: that is another project's tree, and
this package travels to the pod by value). Only the work-directory variable's name differs.

Recipe from the 2026-11-08 privileged-pod probe: privileged dind sidecar with explicit CPU limits,
a shared emptyDir at /work (HOME and TMPDIR for the inner CLI live in it, so the bind mounts that
`flyte start devbox` asks dockerd for resolve to the same files in both containers), dockerd on
plain TCP with `--tls=false`.

The spec is built as a plain dict so it can be tested without the `kubernetes` package; only
`to_pod_template` needs it (at submission time, on the submitting machine).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import flyte

WORK = "/work"
DOCKER_PORT = 2375
DOCKER_HOST = f"tcp://127.0.0.1:{DOCKER_PORT}"
DIND_IMAGE = "docker:27.5.1-dind"
PRIMARY = "primary"

# Sized for a c5.4xlarge spot node (16 CPU / 32 GiB), the only non-GPU pool on the demo cluster
# that holds a devbox plus a factory (the docs' minimum for the factory alone is 4 CPU / 16 GiB).
# The dind sidecar runs the devbox (k3s, Flyte, every inner pod, every image build), so it gets
# the bulk of the pod. Limits are explicit: an unbounded privileged sidecar can starve the node.
DIND_CPU = "11"
DIND_MEMORY = "20Gi"
DIND_DISK = "80Gi"


DOCKERD_ARGS = [
    # Loopback only: an unauthenticated dockerd is root on the node, so it must not be reachable
    # from the cluster network. `--ip` makes every port the devbox publishes (its unauthenticated
    # Flyte API, registry and k3s API) loopback-only too.
    f"--host=tcp://127.0.0.1:{DOCKER_PORT}",
    "--tls=false",
    "--ip=127.0.0.1",
]


def build_pod_spec(
    *, dind_cpu: str = DIND_CPU, dind_memory: str = DIND_MEMORY, dind_disk: str = DIND_DISK
) -> dict[str, Any]:
    """Return the child pod spec: primary container plus privileged dind native sidecar."""
    dind = {
        "name": "dind",
        "image": DIND_IMAGE,
        # Native sidecar: an init container that keeps running beside the primary and is stopped
        # after it, so the pod ends (and dockerd with every inner container) when the task ends.
        "restartPolicy": "Always",
        "securityContext": {"privileged": True},
        "env": [{"name": "DOCKER_TLS_CERTDIR", "value": ""}],
        # The daemon's log goes to the shared volume, so the harness can put dockerd's own error in
        # the report (container logs of a sidecar are not visible to the submitting session).
        "command": ["/bin/sh", "-c"],
        # `dockerd` first: given bare flags the entrypoint prepends `--host=tcp://0.0.0.0:2375`, which
        # opens the daemon on every interface and collides with the loopback listener (seen live).
        "args": [f"exec dockerd-entrypoint.sh dockerd {' '.join(DOCKERD_ARGS)} >>{WORK}/dind.log 2>&1"],
        "resources": {
            "requests": {"cpu": dind_cpu, "memory": dind_memory, "ephemeral-storage": dind_disk},
            "limits": {"cpu": dind_cpu, "memory": dind_memory, "ephemeral-storage": dind_disk},
        },
        "volumeMounts": [
            {"name": "work", "mountPath": WORK},
            {"name": "docker", "mountPath": "/var/lib/docker"},
        ],
    }
    primary = {
        "name": PRIMARY,
        "env": [
            {"name": "DOCKER_HOST", "value": DOCKER_HOST},
            {"name": "DEVBOX_MCP_WORK", "value": WORK},
        ],
        "volumeMounts": [{"name": "work", "mountPath": WORK}],
    }
    return {
        "initContainers": [dind],
        "containers": [primary],
        "volumes": [{"name": "work", "emptyDir": {}}, {"name": "docker", "emptyDir": {}}],
    }


def to_pod_template(spec: dict[str, Any] | None = None) -> flyte.PodTemplate:
    """Return `spec` (default: `build_pod_spec()`) as a Flyte PodTemplate."""
    import flyte
    from kubernetes.client import ApiClient

    spec = spec or build_pod_spec()
    # The client's own deserializer turns the camelCase dict into V1 objects.
    v1 = ApiClient().deserialize(json.dumps(spec), "V1PodSpec", "application/json")
    return flyte.PodTemplate(pod_spec=v1, primary_container_name=PRIMARY)
