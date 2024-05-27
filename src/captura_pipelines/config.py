# =========================================================================== #
import base64
import re
from typing import Annotated, Dict

import docker
import httpx
from pydantic import BeforeValidator, Field, computed_field
from yaml_settings_pydantic import BaseYamlSettings, YamlSettingsConfigDict

# --------------------------------------------------------------------------- #
from captura_pulumi import util
from captura_pulumi.util import BaseYAML

# https://docs.docker.com/reference/cli/docker/image/tag/

# NOTE: The reference states that underscores must be discluded.
PATTERN_REGISTRY = re.compile("^(?P<host>[a-zA-Z0-9\\.-]*)(:(?P<port>))?$")


def validate_registry(v):
    if not isinstance(v, str):
        return v

    if PATTERN_REGISTRY.match(v) is None:
        raise ValueError(f"Field must match `{PATTERN_REGISTRY}`.")

    return v


class RegistryConfig(BaseYAML):
    username: Annotated[str, Field()]
    password: Annotated[str, Field()]
    registry: Annotated[str, Field(), BeforeValidator(validate_registry)]

    def create_client(self) -> docker.DockerClient:
        client = docker.from_env()
        client.login(
            username=self.username,
            password=self.password,
            registry=self.registry,
        )
        return client

    @property
    def basicauth(self) -> str:
        auth = f"{self.username}:{self.password}"
        return base64.b64encode(auth.encode()).decode()

    def headers(self) -> Dict[str, str]:
        return dict(authorization=f"Basic {self.basicauth}")

    @computed_field
    @property
    def registry_api(self) -> str:
        return f"https://{self.registry}/v2"

    def registry_url(self, *segments: str) -> str:
        return self.registry_api + "/" + "/".join(segments)

    def req_catalog(
        self,
        # n: int | None = None,
    ) -> httpx.Request:
        return httpx.Request(
            "GET",
            self.registry_url("_catalog"),
            headers=self.headers(),
        )


PIPELINES_CONFIG = util.path.config("pipelines.yaml")


class PipelineConfig(BaseYamlSettings):

    model_config = YamlSettingsConfigDict(yaml_files=PIPELINES_CONFIG)

    registry: RegistryConfig
