"""Untrusted archive restoration uses temporary fixtures exclusively."""

import gzip
import io
import os
import stat
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoflow.core import restore as core
from autoflow.core.backup import create_backup


def make_archive(tmp_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    path = tmp_path / "input.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, value in entries:
            info = tarfile.TarInfo(name)
            if value in (
                tarfile.SYMTYPE,
                tarfile.LNKTYPE,
                tarfile.FIFOTYPE,
                tarfile.CHRTYPE,
                tarfile.BLKTYPE,
                tarfile.DIRTYPE,
            ):
                info.type = value
                info.linkname = "../outside"
                archive.addfile(info)
            else:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
    return path


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "hello.txt").write_bytes(b"hello\n")
    (source / "nested" / "data.bin").write_bytes(bytes(range(256)))
    result = create_backup(source, tmp_path / "backups")
    return Path(result.archive)


def test_backup_round_trip_and_original_integrity(backup: Path, tmp_path: Path) -> None:
    original_archive = backup.read_bytes()
    output = tmp_path / "restored"
    result = core.restore_backup(backup, output)
    assert result.success and not result.dry_run
    assert result.file_count == 2 and result.total_bytes == 262
    assert (output / "source/hello.txt").read_bytes() == b"hello\n"
    assert (output / "source/nested/data.bin").read_bytes() == bytes(range(256))
    assert (output / "source/empty").is_dir()
    assert backup.read_bytes() == original_archive
    assert (tmp_path / "source/hello.txt").read_bytes() == b"hello\n"
    for path in [output, *output.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert not list(tmp_path.glob(".autoflow-restore-*"))


def test_dry_run_without_writes(backup: Path, tmp_path: Path) -> None:
    before = sorted(tmp_path.rglob("*"))
    result = core.restore_backup(backup, tmp_path / "restored", dry_run=True)
    assert result.dry_run and result.file_count == 2
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "a/../../outside",
        "a/../b",
        "./name",
        "a//b",
        "a\\b",
        "bad\nname",
        "bad\rname",
        "bad\tname",
        "bad\x1bname",
        "bad\x7fname",
        "a/" * 128 + "file",
        "a/" * 126 + "é" * 2000,
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_unsafe_paths(tmp_path: Path, name: str, dry_run: bool) -> None:
    archive = make_archive(tmp_path, [(name, b"payload")])
    with pytest.raises(core.RestoreError, match="Unsafe"):
        core.restore_backup(archive, tmp_path / "restored", dry_run=dry_run)
    assert list(tmp_path.iterdir()) == [archive]


@pytest.mark.parametrize(
    "name",
    [
        "with spaces.txt",
        "ação-日本語.txt",
        "[report].txt",
        "quote'\".txt",
        "-report.txt",
        "a:b.txt",
        ".hidden",
    ],
)
@pytest.mark.parametrize("single_file", [False, True])
def test_special_names_backup_restore_round_trip(
    tmp_path: Path, name: str, single_file: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    entry = source / name
    if single_file:
        entry.write_bytes(b"original")
        source = entry
    else:
        entry.mkdir()
        (entry / name).write_bytes(b"original")
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    backup = Path(create_backup(source, tmp_path / "backups").archive)
    archive_bytes = backup.read_bytes()
    output = tmp_path / "restored"

    preview = core.restore_backup(backup, output, dry_run=True)
    assert preview.success and not output.exists()
    result = core.restore_backup(backup, output)

    assert result.success and result.file_count == 1
    restored = output / name if single_file else output / "source" / name / name
    assert restored.read_bytes() == b"original"
    assert backup.read_bytes() == archive_bytes
    assert all(path.read_bytes() == value for path, value in originals.items())


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, b"X"],
)
def test_special_types(tmp_path: Path, kind: bytes) -> None:
    archive = tmp_path / "input.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("unsafe")
        info.type = kind
        info.linkname = "/outside"
        tar.addfile(info)
    with pytest.raises(core.RestoreError):
        core.restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("existing", ["empty_directory", "directory", "file", "link"])
def test_existing_output_preserved(backup: Path, tmp_path: Path, existing: str) -> None:
    output = tmp_path / "restored"
    if existing in ("empty_directory", "directory"):
        output.mkdir()
        if existing == "directory":
            (output / "keep").write_text("keep")
    elif existing == "file":
        output.write_text("keep")
    else:
        output.symlink_to(tmp_path / "missing")
    with pytest.raises(core.RestoreError, match="already exist"):
        core.restore_backup(backup, output)
    assert output.is_symlink() or output.exists()
    if existing == "directory":
        assert (output / "keep").read_text() == "keep"
    elif existing == "file":
        assert output.read_text() == "keep"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_limits(backup: Path, tmp_path: Path, limit: str, dry_run: bool) -> None:
    options = {"max_files": 2} if limit == "count" else {"max_bytes": 261}
    with pytest.raises(core.RestoreError, match="exceeds"):
        core.restore_backup(backup, tmp_path / "restored", dry_run=dry_run, **options)
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".autoflow-restore-*"))


def test_exact_limits(backup: Path, tmp_path: Path) -> None:
    assert (
        core.restore_backup(backup, tmp_path / "restored", max_files=5, max_bytes=262).total_bytes
        == 262
    )


@pytest.mark.parametrize("data", [b"", b"not gzip", gzip.compress(b"not tar")])
def test_corrupt_archives(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "bad.tar.gz"
    path.write_bytes(data)
    with pytest.raises(core.RestoreError):
        core.restore_backup(path, tmp_path / "restored")
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("damage", ["crc", "truncate", "trailing", "no_end_marker"])
def test_integrity_including_gzip_trailer(backup: Path, tmp_path: Path, damage: str) -> None:
    data = backup.read_bytes()
    if damage == "crc":
        data = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
    elif damage == "truncate":
        data = data[:-4]
    elif damage == "trailing":
        data = gzip.compress(gzip.decompress(data) + b"evil")
    else:
        raw = gzip.decompress(data)
        # First source directory header, with neither end record.
        data = gzip.compress(raw[:512])
    path = tmp_path / "bad.tar.gz"
    path.write_bytes(data)
    with pytest.raises(core.RestoreError):
        core.restore_backup(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_missing_archive(tmp_path: Path) -> None:
    with pytest.raises(core.RestoreError):
        core.restore_backup(tmp_path / "missing", tmp_path / "restored")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("location", ["archive", "archive_parent", "output_parent"])
def test_symlink_components_rejected(backup: Path, tmp_path: Path, location: str) -> None:
    output = tmp_path / "restored"
    alias = tmp_path / "alias"
    if location == "archive":
        alias.symlink_to(backup)
        backup = alias
    elif location == "archive_parent":
        alias.symlink_to(backup.parent, target_is_directory=True)
        backup = alias / backup.name
    else:
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / "restored"
    with pytest.raises(core.RestoreError):
        core.restore_backup(backup, output)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("same", b"one"), ("same", b"two")],
        [("a", b"file"), ("a/b", b"child")],
        [("a/b", b"child"), ("a", b"file")],
    ],
)
def test_duplicates_and_type_conflicts(tmp_path: Path, entries: list) -> None:
    archive = make_archive(tmp_path, entries)
    with pytest.raises(core.RestoreError):
        core.restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("failure", ["write", "publish", "cancel"])
def test_preparation_failure_cleanup(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "write":
        monkeypatch.setattr(core.os, "fsync", Mock(side_effect=OSError(28, "No space left")))
    elif failure == "publish":
        monkeypatch.setattr(core, "_rename_noreplace", Mock(side_effect=PermissionError("denied")))
    else:

        def cancel(*args):
            raise KeyboardInterrupt

        with pytest.raises(core.RestoreError, match="interrupted.*staging retained") as raised:
            core.restore_backup(backup, tmp_path / "restored", progress=cancel)
        assert isinstance(raised.value.__cause__, KeyboardInterrupt)
        assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1
        return
    with pytest.raises(core.RestoreError, match="staging retained"):
        core.restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1


def test_publication_race_preserves_winner(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_publish = core._rename_noreplace
    output = tmp_path / "restored"

    def race(parent, temporary, final):
        output.mkdir()
        (output / "keep").write_text("winner")
        real_publish(parent, temporary, final)

    monkeypatch.setattr(core, "_rename_noreplace", race)
    with pytest.raises(core.RestoreError):
        core.restore_backup(backup, output)
    assert (output / "keep").read_text() == "winner"
    assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1


def test_concurrent_restore(backup: Path, tmp_path: Path) -> None:
    def attempt(_):
        try:
            return core.restore_backup(backup, tmp_path / "restored").success
        except core.RestoreError:
            return False

    with ThreadPoolExecutor(max_workers=3) as executor:
        assert sum(executor.map(attempt, range(3))) == 1
    residues = list(tmp_path.glob(".autoflow-restore-*"))
    assert len(residues) <= 2
    for residue in residues:
        assert (residue / "source/hello.txt").read_bytes() == b"hello\n"


def test_empty_archive_and_implicit_directories(tmp_path: Path) -> None:
    archive = make_archive(tmp_path, [])
    result = core.restore_backup(archive, tmp_path / "empty", max_bytes=0)
    assert result.file_count == 0 and list((tmp_path / "empty").iterdir()) == []
    archive = make_archive(tmp_path, [("a/b/c/empty", b"")])
    core.restore_backup(archive, tmp_path / "implicit", max_bytes=0)
    for path in (tmp_path / "implicit").rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_archive_changes_between_passes(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scan = core._scan

    def change(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        if args[3:]:
            backup.touch()
        return result

    monkeypatch.setattr(core, "_scan", change)
    with pytest.raises(core.RestoreError, match="changed"):
        core.restore_backup(backup, tmp_path / "restored")
    assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1


def test_output_parent_moved_during_preparation(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    real_scan = core._scan

    def move(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        if args[3:]:
            parent.rename(tmp_path / "moved")
            parent.mkdir()
        return result

    monkeypatch.setattr(core, "_scan", move)
    with pytest.raises(core.RestoreError, match="parent changed"):
        core.restore_backup(backup, parent / "restored")
    assert len(list((tmp_path / "moved").glob(".autoflow-restore-*"))) == 1
    assert not list(parent.iterdir())


def test_oversized_metadata_rejected_before_allocation(tmp_path: Path) -> None:
    info = tarfile.TarInfo("metadata")
    info.type = tarfile.XHDTYPE
    info.size = 2 * 1024**2
    path = tmp_path / "metadata.tar.gz"
    path.write_bytes(gzip.compress(info.tobuf() + bytes(1024)))
    with pytest.raises(core.RestoreError, match="metadata"):
        core.restore_backup(path, tmp_path / "restored")
    assert not list(tmp_path.glob(".autoflow-restore-*"))


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a//b", "a\\b", "bad\nname"])
def test_pax_path_override_checked(tmp_path: Path, name: str) -> None:
    path = tmp_path / "pax.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("safe")
        info.pax_headers = {"path": name}
        archive.addfile(info)
    with pytest.raises(core.RestoreError, match="Unsafe"):
        core.restore_backup(path, tmp_path / "restored")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("where", ["parent", "ancestor"])
def test_shared_writable_output_ancestry_rejected(backup: Path, tmp_path: Path, where: str) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    if where == "ancestor":
        (parent / "private").mkdir()
        output = parent / "private/restored"
    else:
        output = parent / "restored"
    parent.chmod(0o777)
    try:
        with pytest.raises(core.RestoreError, match="ancestor|group/world-writable"):
            core.restore_backup(backup, output)
        assert not output.exists()
    finally:
        parent.chmod(0o700)


def test_archive_read_permission_error(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def denied(name, flags, *args, **kwargs):
        if name == backup.name:
            raise PermissionError("read denied")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(core.os, "open", denied)
    with pytest.raises(core.RestoreError, match="read denied"):
        core.restore_backup(backup, tmp_path / "restored")
    assert not list(tmp_path.glob(".autoflow-restore-*"))


def test_temporary_collision_preserves_unowned_directory(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    existing = tmp_path / ".autoflow-restore-fixed.part"
    existing.mkdir()
    (existing / "keep").write_text("keep")
    monkeypatch.setattr(core.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    with pytest.raises(core.RestoreError):
        core.restore_backup(backup, tmp_path / "restored")
    assert (existing / "keep").read_text() == "keep"


def test_cleanup_failure_is_explicit(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify = core._verify_staging
    calls = 0

    def cleanup_denied(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("cleanup identity check denied")
        return verify(*args)

    with monkeypatch.context() as patch:
        patch.setattr(core.os, "fsync", Mock(side_effect=OSError("write failed")))
        patch.setattr(core, "_verify_staging", cleanup_denied)
        with pytest.raises(core.RestoreError, match="cleanup identity check denied") as raised:
            core.restore_backup(backup, tmp_path / "restored")
        assert "write failed" in str(raised.value)
        assert "Cleanup refused" in str(raised.value)
    assert not (tmp_path / "restored").exists()
    assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1


def test_missing_parent_not_created(backup: Path, tmp_path: Path) -> None:
    with pytest.raises(core.RestoreError):
        core.restore_backup(backup, tmp_path / "missing/restored", dry_run=True)
    assert not (tmp_path / "missing").exists()


def test_staging_identity_is_pinned_through_publication(backup, tmp_path, monkeypatch):
    verify = core._verify_staging
    checks = []

    def record(parent, name, identity, descriptor):
        metadata = os.fstat(descriptor)
        assert (metadata.st_dev, metadata.st_ino) == identity
        checks.append(name)
        return verify(parent, name, identity, descriptor)

    monkeypatch.setattr(core, "_verify_staging", record)
    result = core.restore_backup(backup, tmp_path / "restored")
    assert result.success
    assert len(checks) == 3
    assert checks[0] == checks[1] and checks[0].startswith(".autoflow-restore-")
    assert checks[2] == "restored"
    assert not list(tmp_path.glob(".autoflow-restore-*"))


@pytest.mark.parametrize("replacement", ["directory", "symlink", "file", "missing"])
def test_staging_substitution_refuses_publication_and_preserves_trees(
    backup, tmp_path, monkeypatch, replacement
):
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_bytes(b"external data")
    before = marker.stat()
    original_scan = core._scan
    displaced = tmp_path / "original-staging"
    staged = []

    def substitute(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        if args[3:]:
            path = next(tmp_path.glob(".autoflow-restore-*"))
            staged.append(path)
            path.rename(displaced)
            if replacement == "directory":
                path.mkdir()
                (path / "keep.txt").write_bytes(b"replacement data")
            elif replacement == "symlink":
                path.symlink_to(external, target_is_directory=True)
            elif replacement == "file":
                path.write_bytes(b"replacement file")
        return result

    monkeypatch.setattr(core, "_scan", substitute)
    publisher = Mock(side_effect=AssertionError("substitute must not be published"))
    monkeypatch.setattr(core, "_rename_noreplace", publisher)
    monkeypatch.setattr(os, "unlink", Mock(side_effect=AssertionError("deletion attempted")))
    monkeypatch.setattr(
        os, "rmdir", Mock(side_effect=AssertionError("directory deletion attempted"))
    )
    with pytest.raises(core.RestoreError, match="Cleanup refused") as raised:
        core.restore_backup(backup, tmp_path / "restored")
    assert str(staged[0]) in str(raised.value)
    publisher.assert_not_called()
    assert not (tmp_path / "restored").exists()
    assert (displaced / "source/hello.txt").read_bytes() == b"hello\n"
    assert marker.read_bytes() == b"external data"
    assert marker.stat().st_ino == before.st_ino and marker.stat().st_mtime_ns == before.st_mtime_ns
    if replacement == "directory":
        assert (staged[0] / "keep.txt").read_bytes() == b"replacement data"
    elif replacement == "symlink":
        assert staged[0].is_symlink() and staged[0].readlink() == external
    elif replacement == "file":
        assert staged[0].read_bytes() == b"replacement file"
    else:
        assert not staged[0].exists()


@pytest.mark.parametrize("operation", ["stat", "open", "fstat"])
def test_staging_identity_failures_retain_unverified_directory(
    backup, tmp_path, monkeypatch, operation
):
    real_stat, real_open, real_fstat = os.stat, os.open, os.fstat
    stage_fds = set()

    def inspect(name, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".autoflow-restore-"):
            assert kwargs.get("follow_symlinks") is False
            if operation == "stat":
                raise PermissionError("staging stat denied")
        return real_stat(name, *args, **kwargs)

    def open_stage(name, flags, *args, **kwargs):
        staging = isinstance(name, str) and name.startswith(".autoflow-restore-")
        if staging and operation == "open":
            raise PermissionError("staging open denied")
        descriptor = real_open(name, flags, *args, **kwargs)
        if staging:
            assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
            stage_fds.add(descriptor)
        return descriptor

    def inspect_fd(descriptor):
        if descriptor in stage_fds and operation == "fstat":
            raise PermissionError("staging fstat denied")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "stat", inspect)
    monkeypatch.setattr(os, "open", open_stage)
    monkeypatch.setattr(os, "fstat", inspect_fd)
    with pytest.raises(core.RestoreError, match=f"staging {operation} denied") as raised:
        core.restore_backup(backup, tmp_path / "restored")
    assert "Cleanup refused" in str(raised.value)
    assert not (tmp_path / "restored").exists()
    assert len(list(tmp_path.glob(".autoflow-restore-*"))) == 1


def test_substitution_between_creation_and_open_is_rejected(backup, tmp_path, monkeypatch):
    real_open = os.open
    displaced = tmp_path / "created-staging"
    staged = []

    def substitute(name, flags, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".autoflow-restore-"):
            path = tmp_path / name
            staged.append(path)
            path.rename(displaced)
            path.mkdir()
            (path / "keep.txt").write_bytes(b"unowned staging")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", substitute)
    with pytest.raises(core.RestoreError, match="identity changed"):
        core.restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert list(displaced.iterdir()) == []
    assert list(staged[0].iterdir()) == [staged[0] / "keep.txt"]
    assert (staged[0] / "keep.txt").read_bytes() == b"unowned staging"


def test_cleanup_does_not_delete_substitute_after_identity_check(backup, tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_bytes(b"external data")
    displaced = tmp_path / "owned-staging"
    verify = core._verify_staging
    calls = 0
    substituted = []

    def race(parent, name, identity, descriptor):
        nonlocal calls
        calls += 1
        verify(parent, name, identity, descriptor)
        if calls == 2:
            # Initial verification is first; a write error leads directly to cleanup.
            path = tmp_path / name
            path.rename(displaced)
            path.symlink_to(external, target_is_directory=True)
            substituted.append(path)

    monkeypatch.setattr(core, "_verify_staging", race)
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("write failed")))
    monkeypatch.setattr(os, "unlink", Mock(side_effect=AssertionError("deletion attempted")))
    monkeypatch.setattr(
        os, "rmdir", Mock(side_effect=AssertionError("directory deletion attempted"))
    )
    with pytest.raises(core.RestoreError, match="write failed.*staging retained"):
        core.restore_backup(backup, tmp_path / "restored")
    assert substituted[0].is_symlink()
    assert displaced.is_dir()
    assert (external / "keep.txt").read_bytes() == b"external data"


def test_publication_detects_source_replaced_after_final_check(backup, tmp_path, monkeypatch):
    real_publish = core._rename_noreplace
    displaced = tmp_path / "owned-staging"
    output = tmp_path / "restored"

    def race(parent, temporary, final):
        path = tmp_path / temporary
        path.rename(displaced)
        path.mkdir()
        (path / "keep.txt").write_bytes(b"substitute tree")
        real_publish(parent, temporary, final)

    monkeypatch.setattr(core, "_rename_noreplace", race)
    with pytest.raises(core.RestoreError, match="identity changed"):
        core.restore_backup(backup, output)
    # No success and no attempted deletion, including of a substitute moved by the race.
    assert (output / "keep.txt").read_bytes() == b"substitute tree"
    assert (displaced / "source/hello.txt").read_bytes() == b"hello\n"


def test_archive_permissions_and_owners_are_not_applied(tmp_path: Path) -> None:
    path = tmp_path / "permissions.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("executable")
        info.mode = 0o6777
        info.uid = 0
        info.gid = 0
        archive.addfile(info)
    output = tmp_path / "restored"
    core.restore_backup(path, output)
    metadata = (output / "executable").stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_uid == os.geteuid()


@pytest.mark.parametrize("kwargs", [{"max_files": 0}, {"max_bytes": -1}])
def test_invalid_core_limits(backup: Path, tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(core.RestoreError, match="must be"):
        core.restore_backup(backup, tmp_path / "restored", **kwargs)


@pytest.mark.parametrize(
    "output", ["/", "/proc/restore", "/sys/restore", "/dev/restore", "/run/restore"]
)
def test_protected_destination_rejected_without_opening(
    backup: Path, output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_directory", Mock(side_effect=AssertionError("must not open")))
    with pytest.raises(core.RestoreError, match="Protected"):
        core.restore_backup(backup, Path(output))


def test_invalid_header_after_valid_entry_is_not_an_end_marker(tmp_path: Path) -> None:
    info = tarfile.TarInfo("valid")
    raw = info.tobuf() + b"invalid".ljust(512, b"x") + bytes(1024)
    archive = tmp_path / "bad-header.tar.gz"
    archive.write_bytes(gzip.compress(raw))
    with pytest.raises(core.RestoreError, match="end marker"):
        core.restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_sparse_pax_rejected_before_payload_processing(tmp_path: Path) -> None:
    path = tmp_path / "sparse.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("sparse")
        info.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
        archive.addfile(info)
    with pytest.raises(core.RestoreError, match="Sparse"):
        core.restore_backup(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
