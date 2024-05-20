# =========================================================================== #
from collections.abc import Sequence
from os import path as p
from typing import Any, Dict

import yaml
from pydantic.v1.utils import deep_update

PATH_BASE: str = p.realpath(p.join(p.dirname(__file__), "..", ".."))
PATH_CONFIGS: str = p.realpath(p.join("configs"))
PATH_ASSETS: str = p.realpath(p.join("assets"))


class path:
    @staticmethod
    def base(*segments: str) -> str:
        return p.join(PATH_BASE, *segments)

    @staticmethod
    def asset(*segments: str) -> str:
        return p.join(PATH_ASSETS, *segments)

    @staticmethod
    def config(*segments: str) -> str:
        return p.join(PATH_CONFIGS, *segments)


def load(*paths: str, overwrite: Dict[str, Any] | None = None):
    if not len(paths):
        raise ValueError()

    files = tuple(open(path, "r") for path in paths)

    try:
        loaded = list(yaml.safe_load(file) for file in files)
    except Exception as err:
        tuple(file.close() for file in files)
        raise err

    if overwrite is not None:
        loaded.append(overwrite)

    return deep_update(*loaded)
