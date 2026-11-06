"""Activate only simulated timers, validating real unit fixtures under tmp_path."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import schedule as command
from autoflow.core import scheduler as core

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("real command attempted"))
    )
    monkeypatch.setattr(
        subprocess, "Popen", Mock(side_effect=AssertionError("real process attempted"))
    )
    monkeypatch.setattr(os, "system", Mock(side_effect=AssertionError("shell attempted")))


@pytest.fixture
def activation(monkeypatch):
    mocked = Mock(
        side_effect=lambda plan, **kwargs: core.ScheduleActivationResult(True, plan.timer.name, ())
    )
    monkeypatch.setattr(command, "activate_backup_schedule", mocked)
    return mocked


@pytest.fixture
def installed(tmp_path):
    source = tmp_path / "source [bold] data"
    source.mkdir()
    executable = tmp_path / "autoflow"
    executable.write_text("fixture only; never executed")
    executable.chmod(0o700)
    plan = core.plan_backup_schedule(source, tmp_path / "backups", "daily", executable=executable)
    directory = tmp_path / "home" / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700)
    assert core.install_backup_schedule(plan, directory).success
    return plan, directory


def arguments(plan, *, yes=True):
    return ["schedule", "activate", plan.timer.name] + (["--yes"] if yes else [])


@pytest.mark.parametrize("yes", [False, True])
def test_confirmed_activation_uses_core(installed, activation, yes):
    plan, directory = installed
    before = {path: (path.read_bytes(), path.stat()) for path in directory.iterdir()}
    result = runner.invoke(app, arguments(plan, yes=yes), input="y\n", env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    activation.assert_called_once_with(plan, unit_directory=directory)
    assert "Timer enabled and started" in result.stdout
    assert plan.timer.name in result.stdout and plan.service.name in result.stdout
    assert str(directory) in result.stdout
    assert ("Enable and start this timer?" in result.stdout) is (not yes)
    for path, (content, metadata) in before.items():
        assert path.read_bytes() == content
        after = path.stat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_cancel_activation(installed, activation, answer):
    plan, _ = installed
    result = runner.invoke(app, arguments(plan, yes=False), input=answer)
    assert result.exit_code == 0
    assert "Activation cancelled" in result.stdout
    activation.assert_not_called()


def test_confirmation_eof(installed, activation):
    plan, _ = installed
    result = runner.invoke(app, arguments(plan, yes=False), input="")
    assert result.exit_code == 1
    assert "Confirmation unavailable" in result.stderr
    activation.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "other.timer",
        "autoflow-backup-test.timer",
        "autoflow-backup-0123456789ABCDEF.timer",
        "autoflow-backup-0123456789abcdef.service",
        "../autoflow-backup-0123456789abcdef.timer",
        "/tmp/autoflow-backup-0123456789abcdef.timer",
        "*.timer",
        "--help.timer",
        "",
        "autoflow-backup-0123456789abcdef.timer\n",
        "autoflow-backup-0123456789abcdef@x.timer",
    ],
)
def test_invalid_names(name, activation):
    result = runner.invoke(app, ["schedule", "activate", "--yes", "--", name])
    assert result.exit_code == 2
    activation.assert_not_called()


@pytest.mark.parametrize("kind", ["timer", "service", "directory"])
def test_missing_unit_or_directory(installed, activation, kind):
    plan, directory = installed
    if kind == "directory":
        directory.rename(directory.with_name("moved"))
    else:
        (directory / getattr(plan, kind).name).unlink()
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 1
    assert "Cannot activate schedule" in result.stderr
    activation.assert_not_called()


@pytest.mark.parametrize("kind", ["timer", "service", "directory", "ancestor"])
def test_symlinks_are_rejected(installed, activation, kind):
    plan, directory = installed
    if kind in ("timer", "service"):
        path = directory / getattr(plan, kind).name
    else:
        path = directory if kind == "directory" else directory.parent
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target)
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 1
    activation.assert_not_called()


@pytest.mark.parametrize("kind", ["timer", "service"])
@pytest.mark.parametrize(
    "change",
    [
        "foreign",
        "append",
        "symlink_override",
        "override",
        "fifo",
        "directory",
        "shared",
        "large",
        "encoding",
    ],
)
def test_foreign_or_unsafe_units_rejected(installed, activation, kind, change):
    plan, directory = installed
    path = directory / getattr(plan, kind).name
    if change == "foreign":
        path.write_text("[Unit]\nDescription=Other application\n")
    elif change == "append":
        path.write_text(path.read_text() + "\n[Other]\nUnexpected=yes\n")
    elif change in ("override", "symlink_override"):
        override = directory / (path.name + ".d")
        if change == "override":
            override.mkdir()
        else:
            override.symlink_to(directory / "missing")
    elif change in ("fifo", "directory"):
        path.unlink()
        if change == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    elif change == "shared":
        path.chmod(0o666)
    elif change == "large":
        path.write_bytes(b"x" * 65537)
    else:
        path.write_bytes(b"\xff")
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 1, result.output
    assert "Cannot activate schedule" in result.stderr
    activation.assert_not_called()


def test_generated_name_with_wrong_hash_rejected(installed, activation):
    plan, directory = installed
    stem = "autoflow-backup-0000000000000000"
    for unit, suffix in ((plan.service, ".service"), (plan.timer, ".timer")):
        (directory / (stem + suffix)).write_text(
            unit.content.replace(plan.service.name, stem + ".service")
        )
    result = runner.invoke(app, ["schedule", "activate", stem + ".timer", "--yes"])
    assert result.exit_code == 1
    assert "do not match" in result.stderr
    activation.assert_not_called()


@pytest.mark.parametrize("partial", [False, True])
def test_activation_failure_and_partial_warning(installed, activation, partial):
    plan, directory = installed
    activation.side_effect = None
    activation.return_value = core.ScheduleActivationResult(
        False, plan.timer.name, ("systemctl failed [bold]",), partial
    )
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 1
    assert "Timer activation failed" in result.stderr
    assert "systemctl failed [bold]" in result.stderr
    assert ("partially enabled and/or active" in result.stderr) is partial
    assert "Timer enabled and started" not in result.stdout
    activation.assert_called_once_with(plan, unit_directory=directory)


def test_changed_unit_after_confirmation_rejected(installed, activation, monkeypatch):
    plan, directory = installed

    def change_and_confirm(*args, **kwargs):
        (directory / plan.timer.name).write_text("[Timer]\nOnCalendar=hourly\n")
        return True

    monkeypatch.setattr(command.typer, "confirm", change_and_confirm)
    result = runner.invoke(app, arguments(plan, yes=False))
    assert result.exit_code == 1
    activation.assert_not_called()


def test_systemd_escaped_paths_round_trip(installed, activation):
    original, directory = installed
    source = original.source.rename(original.source.with_name('data %h $USER "quoted"'))
    output = source.parent / 'backups %% $$ \\ "quoted"'
    plan = core.plan_backup_schedule(source, output, "weekly", executable=Path(original.command[0]))
    assert core.install_backup_schedule(plan, directory).success
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 0, result.output
    activation.assert_called_once_with(plan, unit_directory=directory)


def test_activate_help(activation):
    result = runner.invoke(app, ["schedule", "activate", "--help"])
    assert result.exit_code == 0
    assert "--yes" in result.output
    activation.assert_not_called()


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("outcome", ["valid", "fragment", "override", "query_error", "timeout"])
def test_cli_checks_effective_manager_configuration(installed, monkeypatch, yes, outcome):
    plan, directory = installed
    calls = []

    def respond(arguments, **kwargs):
        assert kwargs["shell"] is False and kwargs["timeout"] == 30
        calls.append(arguments)
        output = ""
        if "show" in arguments:
            if outcome == "query_error":
                raise subprocess.CalledProcessError(1, arguments, stderr="query failed")
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(arguments, 30)
            name = arguments[-1]
            fragment = directory / name if outcome != "fragment" else directory.parent / name
            overrides = str(directory / "service.d/extra.conf") if outcome == "override" else ""
            output = (
                f"Id={name}\nLoadState=loaded\nFragmentPath={fragment}\n"
                f"DropInPaths={overrides}\nNeedDaemonReload=no\n"
            )
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(subprocess, "run", respond)
    result = runner.invoke(app, arguments(plan, yes=yes), input="y\n")
    assert result.exit_code == (0 if outcome == "valid" else 1), result.output
    assert ("Enable and start this timer?" in result.stdout) is (not yes)
    enable_calls = [item for item in calls if "enable" in item]
    assert enable_calls == (
        [["systemctl", "--user", "enable", "--now", plan.timer.name]] if outcome == "valid" else []
    )


def test_activate_uses_xdg_directory(installed, activation, tmp_path, monkeypatch):
    plan, original_directory = installed
    configured = tmp_path / "xdg config"
    directory = configured / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700)
    assert core.install_backup_schedule(plan, directory).success
    # Validating the old default directory would fail, so this also checks precedence.
    (original_directory / plan.timer.name).write_text("foreign unit")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured))
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 0, result.output
    activation.assert_called_once_with(plan, unit_directory=directory)


def test_relative_xdg_path_is_rejected(installed, activation, monkeypatch):
    plan, _ = installed
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    result = runner.invoke(app, arguments(plan))
    assert result.exit_code == 1
    activation.assert_not_called()


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize("yes", [False, True])
def test_cli_handles_activation_interrupt_without_traceback(installed, monkeypatch, stage, yes):
    plan, directory = installed
    calls = []
    commands = [
        ["systemctl", "--user", "daemon-reload"],
        [
            "systemctl",
            "--user",
            "show",
            "--no-pager",
            "--all",
            "--property=Id",
            "--property=LoadState",
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=NeedDaemonReload",
            plan.service.name,
        ],
        [
            "systemctl",
            "--user",
            "show",
            "--no-pager",
            "--all",
            "--property=Id",
            "--property=LoadState",
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=NeedDaemonReload",
            plan.timer.name,
        ],
        ["systemctl", "--user", "enable", "--now", plan.timer.name],
    ]

    def respond(arguments, **kwargs):
        calls.append(arguments)
        if len(calls) - 1 == stage:
            raise KeyboardInterrupt
        assert len(calls) - 1 < stage, "command executed after interruption"
        output = ""
        if "show" in arguments:
            name = arguments[-1]
            output = (
                f"Id={name}\nLoadState=loaded\nFragmentPath={directory / name}\n"
                "DropInPaths=\nNeedDaemonReload=no\n"
            )
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(subprocess, "run", respond)
    result = runner.invoke(app, arguments(plan, yes=yes), input="y\n")
    assert result.exit_code == 1
    assert "Timer activation failed" in result.stderr
    assert "interrupted by user" in result.stderr
    assert ("Warning: the timer may be partially enabled and/or active" in result.stderr) is (
        stage == 3
    )
    assert "Traceback" not in result.output and "Aborted!" not in result.output
    assert "Timer enabled and started" not in result.stdout
    assert calls == commands[: stage + 1]
    assert ("Enable and start this timer?" in result.stdout) is (not yes)
