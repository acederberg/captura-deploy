# =========================================================================== #

# =========================================================================== #
import os
from collections.abc import Sequence
from os import path as p
from typing import Any, Dict

import yaml
from pydantic.v1.utils import deep_update

PATH_BASE: str = p.realpath(p.join(p.dirname(__file__), "..", ".."))
PATH_CONFIGS: str = p.realpath(p.join("configs"))
PATH_ASSETS: str = p.realpath(p.join("assets"))
PATH_LOGS: str = p.realpath(p.join("logs"))


def ensure(dirpath: str):
    if p.isfile(dirpath):
        raise ValueError(f"`{dirpath}` should not be a file.")

    if p.isdir(dirpath) or p.exists(dirpath):
        return

    os.mkdir(dirpath)


ensure(PATH_LOGS)
ensure(PATH_CONFIGS)


class path:
    @staticmethod
    def base(*segments: str) -> str:
        return p.join(PATH_BASE, *segments)

    @staticmethod
    def logs(*segments: str) -> str:
        return p.join(PATH_LOGS, *segments)

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
