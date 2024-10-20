# =========================================================================== #
from typing import Annotated, List, Optional, Self

import typer
import yaml
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
from captura_pipelines.config import PipelineConfig

FlagFile = Annotated[Optional[str], typer.Option("-f", "--file")]
FlagConfig = Annotated[Optional[str], typer.Option("--config")]
FlagGitRepository = Annotated[
    str,
    typer.Argument(help="Link to the github repository."),
]
FlagBranch = Annotated[str, typer.Option()]
FlagGitTag = Annotated[Optional[str], typer.Option(help="Git tag to build.")]
FlagGitCommit = Annotated[Optional[str], typer.Option(help="Git commit to build.")]


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
