"""Backup schedule planning, file installation and explicit user-timer activation.

Plans are data, not installed schedules: no unit files, processes, or directories
are created. Metadata validation does not inventory source contents or guarantee
future permissions, free space, executable availability, or systemd support.
Calendar aliases use the manager's local timezone. Installation only writes unit
files to an explicitly supplied existing directory; it never activates timers.
Only an explicit activation call invokes systemctl, against the user manager.
"""

import hashlib
import os
import re
import stat
import subprocess
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
    _signature,
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


def _validate_backup_schedule_plan(plan: BackupSchedulePlan) -> None:
    """Share canonical-plan validation between installation and activation.

    Reconstruct through the planner rather than duplicating command/unit rules.
    Explicit unit stems supported by the planner remain valid, but names alone
    never establish that a plan describes an AutoFlow backup. This validates
    consistency and current metadata, not cryptographic proof of authorship.
    """
    if (
        not isinstance(plan, BackupSchedulePlan)
        or not isinstance(plan.source, Path)
        or not isinstance(plan.output, Path)
        or plan.scope != "user"
        or not isinstance(plan.frequency, str)
        or not isinstance(plan.service, SystemdUnit)
        or not isinstance(plan.timer, SystemdUnit)
        or not isinstance(plan.service.name, str)
        or not isinstance(plan.timer.name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,246}\.service", plan.service.name) is None
        or plan.timer.name != plan.service.name.removesuffix(".service") + ".timer"
        or not isinstance(plan.service.content, str)
        or not isinstance(plan.timer.content, str)
        or not isinstance(plan.command, tuple)
        or len(plan.command) != 6
        or not all(isinstance(argument, str) and argument for argument in plan.command)
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
        _validate_backup_schedule_plan(plan)
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


@dataclass(frozen=True)
class ScheduleActivationResult:
    """Report command success, not independently verified systemd state.

    partial_activation_possible means a failed enable/start command may have
    changed state. False does not imply that a previously existing timer is off.
    """

    success: bool
    timer: str | None
    errors: tuple[str, ...]
    partial_activation_possible: bool = False


def user_unit_directory() -> Path:
    """Locate user configuration without creating it; reject unsafe XDG paths."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    if not base.is_absolute():
        raise SchedulerError("XDG_CONFIG_HOME must be an absolute path")
    return _path(base / "systemd" / "user", "unit directory")


def _installed_unit_state(plan: BackupSchedulePlan, directory: Path) -> tuple[tuple[int, ...], ...]:
    """Bind activation to owned, unchanged regular files matching the validated plan."""
    with _directory(directory) as directory_fd:
        _validate_output(directory_fd)
        identities = [_signature(os.fstat(directory_fd))]
        for unit in (plan.service, plan.timer):
            descriptor = os.open(
                unit.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                    or metadata.st_nlink != 1
                ):
                    raise SchedulerError(f"Unsafe installed unit: {unit.name}")
                expected = unit.content.encode("utf-8")
                if stream.read(len(expected) + 1) != expected:
                    raise SchedulerError(f"Installed unit differs from validated plan: {unit.name}")
                signature = _signature(metadata)
                current = os.stat(unit.name, dir_fd=directory_fd, follow_symlinks=False)
                if signature != _signature(current) or signature != _signature(
                    os.fstat(descriptor)
                ):
                    raise SchedulerError(f"Installed unit changed while reading: {unit.name}")
                identities.append(signature)
        return tuple(identities)


def _verify_loaded_unit(output: str, unit: SystemdUnit, directory: Path) -> None:
    """Fail closed on missing/ambiguous properties, aliases, overrides or stale units."""
    properties: dict[str, str] = {}
    if not isinstance(output, str):
        raise SchedulerError(f"Inconclusive systemd query: {unit.name}")
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties:
            raise SchedulerError(f"Ambiguous systemd query: {unit.name}")
        properties[key] = value
    expected = {
        "Id": unit.name,
        "LoadState": "loaded",
        "FragmentPath": str(directory / unit.name),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
    }
    if properties != expected:
        raise SchedulerError(
            f"Systemd configuration differs or is inconclusive for {unit.name}: "
            "expected the validated fragment, no drop-ins, and a current loaded unit"
        )


def activate_backup_schedule(
    plan: BackupSchedulePlan, *, unit_directory: Path | None = None
) -> ScheduleActivationResult:
    """Reload the user manager and enable/start exactly the plan's timer.

    Validate the complete plan against the planner before any subprocess.
    Each command has a 30-second timeout, no shell and no stdin.
    Installation must already have been performed separately. After reload,
    query both units' effective fragments, drop-ins, identity and reload status.
    Require the exact validated files and no overrides, regardless of systemd
    search precedence. The default directory honors XDG_CONFIG_HOME. Query
    failures or inconclusive responses prevent enable/start. File metadata and
    contents are checked before reload and again before enable/start.
    These checks do not provide atomic isolation against concurrent file or
    manager changes after verification; stop other configuration writers.
    Failure after enable/start begins can leave a timer enabled and/or active,
    including after a timeout kills the client. No disable, stop, removal, retry
    or rollback is attempted, and preexisting timer state is never inferred.
    """
    timer = (
        plan.timer.name
        if isinstance(plan, BackupSchedulePlan) and isinstance(plan.timer, SystemdUnit)
        else None
    )
    try:
        _validate_backup_schedule_plan(plan)
    except SchedulerError as exc:
        return ScheduleActivationResult(
            False,
            timer if isinstance(timer, str) else None,
            (f"Invalid backup schedule plan: {exc}",),
        )

    try:
        directory = (
            user_unit_directory()
            if unit_directory is None
            else _path(unit_directory, "unit directory")
        )
        original = _installed_unit_state(plan, directory)
    except (SchedulerError, BackupError, OSError, ValueError, RuntimeError) as exc:
        return ScheduleActivationResult(False, timer, (f"Cannot validate installed units: {exc}",))

    queries = [
        (
            f"show {unit.name}",
            [
                "systemctl",
                "--user",
                "show",
                "--no-pager",
                "--all",
                "--property=Id",
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                unit.name,
            ],
            unit,
        )
        for unit in (plan.service, plan.timer)
    ]
    for stage, arguments, unit in (
        ("daemon-reload", ["systemctl", "--user", "daemon-reload"], None),
        *queries,
        ("enable --now", ["systemctl", "--user", "enable", "--now", timer], None),
    ):
        partial = False
        try:
            if stage == "enable --now":
                if _installed_unit_state(plan, directory) != original:
                    raise SchedulerError("Installed units changed after reload; activation refused")
                partial = True
            # Set partial only when the enable/start process can actually begin.
            result = subprocess.run(
                arguments,
                shell=False,
                check=True,
                capture_output=True,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            if unit is not None:
                if result.stderr:
                    raise SchedulerError(f"Systemd query reported diagnostics: {unit.name}")
                _verify_loaded_unit(result.stdout, unit, directory)
        except (SchedulerError, BackupError, ValueError, RuntimeError) as exc:
            partial = False
            message = f"Activation verification failed: {exc}"
        except (FileNotFoundError, PermissionError) as exc:
            partial = False
            message = f"systemctl --user {stage} could not start: {exc}"
        except subprocess.TimeoutExpired:
            message = f"systemctl --user {stage} timed out after 30 seconds"
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or "No diagnostic output"
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            message = f"systemctl --user {stage} failed (exit {exc.returncode}): {detail.strip()}"
        except (OSError, subprocess.SubprocessError) as exc:
            message = f"systemctl --user {stage} failed: {exc}"
        else:
            continue
        state = (
            "The timer may be partially enabled and/or active; its state was not verified."
            if partial
            else (
                "No timer enable/start was performed by this operation; "
                "preexisting state is unknown."
            )
        )
        return ScheduleActivationResult(False, timer, (message, state), partial)
    return ScheduleActivationResult(True, timer, ())
