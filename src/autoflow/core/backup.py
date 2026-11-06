"""Read-only discovery and fail-closed tar creation for Linux filesystems."""

import ctypes
import errno
import os
import stat
import tarfile
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ProgressCallback = Callable[[int, int, str], None]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_PROTECTED = (Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"))


class BackupError(Exception):
    """A backup could not be safely completed."""


@dataclass(frozen=True)
class SkippedEntry:
    path: str
    reason: str


@dataclass(frozen=True)
class BackupResult:
    success: bool
    dry_run: bool
    source: str
    output: str
    archive: str | None
    file_count: int
    original_bytes: int
    compressed_bytes: int | None
    duration_seconds: float
    files: list[str]
    directories: list[str]
    skipped: list[SkippedEntry]


@dataclass(frozen=True)
class _Entry:
    relative: Path
    metadata: os.stat_result

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.metadata.st_mode)


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    # atime is intentionally ignored: reading may update it on the host filesystem.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _check(metadata: os.stat_result, expected: os.stat_result, name: Path) -> None:
    if _signature(metadata) != _signature(expected):
        raise BackupError(f"Source changed during backup: {name}")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


@contextmanager
def _directory(
    path: Path,
    *,
    create: bool = False,
    missing_ok: bool = False,
    forbidden: set[tuple[int, int]] | None = None,
) -> Iterator[int | None]:
    """Walk from / using no-follow descriptors, including every parent component."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if forbidden:
                current = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) in forbidden:
                    raise BackupError("Output resolves inside the source tree")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not missing_ok:
                    raise
                yield None
                return
            os.close(descriptor)
            descriptor = child
        current = os.fstat(descriptor)
        if forbidden and (current.st_dev, current.st_ino) in forbidden:
            raise BackupError("Output resolves inside the source tree")
        yield descriptor
    finally:
        os.close(descriptor)


def _mount_points() -> set[Path]:
    """Include bind mounts, which may share st_dev with their parent."""
    points = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        encoded = line.split()[4]
        for escape, value in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
            encoded = encoded.replace(escape, value)
        points.add(Path(encoded))
    return points


def _safe_archive_path(name: str) -> bool:
    """Share backup/restore name rules; validate before any path normalization."""
    parts = name.split("/")
    return not (
        not name
        or len(name.encode("utf-8", errors="surrogateescape")) > 4096
        or len(parts) > 128
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


def _discover(parent_fd: int, source: Path) -> tuple[list[_Entry], list[SkippedEntry]]:
    entries: list[_Entry] = []
    skipped: list[SkippedEntry] = []
    mounts = _mount_points()
    root = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
    if not (stat.S_ISREG(root.st_mode) or stat.S_ISDIR(root.st_mode)):
        raise BackupError("Source must be a regular file or directory, never a symbolic link")

    def visit(directory_fd: int, name: str, relative: Path, metadata: os.stat_result) -> None:
        absolute = source.parent / relative
        if stat.S_ISLNK(metadata.st_mode):
            skipped.append(SkippedEntry(relative.as_posix(), "symbolic link (not followed)"))
            return
        if relative != Path(source.name) and (metadata.st_dev != root.st_dev or absolute in mounts):
            skipped.append(
                SkippedEntry(relative.as_posix(), "nested mount or different filesystem")
            )
            return
        if any(absolute == protected or protected in absolute.parents for protected in _PROTECTED):
            raise BackupError(f"Protected system path: {absolute}")
        is_directory = stat.S_ISDIR(metadata.st_mode)
        if not (is_directory or stat.S_ISREG(metadata.st_mode)):
            skipped.append(SkippedEntry(relative.as_posix(), "special file (not read)"))
            return
        if not _safe_archive_path(relative.as_posix()):
            raise BackupError(
                f"Source entry {relative.as_posix()!r} is incompatible with restore: "
                "unsafe archive path (backslashes, control characters, or path limits)"
            )
        flags = _DIRECTORY_FLAGS if is_directory else _FILE_FLAGS
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            _check(os.fstat(descriptor), metadata, relative)
            entries.append(_Entry(relative, metadata))
            if is_directory:
                for child in sorted(os.listdir(descriptor)):
                    child_metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                    visit(descriptor, child, relative / child, child_metadata)
            _check(os.fstat(descriptor), metadata, relative)
            _check(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), metadata, relative)
        finally:
            os.close(descriptor)

    visit(parent_fd, source.name, Path(source.name), root)
    return entries, skipped


@contextmanager
def _open_entry(parent_fd: int, entry: _Entry, expected: dict[Path, _Entry]) -> Iterator[int]:
    descriptor = os.dup(parent_fd)
    try:
        relative = Path()
        for part in entry.relative.parts[:-1]:
            relative /= part
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _check(os.fstat(descriptor), expected[relative].metadata, relative)
        flags = _DIRECTORY_FLAGS if entry.is_directory else _FILE_FLAGS
        child = os.open(entry.relative.name, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        _check(os.fstat(descriptor), entry.metadata, entry.relative)
        yield descriptor
        _check(os.fstat(descriptor), entry.metadata, entry.relative)
    finally:
        os.close(descriptor)


def _write_archive(
    archive: tarfile.TarFile,
    parent_fd: int,
    entries: list[_Entry],
    progress: ProgressCallback | None,
) -> None:
    expected = {entry.relative: entry for entry in entries}
    if progress:
        progress(0, len(entries), "")
    for index, entry in enumerate(entries, 1):
        with _open_entry(parent_fd, entry, expected) as descriptor:
            # Never ask tarfile to resolve a pathname or recurse on our behalf.
            with (
                os.fdopen(os.dup(descriptor), "rb")
                if not entry.is_directory
                else nullcontext() as data
            ):
                info = tarfile.TarInfo(entry.relative.as_posix())
                info.type = tarfile.DIRTYPE if entry.is_directory else tarfile.REGTYPE
                info.size = 0 if entry.is_directory else entry.metadata.st_size
                info.mode = stat.S_IMODE(entry.metadata.st_mode) & 0o777
                info.uid = entry.metadata.st_uid
                info.gid = entry.metadata.st_gid
                info.mtime = entry.metadata.st_mtime
                archive.addfile(info, data)
        if progress:
            progress(index, len(entries), entry.relative.as_posix())


def _archive_name() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"autoflow-{stamp}-{uuid.uuid4().hex}.tar.gz"


def _validate_output(descriptor: int) -> None:
    """Only publish into an owned directory that other users cannot modify."""
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise BackupError("Output must be owned by the current user and not group/world-writable")


def _rename_noreplace(directory_fd: int, temporary: str, final: str) -> None:
    """Atomic Linux rename with RENAME_NOREPLACE; never use overwriting rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise BackupError("Atomic no-overwrite rename is unavailable on this Linux system") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory_fd, os.fsencode(temporary), directory_fd, os.fsencode(final), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), final)


def _publish(directory_fd: int, temporary: str) -> str:
    for _ in range(20):
        name = _archive_name()
        try:
            _rename_noreplace(directory_fd, temporary, name)
            return name
        except FileExistsError:
            continue
    raise BackupError("Could not allocate a unique backup name after 20 attempts")


def _create_archive(
    parent_fd: int,
    output_fd: int,
    source: Path,
    output: Path,
    entries: list[_Entry],
    skipped: list[SkippedEntry],
    progress: ProgressCallback | None,
) -> tuple[str, int]:
    temporary = None
    temporary_identity: tuple[int, int] | None = None
    try:
        for _ in range(20):
            candidate = f".{uuid.uuid4().hex}.tar.gz.part"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=output_fd,
                )
            except FileExistsError:
                continue
            temporary = candidate
            metadata = os.fstat(descriptor)
            temporary_identity = (metadata.st_dev, metadata.st_ino)
            break
        else:
            raise BackupError("Could not allocate a unique temporary backup")
        with os.fdopen(descriptor, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                _write_archive(archive, parent_fd, entries, progress)
            # Re-scan to catch new, removed, replaced, or modified source entries.
            current, current_skipped = _discover(parent_fd, source)
            if [(item.relative, _signature(item.metadata)) for item in current] != [
                (item.relative, _signature(item.metadata)) for item in entries
            ] or current_skipped != skipped:
                raise BackupError("Source changed during backup; archive was not published")
            # A pinned parent descriptor remains readable even after an ancestor
            # is renamed. Verify that the requested path still identifies it.
            with _directory(source.parent) as current_parent:
                assert current_parent is not None
                current = os.fstat(current_parent)
                expected = os.fstat(parent_fd)
                if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                    raise BackupError("Source parent changed during backup")
            stream.flush()
            os.fsync(stream.fileno())
            temporary_metadata = os.fstat(stream.fileno())
            compressed_bytes = temporary_metadata.st_size
        with _directory(output) as current_output:
            assert current_output is not None
            current = os.fstat(current_output)
            expected = os.fstat(output_fd)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise BackupError("Destination changed during backup")
            _validate_output(output_fd)
            _check(
                os.stat(temporary, dir_fd=output_fd, follow_symlinks=False),
                temporary_metadata,
                Path(temporary),
            )
            name = _publish(output_fd, temporary)
        temporary = None
        return name, compressed_bytes
    finally:
        if temporary is not None:
            try:
                metadata = os.stat(temporary, dir_fd=output_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != temporary_identity:
                    raise BackupError(
                        f"Temporary archive name was replaced; refusing to remove {temporary}"
                    )
                os.unlink(temporary, dir_fd=output_fd)
            except FileNotFoundError:
                pass  # The owned temporary is already absent; never remove another path.
            except OSError as exc:
                raise BackupError(f"Could not remove temporary archive {temporary}: {exc}") from exc


def create_backup(
    source: Path,
    output: Path,
    *,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    """Back up one file or directory; exclusions are explicit and read errors are fatal."""
    started = time.monotonic()
    try:
        source, output = _absolute(source), _absolute(output)
        if source == Path("/") or any(
            source == protected or protected in source.parents for protected in _PROTECTED
        ):
            raise BackupError(f"Protected system source: {source}")
        if output == source or source in output.parents:
            raise BackupError("Output must be outside the source tree")
        if any(output == protected or protected in output.parents for protected in _PROTECTED):
            raise BackupError(f"Protected output path: {output}")
        with _directory(source.parent) as parent_fd:
            assert parent_fd is not None
            entries, skipped = _discover(parent_fd, source)
            forbidden = {
                (entry.metadata.st_dev, entry.metadata.st_ino)
                for entry in entries
                if entry.is_directory
            }
            # Validate existing destination components without creating anything.
            with _directory(output, missing_ok=True, forbidden=forbidden) as output_fd:
                if output_fd is not None:
                    _validate_output(output_fd)
            archive_path = None
            compressed_bytes = None
            if not dry_run:
                with _directory(output, create=True, forbidden=forbidden) as output_fd:
                    assert output_fd is not None
                    _validate_output(output_fd)
                    name, compressed_bytes = _create_archive(
                        parent_fd,
                        output_fd,
                        source,
                        output,
                        entries,
                        skipped,
                        progress,
                    )
                    archive_path = str(output / name)
        files = [entry.relative.as_posix() for entry in entries if not entry.is_directory]
        return BackupResult(
            success=True,
            dry_run=dry_run,
            source=str(source),
            output=str(output),
            archive=archive_path,
            file_count=len(files),
            original_bytes=sum(
                entry.metadata.st_size for entry in entries if not entry.is_directory
            ),
            compressed_bytes=compressed_bytes,
            duration_seconds=time.monotonic() - started,
            files=files,
            directories=[entry.relative.as_posix() for entry in entries if entry.is_directory],
            skipped=skipped,
        )
    except (OSError, tarfile.TarError, ValueError, zlib.error, RecursionError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ELOOP:
            raise BackupError("Symbolic links in source/output paths are not allowed") from exc
        raise BackupError(f"Backup failed: {exc}") from exc
