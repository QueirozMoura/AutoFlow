"""Backup safety tests use temporary fixtures only."""

import hashlib
import os
import stat
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autoflow.core import backup as core


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "hello.txt").write_text("hello backup\n")
    (source / "nested" / "data.bin").write_bytes(bytes(range(256)))
    return source


def contents(path: Path) -> tuple[str, int, int]:
    metadata = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), metadata.st_mode, metadata.st_mtime_ns


def test_directory_archive_and_original_integrity(source: Path, tmp_path: Path) -> None:
    originals = {path: contents(path) for path in source.rglob("*") if path.is_file()}
    progress = Mock()

    result = core.create_backup(source, tmp_path / "backups", progress=progress)

    assert result.success and not result.dry_run
    assert result.file_count == 2
    assert result.original_bytes == sum(path.stat().st_size for path in originals)
    assert result.duration_seconds >= 0
    assert result.skipped == []
    archive_path = Path(result.archive)
    assert archive_path.name.startswith("autoflow-") and archive_path.name.endswith(".tar.gz")
    assert archive_path.stat().st_size == result.compressed_bytes
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive_path.parent.stat().st_mode) == 0o700
    assert not list(archive_path.parent.glob("*.part"))
    with tarfile.open(archive_path, "r:gz") as archive:
        assert set(archive.getnames()) == {
            "source",
            "source/hello.txt",
            "source/nested",
            "source/nested/data.bin",
            "source/empty",
        }
        for member in archive.getmembers():
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            if member.isfile():
                original = source.parent / member.name
                assert archive.extractfile(member).read() == original.read_bytes()
    assert originals == {path: contents(path) for path in originals}
    assert progress.call_args.args[:2] == (5, 5)


def test_single_file(source: Path, tmp_path: Path) -> None:
    result = core.create_backup(source / "hello.txt", tmp_path / "backups")

    assert result.files == ["hello.txt"]
    assert result.file_count == 1
    with tarfile.open(result.archive) as archive:
        assert archive.getnames() == ["hello.txt"]
        assert archive.extractfile("hello.txt").read() == b"hello backup\n"


def test_dry_run_has_no_filesystem_side_effects(source: Path, tmp_path: Path) -> None:
    before = sorted(tmp_path.rglob("*"))
    progress = Mock()
    result = core.create_backup(
        source, tmp_path / "missing" / "backups", dry_run=True, progress=progress
    )

    assert result.dry_run and result.archive is None and result.compressed_bytes is None
    assert result.file_count == 2
    assert result.original_bytes == 269
    assert sorted(tmp_path.rglob("*")) == before
    progress.assert_not_called()


def test_missing_source(tmp_path: Path) -> None:
    with pytest.raises(core.BackupError, match="No such file"):
        core.create_backup(tmp_path / "missing", tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_output_inside_source_is_rejected(source: Path, dry_run: bool) -> None:
    with pytest.raises(core.BackupError, match="outside"):
        core.create_backup(source, source / "backups", dry_run=dry_run)
    assert not (source / "backups").exists()


def test_output_equal_to_source(source: Path) -> None:
    with pytest.raises(core.BackupError, match="outside"):
        core.create_backup(source, source)


def test_output_is_a_file(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "backups"
    output.write_text("keep this")
    with pytest.raises(core.BackupError):
        core.create_backup(source, output)
    assert output.read_text() == "keep this"


def test_links_are_excluded_and_never_followed(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("never include")
    (source / "outside-link").symlink_to(outside, target_is_directory=True)
    (source / "inside-link").symlink_to(source / "hello.txt")
    (source / "cycle").symlink_to(source, target_is_directory=True)
    (source / "broken").symlink_to(tmp_path / "does-not-exist")

    result = core.create_backup(source, tmp_path / "backups")

    assert len(result.skipped) == 4
    assert all("symbolic link" in entry.reason for entry in result.skipped)
    with tarfile.open(result.archive) as archive:
        assert not any(member.issym() or member.islnk() for member in archive.getmembers())
        assert not any("private" in name or "link" in name for name in archive.getnames())


@pytest.mark.parametrize("location", ["source", "source_parent", "output", "output_parent"])
def test_symlink_path_components_rejected(source: Path, tmp_path: Path, location: str) -> None:
    alias = tmp_path / "alias"
    output = tmp_path / "backups"
    if location == "source":
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif location == "source_parent":
        alias.symlink_to(source.parent, target_is_directory=True)
        source = alias / source.name
    elif location == "output":
        output.symlink_to(source, target_is_directory=True)
    else:
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / "backups"
    with pytest.raises(core.BackupError):
        core.create_backup(source, output)
    assert not list(tmp_path.rglob("*.tar.gz"))


def test_unreadable_file_fails_before_creating_output(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open

    def denied(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "hello.txt":
            raise PermissionError("read denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(core.os, "open", denied)
    with pytest.raises(core.BackupError, match="read denied"):
        core.create_backup(source, tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_unreadable_directory(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core.os, "listdir", Mock(side_effect=PermissionError("directory denied")))
    with pytest.raises(core.BackupError, match="directory denied"):
        core.create_backup(source, tmp_path / "backups", dry_run=True)
    assert not (tmp_path / "backups").exists()


def test_destination_permission_failure(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core.os, "mkdir", Mock(side_effect=PermissionError("write denied")))
    with pytest.raises(core.BackupError, match="write denied"):
        core.create_backup(source, tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    result = core.create_backup(source, tmp_path / "backups")

    assert result.file_count == 0 and result.original_bytes == 0
    with tarfile.open(result.archive) as archive:
        assert archive.getnames() == ["empty"]
        assert archive.getmember("empty").isdir()


@pytest.mark.parametrize("failure", ["compression", "disk_full", "publish", "fsync"])
def test_failure_cleans_temporary_and_preserves_existing(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    output = tmp_path / "backups"
    output.mkdir()
    existing = output / "existing.tar.gz"
    existing.write_bytes(b"keep existing backup")
    if failure == "compression":
        monkeypatch.setattr(
            core.tarfile.TarFile,
            "addfile",
            Mock(side_effect=tarfile.TarError("compression failed")),
        )
    elif failure == "disk_full":
        monkeypatch.setattr(
            core.tarfile.TarFile, "addfile", Mock(side_effect=OSError(28, "disk full"))
        )
    elif failure == "fsync":
        monkeypatch.setattr(core.os, "fsync", Mock(side_effect=OSError("sync failed")))
    else:
        monkeypatch.setattr(core, "_rename_noreplace", Mock(side_effect=OSError("publish failed")))

    with pytest.raises(core.BackupError):
        core.create_backup(source, output)

    assert list(output.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep existing backup"


def test_colliding_final_name_is_not_overwritten(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "backups"
    output.mkdir()
    existing = output / "collision.tar.gz"
    existing.write_bytes(b"keep existing")
    monkeypatch.setattr(
        core, "_archive_name", Mock(side_effect=["collision.tar.gz", "unique.tar.gz"])
    )

    result = core.create_backup(source, output)

    assert Path(result.archive).name == "unique.tar.gz"
    assert existing.read_bytes() == b"keep existing"
    assert not list(output.glob("*.part"))


def test_exhausted_collisions_cleanup(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "backups"
    output.mkdir()
    existing = output / "collision.tar.gz"
    existing.write_bytes(b"keep existing")
    monkeypatch.setattr(core, "_archive_name", lambda: existing.name)

    with pytest.raises(core.BackupError, match="unique backup name"):
        core.create_backup(source, output)
    assert list(output.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep existing"


def test_temporary_collision_preserves_unowned_file(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "backups"
    output.mkdir()
    existing = output / ".fixed.tar.gz.part"
    existing.write_bytes(b"not ours")
    monkeypatch.setattr(core.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    with pytest.raises(core.BackupError, match="unique temporary"):
        core.create_backup(source, output)
    assert existing.read_bytes() == b"not ours"
    assert list(output.iterdir()) == [existing]


def test_simultaneous_backups_are_distinct(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "backups"
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: core.create_backup(source, output), range(4)))

    assert len({result.archive for result in results}) == 4
    for result in results:
        with tarfile.open(result.archive) as archive:
            assert archive.extractfile("source/hello.txt").read() == b"hello backup\n"
    assert len(list(output.iterdir())) == 4


@pytest.mark.parametrize("change", ["modify", "delete", "replace_with_link", "add_file"])
def test_source_changes_cancel_backup(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    real_addfile = tarfile.TarFile.addfile
    changed = False

    def change_during_copy(
        archive: tarfile.TarFile, info: tarfile.TarInfo, fileobj: object = None
    ) -> None:
        nonlocal changed
        real_addfile(archive, info, fileobj)
        if info.name == "source/hello.txt" and not changed:
            changed = True
            target = source / "hello.txt"
            if change == "modify":
                target.write_text("modified during backup")
            elif change == "delete":
                target.unlink()
            elif change == "replace_with_link":
                target.unlink()
                target.symlink_to(source / "nested" / "data.bin")
            else:
                (source / "new.txt").write_text("new entry")

    monkeypatch.setattr(core.tarfile.TarFile, "addfile", change_during_copy)
    with pytest.raises(core.BackupError):
        core.create_backup(source, tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


def test_parent_replaced_by_link_is_not_followed(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = core._write_archive
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"private data")

    def replace_parent(*args: object) -> None:
        (source / "nested").rename(source / "old-nested")
        (source / "nested").symlink_to(outside, target_is_directory=True)
        real_write(*args)

    monkeypatch.setattr(core, "_write_archive", replace_parent)
    with pytest.raises(core.BackupError):
        core.create_backup(source, tmp_path / "backups")
    assert not list((tmp_path / "backups").iterdir())


def test_same_device_nested_mount_is_excluded(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_mount_points", lambda: {source / "nested"})
    result = core.create_backup(source, tmp_path / "backups")

    assert result.file_count == 1
    assert result.skipped == [
        core.SkippedEntry("source/nested", "nested mount or different filesystem")
    ]
    with tarfile.open(result.archive) as archive:
        assert "source/nested/data.bin" not in archive.getnames()


def test_different_device_is_excluded(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stat = core.os.stat

    def other_device(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == "nested":
            values = list(result)
            values[2] += 1  # st_dev
            return os.stat_result(values)
        return result

    monkeypatch.setattr(core.os, "stat", other_device)
    result = core.create_backup(source, tmp_path / "backups")
    assert result.file_count == 1
    assert result.skipped[0].path == "source/nested"


def test_fifo_is_not_opened(source: Path, tmp_path: Path) -> None:
    os.mkfifo(source / "pipe")
    result = core.create_backup(source, tmp_path / "backups")
    assert core.SkippedEntry("source/pipe", "special file (not read)") in result.skipped
    with tarfile.open(result.archive) as archive:
        assert "source/pipe" not in archive.getnames()


@pytest.mark.parametrize("path", ["/", "/proc", "/proc/self", "/sys", "/dev", "/run"])
def test_protected_sources_rejected_without_reading(
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discover = Mock(side_effect=AssertionError("must not discover protected paths"))
    monkeypatch.setattr(core, "_discover", discover)
    with pytest.raises(core.BackupError, match="Protected"):
        core.create_backup(Path(path), tmp_path / "backups")
    discover.assert_not_called()


def test_hardlinks_are_stored_as_independent_regular_files(source: Path, tmp_path: Path) -> None:
    os.link(source / "hello.txt", source / "hardlink")
    result = core.create_backup(source, tmp_path / "backups")
    with tarfile.open(result.archive) as archive:
        for name in ("source/hello.txt", "source/hardlink"):
            assert archive.getmember(name).isfile()
            assert archive.extractfile(name).read() == b"hello backup\n"


def test_cancellation_cleans_temp(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_write_archive", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        core.create_backup(source, tmp_path / "backups")
    assert not list((tmp_path / "backups").iterdir())


def test_mount_table_failure_is_fatal(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        core, "_mount_points", Mock(side_effect=PermissionError("mount table denied"))
    )
    with pytest.raises(core.BackupError, match="mount table denied"):
        core.create_backup(source, tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_destination_moved_during_copy(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "backups"
    moved = tmp_path / "moved"
    real_write = core._write_archive

    def move_output(*args: object) -> None:
        real_write(*args)
        output.rename(moved)
        output.mkdir()

    monkeypatch.setattr(core, "_write_archive", move_output)
    with pytest.raises(core.BackupError, match="Destination changed"):
        core.create_backup(source, output)
    assert list(output.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_rename_unavailable_fails_closed(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(core.BackupError, match="rename is unavailable"):
        core.create_backup(source, tmp_path / "backups")
    assert not list((tmp_path / "backups").iterdir())


def test_concurrent_forced_name_collision_never_overwrites(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_archive_name", lambda: "same-name.tar.gz")

    def attempt(_: int) -> core.BackupResult | None:
        try:
            return core.create_backup(source, tmp_path / "backups")
        except core.BackupError as exc:
            assert "unique backup name" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(result is not None for result in results) == 1
    assert [path.name for path in (tmp_path / "backups").iterdir()] == ["same-name.tar.gz"]
    with tarfile.open(tmp_path / "backups" / "same-name.tar.gz") as archive:
        assert archive.extractfile("source/hello.txt").read() == b"hello backup\n"


@pytest.mark.parametrize("dry_run", [False, True])
def test_shared_writable_destination_is_rejected(
    source: Path, tmp_path: Path, dry_run: bool
) -> None:
    output = tmp_path / "backups"
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    with pytest.raises(core.BackupError, match="group/world-writable"):
        core.create_backup(source, output, dry_run=dry_run)
    assert list(output.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files regardless of mode bits")
def test_actual_unreadable_file(source: Path, tmp_path: Path) -> None:
    target = source / "hello.txt"
    target.chmod(0)
    try:
        with pytest.raises(core.BackupError, match="Permission denied"):
            core.create_backup(source, tmp_path / "backups")
        assert not (tmp_path / "backups").exists()
    finally:
        target.chmod(0o600)


def test_source_parent_replaced_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = parent / "source"
    source.write_text("original")
    real_write = core._write_archive

    def replace_parent(*args: object) -> None:
        real_write(*args)
        parent.rename(tmp_path / "moved")
        parent.mkdir()
        source.write_text("replacement")

    monkeypatch.setattr(core, "_write_archive", replace_parent)
    with pytest.raises(core.BackupError, match="Source parent changed"):
        core.create_backup(source, tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


def test_empty_file(tmp_path: Path) -> None:
    source = tmp_path / "empty.txt"
    source.touch()
    result = core.create_backup(source, tmp_path / "backups")
    assert result.file_count == 1 and result.original_bytes == 0
    with tarfile.open(result.archive) as archive:
        assert archive.extractfile("empty.txt").read() == b""


def test_replaced_temporary_is_not_published_or_deleted(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "backups"
    real_write = core._write_archive

    def replace_temp(*args: object) -> None:
        real_write(*args)
        temporary = next(output.glob("*.part"))
        temporary.unlink()
        temporary.write_bytes(b"not owned by this backup")

    monkeypatch.setattr(core, "_write_archive", replace_temp)
    with pytest.raises(core.BackupError, match="refusing to remove"):
        core.create_backup(source, output)
    remaining = list(output.iterdir())
    assert len(remaining) == 1 and remaining[0].name.endswith(".part")
    assert remaining[0].read_bytes() == b"not owned by this backup"


def test_cleanup_failure_is_reported(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_write_archive", Mock(side_effect=OSError("copy failed")))
    monkeypatch.setattr(core.os, "unlink", Mock(side_effect=PermissionError("cleanup denied")))
    with pytest.raises(core.BackupError, match="Could not remove temporary archive"):
        core.create_backup(source, tmp_path / "backups")
    assert not list((tmp_path / "backups").glob("*.tar.gz"))
    assert len(list((tmp_path / "backups").glob("*.part"))) == 1
