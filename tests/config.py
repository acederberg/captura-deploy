# =========================================================================== #
import base64
import json

from yaml_settings_pydantic import (
    BaseYamlSettings,
    YamlFileConfigDict,
    YamlSettingsConfigDict,
)

# --------------------------------------------------------------------------- #
from captura_pulumi import util


# NOTE: Pulumi config values in pytest.yaml should be overwritten using their
#       name as in the pulumi config.
def to_pulumi(field):
    if field in CONFIG_PYTEST_EXCLUSIVE_FIELDS:
        return field

    return f"CapturaPulumi:{field}"


CONFIG_PATH_PULUMI = util.path.base("Pulumi.captura-dev.yaml")
CONFIG_PATH_PYTEST = util.path.config("pytest.yaml")
CONFIG_PYTEST_EXCLUSIVE_FIELDS = {"registry_username", "registry_password"}


# NOTE: Inteded as a drop in replacedment for pulumi.config.
class TestConfig(BaseYamlSettings):

    model_config = YamlSettingsConfigDict(
        yaml_files={
            # TODO: Add ``exclude`` to exclude particular sources.
            CONFIG_PATH_PULUMI: YamlFileConfigDict(
                subpath="config",
                required=True,
            ),
            CONFIG_PATH_PYTEST: YamlFileConfigDict(
                subpath=None,
                required=True,
            ),
        },
        alias_generator=to_pulumi,
        extra="allow",
    )

    # NOTE: Should be found in pulumi.yaml
    domain: str

    # NOTE: Should be found in configs/pytest.yaml
    registry_username: str
    registry_password: str

    @property
    def registry_basicauth(self) -> str:
        auth = f"{self.registry_username}:{self.registry_password}"
        return base64.b64encode(auth.encode()).decode()

    @property
    def registry_xregauth(self) -> str:
        auth = {
            "username": self.registry_username,
            "password": self.registry_password,
            "email": "acederberg@acederberg.io",
            "serveraddress": f"registry.{self.domain}",
        }
        return base64.b64encode(json.dumps(auth).encode()).decode()

    # NOTE: Prototyping. Not adding all methods now.
    def require(self, field: str) -> str:
        if field not in self.model_fields:
            msg = f"No such value `{field}` specified in the pulumi test config."
            raise ValueError(msg)
        elif self.model_fields[field].annotation != str:
            msg = f"Cannot require `{field}` as a string."
            raise ValueError(msg)

        return getattr(self, field)

    require_secret = require
