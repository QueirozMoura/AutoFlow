"""Exercise schedule previews and confirmed installs using only temporary data."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import schedule as command
from autoflow.core import scheduler as core

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("process attempted")))
    monkeypatch.setattr(os, "system", Mock(side_effect=AssertionError("shell attempted")))


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = tmp_path / "source [bold] data"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"original data")
    executable = tmp_path / "autoflow"
    executable.write_text("fixture executable; never run")
    executable.chmod(0o700)
    monkeypatch.setattr(core.sys, "executable", str(tmp_path / "python"))
    return source, tmp_path / "missing" / "backup [blue] data"


@pytest.fixture
def unit_directory(paths, tmp_path: Path) -> Path:
    directory = tmp_path / "home" / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700)
    return directory


def arguments(source: Path, output: Path, frequency: str = "daily") -> list[str]:
    return [
        "schedule",
        "add",
        "backup",
        str(source),
        "--output",
        str(output),
        "--every",
        frequency,
        "--dry-run",
    ]


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_preview_uses_core_plan(paths, frequency: str, monkeypatch) -> None:
    source, output = paths
    expected = core.plan_backup_schedule(source, output, frequency)
    planner = Mock(wraps=core.plan_backup_schedule)
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    result = runner.invoke(app, arguments(source, output, frequency), env={"COLUMNS": "240"})

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    planner.assert_called_once_with(source, output, frequency)
    for value in (
        expected.service.name,
        expected.timer.name,
        frequency,
        str(source),
        str(output),
        "Backup schedule preview (dry-run)",
        "Preview only",
        "No files changed",
        "no systemd units created, installed or activated",
    ):
        assert value in result.stdout
    assert not output.parent.exists()


@pytest.mark.parametrize(
    "args, expected",
    [
        (["--help"], "schedule"),
        (["schedule", "--help"], "add"),
        (["schedule", "add", "--help"], "backup"),
        (["schedule", "add", "backup", "--help"], "--dry-run"),
    ],
)
def test_command_help(args: list[str], expected: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert expected in result.output


@pytest.mark.parametrize("frequency", ["hourly", "Daily", "", "daily\nUnit=evil.service"])
def test_invalid_frequency_never_calls_planner(paths, frequency: str, monkeypatch) -> None:
    planner = Mock()
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    result = runner.invoke(app, arguments(*paths, frequency))
    assert result.exit_code == 2
    assert "--every" in result.output
    planner.assert_not_called()


@pytest.mark.parametrize("missing", ["source", "--output", "--every", "--dry-run"])
def test_required_arguments(paths, missing: str, monkeypatch) -> None:
    planner = Mock()
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    args = arguments(*paths)
    if missing == "source":
        del args[3]
    else:
        position = args.index(missing)
        del args[position : position + (1 if missing == "--dry-run" else 2)]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert ("source" if missing == "source" else missing) in result.output
    planner.assert_not_called()


@pytest.mark.parametrize("option", ["--execute", "--no-dry-run", "--install", "--enable"])
def test_execution_options_are_rejected(paths, option: str, monkeypatch) -> None:
    planner = Mock()
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    result = runner.invoke(app, [*arguments(*paths), option])
    assert result.exit_code == 2
    planner.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [[], ["--yes"], ["--dry-run", "--yes"], ["--dry-run", "--install", "--yes"]],
)
def test_invalid_option_combinations(paths, options, monkeypatch) -> None:
    planner = Mock()
    installer = Mock()
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    result = runner.invoke(app, arguments(*paths)[:-1] + options)
    assert result.exit_code == 2
    planner.assert_not_called()
    installer.assert_not_called()


@pytest.mark.parametrize("yes", [False, True])
def test_install_uses_core_and_shows_complete_plan(paths, unit_directory, monkeypatch, yes) -> None:
    plan = core.plan_backup_schedule(*paths, "daily")
    installer = Mock(wraps=core.install_backup_schedule)
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    args = arguments(*paths)[:-1] + ["--install"] + (["--yes"] if yes else [])
    result = runner.invoke(app, args, input="y\n", env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    installer.assert_called_once_with(plan, unit_directory)
    for unit in (plan.service, plan.timer):
        path = unit_directory / unit.name
        assert path.read_text() == unit.content
        assert path.stat().st_mode & 0o777 == 0o600
        assert str(path) in result.stdout
        for line in unit.content.splitlines():
            if line:
                # ExecStart can wrap even in a wide terminal.
                assert "".join(line.split()) in "".join(result.stdout.replace("│", "").split())
    assert "Scope" in result.stdout and "Command" in result.stdout
    assert "The timer is not active" in result.stdout
    assert "Schedule files installed" in result.stdout
    if yes:
        assert "Install these schedule files?" not in result.stdout
    else:
        prompt = result.stdout.index("Install these schedule files?")
        assert result.stdout.index("[Service]") < prompt
        assert result.stdout.index("[Timer]") < prompt
        assert result.stdout.index(str(unit_directory)) < prompt
    assert not paths[1].exists()
    assert (paths[0] / "keep.txt").read_bytes() == b"original data"


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_cancel_installation(paths, unit_directory, monkeypatch, answer) -> None:
    installer = Mock()
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    result = runner.invoke(app, arguments(*paths)[:-1] + ["--install"], input=answer)
    assert result.exit_code == 0
    assert "Installation cancelled" in result.stdout
    installer.assert_not_called()
    assert list(unit_directory.iterdir()) == []


def test_confirmation_eof_is_failure(paths, unit_directory, monkeypatch) -> None:
    installer = Mock()
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    result = runner.invoke(app, arguments(*paths)[:-1] + ["--install"], input="")
    assert result.exit_code == 1
    assert "Confirmation unavailable" in result.stderr
    installer.assert_not_called()
    assert list(unit_directory.iterdir()) == []


def test_confirmation_keyboard_interrupt_is_failure(paths, unit_directory, monkeypatch) -> None:
    installer = Mock()
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    monkeypatch.setattr(command.typer, "confirm", Mock(side_effect=typer.Abort()))
    result = runner.invoke(app, arguments(*paths)[:-1] + ["--install"])
    assert result.exit_code == 1
    installer.assert_not_called()
    assert list(unit_directory.iterdir()) == []


def test_install_requires_existing_directory(paths, tmp_path: Path) -> None:
    result = runner.invoke(app, arguments(*paths)[:-1] + ["--install", "--yes"])
    assert result.exit_code == 1
    assert "Schedule installation failed" in result.stderr
    assert not (tmp_path / "home" / ".config").exists()


@pytest.mark.parametrize("unit_kind", ["service", "timer"])
def test_install_collision_preserves_existing_file(paths, unit_directory, unit_kind) -> None:
    plan = core.plan_backup_schedule(*paths, "daily")
    path = unit_directory / getattr(plan, unit_kind).name
    path.write_bytes(b"preexisting unit")
    result = runner.invoke(app, arguments(*paths)[:-1] + ["--install", "--yes"])
    assert result.exit_code == 1
    assert "Unit already exists" in result.stderr
    assert "Schedule files installed" not in result.stdout
    assert list(unit_directory.iterdir()) == [path]
    assert path.read_bytes() == b"preexisting unit"


def test_install_reports_core_errors_and_residues(paths, unit_directory, monkeypatch) -> None:
    residue = unit_directory / "remaining.service"
    installer = Mock(
        return_value=core.ScheduleInstallResult(
            False, (residue,), ("Write failed [bold]", "Cleanup denied")
        )
    )
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    result = runner.invoke(
        app, arguments(*paths)[:-1] + ["--install", "--yes"], env={"COLUMNS": "240"}
    )
    assert result.exit_code == 1
    assert "Write failed [bold]" in result.stderr
    assert "Cleanup denied" in result.stderr
    assert str(residue) in result.stderr
    assert "File remaining" in result.stderr
    assert "Schedule files installed" not in result.stdout


def test_dry_run_never_calls_installer(paths, unit_directory, monkeypatch) -> None:
    installer = Mock()
    monkeypatch.setattr(command, "install_backup_schedule", installer)
    result = runner.invoke(app, arguments(*paths))
    assert result.exit_code == 0
    installer.assert_not_called()
    assert list(unit_directory.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "file", "inside", "same", "protected", "symlink"])
def test_invalid_paths_report_planning_error(paths, tmp_path: Path, kind: str) -> None:
    source, output = paths
    if kind == "missing":
        source = tmp_path / "absent"
    elif kind == "file":
        source /= "keep.txt"
    elif kind == "inside":
        output = source / "backups"
    elif kind == "same":
        output = source
    elif kind == "protected":
        output = Path("/proc/backups")
    else:
        link = tmp_path / "link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    result = runner.invoke(app, arguments(source, output))
    assert result.exit_code == 1
    assert result.stderr
    assert "Backup schedule preview" not in result.stdout
    assert not paths[1].parent.exists()


def test_planner_error_is_literal_on_stderr(paths, monkeypatch) -> None:
    planner = Mock(side_effect=core.SchedulerError("Permission denied [bold]"))
    monkeypatch.setattr(command, "plan_backup_schedule", planner)
    result = runner.invoke(app, arguments(*paths))
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Permission denied [bold]" in result.stderr


def test_preview_never_mutates_files_or_launches_processes(
    paths, tmp_path: Path, monkeypatch
) -> None:
    before = {path: path.lstat() for path in (tmp_path, *tmp_path.rglob("*"))}
    with monkeypatch.context() as patch:
        for operation in ("mkdir", "rename", "replace", "unlink", "remove", "system"):
            patch.setattr(os, operation, Mock(side_effect=AssertionError("mutation attempted")))
        for operation in ("write_text", "write_bytes", "open"):
            patch.setattr(Path, operation, Mock(side_effect=AssertionError("file I/O attempted")))
        patch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("process attempted")))
        real_open = os.open

        def directory_reads_only(name, flags, *args, **kwargs):
            assert flags & os.O_ACCMODE == os.O_RDONLY
            assert flags & os.O_DIRECTORY
            return real_open(name, flags, *args, **kwargs)

        patch.setattr(os, "open", directory_reads_only)
        result = runner.invoke(app, arguments(*paths))
    assert result.exit_code == 0, result.output
    assert set(tmp_path.rglob("*")) == before.keys() - {tmp_path}
    for path, metadata in before.items():
        after = path.lstat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    assert (paths[0] / "keep.txt").read_bytes() == b"original data"
