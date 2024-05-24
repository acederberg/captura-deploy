# --------------------------------------------------------------------------- #
from captura_pipelines import Command


def main():
    cli = Command.create_typer()
    cli()


if __name__ == "__main__":
    main()
