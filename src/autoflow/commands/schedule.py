"""Preview backup schedules and explicitly confirm core-managed installation."""

import configparser
import os
import re
import shlex
import stat
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from autoflow.core.backup import BackupError, _directory, _validate_output
from autoflow.core.scheduler import (
    BackupSchedulePlan,
    SchedulerError,
    activate_backup_schedule,
    install_backup_schedule,
    plan_backup_schedule,
    user_unit_directory,
)

schedule = typer.Typer(help="Preview, install or explicitly activate backup schedules.")
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
        unit_directory = user_unit_directory() if install else None
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


def _read_unit(directory_fd: int, name: str) -> str:
    """Read a bounded, owned regular unit without following links or blocking on FIFOs."""
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd
    )
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
        ):
            raise SchedulerError(f"Unsafe unit file: {name}")
        content = stream.read(65537)
        if len(content) > 65536:
            raise SchedulerError(f"Unit file is too large: {name}")
    return content.decode("utf-8")


def _load_installed_plan(timer: str, directory: Path) -> BackupSchedulePlan:
    """Recognize exact CLI-generated units, including their command-derived hash.

    This is content validation, not proof of authorship. The core checks manager
    precedence and drop-ins after reload; concurrent writers require isolation.
    """
    service = timer.removesuffix(".timer") + ".service"
    with _directory(directory) as directory_fd:
        _validate_output(directory_fd)
        for name in (timer + ".d", service + ".d"):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise SchedulerError(f"Unit overrides are not supported: {name}")
        timer_content = _read_unit(directory_fd, timer)
        service_content = _read_unit(directory_fd, service)
    timer_config = configparser.ConfigParser(interpolation=None)
    service_config = configparser.ConfigParser(interpolation=None)
    timer_config.read_string(timer_content)
    service_config.read_string(service_content)
    command = tuple(
        argument.replace("$$", "$").replace("%%", "%")
        for argument in shlex.split(service_config["Service"]["ExecStart"])
    )
    if len(command) != 6:
        raise SchedulerError("Unit does not contain an AutoFlow backup command")
    plan = plan_backup_schedule(
        Path(command[2]),
        Path(command[4]),
        timer_config["Timer"]["OnCalendar"],
        executable=Path(command[0]),
    )
    if (
        plan.timer.name != timer
        or plan.service.name != service
        or plan.command != command
        or plan.timer.content != timer_content
        or plan.service.content != service_content
    ):
        raise SchedulerError("Installed units do not match an AutoFlow-generated backup schedule")
    return plan


@schedule.command("activate")
def activate(
    timer: Annotated[str, typer.Argument(help="AutoFlow-generated .timer name, without a path.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip activation confirmation.")] = False,
) -> None:
    """Validate installed AutoFlow files, then confirm enabling and starting the timer."""
    if re.fullmatch(r"autoflow-backup-[0-9a-f]{16}\.timer", timer) is None:
        raise typer.BadParameter(
            "Expected autoflow-backup-<16 lowercase hex digits>.timer", param_hint="timer"
        )
    try:
        directory = user_unit_directory()
        plan = _load_installed_plan(timer, directory)
        console = Console()
        table = Table.grid(padding=(0, 2))
        for label, value in (
            ("Timer", timer),
            ("Service", plan.service.name),
            ("Unit directory", str(directory)),
            ("Frequency", plan.frequency),
            ("Source", str(plan.source)),
            ("Output", str(plan.output)),
        ):
            table.add_row(label, Text(value))
        console.print(Panel(table, title="Activate backup schedule", border_style="cyan"))
        console.print("This will enable and start the timer in the systemd user manager.")
        if not yes:
            try:
                confirmed = typer.confirm("Enable and start this timer?", default=False)
            except typer.Abort as exc:
                Console(stderr=True).print("Confirmation unavailable; activation cancelled.")
                raise typer.Exit(code=1) from exc
            if not confirmed:
                console.print("Activation cancelled. No activation commands were run.")
                return
        if _load_installed_plan(timer, directory) != plan:
            raise SchedulerError("Installed schedule changed; review it before activating again")
    except (
        SchedulerError,
        BackupError,
        OSError,
        ValueError,
        RuntimeError,
        UnicodeError,
        configparser.Error,
        KeyError,
    ) as exc:
        Console(stderr=True).print(Text(f"Cannot activate schedule: {exc}", style="red"))
        raise typer.Exit(code=1) from exc
    result = activate_backup_schedule(plan, unit_directory=directory)
    if not result.success:
        errors = Console(stderr=True)
        errors.print(Text(f"Timer activation failed: {timer}", style="red"))
        for error in result.errors:
            errors.print(Text(error, style="red"))
        if result.partial_activation_possible:
            errors.print(
                "Warning: the timer may be partially enabled and/or active.", style="yellow"
            )
        raise typer.Exit(code=1)
    console.print(Text(f"Timer enabled and started: {result.timer}", style="green"))
