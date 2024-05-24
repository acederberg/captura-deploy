# =========================================================================== #
import base64

import docker
import httpx

# --------------------------------------------------------------------------- #
from captura_pipelines.builder import (
    PATTERN_GITHUB,
    BuildGitInfo,
    BuildImageInfo,
    BuildInfo,
    BuildOptions,
)
from captura_pipelines.config import PipelineConfig
from captura_pulumi import util

from .config import TestConfig


def test_registry_basicauth(config: TestConfig, config_pipelines: PipelineConfig):
    """Use this to ensure that registry basic auth is functioning."""
    domain = config.require("domain")

    # NOTE: In shell, do ``Authorization=$(echo -n "username:password" | base64 --encode )"
    host = f"https://registry.{domain}/v2/"
    response = httpx.get(host)
    assert response.status_code == 401

    headers = {"Authorization": f"Basic {config_pipelines.registry.basicauth}"}
    response = httpx.get(host, headers=headers)
    assert response.status_code == 200


def test_docker_login(config: TestConfig, config_pipelines: PipelineConfig):
    domain = config.require("domain")
    host = f"https://registry.{domain}/v2/"

    client = docker.DockerClient()
    for key, value in client.api.headers.items():
        print(f"{key:<32}{value}")

    # https://docs.docker.com/engine/api/v1.45/#section/Versioning
    # https://github.com/docker/docker-py/blob/a3652028b1ead708bd9191efb286f909ba6c2a49/docker/api/daemon.py#L97
    client.api.login(
        username=config_pipelines.registry.username,
        password=config_pipelines.registry.password,
        registry=host,
        dockercfg_path="/etc/docker/config.json",
    )


def test_pattern_github():
    p = PATTERN_GITHUB

    m = p.match("https://github.com/acederberg/captura")
    assert m is not None
    assert m.group("scheme") == "https"
    assert m.group("auth") is None
    assert m.group("username") == "acederberg"
    assert m.group("repository") == "captura"
    assert m.group("slug") == "acederberg/captura"
    assert m.group("dotgit") == None
    assert m.group("path") == None

    m = p.match("ssh://git@github.com/acederberg/yaml_setting_pydantic.git")
    assert m is not None
    assert m.group("scheme") == "ssh"
    assert m.group("auth") == "git@"
    assert m.group("auth_username") == "git"
    assert m.group("auth_password") is None
    assert m.group("username") == "acederberg"
    assert m.group("repository") == "yaml_setting_pydantic"
    assert m.group("dotgit") == ".git"
    assert m.group("path") == None


class TestBuildInfo:
    def test_properties(self, config_pipelines: PipelineConfig):

        build_info = BuildInfo(
            config=config_pipelines,
            image=BuildImageInfo(
                repository="test-properties",
                tags={"test", "properties"},
                labels=dict(test="properties", because="necessary"),
            ),
            git=BuildGitInfo(
                repository="https://github.com/acederberg/captura",
                branch="master",
                tag=None,
                commit=None,
                dockerdir="docker",
                dockerfile="dockerfile",
                dockertarget=None,
            ),
            options=BuildOptions(
                tier=util.LabelTier.base,
                push=False,
            ),
        )

        # NOTE: Check git commit and git path.
        assert build_info.git.commit is None
        build_info.git.configure(build_info.git_path)
        assert build_info.git.commit is not None
        assert isinstance(build_info.git.commit, str)

        assert util.p.basename(build_info.git_path) == build_info.image.repository
        assert util.p.isdir(build_info.git_path)

        # NOTE: Check labels and tags.
        labels = build_info.image_labels
        assert isinstance(labels, dict)
        assert len(labels) == 5
        assert labels["acederberg.io/from"] == "builder"
        assert labels["acederberg.io/tier"] == "base"
        assert labels["acederberg.io/component"] == "registry"
        assert labels["acederberg.io/test"] == "properties"
        assert labels["acederberg.io/because"] == "necessary"

        tags = build_info.image_tags
        commit = build_info.git.commit
        assert f"acederberg.io/test-properties:{commit}" in tags
        assert len(tags) == 3

    def test_execute(self):
        # NOTE: Build self tests using the local docker client.
        # NOTE: Run some basic tests in the client.
        # BuilderCommand
        ...
