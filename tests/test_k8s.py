# =========================================================================== #
import base64

import httpx

from .config import TestConfig


def test_registry(config: TestConfig):
    """Use this to ensure that registry basic auth is functioning."""
    domain = config.require("domain")

    registry_username = config.require("registry_username")
    registry_password = config.require("registry_password")
    registry_auth = f"{registry_username}:{registry_password}".encode()
    registry_auth = base64.b64encode(registry_auth).decode()

    url = f"https://registry.{domain}/v2/"
    response = httpx.get(url)
    assert response.status_code == 401

    headers = {"Authorization": f"Basic {registry_auth}"}
    response = httpx.get(url, headers=headers)
    assert response.status_code == 200
