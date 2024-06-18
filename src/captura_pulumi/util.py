# =========================================================================== #

# =========================================================================== #
import enum
import logging
import logging.config
import os
from collections.abc import Sequence
from json import JSONDecodeError
from os import path as p
from typing import Any, Dict, List, Self, Set, Tuple

import httpx
import yaml
from jsonpath_ng import parse
from pydantic import BaseModel
from pydantic.v1.utils import deep_update
from rich.console import Console
from rich.syntax import Syntax

CONSOLE = Console()
DOMAIN: str = "acederberg.io"
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


PATH_CONFIG_LOG = path.base("logging.yaml")


def params(**kwargs) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def check(
    res: httpx.Response, *, status_code: int = 200
) -> Tuple[Any, AssertionError | None]:
    try:
        data = res.json()
    except JSONDecodeError as err:
        data = res.content.decode().strip()

    err = None
    if res.status_code != status_code:
        msg = f"Unexpected response from `{res.request.url}`. "
        msg += "Expected response status code `{}`, got `{}`. Data=`{}`."
        err = AssertionError(msg.format(res.status_code, status_code, data))
        return data, err

    return data, err


def print_yaml(raw, *, is_dumped: bool = False, syntax: bool = True):
    rendered = yaml.dump(raw) if not is_dumped else raw

    if syntax:
        CONSOLE.print(Syntax(rendered, "yaml", background_color="default"))
        return
    else:
        print(rendered)


def load(
    *paths: str,
    loaded: List[Dict[str, Any]] | None = None,
    overwrite: Dict[str, Any] | None = None,
    exclude: Dict[str, Any] | Set[str] | None = None,
):
    files = tuple(open(path, "r") for path in paths)

    try:
        loaded_ = list(yaml.safe_load(file) for file in files)
    except Exception as err:
        tuple(file.close() for file in files)
        raise err

    if loaded is not None:
        loaded += loaded_
    else:
        loaded = loaded_

    if overwrite is not None:
        loaded.append(overwrite)

    data = deep_update(*loaded)
    if exclude is None:
        return data

    cond = lambda field: data.get(field) is not None
    if len(bad := {field for field in exclude if cond(field)}):
        msg_fmt = "Helm values must not specify `{}`."
        raise ValueError(msg_fmt.format(bad))

    if not isinstance(exclude, dict):
        return data

    # NOTE: Adding ``None`` values in exclude will result in the field not
    #       being set.
    data = deep_update(data, {k: v for k, v in exclude.items() if v is not None})
    return data


# NOTE: Might want to move to yaml-settings-pydantic. This functionality goes
#       along with that code very well.
class BaseYAML(BaseModel):

    # NOTE: When this is added to pydantic yaml settings I'd like to make paths
    #       into YamlFileConfigDict.
    @classmethod
    def fromYAML(
        cls,
        *paths: str,
        loaded: List[Dict[str, Any]] | None = None,
        subpath: str | None = None,
        overwrite: Dict[str, Any] | None = None,
        exclude: Dict[str, Any] | None = None,
    ) -> Self:

        data = load(*paths, loaded=loaded, overwrite=overwrite, exclude=exclude)
        if subpath is not None:
            subpath_parsed = parse(subpath)
            data = next(iter(subpath_parsed.find(data)), None)

        return cls.model_validate(data)


class LabelTier(str, enum.Enum):
    base = "base"
    client = "client"
    api = "api"


class LabelComponent(str, enum.Enum):
    traefik = "traefik"
    error_pages = "error-pages"
    registry = "registry"
    captura = "captura"


def create_labels(
    domain: str = DOMAIN,
    *,
    tier: LabelTier,
    component: LabelComponent,
    from_: str,
    **extra: str,
):
    tags = {"tier": tier.value, "component": component.value, "from": from_, **extra}
    return {f"{domain}/{field}": value for field, value in tags.items()}


def setup_logging(config_path: str = PATH_CONFIG_LOG):
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)

    logging.config.dictConfig(config)

    return config, logging.getLogger


DEFAULT_LOGGING_CONFIG, _get_logger = setup_logging()


def get_logger(name: str) -> logging.Logger:
    ll = _get_logger(name)
    return ll


if __name__ == "__main__":
    ensure(PATH_LOGS)
    ensure(PATH_CONFIGS)
