"""Log analysis CLI: argument selection, Rich summaries, and plain JSON."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from autoflow.core.logs import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_GROUPS,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_LINES,
    LogsError,
    LogsResult,
    analyze_file,
    analyze_stream,
)
from autoflow.integrations.journal import JournalError, open_journal, parse_journal
from autoflow.presentation import safe_text


def render_logs(result: LogsResult, console: Console) -> None:
    """Render sanitized templates and counts without reading any logs."""
    summary = Table.grid(padding=(0, 2))
    summary.add_column(no_wrap=True)
    summary.add_column(overflow="fold")
    summary.add_row("Source", safe_text(result.source))
    summary.add_row("Analyzed lines", str(result.total_lines))
    summary.add_row("Read", f"{result.bytes_read:,} bytes")
    summary.add_row("Selected lines", str(result.selected_lines))
    summary.add_row("Duration", f"{result.duration_seconds:.3f} s")
    colors = {
        "ERROR": "red",
        "WARNING": "yellow",
        "INFO": "green",
        "DEBUG": "cyan",
        "UNKNOWN": "white",
    }
    for level, count in result.severity_counts.items():
        summary.add_row(safe_text(level, style=colors[level]), str(count))
    console.print(Panel(summary, title="AutoFlow • Log analysis", border_style="cyan"))
    table = Table(title="Most frequent messages (sanitized templates)")
    table.add_column("Level", no_wrap=True)
    table.add_column("Count", justify="right")
    table.add_column("Message", overflow="fold")
    for group in result.groups:
        table.add_row(
            safe_text(group.severity, style=colors[group.severity]),
            str(group.count),
            safe_text(group.message),
        )
    console.print(table)
    if not result.groups:
        console.print("No matching messages.")
    if result.limited:
        console.print(
            safe_text("Partial analysis: " + ", ".join(result.limit_reasons), style="yellow")
        )
    if result.ungrouped_lines:
        console.print(f"Ungrouped selected lines: {result.ungrouped_lines}")
    for warning in result.warnings:
        console.print(safe_text(warning, style="yellow"))


def logs(
    file: Annotated[
        Path | None, typer.Option("--file", help="Read a regular UTF-8 log file.")
    ] = None,
    journal: Annotated[
        bool, typer.Option("--journal", help="Read the accessible systemd journal.")
    ] = False,
    since: Annotated[
        str | None, typer.Option("--since", help="Journal interval, e.g. 1h or 30m.")
    ] = None,
    errors: Annotated[
        bool, typer.Option("--errors", help="Group only ERROR and equivalent levels.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit only structured JSON.")] = False,
    top: Annotated[int, typer.Option("--top", min=1, help="Maximum groups to display.")] = 10,
    max_lines: Annotated[int, typer.Option("--max-lines", min=1)] = DEFAULT_MAX_LINES,
    max_bytes: Annotated[int, typer.Option("--max-bytes", min=1)] = DEFAULT_MAX_BYTES,
    max_line_bytes: Annotated[
        int, typer.Option("--max-line-bytes", min=1)
    ] = DEFAULT_MAX_LINE_BYTES,
    max_groups: Annotated[int, typer.Option("--max-groups", min=1)] = DEFAULT_MAX_GROUPS,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0.1, max=3600, help="Journal execution deadline in seconds."),
    ] = 10.0,
) -> None:
    """Analyze local logs with bounded reads, grouping, and privacy reduction."""
    try:
        if (file is not None) == journal:
            raise LogsError("Select exactly one of --file or --journal")
        if file is not None and since is not None:
            raise LogsError("--since requires --journal")
        options = dict(
            errors_only=errors,
            top=top,
            max_lines=max_lines,
            max_bytes=max_bytes,
            max_line_bytes=max_line_bytes,
            max_groups=max_groups,
        )
        if file is not None:
            result = analyze_file(file, **options)
        else:
            interval = since or "1h"
            with open_journal(interval, timeout) as stream:
                result = analyze_stream(
                    stream,
                    source=f"journal since -{interval}",
                    source_kind="journal",
                    parser=parse_journal,
                    **options,
                )
                warnings = []
                if stream.diagnostic_bytes:
                    warnings.append("journalctl reported diagnostics; visibility may be restricted")
                if not stream.finished:
                    warnings.append("Journal stopped at read limit; exit status unavailable")
                result = replace(result, warnings=warnings)
    except (LogsError, JournalError, OSError) as exc:
        if json_output:
            typer.echo(json.dumps({"success": False, "error": str(exc)}))
        else:
            Console(stderr=True).print(safe_text(str(exc), style="red"))
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(asdict(result), allow_nan=False))
    else:
        render_logs(result, Console())
