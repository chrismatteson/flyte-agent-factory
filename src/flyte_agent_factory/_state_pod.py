"""What a pod that touches the factory's state needs: the state volume mounted, and the right identity.

The volume backend is a directory, so every pod must see the same one. The profile names a
PersistentVolumeClaim (`[state] volume_claim`) and the path it is mounted at (`[state] volume`). A claim and
not a `hostPath`: containers run unprivileged, and a `DirectoryOrCreate` host path is a root-owned directory
the pod cannot write.

The service accounts are how a bucket is reached (IRSA, Workload Identity): the factory's pods share one, the
personal browser app has its own, so that the bucket's policy can grant each only its own prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

import flyte

if TYPE_CHECKING:
    from kubernetes.client import V1PodSpec

    from ._profile import Profile

_VOLUME_NAME = "factory-state"


class EnvironmentKwargs(TypedDict, total=False):
    """The keyword arguments `environment_kwargs` returns; a TypedDict so that `**` type-checks."""

    pod_template: flyte.PodTemplate
    service_account: str


class StateVolumeTemplate(flyte.PodTemplate):
    """A pod template that mounts the state volume claim (when there is one) and may set the grace period.

    The Kubernetes objects are built only when Flyte reads `pod_spec`, in its serializer, which imports
    `kubernetes` itself first. This module is imported where `kubernetes` is not installed: by the deployer
    and by every slot pod (agent image), so building the `V1PodSpec` on first read keeps that package a need
    of the process that deploys alone. Flyte's app serializer requires the primary container to be `app`.
    """

    def __init__(
        self,
        *,
        primary_container_name: str,
        grace_seconds: int | None,
        volume: str = "",
        claim: str = "",
    ) -> None:
        self._spec: V1PodSpec | None = None
        self._grace_seconds = grace_seconds
        self._volume = volume
        self._claim = claim
        super().__init__(primary_container_name=primary_container_name)

    @property
    def pod_spec(self) -> V1PodSpec:
        if self._spec is None:
            from kubernetes.client import (
                V1Container,
                V1PersistentVolumeClaimVolumeSource,
                V1PodSpec,
                V1Volume,
                V1VolumeMount,
            )

            container = V1Container(name=self.primary_container_name)
            volumes = None
            if self._claim:
                container.volume_mounts = [V1VolumeMount(name=_VOLUME_NAME, mount_path=self._volume)]
                volumes = [
                    V1Volume(
                        name=_VOLUME_NAME,
                        persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(claim_name=self._claim),
                    )
                ]
            self._spec = V1PodSpec(
                containers=[container],
                volumes=volumes,
                termination_grace_period_seconds=self._grace_seconds,
            )
        return self._spec

    @pod_spec.setter
    def pod_spec(self, value: V1PodSpec | None) -> None:
        self._spec = value

    def __repr__(self) -> str:
        # The dataclass repr would read `pod_spec`, and so import `kubernetes`.
        return f"{type(self).__name__}(termination_grace_period_seconds={self._grace_seconds})"


def volume_template(
    profile: Profile | None, primary_container_name: str, grace_seconds: int | None
) -> StateVolumeTemplate | None:
    """Return the template for `profile`'s state, or None when it needs none (no volume, no grace period)."""
    state = profile.state if profile else None
    on_volume = state is not None and state.backend == "volume"
    if not on_volume and grace_seconds is None:
        return None
    return StateVolumeTemplate(
        primary_container_name=primary_container_name,
        grace_seconds=grace_seconds,
        volume=state.volume if state and on_volume else "",
        claim=state.volume_claim if state and on_volume else "",
    )


def environment_kwargs(
    profile: Profile | None,
    role: Literal["factory", "browser"],
    *,
    app: bool = False,
    grace_seconds: int | None = None,
) -> EnvironmentKwargs:
    """Return the `pod_template=` and `service_account=` an environment of `role` needs for `[state]`.

    Empty for a git or bucket profile with no service account: the zero-setup deploy is unchanged and needs
    no `kubernetes` package. The pod template is built only for the volume backend (or a grace period).
    `app` marks an app environment, whose primary container Flyte requires to be named `app`.
    """
    kwargs: EnvironmentKwargs = {}
    if profile is None:
        return kwargs
    primary = "app" if app or role == "browser" else "primary"
    template = volume_template(profile, primary, grace_seconds)
    if template is not None:
        kwargs["pod_template"] = template
    account = (
        profile.state.browser_service_account if role == "browser" else profile.state.factory_service_account
    )
    if account:
        kwargs["service_account"] = account
    return kwargs
