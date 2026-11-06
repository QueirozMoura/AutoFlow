"""Compose the AutoFlow command-line interface."""

from importlib.metadata import version
from typing import Annotated

import typer
from rich.console import Console

from autoflow.commands.backup import backup
from autoflow.commands.logs import logs
from autoflow.commands.organize import organize
from autoflow.commands.restore import restore
from autoflow.commands.schedule import schedule
from autoflow.commands.status import status
from autoflow.presentation import safe_text

app = typer.Typer(
    name="autoflow",
    help="AutoFlow CLI: modular Linux automation and system administration.",
    add_completion=False,
    rich_markup_mode="rich",
)

app.command()(status)
app.command()(backup)
app.command()(restore)
app.command()(logs)
app.command()(organize)
app.add_typer(schedule, name="schedule")


def _show_version(value: bool) -> None:
    if value:
        Console().print("[bold cyan]AutoFlow CLI[/bold cyan]", safe_text(version("autoflow-cli")))
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed version and exit.",
            callback=_show_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Handle global options and display help when no command is given."""
    if ctx.invoked_subcommand is None:
        Console().print(ctx.get_help(), markup=False, highlight=False)


def main() -> None:
    """Run the installed console entry point."""
    app()
