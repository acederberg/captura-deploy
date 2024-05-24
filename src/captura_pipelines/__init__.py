import typer

# --------------------------------------------------------------------------- #
from captura_pipelines import flags
from captura_pipelines.builder import Builder, BuilderCommand


class Command:

    @classmethod
    def create_typer(cls):
        cli = typer.Typer()
        cli.callback()(flags.ContextData.typer_callback)
        cli.add_typer(BuilderCommand.create_typer(), name="builder")
        return cli
