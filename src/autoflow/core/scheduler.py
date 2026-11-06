"""Backup schedule planning and explicit file installation for systemd users.

Plans are data, not installed schedules: no unit files, processes, or directories
are created. Metadata validation does not inventory source contents or guarantee
future permissions, free space, executable availability, or systemd support.
Calendar aliases use the manager's local timezone. User-manager lifetime and
activation belongs to a future execution layer. Installation only writes unit
files to an explicitly supplied existing directory; it never activates timers.
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


@dataclass(frozen=True)
class ScheduleInstallResult:
    """Paths contains only installed files, or operation-owned rollback residues.

    A failed operation can leave paths when cleanup fails; errors includes those
    failures. Preexisting or concurrently replaced files are never claimed.
    """

    success: bool
    paths: tuple[Path, ...]
    errors: tuple[str, ...]


def _validate_install_plan(plan: BackupSchedulePlan) -> None:
    """Accept only a complete, canonical plan from the existing planner."""
    if (
        not isinstance(plan, BackupSchedulePlan)
        or not isinstance(plan.service, SystemdUnit)
        or not isinstance(plan.timer, SystemdUnit)
        or not isinstance(plan.service.name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,246}\.service", plan.service.name) is None
        or not isinstance(plan.command, tuple)
        or len(plan.command) != 6
        or not all(isinstance(argument, str) for argument in plan.command)
    ):
        raise SchedulerError("Invalid backup schedule plan")
    expected = plan_backup_schedule(
        plan.source,
        plan.output,
        plan.frequency,
        unit_name=plan.service.name.removesuffix(".service"),
        executable=Path(plan.command[0]),
    )
    if plan != expected:
        raise SchedulerError("Backup schedule plan differs from the validated planner output")


def install_backup_schedule(
    plan: BackupSchedulePlan, unit_directory: Path
) -> ScheduleInstallResult:
    """Install two unit files with mode 0600, without overwriting or activation.

    The explicit destination must already exist, be owned by the current user,
    allow owner write/search, and not be group/world-writable. All directory
    components are opened without following symlinks. Plans are revalidated
    against current metadata and canonical planner output before writing.

    On a handled failure, roll back only files whose inode still matches the
    descriptor created here. Cleanup failures and remaining owned paths are
    reported. Two-file publication is not atomic or crash recoverable: a process
    kill or power loss can leave incomplete units. Concurrent same-user writers
    and ancestor renames require external isolation; identity checks cannot
    eliminate every check-to-unlink race. No reload, enable, or start is run.
    """
    errors: list[str] = []
    remaining: list[Path] = []
    try:
        _validate_install_plan(plan)
        directory = _path(unit_directory, "unit directory")
        if directory == Path("/") or any(
            directory == protected or protected in directory.parents for protected in _PROTECTED
        ):
            raise SchedulerError(f"Protected unit directory: {directory}")
        with _directory(directory) as directory_fd:
            _validate_output(directory_fd)
            if os.fstat(directory_fd).st_mode & 0o300 != 0o300:
                raise SchedulerError("Unit directory requires owner write and search permissions")
            created: list[tuple[str, int]] = []
            try:
                # Preflight both names, including dangling links and special files.
                for unit in (plan.service, plan.timer):
                    try:
                        os.stat(unit.name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise SchedulerError(f"Unit already exists: {unit.name}")
                for unit in (plan.service, plan.timer):
                    descriptor = os.open(
                        unit.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    created.append((unit.name, descriptor))
                    os.fchmod(descriptor, 0o600)
                    content = memoryview(unit.content.encode("utf-8"))
                    while content:
                        written = os.write(descriptor, content)
                        if written <= 0:
                            raise OSError("Unit write made no progress")
                        content = content[written:]
                    os.fsync(descriptor)
                with _directory(directory) as current_fd:
                    current, original = os.fstat(current_fd), os.fstat(directory_fd)
                    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                        raise SchedulerError("Unit directory changed during installation")
                _validate_output(directory_fd)
                for name, descriptor in created:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    original = os.fstat(descriptor)
                    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                        raise SchedulerError(f"Unit changed during installation: {name}")
                os.fsync(directory_fd)
            except BaseException:
                for name, descriptor in reversed(created):
                    try:
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        original = os.fstat(descriptor)
                        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                            errors.append(f"Cleanup preserved replaced unit: {directory / name}")
                            continue
                        try:
                            os.unlink(name, dir_fd=directory_fd)
                        except OSError:
                            remaining.append(directory / name)
                            raise
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        errors.append(f"Cannot clean up {directory / name}: {exc}")
                raise
            finally:
                for _, descriptor in created:
                    os.close(descriptor)
        return ScheduleInstallResult(
            success=True,
            paths=(directory / plan.service.name, directory / plan.timer.name),
            errors=(),
        )
    except (SchedulerError, BackupError, OSError, ValueError, TypeError, RuntimeError) as exc:
        return ScheduleInstallResult(False, tuple(remaining), (str(exc), *errors))
