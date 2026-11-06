"""Display organization plans; execution is intentionally unavailable."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from autoflow.core.organizer import OrganizationPlan, OrganizerError, plan_organization


def _literal(value: str) -> Text:
    """Keep markup literal and make unsafe display characters visible."""
    return Text("".join(char if char.isprintable() else repr(char)[1:-1] for char in value))


def render_plan(plan: OrganizationPlan, console: Console) -> None:
    """Present relative proposed paths and extension counts without filesystem I/O."""
    console.print(_literal(f"Source: {plan.source}"))
    table = Table(title="Organization preview (dry-run)")
    table.add_column("Original file", overflow="fold")
    table.add_column("Proposed destination", overflow="fold")
    for entry in plan.entries:
        table.add_row(
            _literal(str(entry.source.relative_to(plan.source))),
            _literal(str(entry.destination.relative_to(plan.source))),
        )
    console.print(table)
    summary = Table(title="Summary by extension")
    summary.add_column("Extension")
    summary.add_column("Files", justify="right")
    for extension, entries in sorted(plan.groups.items()):
        summary.add_row(_literal(extension), str(len(entries)))
    console.print(summary)
    if not plan.entries:
        console.print("No regular files to organize.")
    console.print(f"Files planned: {len(plan.entries)}")
    console.print("Dry-run only; no files changed.")


def organize(
    source: Annotated[Path, typer.Argument(help="Directory to organize by extension.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Required: preview the plan without changes."),
    ],
) -> None:
    """Preview organization of immediate regular files; --dry-run is required."""
    if not dry_run:
        raise typer.BadParameter("--dry-run is required at this stage", param_hint="--dry-run")
    try:
        plan = plan_organization(source)
    except OrganizerError as exc:
        Console(stderr=True).print(_literal(str(exc)), style="red")
        raise typer.Exit(1) from exc
    render_plan(plan, Console())
    if not plan.is_valid:
        console = Console(stderr=True)
        for issue in plan.issues:
            console.print(_literal(f"Warning: {issue.reason}: {issue.path}"), style="yellow")
        console.print(f"Invalid plan: {len(plan.issues)} issue(s).", style="red")
        raise typer.Exit(1)
