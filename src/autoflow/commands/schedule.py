"""Preview backup schedules and explicitly confirm core-managed installation."""

import shlex
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from autoflow.core.scheduler import (
    SchedulerError,
    install_backup_schedule,
    plan_backup_schedule,
)

schedule = typer.Typer(help="Preview or install schedule files without activating timers.")
add = typer.Typer(help="Plan a new schedule and optionally install its unit files.")
schedule.add_typer(add, name="add")


class Frequency(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


@add.command("backup")
def backup(
    source: Annotated[Path, typer.Argument(help="Existing directory to back up.")],
    output: Annotated[
        Path, typer.Option("--output", help="Destination directory outside the source.")
    ],
    every: Annotated[Frequency, typer.Option("--every", help="Backup frequency.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without making changes.")
    ] = False,
    install: Annotated[
        bool, typer.Option("--install", help="Preview and install unit files after confirmation.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip confirmation; requires --install.")
    ] = False,
) -> None:
    """Choose --dry-run or --install; installation does not activate the timer."""
    if yes and not install:
        raise typer.BadParameter("--yes requires --install", param_hint="--yes")
    if dry_run == install:
        raise typer.BadParameter(
            "Select exactly one of --dry-run or --install", param_hint="--dry-run / --install"
        )
    try:
        plan = plan_backup_schedule(source, output, every.value)
        unit_directory = Path.home() / ".config" / "systemd" / "user" if install else None
    except (SchedulerError, OSError, RuntimeError) as exc:
        Console(stderr=True).print(Text(str(exc), style="red"))
        raise typer.Exit(code=1) from exc

    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    table.add_row("Service", Text(plan.service.name))
    table.add_row("Timer", Text(plan.timer.name))
    table.add_row("Frequency", Text(plan.frequency))
    table.add_row("Source", Text(str(plan.source)))
    table.add_row("Output", Text(str(plan.output)))
    table.add_row("Scope", Text(plan.scope))
    table.add_row("Command", Text(shlex.join(plan.command)))
    if unit_directory is not None:
        table.add_row("Unit directory", Text(str(unit_directory)))
    console = Console()
    mode = "dry-run" if dry_run else "install"
    console.print(Panel(table, title=f"Backup schedule preview ({mode})", border_style="cyan"))
    for unit in (plan.service, plan.timer):
        console.print(Panel(Text(unit.content), title=Text(unit.name), border_style="cyan"))
    if dry_run:
        console.print(
            "Preview only. No files changed; no systemd units created, installed or activated."
        )
        return
    console.print(
        "Installation writes unit files only. The directory must already exist. "
        "The timer will not be activated."
    )
    if not yes:
        try:
            confirmed = typer.confirm("Install these schedule files?", default=False)
        except typer.Abort as exc:
            Console(stderr=True).print("Confirmation unavailable; installation cancelled.")
            raise typer.Exit(code=1) from exc
        if not confirmed:
            console.print("Installation cancelled. No files changed.")
            return
    result = install_backup_schedule(plan, unit_directory)
    if not result.success:
        errors = Console(stderr=True)
        errors.print("Schedule installation failed.", style="red")
        for error in result.errors:
            errors.print(Text(error, style="red"))
        for path in result.paths:
            errors.print(Text(f"File remaining after failed cleanup: {path}", style="yellow"))
        raise typer.Exit(code=1)
    console.print("Schedule files installed:", style="green")
    for path in result.paths:
        console.print(Text(str(path)))
    console.print("The timer is not active. No timers were enabled or started.")
