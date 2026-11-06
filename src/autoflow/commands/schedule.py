"""Preview backup schedules using the read-only core planner."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from autoflow.core.scheduler import SchedulerError, plan_backup_schedule

schedule = typer.Typer(help="Preview schedules without installing systemd units.")
add = typer.Typer(help="Plan a new schedule (dry-run only).")
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
        bool, typer.Option("--dry-run", help="Required: preview without making changes.")
    ] = False,
) -> None:
    """Preview a backup service and timer; --dry-run is required."""
    if not dry_run:
        raise typer.BadParameter("--dry-run is required at this stage", param_hint="--dry-run")
    try:
        plan = plan_backup_schedule(source, output, every.value)
    except SchedulerError as exc:
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
    console = Console()
    console.print(Panel(table, title="Backup schedule preview (dry-run)", border_style="cyan"))
    console.print(
        "Preview only. No files changed; no systemd units created, installed or activated."
    )
