"""Status command and Rich rendering of system snapshots."""

import json
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from autoflow.core.health import classify_usage
from autoflow.core.system import SystemStatus, collect_system_status

_COLORS = {"normal": "green", "warning": "yellow", "critical": "red"}


def _bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("Unreachable unit")


def _uptime(seconds: float | None) -> str:
    if seconds is None:
        return "Unavailable"
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02}h {minutes:02}m {seconds:02}s"


def render_status(snapshot: SystemStatus, console: Console) -> None:
    """Render a snapshot without collecting data or changing its values."""
    info = Table.grid(padding=(0, 2))
    os_info = snapshot.operating_system
    info.add_row("OS", Text(f"{os_info.name} {os_info.version}" if os_info else "Unavailable"))
    info.add_row("Hostname", Text(snapshot.hostname or "Unavailable"))
    info.add_row("Uptime", _uptime(snapshot.uptime_seconds))

    metrics = Table(expand=True)
    for heading in ("Resource", "Usage", "Used / Total", "Level", "Utilization"):
        metrics.add_column(heading)
    for label, percent, resource in (
        ("CPU", snapshot.cpu_percent, None),
        ("RAM", snapshot.memory.percent if snapshot.memory else None, snapshot.memory),
        ("Disk (/)", snapshot.disk.percent if snapshot.disk else None, snapshot.disk),
    ):
        if percent is None:
            metrics.add_row(label, "Unavailable", "—", "—", "—")
            continue
        level = classify_usage(percent)
        color = _COLORS[level]
        amount = (
            f"{_bytes(resource.used_bytes)} / {_bytes(resource.total_bytes)}" if resource else "—"
        )
        metrics.add_row(
            label,
            f"{percent:.1f}%",
            amount,
            Text(f"● {level}", style=color),
            ProgressBar(total=100, completed=percent, complete_style=color, finished_style=color),
        )

    console.print(
        Panel(Group(info, Text(), metrics), title="AutoFlow • System status", border_style="cyan")
    )
    for name, error in snapshot.errors.items():
        console.print(Text(f"Unavailable: {name}: {error}", style="yellow"))


def status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Output a JSON snapshot without terminal formatting.")
    ] = False,
) -> None:
    """Show a read-only snapshot of system health and resource utilization."""
    snapshot = collect_system_status()
    if json_output:
        typer.echo(json.dumps(asdict(snapshot), allow_nan=False))
    else:
        render_status(snapshot, Console())
    if snapshot.errors:
        raise typer.Exit(code=1)
