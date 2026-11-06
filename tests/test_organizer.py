"""Organization planning inspects temporary fixtures and never executes moves."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoflow.core import organizer as core


def test_plan_groups_direct_regular_files(tmp_path: Path) -> None:
    names = ["report.TXT", "notes.txt", "archive.tar.gz", "LICENSE", ".env", "café.PDF"]
    for name in names:
        (tmp_path / name).write_text(f"contents of {name}")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "not-planned.txt").write_text("nested content")

    plan = core.plan_organization(tmp_path)

    assert plan.is_valid and plan.issues == ()
    assert plan.source == tmp_path
    assert [entry.source.name for entry in plan.entries] == sorted(names)
    assert set(plan.groups) == {"txt", "gz", "no_extension", "pdf"}
    assert len(plan.groups["txt"]) == 2
    assert {entry.source.name for entry in plan.groups["no_extension"]} == {"LICENSE", ".env"}
    for entry in plan.entries:
        assert entry.source == tmp_path / entry.source.name
        assert entry.destination == tmp_path / entry.extension / entry.source.name
        assert entry.destination.is_relative_to(tmp_path)
    assert not (tmp_path / "txt").exists()


@pytest.mark.parametrize(
    "name,extension",
    [
        ("image.JPG", "jpg"),
        ("archive.tar.gz", "gz"),
        (".config.json", "json"),
        (".gitignore", "no_extension"),
        ("README", "no_extension"),
        ("trailing.", "no_extension"),
        ("multiple..", "no_extension"),
        ("file.no_extension", "no_extension"),
    ],
)
def test_extension_policy(tmp_path: Path, name: str, extension: str) -> None:
    source = tmp_path / name
    source.touch()
    plan = core.plan_organization(tmp_path)
    assert plan.entries == (core.PlannedMove(source, tmp_path / extension / name, extension),)
    assert plan.is_valid


def test_empty_directory(tmp_path: Path) -> None:
    plan = core.plan_organization(tmp_path)
    assert plan.is_valid and plan.entries == () and plan.groups == {}
    assert list(tmp_path.iterdir()) == []


def test_symbolic_links_and_special_entries_are_ignored(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    real = source / "real.txt"
    real.write_text("keep")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "external.txt").symlink_to(outside)
    (source / "internal.txt").symlink_to(real)
    (source / "broken.txt").symlink_to(tmp_path / "missing")
    (source / "cycle").symlink_to(source, target_is_directory=True)
    os.mkfifo(source / "pipe.txt")

    plan = core.plan_organization(source)

    assert plan.is_valid
    assert [entry.source for entry in plan.entries] == [real]
    assert outside.read_text() == "outside"


@pytest.mark.parametrize("kind", ["missing", "file", "link", "parent_link", "fifo"])
def test_invalid_sources(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    if kind == "file":
        source.write_text("not a directory")
    elif kind == "link":
        source.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "parent_link":
        source.symlink_to(tmp_path, target_is_directory=True)
        source = source / "child"
    elif kind == "fifo":
        os.mkfifo(source)
    with pytest.raises(core.OrganizerError):
        core.plan_organization(source)


def test_parent_traversal_rejected_before_opening(tmp_path: Path, monkeypatch) -> None:
    opener = Mock(side_effect=AssertionError("must not inspect paths"))
    monkeypatch.setattr(core, "_directory", opener)
    with pytest.raises(core.OrganizerError, match="parent traversal"):
        core.plan_organization(tmp_path / "child" / "..")
    opener.assert_not_called()


def test_null_byte_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(core.OrganizerError):
        core.plan_organization(tmp_path / "invalid\0path")


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken_link"])
def test_existing_destination_is_a_collision(tmp_path: Path, kind: str) -> None:
    original = tmp_path / "report.txt"
    original.write_text("original")
    category = tmp_path / "txt"
    category.mkdir()
    target = category / original.name
    if kind == "file":
        target.write_text("existing")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(original if kind == "symlink" else tmp_path / "missing")

    plan = core.plan_organization(tmp_path)

    assert not plan.is_valid
    assert plan.issues == (core.PlanIssue(target, "Destination already exists"),)
    assert len(plan.entries) == 1
    assert original.read_text() == "original"
    if kind == "file":
        assert target.read_text() == "existing"
    if kind in ("symlink", "broken_link"):
        assert target.is_symlink()


@pytest.mark.parametrize("kind", ["file", "symlink", "broken_link", "fifo"])
def test_blocked_category_is_a_collision(tmp_path: Path, kind: str) -> None:
    (tmp_path / "report.txt").write_text("keep")
    category = tmp_path / "txt"
    if kind == "file":
        category.write_text("also an immediate regular file")
    elif kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        category.symlink_to(outside, target_is_directory=True)
    elif kind == "broken_link":
        category.symlink_to(tmp_path / "missing")
    else:
        os.mkfifo(category)
    plan = core.plan_organization(tmp_path)
    assert not plan.is_valid
    assert core.PlanIssue(category, "Destination category is not a real directory") in plan.issues


def test_existing_real_category_is_allowed_without_recursing(tmp_path: Path) -> None:
    (tmp_path / "report.txt").write_text("keep")
    category = tmp_path / "txt"
    category.mkdir()
    (category / "unrelated.txt").write_text("not planned")
    (category / "nested").mkdir()
    plan = core.plan_organization(tmp_path)
    assert plan.is_valid and len(plan.entries) == 1
    assert plan.entries[0].destination == category / "report.txt"


def test_reports_multiple_collisions(tmp_path: Path) -> None:
    (tmp_path / "txt").mkdir()
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("original")
        (tmp_path / "txt" / name).write_text("existing")
    plan = core.plan_organization(tmp_path)
    assert not plan.is_valid and len(plan.issues) == 2
    assert [issue.path.name for issue in plan.issues] == ["a.txt", "b.txt"]


def test_invalid_normalized_category_length(tmp_path: Path) -> None:
    # U+0130 takes two UTF-8 bytes; its lowercase form takes three.
    original = tmp_path / ("file." + "İ" * 100)
    original.touch()
    plan = core.plan_organization(tmp_path)
    assert not plan.is_valid
    assert plan.issues[0].reason == "Invalid destination path"
    assert original.exists()


def test_control_character_name_invalidates_plan(tmp_path: Path) -> None:
    original = tmp_path / "bad\nname.txt"
    original.touch()
    plan = core.plan_organization(tmp_path)
    assert not plan.is_valid
    assert plan.issues == (
        core.PlanIssue(tmp_path / "txt" / original.name, "Invalid destination path"),
    )


def test_source_listing_failure_is_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(core.os, "listdir", Mock(side_effect=PermissionError("listing denied")))
    with pytest.raises(core.OrganizerError, match="listing denied"):
        core.plan_organization(tmp_path)


def test_destination_inspection_failure_is_explicit(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").touch()
    real_stat = os.stat

    def denied(name, *args, **kwargs):
        if name == "txt":
            raise PermissionError("inspection denied")
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(core.os, "stat", denied)
    with pytest.raises(core.OrganizerError, match="inspection denied"):
        core.plan_organization(tmp_path)


def test_relative_source(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").touch()
    monkeypatch.chdir(tmp_path)
    plan = core.plan_organization(Path("."))
    assert plan.source == tmp_path
    assert plan.entries[0].source == tmp_path / "report.txt"


def test_plan_has_no_mutations_or_file_content_reads(tmp_path: Path, monkeypatch) -> None:
    original = tmp_path / "report.txt"
    original.write_bytes(b"preserve original content")
    original.chmod(0o640)
    (tmp_path / "txt").mkdir()
    before_paths = sorted(tmp_path.rglob("*"))
    before_metadata = {path: path.stat() for path in [tmp_path, *before_paths]}
    before_content = original.read_bytes()
    real_open = os.open

    def readonly(name, flags, *args, **kwargs):
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert name != original.name  # No file contents are needed for planning.
        return real_open(name, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(core.os, "open", readonly)
        for operation in ("mkdir", "rename", "replace", "unlink", "remove"):
            patch.setattr(core.os, operation, Mock(side_effect=AssertionError("write attempted")))
        assert core.plan_organization(tmp_path).is_valid

    assert sorted(tmp_path.rglob("*")) == before_paths
    assert original.read_bytes() == before_content
    for path, metadata in before_metadata.items():
        after = path.stat()
        assert (
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) == (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
