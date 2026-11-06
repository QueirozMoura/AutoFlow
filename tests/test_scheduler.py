"""Scheduler plans use temporary metadata only; no systemd process is invoked."""

import configparser
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoflow.core import scheduler as core


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_bytes(b"original")
    executable = tmp_path / "autoflow"
    executable.write_text("synthetic executable; never run")
    executable.chmod(0o700)
    return source, tmp_path / "backups", executable


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_plan_user_service_and_timer(paths, frequency: str) -> None:
    source, output, executable = paths
    plan = core.plan_backup_schedule(
        source, output, frequency, unit_name="autoflow-backup-test", executable=executable
    )

    assert plan.source == source and plan.output == output
    assert plan.scope == "user" and plan.frequency == frequency
    assert plan.command == (
        str(executable),
        "backup",
        str(source),
        "--output",
        str(output),
        "--json",
    )
    assert plan.service.name == "autoflow-backup-test.service"
    assert plan.timer.name == "autoflow-backup-test.timer"
    service = configparser.ConfigParser(interpolation=None)
    service.read_string(plan.service.content)
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["UMask"] == "0077"
    assert "User" not in service["Service"]
    timer = configparser.ConfigParser(interpolation=None)
    timer.read_string(plan.timer.content)
    assert timer["Timer"]["OnCalendar"] == frequency
    assert timer["Timer"]["Unit"] == plan.service.name
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert not output.exists()
    with pytest.raises(FrozenInstanceError):
        plan.scope = "system"


def test_stable_names_distinguish_schedules(paths) -> None:
    source, output, executable = paths
    plan = core.plan_backup_schedule(source, output, "daily", executable=executable)
    assert plan == core.plan_backup_schedule(source, output, "daily", executable=executable)
    assert plan.service.name.startswith("autoflow-backup-")
    for other_output, frequency in ((output, "weekly"), (output.parent / "other", "daily")):
        other = core.plan_backup_schedule(source, other_output, frequency, executable=executable)
        assert other.service.name != plan.service.name


@pytest.mark.parametrize("frequency", ["", "Daily", "hourly", "daily\nUnit=evil.service", None, 1])
def test_invalid_frequency(paths, frequency) -> None:
    source, output, executable = paths
    with pytest.raises(core.SchedulerError, match="Frequency"):
        core.plan_backup_schedule(source, output, frequency, executable=executable)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../evil",
        "/absolute",
        "a.service",
        "a.timer",
        "a@b",
        "%n",
        "a b",
        "a\nx",
        "-a",
        "é",
        "a" * 248,
        1,
    ],
)
def test_invalid_unit_name(paths, name) -> None:
    source, output, executable = paths
    with pytest.raises(core.SchedulerError, match="unit name"):
        core.plan_backup_schedule(source, output, "daily", unit_name=name, executable=executable)


def test_maximum_unit_name(paths) -> None:
    source, output, executable = paths
    plan = core.plan_backup_schedule(
        source, output, "daily", unit_name="a" * 247, executable=executable
    )
    assert len(plan.service.name) == 255


@pytest.mark.parametrize("label", ["source", "output"])
@pytest.mark.parametrize(
    "name",
    [
        "",
        "bad\nname",
        "bad\rname",
        "bad\tname",
        "bad\x00name",
        "bad\x1bname",
        "bad\u202ename",
        "../escape",
    ],
)
def test_unsafe_path_text(paths, label: str, name: str) -> None:
    source, output, executable = paths
    with pytest.raises(core.SchedulerError, match="path"):
        core.plan_backup_schedule(
            name if label == "source" else source,
            name if label == "output" else output,
            "daily",
            executable=executable,
        )


@pytest.mark.parametrize("label", ["source", "output"])
@pytest.mark.parametrize("path", ["/", "/proc", "/sys/data", "/dev/data", "/run/data"])
def test_protected_paths(paths, label: str, path: str) -> None:
    source, output, executable = paths
    with pytest.raises(core.SchedulerError, match="Protected"):
        core.plan_backup_schedule(
            Path(path) if label == "source" else source,
            Path(path) if label == "output" else output,
            "daily",
            executable=executable,
        )


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "dangling", "ancestor_symlink"])
def test_invalid_sources(paths, tmp_path: Path, kind: str) -> None:
    source, output, executable = paths
    candidate = tmp_path / "candidate"
    if kind == "file":
        candidate.write_bytes(b"keep")
    elif kind == "symlink":
        candidate.symlink_to(source, target_is_directory=True)
    elif kind == "dangling":
        candidate.symlink_to(tmp_path / "missing")
    elif kind == "ancestor_symlink":
        candidate.symlink_to(tmp_path, target_is_directory=True)
        candidate /= source.name
    with pytest.raises(core.SchedulerError):
        core.plan_backup_schedule(candidate, output, "daily", executable=executable)
    assert not output.exists()


@pytest.mark.parametrize(
    "kind", ["same", "inside", "file", "symlink", "dangling", "ancestor_symlink", "shared"]
)
def test_invalid_outputs(paths, tmp_path: Path, kind: str) -> None:
    source, output, executable = paths
    if kind == "same":
        output = source
    elif kind == "inside":
        output = source / "backups"
    elif kind == "file":
        output.write_bytes(b"keep")
    elif kind == "symlink":
        output.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "dangling":
        output.symlink_to(tmp_path / "missing")
    elif kind == "ancestor_symlink":
        output.symlink_to(tmp_path, target_is_directory=True)
        output /= "missing"
    else:
        output.mkdir(mode=0o777)
        output.chmod(0o777)
    with pytest.raises(core.SchedulerError):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


def test_relative_paths_and_home_expansion(paths, tmp_path: Path, monkeypatch) -> None:
    source, output, executable = paths
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    plan = core.plan_backup_schedule(
        Path("~/source"), Path("backups"), "daily", executable=executable
    )
    assert (plan.source, plan.output) == (source, output)


def test_default_executable_is_pinned_to_python_environment(paths, monkeypatch) -> None:
    source, output, executable = paths
    monkeypatch.setattr(core.sys, "executable", str(executable.parent / "python"))
    plan = core.plan_backup_schedule(source, output, "daily")
    assert plan.command[0] == str(executable)


@pytest.mark.parametrize("location", ["home", "cwd"])
def test_expansion_cannot_inject_control_characters(
    paths, tmp_path: Path, monkeypatch, location
) -> None:
    source, output, executable = paths
    unsafe = tmp_path / "bad\nExecStart=evil"
    unsafe.mkdir()
    if location == "home":
        monkeypatch.setenv("HOME", str(unsafe))
        output = Path("~/backups")
    else:
        monkeypatch.chdir(unsafe)
        output = Path("backups")
    with pytest.raises(core.SchedulerError, match="unsafe characters"):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


def test_dollar_sign_in_executable_cannot_change_program_path(paths, tmp_path: Path) -> None:
    source, output, executable = paths
    executable = executable.rename(tmp_path / "autoflow$literal")
    with pytest.raises(core.SchedulerError, match="Executable path"):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


def test_missing_output_ancestors_are_not_created(paths) -> None:
    source, output, executable = paths
    plan = core.plan_backup_schedule(source, output / "nested", "daily", executable=executable)
    assert plan.output == output / "nested"
    assert not output.exists()


def test_output_owned_by_another_user_is_rejected(paths, monkeypatch) -> None:
    source, output, executable = paths
    output.mkdir(mode=0o700)
    uid = os.geteuid()
    monkeypatch.setattr(core.os, "geteuid", lambda: uid + 1)
    with pytest.raises(core.SchedulerError, match="owned by the current user"):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


def test_permission_error_becomes_scheduler_error(paths, monkeypatch) -> None:
    source, output, executable = paths
    monkeypatch.setattr(core.os, "open", Mock(side_effect=PermissionError("inspection denied")))
    with pytest.raises(core.SchedulerError, match="inspection denied"):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


@pytest.mark.parametrize("kind", ["missing", "relative", "directory", "symlink", "not_executable"])
def test_invalid_executable(paths, tmp_path: Path, kind: str) -> None:
    source, output, executable = paths
    if kind == "missing":
        executable = tmp_path / "missing"
    elif kind == "relative":
        executable = Path("autoflow")
    elif kind == "directory":
        executable = source
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(executable)
        executable = link
    else:
        executable.chmod(0o600)
    with pytest.raises(core.SchedulerError):
        core.plan_backup_schedule(source, output, "daily", executable=executable)


def test_systemd_escaping_keeps_paths_as_literal_arguments(paths, tmp_path: Path) -> None:
    source, _, executable = paths
    source = source.rename(tmp_path / 'source %h ${HOME} "quoted"; & café')
    output = tmp_path / 'backup %n $USER \\ "quoted"; $(touch marker)'
    plan = core.plan_backup_schedule(source, output, "daily", executable=executable)
    line = next(line for line in plan.service.content.splitlines() if line.startswith("ExecStart="))
    assert 'source %%h $${HOME} \\"quoted\\"; & café"' in line
    assert 'backup %%n $$USER \\\\ \\"quoted\\"; $$(touch marker)"' in line
    assert plan.command[2] == str(source) and plan.command[4] == str(output)
    assert len(plan.service.content.split("ExecStart=")) == 2
    assert not (tmp_path / "marker").exists()


def test_planning_never_writes_reads_contents_or_launches_processes(
    paths, tmp_path: Path, monkeypatch
) -> None:
    source, output, executable = paths
    output.mkdir(mode=0o700)
    (output / "keep.tar.gz").write_bytes(b"previous backup")
    before = {path: path.lstat() for path in (tmp_path, *tmp_path.rglob("*"))}
    real_open = os.open

    def directory_reads_only(name, flags, *args, **kwargs):
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert flags & os.O_DIRECTORY
        return real_open(name, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(core.os, "open", directory_reads_only)
        for operation in ("mkdir", "rename", "replace", "unlink", "remove", "system"):
            patch.setattr(
                core.os, operation, Mock(side_effect=AssertionError("mutation attempted"))
            )
        patch.setattr(Path, "write_text", Mock(side_effect=AssertionError("unit write attempted")))
        patch.setattr(Path, "write_bytes", Mock(side_effect=AssertionError("write attempted")))
        patch.setattr(Path, "open", Mock(side_effect=AssertionError("content read attempted")))
        patch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("process attempted")))
        core.plan_backup_schedule(source, output, "daily", executable=executable)

    assert set(tmp_path.rglob("*")) == before.keys() - {tmp_path}
    for path, metadata in before.items():
        after = path.lstat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    assert (source / "original.txt").read_bytes() == b"original"
    assert (output / "keep.tar.gz").read_bytes() == b"previous backup"
