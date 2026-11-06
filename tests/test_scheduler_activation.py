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
    def respond(arguments, **kwargs):
        output = loaded_properties(arguments[-1]) if "show" in arguments else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    mocked = Mock(side_effect=respond)
    monkeypatch.setattr(core.subprocess, "run", mocked)
    monkeypatch.setattr(
        subprocess, "Popen", Mock(side_effect=AssertionError("real process attempted"))
    )
    return mocked


@pytest.fixture
def plan(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    executable = tmp_path / "autoflow"
    executable.write_text("fixture only; never executed")
    executable.chmod(0o700)
    plan = core.plan_backup_schedule(source, tmp_path / "backups", "daily", executable=executable)
    directory = core.user_unit_directory()
    directory.mkdir(parents=True, mode=0o700)
    assert core.install_backup_schedule(plan, directory).success
    return plan


def loaded_properties(name, directory=None):
    directory = core.user_unit_directory() if directory is None else directory
    return (
        f"Id={name}\nLoadState=loaded\nFragmentPath={directory / name}\n"
        "DropInPaths=\nNeedDaemonReload=no\n"
    )


def successful_results(plan):
    return [
        subprocess.CompletedProcess(
            item.args[0],
            0,
            loaded_properties(item.args[0][-1]) if "show" in item.args[0] else "",
            "",
        )
        for item in expected_calls(plan.timer.name)
    ]


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
        *[
            call(
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
                    name,
                ],
                **options,
            )
            for name in (timer.removesuffix(".timer") + ".service", timer)
        ],
        call(["systemctl", "--user", "enable", "--now", timer], **options),
    ]


def test_activation_success_exact_commands(plan, process):
    result = core.activate_backup_schedule(plan)
    assert result.success
    assert result.timer == plan.timer.name
    assert result.errors == ()
    assert not result.partial_activation_possible
    assert process.call_args_list == expected_calls(plan.timer.name)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize("diagnostic", ["user bus unavailable", b"invalid unit \xff", ""])
def test_failed_command(plan, process, stage, diagnostic):
    commands = expected_calls(plan.timer.name)
    failure = subprocess.CalledProcessError(1, commands[stage].args[0], stderr=diagnostic)
    process.side_effect = successful_results(plan)[:stage] + [failure]
    result = core.activate_backup_schedule(plan)
    assert not result.success and result.timer == plan.timer.name
    assert result.partial_activation_possible is (stage == 3)
    assert "exit 1" in result.errors[0]
    assert commands[stage].args[0][2] in result.errors[0]
    assert (
        "partially enabled and/or active" if stage == 3 else "No timer enable/start"
    ) in result.errors[1]
    assert process.call_args_list == commands[: stage + 1]


def test_failure_uses_stdout_when_stderr_is_empty(plan, process):
    process.side_effect = subprocess.CalledProcessError(2, [], output="stdout diagnostic")
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "stdout diagnostic" in result.errors[0]
    assert not result.partial_activation_possible


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
def test_timeout_reports_uncertain_activation_state(plan, process, stage):
    failure = subprocess.TimeoutExpired([], 30, stderr=b"timeout diagnostic")
    process.side_effect = successful_results(plan)[:stage] + [failure]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "timed out after 30 seconds" in result.errors[0]
    assert result.partial_activation_possible is (stage == 3)
    assert process.call_args_list == expected_calls(plan.timer.name)[: stage + 1]


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "error", [FileNotFoundError("systemctl missing"), PermissionError("execution denied")]
)
def test_command_cannot_start(plan, process, stage, error):
    process.side_effect = successful_results(plan)[:stage] + [error]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert "could not start" in result.errors[0]
    assert str(error) in result.errors[0]
    assert not result.partial_activation_possible
    assert "preexisting state is unknown" in result.errors[1]
    assert process.call_args_list == expected_calls(plan.timer.name)[: stage + 1]


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "error", [OSError("I/O failure"), subprocess.SubprocessError("wait failed")]
)
def test_unexpected_process_error(plan, process, stage, error):
    process.side_effect = successful_results(plan)[:stage] + [error]
    result = core.activate_backup_schedule(plan)
    assert not result.success
    assert str(error) in result.errors[0]
    assert result.partial_activation_possible is (stage == 3)
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
    assert core.install_backup_schedule(custom, core.user_unit_directory()).success
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


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize(
    "change",
    [
        "fragment",
        "missing_fragment",
        "relative_fragment",
        "dropin",
        "prefix_dropin",
        "global_dropin",
        "other_search_path_dropin",
        "missing_dropins",
        "alias",
        "unloaded",
        "stale",
        "missing_reload_status",
        "empty",
        "duplicate",
        "malformed",
        "diagnostics",
    ],
)
def test_effective_configuration_must_match_before_enable(plan, process, tmp_path, index, change):
    responses = successful_results(plan)
    response = responses[index]
    name = plan.service.name if index == 1 else plan.timer.name
    fragment = str(core.user_unit_directory() / name)
    content = response.stdout
    if change == "fragment":
        content = content.replace(fragment, str(tmp_path / "runtime" / name))
    elif change == "missing_fragment":
        content = content.replace(f"FragmentPath={fragment}", "FragmentPath=")
    elif change == "relative_fragment":
        content = content.replace(fragment, name)
    elif change in ("dropin", "prefix_dropin", "global_dropin", "other_search_path_dropin"):
        paths = {
            "dropin": core.user_unit_directory() / (name + ".d") / "override.conf",
            "prefix_dropin": core.user_unit_directory() / "autoflow-backup-.service.d/extra.conf",
            "global_dropin": core.user_unit_directory() / "service.d/extra.conf",
            "other_search_path_dropin": tmp_path / "system-config" / "user" / "extra.conf",
        }
        content = content.replace("DropInPaths=", f"DropInPaths={paths[change]}")
    elif change == "missing_dropins":
        content = content.replace("DropInPaths=\n", "")
    elif change == "alias":
        content = content.replace(f"Id={name}", "Id=other.timer")
    elif change == "unloaded":
        content = content.replace("LoadState=loaded", "LoadState=not-found")
    elif change == "stale":
        content = content.replace("NeedDaemonReload=no", "NeedDaemonReload=yes")
    elif change == "missing_reload_status":
        content = content.replace("NeedDaemonReload=no\n", "")
    elif change == "empty":
        content = ""
    elif change == "duplicate":
        content += f"FragmentPath={fragment}\n"
    elif change == "malformed":
        content += "unparseable property\n"
    else:
        response.stderr = "query warning"
    response.stdout = content
    process.side_effect = responses
    result = core.activate_backup_schedule(plan)
    assert not result.success and result.errors
    assert not result.partial_activation_possible
    assert process.call_args_list == expected_calls(plan.timer.name)[: index + 1]
    assert all("enable" not in item.args[0] for item in process.call_args_list)


@pytest.mark.parametrize("change", ["content", "replaced", "symlink"])
def test_local_unit_changed_during_query_prevents_enable(plan, process, change):
    responses = successful_results(plan)
    path = core.user_unit_directory() / plan.service.name

    def respond(arguments, **kwargs):
        response = responses.pop(0)
        if arguments[-1] == plan.timer.name:
            if change == "content":
                path.write_text("modified content")
            else:
                replacement = path.with_name("replacement")
                replacement.write_text(plan.service.content)
                if change == "replaced":
                    replacement.replace(path)
                else:
                    path.unlink()
                    path.symlink_to(replacement)
        return response

    process.side_effect = respond
    result = core.activate_backup_schedule(plan)
    assert not result.success and not result.partial_activation_possible
    assert process.call_args_list == expected_calls(plan.timer.name)[:3]


def test_missing_installed_unit_prevents_all_commands(plan, process):
    (core.user_unit_directory() / plan.timer.name).unlink()
    result = core.activate_backup_schedule(plan)
    assert not result.success
    process.assert_not_called()


def test_xdg_config_directory(plan, process, tmp_path, monkeypatch):
    alternate = tmp_path / "alternate config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(alternate))
    directory = alternate / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700)
    assert core.install_backup_schedule(plan, directory).success
    result = core.activate_backup_schedule(plan)
    assert result.success, result.errors
    assert process.call_args_list == expected_calls(plan.timer.name)


def test_explicit_directory_is_bound_to_manager_fragments(plan, process, tmp_path):
    directory = tmp_path / "explicit-units"
    directory.mkdir(mode=0o700)
    assert core.install_backup_schedule(plan, directory).success
    # The simulated manager still resolves the HOME directory instead.
    result = core.activate_backup_schedule(plan, unit_directory=directory)
    assert not result.success
    assert all("enable" not in item.args[0] for item in process.call_args_list)


def test_invalid_xdg_path_prevents_all_commands(plan, process, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    result = core.activate_backup_schedule(plan)
    assert not result.success and "absolute" in result.errors[0]
    process.assert_not_called()
