# =========================================================================== #
import base64

import docker
import httpx

from .config import TestConfig


def test_registry_basicauth(config: TestConfig):
    """Use this to ensure that registry basic auth is functioning."""
    domain = config.require("domain")

    # NOTE: In shell, do ``Authorization=$(echo -n "username:password" | base64 --encode )"

    host = f"https://registry.{domain}/v2/"
    response = httpx.get(host)
    assert response.status_code == 401

    headers = {"Authorization": f"Basic {config.registry_basicauth}"}
    response = httpx.get(host, headers=headers)
    assert response.status_code == 200


def test_docker_login(config: TestConfig):
    domain = config.require("domain")
    host = f"https://registry.{domain}/v2/"

    client = docker.DockerClient()

    client.api.headers["X-Registry-Auth"] = f"Basic {config.registry_xregauth}"
    client.api.headers["Authorization"] = f"Basic {config.registry_basicauth}"
    for key, value in client.api.headers.items():
        print(f"{key:<32}{value}")

    # https://docs.docker.com/engine/api/v1.45/#section/Versioning
    # https://github.com/docker/docker-py/blob/a3652028b1ead708bd9191efb286f909ba6c2a49/docker/api/daemon.py#L97
    client.api.login(
        username=config.registry_username,
        password=config.registry_password,
        registry=host,
        dockercfg_path="/etc/docker/config.json",
    )
