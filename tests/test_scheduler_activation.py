"""Simulate every systemctl call; no real user manager is contacted."""

import runpy
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from autoflow.core import scheduler as core


@pytest.fixture(autouse=True)
def process(monkeypatch):
    mocked = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(core.subprocess, "run", mocked)
    monkeypatch.setattr(
        subprocess, "Popen", Mock(side_effect=AssertionError("real process attempted"))
    )
    return mocked


@pytest.fixture
def plan(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    executable = tmp_path / "autoflow"
    executable.write_text("fixture only; never executed")
    executable.chmod(0o700)
    return core.plan_backup_schedule(source, tmp_path / "backups", "daily", executable=executable)


def expected_calls(timer: str):
    options = {
        "shell": False,
        "check": True,
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "timeout": 30,
    }
    return [
        call(["systemctl", "--user", "daemon-reload"], **options),
        call(["systemctl", "--user", "enable", "--now", timer], **options),
    ]


def test_activation_success_exact_commands(plan, process):
    result = core.activate_backup_schedule(plan)
    assert result.success
    assert result.timer == plan.timer.name
    assert result.errors == ()
    assert not result.partial_activation_possible
    assert process.call_args_list == expected_calls(plan.timer.name)


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("diagnostic", ["user bus unavailable", b"invalid unit \xff", ""])
def test_failed_command(plan, process, stage, diagnostic):
    commands = expected_calls(plan.timer.name)
    failure = subprocess.CalledProcessError(1, commands[stage].args[0], stderr=diagnostic)
    process.side_effect = [failure] if stage == 0 else [process.return_value, failure]
    result = core.activate_backup_schedule(plan)
    assert not result.success and result.timer == plan.timer.name
    assert result.partial_activation_possible is (stage == 1)
    assert "exit 1" in result.errors[0]
    assert ("daemon-reload" if stage == 0 else "enable --now") in result.errors[0]
    assert (
        "partially enabled and/or active" if stage else "No timer enable/start"
    ) in result.errors[1]
    assert process.call_args_list == commands[: stage + 1]


def test_failure_uses_stdout_when_stderr_is_empty(plan, process):
    process.side_effect = subprocess.CalledProcessError(2, [], output="stdout diagnostic")
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "stdout diagnostic" in result.errors[0]
    assert not result.partial_activation_possible


@pytest.mark.parametrize("stage", [0, 1])
def test_timeout_reports_uncertain_activation_state(plan, process, stage):
    failure = subprocess.TimeoutExpired([], 30, stderr=b"timeout diagnostic")
    process.side_effect = [failure] if stage == 0 else [process.return_value, failure]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "timed out after 30 seconds" in result.errors[0]
    assert result.partial_activation_possible is (stage == 1)
    assert process.call_args_list == expected_calls(plan.timer.name)[: stage + 1]


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize(
    "error", [FileNotFoundError("systemctl missing"), PermissionError("execution denied")]
)
def test_command_cannot_start(plan, process, stage, error):
    process.side_effect = [error] if stage == 0 else [process.return_value, error]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "could not start" in result.errors[0]
    assert str(error) in result.errors[0]
    assert not result.partial_activation_possible
    assert "preexisting state is unknown" in result.errors[1]
    assert process.call_args_list == expected_calls(plan.timer.name)[: stage + 1]


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize(
    "error", [OSError("I/O failure"), subprocess.SubprocessError("wait failed")]
)
def test_unexpected_process_error(plan, process, stage, error):
    process.side_effect = [error] if stage == 0 else [process.return_value, error]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert str(error) in result.errors[0]
    assert result.partial_activation_possible is (stage == 1)
    assert process.call_args_list == expected_calls(plan.timer.name)[: stage + 1]


@pytest.mark.parametrize(
    "name",
    [
        "",
        "other.service",
        "../escape.timer",
        "/absolute.timer",
        "--help.timer",
        "name with spaces.timer",
        "name.timer\n",
        "name\x00.timer",
        "name\x1b.timer",
        "*.timer",
        "name@instance.timer",
        "name;touch.timer",
        "$(touch marker).timer",
        "café.timer",
        "a" * 248 + ".timer",
        None,
        1,
    ],
)
def test_unsafe_timer_rejected_before_subprocess(plan, process, name):
    altered = replace(plan, timer=replace(plan.timer, name=name))
    result = core.activate_backup_schedule(altered)
    assert not result.success and result.errors
    assert not result.partial_activation_possible
    process.assert_not_called()


@pytest.mark.parametrize("kind", ["object", "timer", "service", "mismatch", "scope"])
def test_invalid_plan_or_service_relationship(plan, process, kind):
    altered = {
        "object": None,
        "timer": replace(plan, timer=None),
        "service": replace(plan, service=None),
        "mismatch": replace(plan, timer=replace(plan.timer, name="unrelated.timer")),
        "scope": replace(plan, scope="system"),
    }[kind]
    result = core.activate_backup_schedule(altered)
    assert not result.success and result.errors
    process.assert_not_called()


@pytest.mark.parametrize("stem", ["custom_backup-123", "a" * 247])
def test_custom_planner_unit_names_are_supported(plan, process, stem):
    custom = core.plan_backup_schedule(
        plan.source, plan.output, plan.frequency, unit_name=stem, executable=Path(plan.command[0])
    )
    result = core.activate_backup_schedule(custom)
    assert result.success and result.timer == stem + ".timer"
    assert process.call_args_list == expected_calls(custom.timer.name)


def test_import_and_planning_do_not_execute_commands(plan, process):
    runpy.run_path(core.__file__)
    core.plan_backup_schedule(
        plan.source, plan.output, plan.frequency, executable=Path(plan.command[0])
    )
    process.assert_not_called()


def test_activation_does_not_reinstall_or_remove_files(plan, process, monkeypatch):
    monkeypatch.setattr(
        core, "install_backup_schedule", Mock(side_effect=AssertionError("install"))
    )
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=AssertionError("write")))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=AssertionError("remove")))
    result = core.activate_backup_schedule(plan)
    assert result.success
    assert process.call_args_list == expected_calls(plan.timer.name)
