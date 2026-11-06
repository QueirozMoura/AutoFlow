"""Read-only planning of backup services and timers for the systemd user manager.

Plans are data, not installed schedules: no unit files, processes, or directories
are created. Metadata validation does not inventory source contents or guarantee
future permissions, free space, executable availability, or systemd support.
Calendar aliases use the manager's local timezone. User-manager lifetime and
installation/activation belong to a future execution layer.
"""

import hashlib
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from autoflow.core.backup import (
    _PROTECTED,
    BackupError,
    _absolute,
    _directory,
    _safe_archive_path,
    _validate_output,
)


class SchedulerError(Exception):
    """A safe backup schedule could not be planned."""


@dataclass(frozen=True)
class SystemdUnit:
    name: str
    content: str


@dataclass(frozen=True)
class BackupSchedulePlan:
    source: Path
    output: Path
    frequency: str
    command: tuple[str, ...]
    service: SystemdUnit
    timer: SystemdUnit
    scope: Literal["user"] = "user"


def _path(value: Path, label: str) -> Path:
    if not str(value):
        raise SchedulerError(f"Invalid {label} path: empty")
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SchedulerError(f"Invalid {label} path: parent traversal is not allowed")
    path = _absolute(path)
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in str(path)):
        raise SchedulerError(f"Invalid {label} path: unsafe characters")
    return path


def _quote_argument(value: str) -> str:
    """Quote one systemd ExecStart argument, including literal $ and % expansion."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("%", "%%").replace("$", "$$")
    return f'"{value}"'


def plan_backup_schedule(
    source: Path,
    output: Path,
    frequency: str,
    *,
    unit_name: str | None = None,
    executable: Path | None = None,
) -> BackupSchedulePlan:
    """Plan a user backup without writes, source-content reads, or subprocesses.

    Source must be an existing directory; output may be absent but must be
    outside it. Symlink components, protected paths, traversal, and unsafe text
    are rejected. Existing output ownership/write policy matches backup.
    The executable defaults to the autoflow entry point beside sys.executable;
    an explicit executable must be absolute, regular, and executable. Dollar
    signs in executable paths are rejected: systemd does not expand the program
    path like its arguments, so argument-style dollar escaping is unsuitable.
    unit_name is a bare ASCII stem (no suffix/templates), at most 247 characters.
    Omitted names are stable hashes of the command and frequency. No existing
    installed unit is inspected, replaced, or reserved by planning.
    """
    if frequency not in ("daily", "weekly", "monthly"):
        raise SchedulerError("Frequency must be daily, weekly, or monthly")
    if unit_name is not None and (
        not isinstance(unit_name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,246}", unit_name) is None
    ):
        raise SchedulerError(
            "Invalid unit name: use a bare ASCII letter/digit/hyphen/underscore stem"
        )
    try:
        source, output = _path(source, "source"), _path(output, "output")
        for label, path in (("source", source), ("output", output)):
            if path == Path("/") or any(path == p or p in path.parents for p in _PROTECTED):
                raise SchedulerError(f"Protected {label} path: {path}")
        if source == output or source in output.parents:
            raise SchedulerError("Output must be outside the source tree")
        if not _safe_archive_path(source.name):
            raise SchedulerError("Source directory name is incompatible with restore")
        with _directory(source):
            pass
        with _directory(output, missing_ok=True) as descriptor:
            if descriptor is not None:
                _validate_output(descriptor)

        executable = (
            executable if executable is not None else Path(sys.executable).parent / "autoflow"
        )
        if not Path(executable).is_absolute():
            raise SchedulerError("Executable path must be absolute")
        executable = _path(executable, "executable")
        if "$" in str(executable):
            raise SchedulerError("Executable path must not contain dollar signs")
        with _directory(executable.parent) as parent:
            metadata = os.stat(executable.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
                raise SchedulerError("Executable must be a regular executable file, not a symlink")

        command = (str(executable), "backup", str(source), "--output", str(output), "--json")
        if unit_name is None:
            digest = hashlib.sha256("\0".join((*command, frequency)).encode()).hexdigest()[:16]
            unit_name = f"autoflow-backup-{digest}"
        service = SystemdUnit(
            name=f"{unit_name}.service",
            content=(
                "[Unit]\nDescription=AutoFlow scheduled backup\n\n"
                "[Service]\nType=oneshot\nUMask=0077\n"
                f"ExecStart={' '.join(_quote_argument(arg) for arg in command)}\n"
            ),
        )
        timer = SystemdUnit(
            name=f"{unit_name}.timer",
            content=(
                "[Unit]\nDescription=AutoFlow backup timer\n\n"
                f"[Timer]\nOnCalendar={frequency}\nUnit={service.name}\n\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
        )
        return BackupSchedulePlan(source, output, frequency, command, service, timer)
    except (OSError, ValueError, TypeError, RuntimeError, BackupError) as exc:
        raise SchedulerError(f"Cannot plan backup schedule: {exc}") from exc
