"""Restore command presentation and structured output."""

import json
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from autoflow.core.restore import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    RestoreError,
    restore_backup,
)
from autoflow.presentation import safe_text


def restore(
    archive: Annotated[
        Path, typer.Argument(help="Untrusted .tar.gz archive to validate and restore.")
    ],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New destination; its parent must already exist.")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate all data without filesystem writes.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit only structured JSON.")] = False,
    max_files: Annotated[
        int, typer.Option("--max-files", min=1, help="Maximum entries, including directories.")
    ] = DEFAULT_MAX_FILES,
    max_bytes: Annotated[
        int, typer.Option("--max-bytes", min=0, help="Maximum total uncompressed file bytes.")
    ] = DEFAULT_MAX_BYTES,
) -> None:
    """Validate and restore a backup atomically into a new directory."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=Console(stderr=True),
    )
    task = progress.add_task("Validating archive", total=None)

    def update(completed: int, total: int, name: str) -> None:
        progress.update(task, description=f"Preparing restore: {completed} entries")

    try:
        with progress if not (dry_run or json_output) else nullcontext():
            result = restore_backup(
                archive,
                output,
                dry_run=dry_run,
                max_files=max_files,
                max_bytes=max_bytes,
                progress=update if not (dry_run or json_output) else None,
            )
    except RestoreError as exc:
        if json_output:
            typer.echo(json.dumps({"success": False, "dry_run": dry_run, "error": str(exc)}))
        else:
            Console(stderr=True).print(safe_text(str(exc), style="red"))
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(asdict(result), allow_nan=False))
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    table.add_row("Archive", safe_text(result.archive))
    table.add_row("Output", safe_text(result.output))
    table.add_row("Files", str(result.file_count))
    table.add_row("Uncompressed size", f"{result.total_bytes:,} bytes")
    table.add_row("Duration", f"{result.duration_seconds:.3f} s")
    title = "Restore preview — no files created" if dry_run else "Restore completed"
    console = Console()
    console.print(Panel(table, title=title, border_style="green"))
    if dry_run:
        for name in result.directories:
            console.print(safe_text(f"  {name}/"))
        for name in result.files:
            console.print(safe_text(f"  {name}"))
