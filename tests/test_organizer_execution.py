"""Safe organizer execution and injected failures use temporary fixtures only."""

import errno
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autoflow.core import organizer as core


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"first original")
    (source / "b.txt").write_bytes(b"second original")
    return source


def test_moves_files_preserving_content_and_metadata(source: Path) -> None:
    (source / "photo.JPG").write_bytes(b"synthetic photo")
    (source / "README").write_bytes(b"no extension")
    (source / ".env").write_bytes(b"synthetic hidden")
    originals = {path: (path.read_bytes(), path.stat()) for path in source.iterdir()}

    result = core.execute_organization(source)

    assert result.success and not result.partial and result.errors == ()
    assert len(result.moved) == 5 and result.skipped == ()
    assert set(result.created_directories) == {
        source / "txt",
        source / "jpg",
        source / "no_extension",
    }
    for entry in result.moved:
        contents, metadata = originals[entry.source]
        assert not entry.source.exists()
        assert entry.destination.read_bytes() == contents
        current = entry.destination.stat()
        assert (current.st_ino, current.st_mode, current.st_mtime_ns) == (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_mtime_ns,
        )
        assert entry.destination.is_relative_to(source)
    for directory in result.created_directories:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert source.is_dir()


def test_existing_category_and_original_directories_retained(source: Path) -> None:
    category = source / "txt"
    category.mkdir(mode=0o750)
    (category / "keep.txt").write_bytes(b"keep existing")
    nested = source / "nested"
    nested.mkdir()
    (nested / "unplanned.txt").write_bytes(b"nested original")
    result = core.execute_organization(source)
    assert result.success and result.created_directories == ()
    assert len(result.moved) == 2
    assert stat.S_IMODE(category.stat().st_mode) == 0o750
    assert (category / "keep.txt").read_bytes() == b"keep existing"
    assert (nested / "unplanned.txt").read_bytes() == b"nested original"
    assert {entry.path for entry in result.skipped} == {category, nested}


def test_empty_source(tmp_path: Path) -> None:
    result = core.execute_organization(tmp_path)
    assert result.success and not result.partial
    assert result.moved == result.skipped == result.errors == result.created_directories == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "directory", "link", "broken_link"])
def test_collision_aborts_all_moves(source: Path, kind: str) -> None:
    category = source / "txt"
    category.mkdir()
    target = category / "b.txt"  # Last entry, so an unsafe per-file loop would move a.txt first.
    if kind == "file":
        target.write_bytes(b"existing")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(source / ("a.txt" if kind == "link" else "missing"))
    result = core.execute_organization(source)
    assert not result.success and not result.partial
    assert result.moved == () and len(result.errors) == 1
    assert "already exists" in result.errors[0].reason
    assert {entry.path.name for entry in result.skipped} == {"a.txt", "b.txt"}
    assert (source / "a.txt").read_bytes() == b"first original"
    assert (source / "b.txt").read_bytes() == b"second original"
    if kind == "file":
        assert target.read_bytes() == b"existing"
    elif kind in ("link", "broken_link"):
        assert target.is_symlink()


def test_discovered_links_and_special_files_are_skipped(source: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside original")
    (source / "external.txt").symlink_to(outside)
    (source / "internal.txt").symlink_to(source / "a.txt")
    (source / "broken.txt").symlink_to(tmp_path / "missing")
    os.mkfifo(source / "pipe")
    result = core.execute_organization(source)
    assert result.success and len(result.moved) == 2
    assert {item.path.name for item in result.skipped} == {
        "external.txt",
        "internal.txt",
        "broken.txt",
        "pipe",
    }
    assert all(
        (source / name).is_symlink() for name in ("external.txt", "internal.txt", "broken.txt")
    )
    assert outside.read_bytes() == b"outside original"
    assert not (source / "txt/external.txt").exists()


@pytest.mark.parametrize("location", ["source", "ancestor", "category"])
def test_symlink_paths_rejected_without_moves(source: Path, tmp_path: Path, location: str) -> None:
    requested = source
    alias = tmp_path / "alias"
    if location == "source":
        alias.symlink_to(source, target_is_directory=True)
        requested = alias
    elif location == "ancestor":
        alias.symlink_to(tmp_path, target_is_directory=True)
        requested = alias / source.name
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (source / "txt").symlink_to(outside, target_is_directory=True)
    result = core.execute_organization(requested)
    assert not result.success and result.moved == () and result.errors
    assert (source / "a.txt").read_bytes() == b"first original"
    assert (source / "b.txt").read_bytes() == b"second original"


@pytest.mark.parametrize("path_kind", ["missing", "file", "traversal"])
def test_invalid_sources_return_errors(tmp_path: Path, path_kind: str) -> None:
    source = tmp_path / "invalid"
    if path_kind == "file":
        source.write_bytes(b"original")
    elif path_kind == "traversal":
        source = tmp_path / "child" / ".."
    result = core.execute_organization(source)
    assert not result.success and result.errors
    assert result.moved == () and result.created_directories == ()


def test_invalid_destination_path_aborts(source: Path) -> None:
    original = source / "invalid\nname.txt"
    original.touch()
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert any("Invalid destination" in issue.reason for issue in result.errors)
    assert original.exists() and not (source / "txt").exists()


def test_replanning_catches_new_collision(source: Path, monkeypatch) -> None:
    real_plan = core.plan_organization
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "txt").mkdir()
            (source / "txt/b.txt").write_bytes(b"new collision")
        return real_plan(path)

    monkeypatch.setattr(core, "plan_organization", changed)
    result = core.execute_organization(source)
    assert calls == 2 and not result.success and result.moved == ()
    assert (source / "a.txt").exists() and (source / "b.txt").exists()
    assert (source / "txt/b.txt").read_bytes() == b"new collision"


def test_replanning_catches_added_file(source: Path, monkeypatch) -> None:
    real_plan = core.plan_organization
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "new.txt").touch()
        return real_plan(path)

    monkeypatch.setattr(core, "plan_organization", changed)
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert "plan changed" in result.errors[0].reason
    assert not (source / "txt").exists()


def test_post_preparation_collision_prevents_first_move(source: Path, monkeypatch) -> None:
    real_check = core._check_attachments
    injected = False

    def collide(*args):
        nonlocal injected
        if not injected:
            (source / "txt/b.txt").write_bytes(b"preflight collision")
            injected = True
        real_check(*args)

    monkeypatch.setattr(core, "_check_attachments", collide)
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert (source / "a.txt").exists() and (source / "b.txt").exists()
    assert (source / "txt/b.txt").read_bytes() == b"preflight collision"


def test_atomic_rename_never_overwrites_racing_target(source: Path, monkeypatch) -> None:
    real_move = core._move_noreplace

    def collide(root, name, category, target):
        (source / "txt" / target).write_bytes(b"racing original")
        real_move(root, name, category, target)

    monkeypatch.setattr(core, "_move_noreplace", collide)
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert (source / "txt/a.txt").read_bytes() == b"racing original"
    assert (source / "a.txt").read_bytes() == b"first original"


@pytest.mark.parametrize("phase", ["mkdir", "move"])
def test_permission_errors_are_structured(source: Path, monkeypatch, phase: str) -> None:
    if phase == "mkdir":
        monkeypatch.setattr(core.os, "mkdir", Mock(side_effect=PermissionError("mkdir denied")))
    else:
        monkeypatch.setattr(
            core, "_move_noreplace", Mock(side_effect=PermissionError("move denied"))
        )
    result = core.execute_organization(source)
    assert not result.success and result.moved == () and result.errors
    assert "denied" in result.errors[0].reason
    assert (source / "a.txt").exists() and (source / "b.txt").exists()


def test_nonwritable_last_category_prevents_all_moves(source: Path) -> None:
    (source / "last.zip").touch()
    category = source / "zip"
    category.mkdir(mode=0o500)
    try:
        result = core.execute_organization(source)
        assert not result.success and result.moved == ()
        assert "permissions" in result.errors[0].reason
        assert (source / "a.txt").exists() and (source / "last.zip").exists()
    finally:
        category.chmod(0o700)


@pytest.mark.parametrize("directory", ["source", "category"])
def test_shared_writable_directories_rejected(source: Path, directory: str) -> None:
    target = source if directory == "source" else source / "txt"
    if directory == "category":
        target.mkdir()
    target.chmod(0o777)
    try:
        result = core.execute_organization(source)
        assert not result.success and result.moved == ()
        assert "group/world-writable" in result.errors[0].reason
    finally:
        target.chmod(0o700)


def test_partial_move_failure_preserves_completed_and_pending_files(
    source: Path, monkeypatch
) -> None:
    real_move = core._move_noreplace
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected movement failure")
        real_move(*args)

    monkeypatch.setattr(core, "_move_noreplace", fail_second)
    result = core.execute_organization(source)
    assert not result.success and result.partial and len(result.moved) == 1
    assert result.moved[0].source == source / "a.txt"
    assert (source / "txt/a.txt").read_bytes() == b"first original"
    assert (source / "b.txt").read_bytes() == b"second original"
    assert not (source / "txt/b.txt").exists()
    assert {item.path for item in result.skipped} == {source / "b.txt"}
    assert result.errors and result.created_directories == (source / "txt",)


def test_late_concurrent_collision_is_a_partial_failure(source: Path, monkeypatch) -> None:
    real_move = core._move_noreplace

    def race(root, name, category, target):
        real_move(root, name, category, target)
        (source / "txt/b.txt").write_bytes(b"late collision")

    monkeypatch.setattr(core, "_move_noreplace", race)
    result = core.execute_organization(source)
    assert not result.success and result.partial and len(result.moved) == 1
    assert (source / "txt/a.txt").read_bytes() == b"first original"
    assert (source / "b.txt").read_bytes() == b"second original"
    assert (source / "txt/b.txt").read_bytes() == b"late collision"


def test_source_replaced_with_link_after_replanning_is_not_moved(source: Path, monkeypatch) -> None:
    real_mkdir = os.mkdir
    changed = False

    def replace_file(*args, **kwargs):
        nonlocal changed
        if not changed:
            (source / "a.txt").rename(source / "saved-original")
            (source / "a.txt").symlink_to(source / "b.txt")
            changed = True
        return real_mkdir(*args, **kwargs)

    monkeypatch.setattr(core.os, "mkdir", replace_file)
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert (source / "a.txt").is_symlink()
    assert (source / "saved-original").read_bytes() == b"first original"
    assert not (source / "txt/a.txt").exists()


def test_category_replaced_with_link_after_open_is_rejected(
    source: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    real_check = core._check_attachments
    changed = False

    def replace_category(*args):
        nonlocal changed
        if not changed:
            (source / "txt").rename(source / "saved-category")
            (source / "txt").symlink_to(outside, target_is_directory=True)
            changed = True
        real_check(*args)

    monkeypatch.setattr(core, "_check_attachments", replace_category)
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert list(outside.iterdir()) == []
    assert (source / "a.txt").exists()


def test_forged_unsafe_plan_is_rejected(source: Path, tmp_path: Path, monkeypatch) -> None:
    plan = core.plan_organization(source)
    forged = replace(plan.entries[0], destination=tmp_path / "outside.txt")
    monkeypatch.setattr(core, "plan_organization", lambda _: replace(plan, entries=(forged,)))
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert "Unsafe" in result.errors[0].reason
    assert not (tmp_path / "outside.txt").exists()


def test_unsupported_rename_fails_without_fallback(source: Path, monkeypatch) -> None:
    monkeypatch.setattr(core.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert "unavailable" in result.errors[0].reason
    assert (source / "a.txt").read_bytes() == b"first original"


def test_cross_device_failure_does_not_copy_or_delete(source: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        core, "_move_noreplace", Mock(side_effect=OSError(errno.EXDEV, "cross device"))
    )
    result = core.execute_organization(source)
    assert not result.success and result.moved == ()
    assert (source / "a.txt").read_bytes() == b"first original"
    assert not (source / "txt/a.txt").exists()


def test_hardlinked_regular_files_preserve_identity(source: Path) -> None:
    os.link(source / "a.txt", source / "copy.txt")
    inode = (source / "a.txt").stat().st_ino
    result = core.execute_organization(source)
    assert result.success and len(result.moved) == 3
    assert (source / "txt/a.txt").stat().st_ino == inode
    assert (source / "txt/copy.txt").stat().st_ino == inode


def test_execution_never_uses_delete_operations(source: Path, monkeypatch) -> None:
    for operation in ("unlink", "remove", "rmdir", "removedirs"):
        monkeypatch.setattr(
            core.os, operation, Mock(side_effect=AssertionError("delete attempted"))
        )
    assert core.execute_organization(source).success


def test_reexecution_is_empty_and_keeps_files(source: Path) -> None:
    first = core.execute_organization(source)
    second = core.execute_organization(source)
    assert first.success and second.success
    assert second.moved == () and second.created_directories == ()
    assert (source / "txt/a.txt").read_bytes() == b"first original"
    assert (source / "txt/b.txt").read_bytes() == b"second original"
