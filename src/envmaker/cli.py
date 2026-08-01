"""EnvMaker command-line interface (extended by later tasks)."""

import typer

app = typer.Typer(name="envmaker", no_args_is_help=True, add_completion=False)


@app.callback()
def _root() -> None:
    """EnvMaker: text-to-environment agent harness."""


@app.command()
def version() -> None:
    """Print the EnvMaker version."""
    from envmaker import __version__

    typer.echo(__version__)


def main() -> None:
    app()
