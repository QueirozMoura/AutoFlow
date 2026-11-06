"""Backup CLI presentation; filesystem operations belong to the core."""

import json
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from autoflow.core.backup import BackupError, BackupResult, create_backup


def _render_result(result: BackupResult, console: Console) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_row("Source", Text(result.source))
    table.add_row("Output", Text(result.output))
    table.add_row("Files", str(result.file_count))
    table.add_row("Original size", f"{result.original_bytes:,} bytes")
    if result.archive:
        table.add_row("Archive", Text(result.archive))
        table.add_row("Compressed size", f"{result.compressed_bytes:,} bytes")
    table.add_row("Duration", f"{result.duration_seconds:.3f} s")
    title = "Backup preview — no files created" if result.dry_run else "Backup completed"
    console.print(Panel(table, title=title, border_style="green"))
    if result.dry_run:
        console.print("Entries to include:", style="bold")
        for name in result.directories:
            console.print(Text(f"  {name}/"))
        for name in result.files:
            console.print(Text(f"  {name}"))
    if result.skipped:
        console.print("Excluded by safety policy:", style="yellow")
        for entry in result.skipped:
            console.print(Text(f"  {entry.path}: {entry.reason}", style="yellow"))


def backup(
    source: Annotated[Path, typer.Argument(help="Regular file or directory to back up.")],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Destination directory outside the source.")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without creating files or directories.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit only a structured JSON result.")
    ] = False,
) -> None:
    """Create a safe .tar.gz backup, without following links or nested mounts."""
    progress = Progress(
        TextColumn("Creating backup"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
    )
    task = progress.add_task("backup", total=None)

    def update(completed: int, total: int, name: str) -> None:
        progress.update(task, completed=completed, total=total)

    try:
        with progress if not (json_output or dry_run) else nullcontext():
            result = create_backup(
                source,
                output,
                dry_run=dry_run,
                progress=update if not (json_output or dry_run) else None,
            )
    except BackupError as exc:
        if json_output:
            typer.echo(json.dumps({"success": False, "dry_run": dry_run, "error": str(exc)}))
        else:
            Console(stderr=True).print(Text(str(exc), style="red"))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(asdict(result), allow_nan=False))
    else:
        _render_result(result, Console())
