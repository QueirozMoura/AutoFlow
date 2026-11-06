"""Bounded validation and atomic publication of untrusted Linux tar.gz backups."""

import gzip
import os
import shutil
import stat
import tarfile
import time
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from autoflow.core.backup import (
    _DIRECTORY_FLAGS,
    _FILE_FLAGS,
    _PROTECTED,
    BackupError,
    ProgressCallback,
    _absolute,
    _directory,
    _rename_noreplace,
    _signature,
    _validate_output,
)

DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_BYTES = 10 * 1024**3
_CHUNK = 64 * 1024
_METADATA_LIMIT = 1024**2


class RestoreError(Exception):
    """An archive cannot be safely restored."""


@dataclass(frozen=True)
class RestoreResult:
    success: bool
    dry_run: bool
    archive: str
    output: str
    file_count: int
    total_bytes: int
    duration_seconds: float
    files: list[str]
    directories: list[str]
    max_files: int
    max_bytes: int


@contextmanager
def _output_parent(path: Path) -> Iterator[int]:
    """Reject ancestor permissions that allow other users to replace our paths."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    # In a user namespace the system root owner may be mapped to a nonzero uid.
    system_owner = os.fstat(descriptor).st_uid
    try:
        for part in (*path.parts[1:], None):
            metadata = os.fstat(descriptor)
            # Root-owned sticky directories such as /tmp protect child names
            # owned by root or this user. All other shared writable ancestors
            # are unsafe, even when the destination parent itself is private.
            sticky_root = metadata.st_uid == system_owner and metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in (system_owner, os.geteuid()) or (
                metadata.st_mode & 0o022 and not sticky_root
            ):
                raise RestoreError("Unsafe output ancestor ownership or write permissions")
            if part is not None:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        _validate_output(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


class _BoundedGzip:
    """Bound metadata allocations and all forward decompression, including seeks."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        self.stream = stream
        self.limit = limit

    def tell(self) -> int:
        return self.stream.tell()

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > _METADATA_LIMIT:
            raise RestoreError("Archive metadata exceeds the per-record limit (1 MiB)")
        remaining = self.limit - self.tell()
        if remaining < 0:
            raise RestoreError("Decompressed archive exceeds its framing limit")
        data = self.stream.read(min(size, remaining + 1))
        if len(data) > remaining:
            raise RestoreError("Decompressed archive exceeds its framing limit")
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0 or offset < 0 or offset > self.limit:
            raise RestoreError("Invalid or excessive archive offset")
        while self.tell() < offset:
            if not self.read(min(_CHUNK, offset - self.tell())):
                raise RestoreError("Truncated archive")
        if self.tell() != offset:
            return self.stream.seek(offset)
        return offset


class _SafeTarInfo(tarfile.TarInfo):
    """Bound extension records before tarfile allocates or parses their payload."""

    @classmethod
    def fromtarfile(cls, archive: tarfile.TarFile) -> "_SafeTarInfo":
        header = archive.fileobj.read(tarfile.BLOCKSIZE)
        info = cls.frombuf(header, archive.encoding, archive.errors)
        info.offset = archive.fileobj.tell() - tarfile.BLOCKSIZE
        allowed = (
            tarfile.REGTYPE,
            tarfile.AREGTYPE,
            tarfile.DIRTYPE,
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.GNUTYPE_LONGNAME,
        )
        if info.type not in allowed:
            raise RestoreError("Only regular files and directories are allowed")
        if (
            info.type
            in (
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK,
            )
            and not 0 <= info.size <= _METADATA_LIMIT
        ):
            raise RestoreError("Archive metadata exceeds the per-record limit (1 MiB)")
        return info._proc_member(archive)

    def _proc_pax(self, archive: tarfile.TarFile) -> "_SafeTarInfo":
        # tarfile can consume sparse payload maps while processing PAX, before
        # returning a member to our validator. Reject these extensions first.
        position = archive.fileobj.tell()
        payload = archive.fileobj.read(self.size)
        if b"GNU.sparse" in payload:
            raise RestoreError("Sparse archive metadata is not supported")
        archive.fileobj.seek(position)
        return super()._proc_pax(archive)


def _member_path(info: tarfile.TarInfo) -> str:
    if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
        raise RestoreError("Only regular files and directories are allowed")
    if info.sparse is not None or any(key.startswith("GNU.sparse") for key in info.pax_headers):
        raise RestoreError("Sparse archive entries are not supported")
    name = info.name.removesuffix("/") if info.isdir() else info.name
    parts = name.split("/")
    if (
        not name
        or len(name.encode("utf-8", errors="surrogateescape")) > 4096
        or len(parts) > 128
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise RestoreError("Unsafe archive path")
    if info.size < 0 or (info.isdir() and info.size != 0):
        raise RestoreError("Invalid archive entry size")
    return name


def _stage_directory(stage: int, parts: list[str]) -> int:
    """Create restrictive directories and traverse only through no-follow fds."""
    descriptor = os.dup(stage)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _scan(
    descriptor: int,
    max_files: int,
    max_bytes: int,
    stage: int | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[list[str], list[str], int]:
    files: list[str] = []
    directories: set[str] = set()
    explicit: set[str] = set()
    kinds: dict[str, bool] = {}
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
        reader = _BoundedGzip(gz, max_bytes + max_files * 8192 + _METADATA_LIMIT)
        with tarfile.open(fileobj=reader, mode="r:", tarinfo=_SafeTarInfo) as archive:
            for info in archive:
                name = _member_path(info)
                if name in explicit:
                    raise RestoreError("Duplicate archive entry")
                explicit.add(name)
                parts = name.split("/")
                for index in range(1, len(parts)):
                    parent = "/".join(parts[:index])
                    if kinds.get(parent) is False:
                        raise RestoreError("File/directory conflict in archive")
                    kinds[parent] = True
                    directories.add(parent)
                is_directory = info.isdir()
                if name in kinds and kinds[name] != is_directory:
                    raise RestoreError("File/directory conflict in archive")
                kinds[name] = is_directory
                if len(kinds) > max_files:
                    raise RestoreError(
                        "Archive entry count exceeds max-files (including directories)"
                    )
                total += info.size
                if total > max_bytes:
                    raise RestoreError("Archive uncompressed size exceeds max-bytes")
                if is_directory:
                    directories.add(name)
                    if stage is not None:
                        directory = _stage_directory(stage, parts)
                        os.close(directory)
                else:
                    files.append(name)
                    # The stage is private and names have been validated above.
                    # Never apply archive permissions, owners, links, or timestamps.
                    destination = None
                    try:
                        if stage is not None:
                            directory = _stage_directory(stage, parts[:-1])
                            try:
                                fd = os.open(
                                    parts[-1],
                                    os.O_WRONLY
                                    | os.O_CREAT
                                    | os.O_EXCL
                                    | os.O_NOFOLLOW
                                    | os.O_CLOEXEC,
                                    0o600,
                                    dir_fd=directory,
                                )
                                destination = os.fdopen(fd, "wb")
                            finally:
                                os.close(directory)
                        data = archive.extractfile(info)
                        if data is None:
                            raise RestoreError("Missing regular file data")
                        with data:
                            remaining = info.size
                            while remaining:
                                block = data.read(min(_CHUNK, remaining))
                                if not block:
                                    raise RestoreError("Truncated file data")
                                if destination is not None:
                                    destination.write(block)
                                remaining -= len(block)
                        if destination is not None:
                            destination.flush()
                            os.fsync(destination.fileno())
                    finally:
                        if destination is not None:
                            destination.close()
                if progress:
                    progress(len(explicit), 0, name)
            # tarfile normally accepts missing terminators and ignores trailing
            # data. Require both zero end records, then verify gzip CRC and EOF.
            if reader.tell() != archive.offset + 512:
                raise RestoreError("Missing tar end marker")
            reader.seek(archive.offset)
            if reader.read(1024) != bytes(1024):
                raise RestoreError("Missing two zero tar end markers")
            padding = 0
            while block := reader.read(_CHUNK):
                padding += len(block)
                if any(block) or padding > _METADATA_LIMIT:
                    raise RestoreError("Unexpected trailing archive data")
    return files, sorted(directories), total


def _ensure_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RestoreError("Output must not already exist, even as an empty directory or link")


def restore_backup(
    archive: Path,
    output: Path,
    *,
    dry_run: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    progress: ProgressCallback | None = None,
) -> RestoreResult:
    """Validate twice and publish one new directory atomically without replacement."""
    started = time.monotonic()
    temporary = None
    try:
        if max_files < 1 or max_bytes < 0:
            raise RestoreError("max-files must be positive and max-bytes must be nonnegative")
        archive, output = _absolute(archive), _absolute(output)
        if output == Path("/") or any(
            output == path or path in output.parents for path in _PROTECTED
        ):
            raise RestoreError("Protected output path")
        with _directory(archive.parent) as source_parent, _output_parent(output.parent) as parent:
            assert source_parent is not None and parent is not None
            _validate_output(parent)
            _ensure_absent(parent, output.name)
            parent_identity = os.fstat(parent)
            fd = os.open(archive.name, _FILE_FLAGS, dir_fd=source_parent)
            try:
                original = os.fstat(fd)
                if not stat.S_ISREG(original.st_mode):
                    raise RestoreError("Archive must be a regular file without symbolic links")
                inventory = _scan(fd, max_files, max_bytes)
                if _signature(os.fstat(fd)) != _signature(original):
                    raise RestoreError("Archive changed during validation")
                if not dry_run:
                    temporary = f".autoflow-restore-{uuid.uuid4().hex}.part"
                    # A collision never grants ownership of someone else's path.
                    name = temporary
                    temporary = None
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                    temporary = name
                    stage = os.open(temporary, _DIRECTORY_FLAGS, dir_fd=parent)
                    try:
                        prepared = _scan(fd, max_files, max_bytes, stage, progress)
                    finally:
                        os.close(stage)
                    if prepared != inventory or _signature(os.fstat(fd)) != _signature(original):
                        raise RestoreError("Archive changed during restoration")
                    with _output_parent(output.parent) as current_parent:
                        assert current_parent is not None
                        current = os.fstat(current_parent)
                        if (current.st_dev, current.st_ino) != (
                            parent_identity.st_dev,
                            parent_identity.st_ino,
                        ):
                            raise RestoreError("Output parent changed during restoration")
                        _validate_output(parent)
                        _rename_noreplace(parent, temporary, output.name)
                    temporary = None
            finally:
                try:
                    os.close(fd)
                finally:
                    if temporary is not None:
                        shutil.rmtree(temporary, dir_fd=parent)
        files, directories, total = inventory
        return RestoreResult(
            True,
            dry_run,
            str(archive),
            str(output),
            len(files),
            total,
            time.monotonic() - started,
            files,
            directories,
            max_files,
            max_bytes,
        )
    except (
        OSError,
        tarfile.TarError,
        EOFError,
        ValueError,
        zlib.error,
        BackupError,
        RecursionError,
    ) as exc:
        raise RestoreError(f"Restore failed: {exc}") from exc
