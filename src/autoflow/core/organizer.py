"""Extension organization planning and explicit, no-overwrite Linux execution.

Only immediate regular children are planned, including hidden files. The last
suffix, lowercased and without its dot, names a directory inside the source;
dotfiles without another suffix and trailing-dot names use ``no_extension``.
Existing real destination directories are allowed, but any existing target or
non-directory/symlink category is an issue. Issues invalidate the complete plan;
proposals remain available for inspection. Planning never modifies files.

Execution re-plans and preflights every target before moving anything, then uses
descriptor-relative renameat2(RENAME_NOREPLACE). It requires owned, non-shared,
writable source/category directories on the same device. No copy/delete fallback,
rollback, unlink, or directory cleanup is performed. Successfully moved files and
created directories remain after a later failure, reported as partial failure.
The batch is not atomic: concurrent collisions after preflight can cause partial
execution. Metadata/attachment checks cannot close every check-to-rename window;
concurrent writers, mount/ancestor changes, or hostile same-credential processes
require additional isolation or external locking. Stop such activity before use.
"""

import ctypes
import os
import stat
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from autoflow.core.backup import (
    _DIRECTORY_FLAGS,
    BackupError,
    _absolute,
    _directory,
    _validate_output,
)


class OrganizerError(Exception):
    """Source inspection failed; a reliable plan could not be produced."""


@dataclass(frozen=True)
class PlannedMove:
    source: Path
    destination: Path
    extension: str


@dataclass(frozen=True)
class PlanIssue:
    path: Path
    reason: str


@dataclass(frozen=True)
class OrganizationPlan:
    source: Path
    entries: tuple[PlannedMove, ...]
    issues: tuple[PlanIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def groups(self) -> dict[str, tuple[PlannedMove, ...]]:
        """Return a new grouping without exposing mutable plan internals."""
        grouped: dict[str, list[PlannedMove]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.extension, []).append(entry)
        return {extension: tuple(entries) for extension, entries in grouped.items()}


@dataclass(frozen=True)
class OrganizationResult:
    success: bool
    source: Path
    moved: tuple[PlannedMove, ...]
    skipped: tuple[PlanIssue, ...]
    errors: tuple[PlanIssue, ...]
    created_directories: tuple[Path, ...]

    @property
    def partial(self) -> bool:
        """At least one file moved, but the complete operation failed."""
        return bool(self.moved) and not self.success


def _invalid_path(path: Path, name_max: int, path_max: int) -> bool:
    return (
        any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in str(path))
        or (name_max > 0 and any(len(os.fsencode(part)) > name_max for part in path.parts[1:]))
        or (path_max > 0 and len(os.fsencode(path)) >= path_max)
    )


def _inspect_destination(root: int, entry: PlannedMove) -> PlanIssue | None:
    category = entry.destination.parent
    try:
        metadata = os.stat(entry.extension, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return PlanIssue(category, "Destination category is not a real directory")
    descriptor = os.open(entry.extension, _DIRECTORY_FLAGS, dir_fd=root)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OrganizerError(f"Destination category changed during planning: {category}")
        try:
            os.stat(entry.destination.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return PlanIssue(entry.destination, "Destination already exists")
    finally:
        os.close(descriptor)


def plan_organization(source: Path) -> OrganizationPlan:
    """Inspect metadata only; never recurse, follow symlinks, or modify files.

    Invalid sources or inspection failures raise OrganizerError. Path and
    collision issues are returned in an invalid plan, without automatic renaming.
    Source paths containing '..' are rejected before lexical normalization.
    """
    try:
        source = Path(source).expanduser()
        if ".." in source.parts:
            raise OrganizerError("Source path must not contain parent traversal ('..')")
        source = _absolute(source)
        with _directory(source) as root:
            assert root is not None
            name_max = os.fpathconf(root, "PC_NAME_MAX")
            path_max = os.fpathconf(root, "PC_PATH_MAX")
            if _invalid_path(source, name_max, path_max):
                raise OrganizerError("Invalid source path")
            entries: list[PlannedMove] = []
            issues: list[PlanIssue] = []
            for name in sorted(os.listdir(root)):
                metadata = os.stat(name, dir_fd=root, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                original = source / name
                extension = original.suffix[1:].lower() or "no_extension"
                destination = source / extension / name
                entry = PlannedMove(original, destination, extension)
                entries.append(entry)
                if _invalid_path(destination, name_max, path_max):
                    issues.append(PlanIssue(destination, "Invalid destination path"))
                elif issue := _inspect_destination(root, entry):
                    issues.append(issue)
        return OrganizationPlan(source, tuple(entries), tuple(issues))
    except (OSError, ValueError, TypeError, RuntimeError, BackupError) as exc:
        raise OrganizerError(f"Cannot plan organization: {exc}") from exc


def _file_state(metadata: os.stat_result) -> tuple[int, ...]:
    # A rename changes ctime, including for another hard link to the same inode.
    # Access times are also irrelevant. This is change detection, not a snapshot.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _execution_directory(descriptor: int, device: int) -> None:
    _validate_output(descriptor)
    metadata = os.fstat(descriptor)
    if metadata.st_mode & 0o300 != 0o300:
        raise OrganizerError("Execution directories require owner write and search permissions")
    if metadata.st_dev != device:
        raise OrganizerError("Destination category is on a different filesystem")


def _check_attachments(source: Path, root: int, categories: dict[str, int]) -> None:
    """Check that pinned directories still belong to the requested source tree."""
    with _directory(source) as current:
        assert current is not None
        actual, expected = os.fstat(current), os.fstat(root)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise OrganizerError("Source directory changed during execution")
    _execution_directory(root, expected.st_dev)
    for name, descriptor in categories.items():
        actual = os.stat(name, dir_fd=root, follow_symlinks=False)
        expected_category = os.fstat(descriptor)
        if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
            expected_category.st_dev,
            expected_category.st_ino,
        ):
            raise OrganizerError(f"Destination category changed during execution: {name}")
        _execution_directory(descriptor, expected.st_dev)


def _require_absent(descriptor: int, entry: PlannedMove) -> None:
    try:
        os.stat(entry.destination.name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise OrganizerError(f"Destination already exists: {entry.destination}")


def _move_noreplace(root: int, name: str, category: int, target: str) -> None:
    """Atomic cross-directory rename; unsupported Linux/filesystem support fails closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise OrganizerError("Atomic no-overwrite rename is unavailable") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(root, os.fsencode(name), category, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def execute_organization(source: Path) -> OrganizationResult:
    """Execute a freshly validated plan; expected failures return structured errors.

    Discovered links/special files and subdirectories are left untouched and
    reported as skipped. Links replacing planned files or destination directories
    are errors. Unprocessed planned files are also reported as skipped on failure.
    Only original source names are relocated; contents are never copied/deleted.
    No automatic rollback is attempted, and created directories are retained.
    """
    source = Path(source)
    moved: list[PlannedMove] = []
    skipped: list[PlanIssue] = []
    errors: list[PlanIssue] = []
    created: list[Path] = []
    plan: OrganizationPlan | None = None
    active = source
    try:
        plan = plan_organization(source)
        source = plan.source
        active = source
        if not plan.is_valid:
            errors.extend(plan.issues)
        else:
            with ExitStack() as stack:
                root = stack.enter_context(_directory(source))
                assert root is not None
                device = os.fstat(root).st_dev
                _execution_directory(root, device)
                expected: dict[Path, os.stat_result] = {}
                for name in sorted(os.listdir(root)):
                    metadata = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        reason = (
                            "Symbolic link (not followed)"
                            if stat.S_ISLNK(metadata.st_mode)
                            else "Directory or special file (not traversed)"
                        )
                        skipped.append(PlanIssue(source / name, reason))
                for entry in plan.entries:
                    active = entry.source
                    extension = entry.source.suffix[1:].lower() or "no_extension"
                    if (
                        entry.source.parent != source
                        or entry.extension != extension
                        or entry.destination != source / extension / entry.source.name
                    ):
                        raise OrganizerError("Unsafe planned source or destination path")
                    metadata = os.stat(entry.source.name, dir_fd=root, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OrganizerError("Planned source is no longer a regular file")
                    expected[entry.source] = metadata
                # Re-plan just before preparing directories. Any newly observed
                # collision or changed file set prevents every move.
                active = source
                current_plan = plan_organization(source)
                if not current_plan.is_valid:
                    errors.extend(current_plan.issues)
                elif current_plan != plan:
                    raise OrganizerError("Organization plan changed before execution")
                else:
                    categories: dict[str, int] = {}
                    for extension in sorted(plan.groups):
                        active = source / extension
                        try:
                            os.mkdir(extension, mode=0o700, dir_fd=root)
                            created.append(active)
                        except FileExistsError:
                            pass
                        descriptor = os.open(extension, _DIRECTORY_FLAGS, dir_fd=root)
                        stack.callback(os.close, descriptor)
                        _execution_directory(descriptor, device)
                        categories[extension] = descriptor
                    active = source
                    _check_attachments(source, root, categories)
                    # Complete preflight after directory preparation, before the
                    # first move. Even a late collision in the final entry aborts.
                    for entry in plan.entries:
                        active = entry.source
                        metadata = os.stat(entry.source.name, dir_fd=root, follow_symlinks=False)
                        if _file_state(metadata) != _file_state(expected[entry.source]):
                            raise OrganizerError("Planned source changed before execution")
                        _require_absent(categories[entry.extension], entry)
                    for entry in plan.entries:
                        active = entry.source
                        _check_attachments(source, root, categories)
                        category = categories[entry.extension]
                        _require_absent(category, entry)
                        # O_PATH does not require file-read permissions and pins
                        # the inode without reading contents or following a link.
                        descriptor = os.open(
                            entry.source.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root
                        )
                        try:
                            if _file_state(os.fstat(descriptor)) != _file_state(
                                expected[entry.source]
                            ):
                                raise OrganizerError("Planned source changed during execution")
                            _move_noreplace(
                                root, entry.source.name, category, entry.destination.name
                            )
                            moved.append(entry)
                            actual = os.stat(
                                entry.destination.name, dir_fd=category, follow_symlinks=False
                            )
                            if _file_state(actual) != _file_state(expected[entry.source]):
                                raise OrganizerError("Moved file changed during execution")
                            _check_attachments(source, root, categories)
                        finally:
                            os.close(descriptor)
    except (OSError, ValueError, RuntimeError, OrganizerError, BackupError) as exc:
        errors.append(PlanIssue(active, str(exc)))
    if errors and plan is not None:
        moved_sources = {entry.source for entry in moved}
        skipped.extend(
            PlanIssue(entry.source, "Not moved: organization stopped")
            for entry in plan.entries
            if entry.source not in moved_sources
        )
    return OrganizationResult(
        success=not errors,
        source=source,
        moved=tuple(moved),
        skipped=tuple(skipped),
        errors=tuple(errors),
        created_directories=tuple(created),
    )
