"""External controls are escaped only at the human presentation boundary."""

import io
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from autoflow import cli
from autoflow.commands import backup as backup_command
from autoflow.commands import logs as logs_command
from autoflow.commands import organize as organize_command
from autoflow.commands import restore as restore_command
from autoflow.commands import schedule as schedule_command
from autoflow.commands import status as status_command
from autoflow.core.backup import BackupError, BackupResult, SkippedEntry
from autoflow.core.logs import LogsError, MessageGroup, analyze_stream
from autoflow.core.organizer import OrganizationPlan, OrganizerError, PlanIssue, PlannedMove
from autoflow.core.restore import RestoreError, RestoreResult
from autoflow.core.scheduler import (
    BackupSchedulePlan,
    ScheduleActivationResult,
    ScheduleInstallResult,
    SchedulerError,
    SystemdUnit,
)
from autoflow.core.system import OperatingSystem, SystemStatus
from autoflow.presentation import safe_text

runner = CliRunner()
UNICODE = "ação café 中文 العربية e\u0301 👩\u200d💻 🇧🇷\u00a0♥️"
PAYLOAD = "[red]" + UNICODE + "\x1b[2J\x1b]0;owned\x07\x9dtitle\x9c\r\t\x00\x1b["
COLOR_ENV = {"FORCE_COLOR": "1", "NO_COLOR": None, "COLUMNS": "500"}


def assert_safe_human(output):
    # Application-created SGR styling is allowed; injected control protocols are not.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    for control in ("\x1b", "\x07", "\x9b", "\x9d", "\x9c", "\r", "\t", "\x00"):
        assert control not in plain
    assert "\\x1b" in plain
    assert UNICODE in plain
    assert "[red]" in plain


@pytest.mark.parametrize(
    "value",
    [
        "\x1b[2J",
        "\x1b]52;c;clipboard\x07",
        "\x1b]0;title\x1b\\",
        "\x9b2J",
        "\x9d0;title\x9c",
        "\x1b",
        "\x1b[",
        "\x1b]52;unfinished",
        "\r\n\t\b\x00\x07\x7f\x85",
        "\u202eunsafe\u2066",
        "\u2028\u2029",
        "\udcff",
    ],
)
def test_controls_and_incomplete_sequences_are_visible_but_inert(value):
    text = safe_text(value)
    assert text.plain != value
    assert all(ord(char) >= 32 and not 127 <= ord(char) <= 159 for char in text.plain)
    assert "\u202e" not in text.plain and "\u2066" not in text.plain


def test_all_c0_and_c1_controls_are_neutralized():
    value = "".join(chr(number) for number in (*range(32), *range(127, 160)))
    assert safe_text(value).plain.isascii()
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in safe_text(value).plain)


def test_unicode_and_literal_markup_are_preserved_with_rich_styles():
    value = UNICODE + " [red]literal[/red]"
    text = safe_text(value, style="green")
    assert text.plain == value
    buffer = io.StringIO()
    Console(
        file=buffer, force_terminal=True, color_system="standard", no_color=False, width=200
    ).print(text)
    assert "\x1b[32m" in buffer.getvalue()
    assert value in re.sub(r"\x1b\[[0-9;]*m", "", buffer.getvalue())


def test_only_explicit_multiline_layouts_keep_line_feeds():
    value = "[Unit]\nDescription=café\r\n\x1b[2J\t"
    assert "\n" not in safe_text(value).plain
    assert safe_text(value, multiline=True).plain == "[Unit]\nDescription=café\\r\n\\x1b[2J\\t"


@pytest.fixture
def reports():
    backup = BackupResult(
        True,
        True,
        PAYLOAD,
        PAYLOAD,
        PAYLOAD,
        1,
        10,
        5,
        0.25,
        [PAYLOAD],
        [PAYLOAD],
        [SkippedEntry(PAYLOAD, PAYLOAD)],
    )
    restore = RestoreResult(
        True, True, PAYLOAD, PAYLOAD, 1, 10, 0.25, [PAYLOAD], [PAYLOAD], 10, 100
    )
    logs = replace(
        analyze_stream(io.BytesIO(b"ERROR ready\n"), source=PAYLOAD),
        groups=[MessageGroup("ERROR", PAYLOAD, 1)],
        warnings=[PAYLOAD],
        limited=True,
        limit_reasons=[PAYLOAD],
    )
    status = SystemStatus(
        OperatingSystem(PAYLOAD, PAYLOAD),
        PAYLOAD,
        1.0,
        None,
        None,
        None,
        errors={PAYLOAD: PAYLOAD},
    )
    return {
        "backup": (
            backup_command,
            "create_backup",
            backup,
            ["backup", "source", "--output", "output", "--dry-run"],
        ),
        "restore": (
            restore_command,
            "restore_backup",
            restore,
            ["restore", "archive", "--output", "output", "--dry-run"],
        ),
        "logs": (logs_command, "analyze_file", logs, ["logs", "--file", "logfile"]),
        "status": (status_command, "collect_system_status", status, ["status"]),
    }


@pytest.mark.parametrize("command", ["backup", "restore", "logs", "status"])
def test_human_results_are_safe_without_mutating_reports(reports, monkeypatch, command):
    module, operation, report, args = reports[command]
    before = asdict(report)
    monkeypatch.setattr(module, operation, Mock(return_value=report))
    result = runner.invoke(cli.app, args, color=True, env=COLOR_ENV)
    assert result.exit_code == (1 if command == "status" else 0), result.output
    assert_safe_human(result.output)
    assert "\x1b[" in result.output  # Rich styling from the application survives.
    assert asdict(report) == before


@pytest.mark.parametrize("command", ["backup", "restore", "logs", "status"])
def test_json_results_are_byte_for_byte_unchanged(reports, monkeypatch, command):
    module, operation, report, args = reports[command]
    expected = json.dumps(asdict(report), allow_nan=False) + "\n"
    monkeypatch.setattr(module, operation, Mock(return_value=report))
    result = runner.invoke(cli.app, [*args, "--json"], color=True, env=COLOR_ENV)
    assert result.exit_code == (1 if command == "status" else 0)
    assert result.stdout == expected
    assert result.stderr == ""
    assert json.loads(result.stdout) == asdict(report)


ERROR_CASES = [
    (
        backup_command,
        "create_backup",
        BackupError,
        ["backup", "source", "--output", "output", "--dry-run"],
    ),
    (
        restore_command,
        "restore_backup",
        RestoreError,
        ["restore", "archive", "--output", "output", "--dry-run"],
    ),
    (logs_command, "analyze_file", LogsError, ["logs", "--file", "logfile"]),
    (organize_command, "plan_organization", OrganizerError, ["organize", "source", "--dry-run"]),
    (
        schedule_command,
        "plan_backup_schedule",
        SchedulerError,
        [
            "schedule",
            "add",
            "backup",
            "source",
            "--output",
            "output",
            "--every",
            "daily",
            "--dry-run",
        ],
    ),
]


@pytest.mark.parametrize("module, operation, error, args", ERROR_CASES)
def test_manipulated_error_messages_are_safe(module, operation, error, args, monkeypatch):
    monkeypatch.setattr(module, operation, Mock(side_effect=error(PAYLOAD)))
    result = runner.invoke(cli.app, args, color=True, env=COLOR_ENV)
    assert result.exit_code == 1
    assert_safe_human(result.stderr)
    assert "\x1b[31m" in result.stderr


@pytest.mark.parametrize("module, operation, error, args", ERROR_CASES[:3])
def test_json_error_values_are_not_sanitized(module, operation, error, args, monkeypatch):
    monkeypatch.setattr(module, operation, Mock(side_effect=error(PAYLOAD)))
    result = runner.invoke(cli.app, [*args, "--json"], color=True, env=COLOR_ENV)
    assert result.exit_code == 1
    expected = {"success": False, "error": PAYLOAD}
    if module in (backup_command, restore_command):
        expected["dry_run"] = True
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""


def test_organize_uses_shared_safe_presentation(tmp_path):
    source = tmp_path / PAYLOAD
    original = source / PAYLOAD
    plan = OrganizationPlan(
        source,
        (PlannedMove(original, source / "txt" / PAYLOAD, PAYLOAD),),
        (PlanIssue(original, PAYLOAD),),
    )
    before = asdict(plan)
    buffer = io.StringIO()
    organize_command.render_plan(plan, Console(file=buffer, width=500))
    assert_safe_human(buffer.getvalue())
    assert asdict(plan) == before


@pytest.fixture
def schedule_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    plan = BackupSchedulePlan(
        tmp_path / PAYLOAD,
        tmp_path / PAYLOAD,
        "daily",
        (PAYLOAD,),
        SystemdUnit("autoflow-backup-0123456789abcdef.service", "[Service]\n" + PAYLOAD),
        SystemdUnit("autoflow-backup-0123456789abcdef.timer", "[Timer]\n" + PAYLOAD),
    )
    monkeypatch.setattr(schedule_command, "plan_backup_schedule", Mock(return_value=plan))
    return plan


def test_schedule_preview_keeps_unit_layout_and_neutralizes_controls(schedule_plan):
    before = asdict(schedule_plan)
    result = runner.invoke(cli.app, ERROR_CASES[-1][-1], color=True, env=COLOR_ENV)
    assert result.exit_code == 0, result.output
    assert_safe_human(result.stdout)
    assert "[Service]" in result.stdout and "[Timer]" in result.stdout
    assert "[Service]\\n" not in result.stdout
    assert asdict(schedule_plan) == before


def test_schedule_install_errors_and_residues_are_sanitized(schedule_plan, monkeypatch):
    monkeypatch.setattr(
        schedule_command,
        "install_backup_schedule",
        Mock(return_value=(ScheduleInstallResult(False, (Path(PAYLOAD),), (PAYLOAD,)))),
    )
    args = ERROR_CASES[-1][-1][:-1] + ["--install", "--yes"]
    result = runner.invoke(cli.app, args, color=True, env=COLOR_ENV)
    assert result.exit_code == 1
    assert_safe_human(result.stderr)


def test_schedule_activation_diagnostics_are_sanitized(schedule_plan, monkeypatch):
    monkeypatch.setattr(schedule_command, "_load_installed_plan", Mock(return_value=schedule_plan))
    monkeypatch.setattr(
        schedule_command,
        "activate_backup_schedule",
        Mock(
            return_value=(
                ScheduleActivationResult(False, schedule_plan.timer.name, (PAYLOAD,), True)
            )
        ),
    )
    result = runner.invoke(
        cli.app,
        ["schedule", "activate", schedule_plan.timer.name, "--yes"],
        color=True,
        env=COLOR_ENV,
    )
    assert result.exit_code == 1
    assert_safe_human(result.stderr)
    assert "partially enabled and/or active" in result.stderr


def test_version_metadata_is_literal_and_safe(monkeypatch):
    monkeypatch.setattr(cli, "version", Mock(return_value=PAYLOAD))
    result = runner.invoke(cli.app, ["--version"], color=True, env=COLOR_ENV)
    assert result.exit_code == 0
    assert_safe_human(result.stdout)
    assert "AutoFlow CLI" in result.stdout
