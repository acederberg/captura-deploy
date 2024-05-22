# =========================================================================== #
import base64
from typing import Annotated

import httpx
import pytest
from pydantic import Field
from yaml_settings_pydantic import (
    BaseYamlSettings,
    YamlFileConfigDict,
    YamlSettingsConfigDict,
)

# --------------------------------------------------------------------------- #
from captura_pulumi import util
from tests.config import TestConfig

PYTEST_STASHKEY_CONFIG = pytest.StashKey[TestConfig]()


# NOTE: Using the stash means that there are many cases where the number of
#       fixtures used is fewer! I'm going to keep using this pattern since it
#       makes solving session dependencies more straightforward.
def pytest_configure(config: pytest.Config):
    config.stash[PYTEST_STASHKEY_CONFIG] = TestConfig()  # type: ignore


@pytest.fixture
def config(pytestconfig: pytest.Config) -> TestConfig:
    return pytestconfig.stash[PYTEST_STASHKEY_CONFIG]
