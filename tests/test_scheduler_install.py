"""Install only into tmp_path, with injected failures and no systemd processes."""

import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoflow.core import scheduler as core


@pytest.fixture
def installation(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"original")
    executable = tmp_path / "autoflow"
    executable.write_text("fixture only; never executed")
    executable.chmod(0o700)
    plan = core.plan_backup_schedule(source, tmp_path / "backups", "daily", executable=executable)
    directory = tmp_path / "units"
    directory.mkdir(mode=0o700)
    return plan, directory


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_install_content_permissions_and_result(installation, frequency, monkeypatch):
    plan, directory = installation
    plan = core.plan_backup_schedule(
        plan.source, plan.output, frequency, executable=Path(plan.command[0])
    )
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("process attempted")))
    monkeypatch.setattr(os, "system", Mock(side_effect=AssertionError("shell attempted")))
    result = core.install_backup_schedule(plan, directory)
    assert result.success and result.errors == ()
    assert result.paths == (directory / plan.service.name, directory / plan.timer.name)
    for unit, path in zip((plan.service, plan.timer), result.paths, strict=True):
        assert path.read_text() == unit.content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(directory.iterdir()) == set(result.paths)
    assert (plan.source / "keep.txt").read_bytes() == b"original"
    assert not plan.output.exists()


@pytest.mark.parametrize("unit_kind", ["service", "timer"])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling", "fifo"])
def test_collision_preserves_all_existing_entries(installation, tmp_path, unit_kind, kind):
    plan, directory = installation
    unit = getattr(plan, unit_kind)
    collision = directory / unit.name
    target = tmp_path / "untouched"
    target.write_bytes(b"preexisting")
    if kind == "file":
        collision.write_bytes(b"preexisting")
    elif kind == "directory":
        collision.mkdir()
    elif kind == "fifo":
        os.mkfifo(collision)
    else:
        collision.symlink_to(target if kind == "symlink" else tmp_path / "absent")
    before = collision.lstat()
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and result.errors and result.paths == ()
    assert list(directory.iterdir()) == [collision]
    assert collision.lstat() == before
    assert target.read_bytes() == b"preexisting"


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o702, 0o500, 0o600])
def test_unsafe_directory_permissions(installation, mode):
    plan, directory = installation
    directory.chmod(mode)
    try:
        result = core.install_backup_schedule(plan, directory)
        assert not result.success and result.errors and result.paths == ()
    finally:
        directory.chmod(0o700)
    assert list(directory.iterdir()) == []


def test_foreign_directory_owner(installation, monkeypatch):
    plan, directory = installation
    uid = os.geteuid()
    # Patch only destination validation, so source/output planning is unaffected.
    validate = core._validate_output

    def foreign_owner(descriptor):
        with monkeypatch.context() as patch:
            patch.setattr(os, "geteuid", lambda: uid + 1)
            validate(descriptor)

    monkeypatch.setattr(core, "_validate_output", foreign_owner)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and "owned" in result.errors[0]
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "file",
        "symlink",
        "ancestor_symlink",
        "traversal",
        "control",
        "empty",
        "root",
        "protected",
    ],
)
def test_invalid_destination(installation, tmp_path, kind):
    plan, directory = installation
    if kind == "missing":
        destination = tmp_path / "absent" / "units"
    elif kind == "file":
        destination = tmp_path / "file"
        destination.write_bytes(b"keep")
    elif kind in ("symlink", "ancestor_symlink"):
        destination = tmp_path / "link"
        destination.symlink_to(directory if kind == "symlink" else tmp_path)
        if kind == "ancestor_symlink":
            destination /= "units"
    else:
        destination = {
            "traversal": str(directory / ".." / "units"),
            "control": str(directory) + "\nunsafe",
            "empty": "",
            "root": "/",
            "protected": "/proc/units",
        }[kind]
    result = core.install_backup_schedule(plan, destination)
    assert not result.success and result.errors and result.paths == ()
    assert list(directory.iterdir()) == []
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "change",
    [
        "object",
        "service_path",
        "service_content",
        "timer_path",
        "timer_content",
        "scope",
        "frequency",
        "command",
        "source",
    ],
)
def test_invalid_plan_never_writes(installation, tmp_path, change):
    plan, directory = installation
    altered = {
        "object": None,
        "service_path": replace(plan, service=replace(plan.service, name="../escape.service")),
        "service_content": replace(plan, service=replace(plan.service, content="ExecStart=evil")),
        "timer_path": replace(plan, timer=replace(plan.timer, name="/escape.timer")),
        "timer_content": replace(plan, timer=replace(plan.timer, content="OnCalendar=hourly")),
        "scope": replace(plan, scope="system"),
        "frequency": replace(plan, frequency="hourly"),
        "command": replace(plan, command=()),
        "source": replace(plan, source=tmp_path / "absent"),
    }[change]
    result = core.install_backup_schedule(altered, directory)
    assert not result.success and result.errors and result.paths == ()
    assert list(directory.iterdir()) == []


def test_stale_plan_is_revalidated(installation):
    plan, directory = installation
    Path(plan.command[0]).chmod(0o600)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and result.errors
    assert list(directory.iterdir()) == []


def test_installation_and_activation_share_plan_validator(installation, monkeypatch):
    plan, directory = installation
    validator = Mock(side_effect=core.SchedulerError("shared validation rejected plan"))
    process = Mock(side_effect=AssertionError("systemctl attempted"))
    monkeypatch.setattr(core, "_validate_backup_schedule_plan", validator)
    monkeypatch.setattr(subprocess, "run", process)
    installed = core.install_backup_schedule(plan, directory)
    activated = core.activate_backup_schedule(plan, unit_directory=directory)
    assert not installed.success and not activated.success
    assert all(
        "shared validation rejected plan" in result.errors[0] for result in (installed, activated)
    )
    assert validator.call_count == 2
    process.assert_not_called()
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("operation", ["timer_open", "fchmod", "write", "fsync", "directory_fsync"])
def test_partial_failure_rolls_back_only_created_files(installation, monkeypatch, operation):
    plan, directory = installation
    unrelated = directory / "keep.service"
    unrelated.write_bytes(b"keep")
    original = getattr(
        os,
        "open"
        if operation == "timer_open"
        else "fsync"
        if operation == "directory_fsync"
        else operation,
    )

    def fail(*args, **kwargs):
        if operation == "timer_open":
            if args[0] != plan.timer.name:
                return original(*args, **kwargs)
        elif operation == "directory_fsync":
            if not stat.S_ISDIR(os.fstat(args[0]).st_mode):
                return original(*args, **kwargs)
        raise PermissionError("injected failure")

    monkeypatch.setattr(
        os,
        "open"
        if operation == "timer_open"
        else "fsync"
        if operation == "directory_fsync"
        else operation,
        fail,
    )
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and "injected failure" in result.errors[0]
    assert result.paths == ()
    assert list(directory.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"keep"


def test_short_writes_are_completed(installation, monkeypatch):
    plan, directory = installation
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:3]))
    result = core.install_backup_schedule(plan, directory)
    assert result.success
    assert (directory / plan.service.name).read_text() == plan.service.content
    assert (directory / plan.timer.name).read_text() == plan.timer.content


def test_second_unit_write_failure_cleans_up_both_units(installation, monkeypatch):
    plan, directory = installation
    write = os.write
    calls = 0

    def fail_second(fd, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("timer write failed")
        return write(fd, data)

    monkeypatch.setattr(os, "write", fail_second)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and "timer write failed" in result.errors[0]
    assert result.paths == ()
    assert list(directory.iterdir()) == []


def test_directory_replacement_rolls_back_in_original_directory(installation, monkeypatch):
    plan, directory = installation
    moved = directory.with_name("moved-units")
    fsync = os.fsync
    calls = 0

    def move_after_second_write(fd):
        nonlocal calls
        calls += 1
        fsync(fd)
        if calls == 2:
            directory.rename(moved)
            directory.mkdir(mode=0o700)
            (directory / "keep.timer").write_bytes(b"replacement directory")

    monkeypatch.setattr(os, "fsync", move_after_second_write)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and "directory changed" in result.errors[0]
    assert result.paths == ()
    assert list(moved.iterdir()) == []
    assert (directory / "keep.timer").read_bytes() == b"replacement directory"


def test_zero_write_rolls_back(installation, monkeypatch):
    plan, directory = installation
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and "no progress" in result.errors[0]
    assert list(directory.iterdir()) == []


def test_cleanup_failure_reports_residue(installation, monkeypatch):
    plan, directory = installation
    monkeypatch.setattr(os, "write", Mock(side_effect=OSError("write failed")))
    monkeypatch.setattr(os, "unlink", Mock(side_effect=PermissionError("cleanup denied")))
    result = core.install_backup_schedule(plan, directory)
    assert not result.success
    assert result.paths == (directory / plan.service.name,)
    assert any("write failed" in error for error in result.errors)
    assert any("cleanup denied" in error for error in result.errors)
    assert list(directory.iterdir()) == list(result.paths)


def test_racing_collision_is_not_removed(installation, monkeypatch):
    plan, directory = installation
    real_open = os.open

    def collide(name, flags, *args, **kwargs):
        if name == plan.timer.name:
            (directory / name).write_bytes(b"concurrent file")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", collide)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and result.paths == ()
    assert list(directory.iterdir()) == [directory / plan.timer.name]
    assert (directory / plan.timer.name).read_bytes() == b"concurrent file"


def test_replaced_created_file_is_preserved_during_rollback(installation, monkeypatch):
    plan, directory = installation

    def replace_and_fail(fd, data):
        (directory / plan.service.name).unlink()
        (directory / plan.service.name).write_bytes(b"replacement")
        raise OSError("write failed")

    monkeypatch.setattr(os, "write", replace_and_fail)
    result = core.install_backup_schedule(plan, directory)
    assert not result.success and result.paths == ()
    assert any("preserved replaced" in error for error in result.errors)
    assert (directory / plan.service.name).read_bytes() == b"replacement"


def test_repeat_install_does_not_overwrite(installation):
    plan, directory = installation
    first = core.install_backup_schedule(plan, directory)
    before = {path: path.stat() for path in first.paths}
    second = core.install_backup_schedule(plan, directory)
    assert first.success and not second.success
    assert second.paths == ()
    assert {path: path.stat() for path in first.paths} == before
