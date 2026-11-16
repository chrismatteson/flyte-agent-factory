"""Every image the factory deploys must end as the unprivileged user under the SDK's local builder."""

import flyte
import pytest
from flyte._internal.imagebuild.docker_builder import DockerImageBuilder, PodmanImageBuilder

from flyte_agent_factory import _bench_check, _image_user, _images
from flyte_agent_factory._deploy import Refused

FACTORY_IMAGES = {
    "agent": lambda: _images.agent_image(),
    "agent-browser": lambda: _images.agent_image(browser=True),
    "agent-tailscale": lambda: _images.agent_image(tailscale=True),
    "browser": lambda: _images.browser_image(),
    "browser-tailscale": lambda: _images.browser_image(tailscale=True),
    "agent-extra-pip": lambda: _images.agent_image().with_pip_packages("httpx>=0.27"),
    "bench-check": lambda: _bench_check.env.image,
}


def _root_image() -> flyte.Image:
    return flyte.Image.from_debian_base(name="x", python_version=(3, 12)).with_commands(["true"])


@pytest.mark.parametrize("name", FACTORY_IMAGES)
def test_factory_image_ends_as_flyte(name):
    image = FACTORY_IMAGES[name]()
    assert isinstance(image, flyte.Image)
    dockerfile = _image_user.local_dockerfile(image)
    assert _image_user.final_user(dockerfile) == "flyte"


def test_podman_builder_renders_the_same_dockerfile():
    # The check renders through the docker builder; podman must not diverge from it.
    assert PodmanImageBuilder._build_image is DockerImageBuilder._build_image


def test_final_user_is_the_last_instruction_without_its_group():
    assert _image_user.final_user("USER root\nRUN x\nUSER flyte:flyte\n") == "flyte"
    assert _image_user.final_user("FROM x\n") is None


def test_root_image_is_refused_before_deploy():
    env = flyte.TaskEnvironment(name="rooted", image=_root_image())
    with pytest.raises(Refused, match="rooted.*x.*run_as_flyte"):
        _image_user.check_unprivileged(env)


def test_root_image_of_a_dependency_is_refused():
    dep = flyte.TaskEnvironment(name="dep", image=_root_image())
    env = flyte.TaskEnvironment(name="top", image="ghcr.io/example/image:1", depends_on=[dep])
    with pytest.raises(Refused, match="dep"):
        _image_user.check_unprivileged(env)


def test_string_image_is_skipped():
    _image_user.check_unprivileged(flyte.TaskEnvironment(name="prebuilt", image="ghcr.io/example/image:1"))


def test_factory_image_is_accepted():
    _image_user.check_unprivileged(flyte.TaskEnvironment(name="ok", image=_images.agent_image()))


def test_deployed_environments_are_unprivileged():
    from flyte_agent_factory import (
        _bench_flow,
        _deployer,
        _factory,
        _factory_mcp,
        _loop_flow,
        _reconcile,
        _webhook,
        agent,
        browser,
    )

    envs = [
        agent.env,
        agent.subagent_env,
        agent.setup_env,
        _factory.env,
        _factory_mcp.app,
        _deployer.env,
        _reconcile.env,
        _webhook.env,
        _bench_flow.env,
        _loop_flow.env,
        *browser.identity_apps.values(),
    ]
    _image_user.check_unprivileged(*envs)
