"""Preview organization plans and explicitly confirm core-managed execution."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from autoflow.core.organizer import (
    OrganizationPlan,
    OrganizationResult,
    OrganizerError,
    execute_organization,
    plan_organization,
)
from autoflow.presentation import safe_text as _literal


def render_plan(plan: OrganizationPlan, console: Console, *, dry_run: bool = True) -> None:
    """Present relative proposed paths and extension counts without filesystem I/O."""
    console.print(_literal(f"Source: {plan.source}"))
    table = Table(title=f"Organization preview ({'dry-run' if dry_run else 'execute'})")
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
    if dry_run:
        console.print("Dry-run only; no files changed.")


def render_result(result: OrganizationResult, console: Console) -> None:
    """Report completed work and failures without attempting recovery operations."""
    failed = not result.success or bool(result.errors)
    title = (
        "Organization partially completed"
        if failed and result.moved
        else "Organization failed"
        if failed
        else "Organization completed"
    )
    summary = Table(title=title)
    summary.add_column("Moved", justify="right")
    summary.add_column("Ignored / not moved", justify="right")
    summary.add_column("Errors", justify="right")
    summary.add_row(str(len(result.moved)), str(len(result.skipped)), str(len(result.errors)))
    console.print(summary)
    if result.moved:
        moved = Table(title="Moved files")
        moved.add_column("Original file", overflow="fold")
        moved.add_column("Destination", overflow="fold")
        for entry in result.moved:
            moved.add_row(_literal(str(entry.source)), _literal(str(entry.destination)))
        console.print(moved)
    if result.skipped:
        skipped = Table(title="Ignored / not moved")
        skipped.add_column("Path", overflow="fold")
        skipped.add_column("Reason", overflow="fold")
        for issue in result.skipped:
            skipped.add_row(_literal(str(issue.path)), _literal(issue.reason))
        console.print(skipped)
    errors = Console(stderr=True)
    for issue in result.errors:
        errors.print(_literal(f"Error: {issue.reason}: {issue.path}"), style="red")
    if failed:
        errors.print(
            "No automatic rollback: moved files and created directories are retained.",
            style="yellow",
        )


def organize(
    source: Annotated[Path, typer.Argument(help="Directory to organize by extension.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the plan without changes."),
    ] = False,
    execute: Annotated[
        bool, typer.Option("--execute", help="Preview and execute after confirmation.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip confirmation; requires --execute.")
    ] = False,
) -> None:
    """Organize immediate regular files; exactly one of --dry-run or --execute is required."""
    if yes and not execute:
        raise typer.BadParameter("--yes requires --execute", param_hint="--yes")
    if dry_run == execute:
        raise typer.BadParameter(
            "Select exactly one of --dry-run or --execute", param_hint="--dry-run / --execute"
        )
    try:
        plan = plan_organization(source)
    except OrganizerError as exc:
        Console(stderr=True).print(_literal(str(exc)), style="red")
        raise typer.Exit(1) from exc
    console = Console()
    render_plan(plan, console, dry_run=dry_run)
    if not plan.is_valid:
        console = Console(stderr=True)
        for issue in plan.issues:
            console.print(_literal(f"Warning: {issue.reason}: {issue.path}"), style="yellow")
        console.print(f"Invalid plan: {len(plan.issues)} issue(s).", style="red")
        raise typer.Exit(1)
    if dry_run:
        return
    console.print(
        "Partial operations have no automatic rollback; completed moves and created directories "
        "will be retained after failure.",
        style="yellow",
    )
    if not yes:
        try:
            confirmed = typer.confirm("Execute this organization plan?", default=False)
        except typer.Abort as exc:
            console.print("Confirmation unavailable; organization cancelled. No files changed.")
            raise typer.Exit(1) from exc
        if not confirmed:
            console.print("Organization cancelled; no files changed.")
            return
    try:
        # Do not authorize newly appeared files merely because the core re-plans.
        # The core still performs its own safety validation during execution.
        if plan_organization(plan.source) != plan:
            console.print("Plan changed; run organize again to review it.", style="red")
            raise typer.Exit(1)
        result = execute_organization(plan.source)
    except OrganizerError as exc:
        Console(stderr=True).print(_literal(str(exc)), style="red")
        raise typer.Exit(1) from exc
    render_result(result, console)
    if not result.success or result.partial or result.errors:
        raise typer.Exit(1)
