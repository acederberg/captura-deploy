# =========================================================================== #
from typing import Annotated, List, Optional, Self

import typer
import yaml
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
from captura_pipelines.config import PipelineConfig

FlagFile = Annotated[str, typer.Option("-f", "--file")]
FlagConfig = Annotated[Optional[str], typer.Option("--config")]


class ContextData(BaseModel):
    config: PipelineConfig

    @classmethod
    def typer_callback(
        cls,
        context: typer.Context,
        config_path: FlagConfig = None,
    ) -> None:

        if config_path is None:
            config = PipelineConfig()  # type: ignore
        else:
            with open(config_path, "r") as file:
                raw = yaml.safe_load(file)

            config = PipelineConfig.model_validate(raw)

        self = cls(config=config)
        context.obj = self
