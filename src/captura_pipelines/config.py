# =========================================================================== #
import base64
from typing import Annotated

import docker
from pydantic import Field
from yaml_settings_pydantic import BaseYamlSettings, YamlSettingsConfigDict

# --------------------------------------------------------------------------- #
from captura_pulumi import util
from captura_pulumi.util import BaseYAML


class RegistryConfig(BaseYAML):
    username: Annotated[str, Field()]
    password: Annotated[str, Field()]
    registry: Annotated[str, Field()]

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


PIPELINES_CONFIG = util.path.config("pipelines.yaml")


class PipelineConfig(BaseYamlSettings):

    model_config = YamlSettingsConfigDict(yaml_files=PIPELINES_CONFIG)

    registry: RegistryConfig
