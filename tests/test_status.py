"""Status output tests using a mocked snapshot."""

import json
from dataclasses import asdict, replace
from io import StringIO
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import status as status_command
from autoflow.core.system import OperatingSystem, ResourceUsage, SystemStatus

runner = CliRunner()


@pytest.fixture
def snapshot(monkeypatch: pytest.MonkeyPatch) -> SystemStatus:
    snapshot = SystemStatus(
        operating_system=OperatingSystem("Fedora Linux", "44"),
        hostname="test-host",
        uptime_seconds=90061.0,
        cpu_percent=42.5,
        memory=ResourceUsage(70.0, 7 * 1024**3, 10 * 1024**3),
        disk=ResourceUsage(90.0, 90 * 1024**3, 100 * 1024**3),
    )
    monkeypatch.setattr(status_command, "collect_system_status", Mock(return_value=snapshot))
    return snapshot


def test_status_panel(snapshot: SystemStatus) -> None:
    result = runner.invoke(app, ["status"], terminal_width=120)

    assert result.exit_code == 0
    for value in (
        "System status",
        "Fedora Linux 44",
        "test-host",
        "1d 01h 01m 01s",
        "42.5%",
        "7.0 GiB / 10.0 GiB",
        "Disk (/)",
        "normal",
        "warning",
        "critical",
    ):
        assert value in result.output
    assert "Usage: autoflow" not in result.output


def test_json_is_plain_and_complete(snapshot: SystemStatus) -> None:
    result = runner.invoke(app, ["status", "--json"], color=True, env={"FORCE_COLOR": "1"})

    assert result.exit_code == 0
    assert json.loads(result.stdout) == asdict(snapshot)
    assert "\x1b" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("json_output", [False, True])
def test_partial_failure(
    snapshot: SystemStatus, monkeypatch: pytest.MonkeyPatch, json_output: bool
) -> None:
    partial = replace(snapshot, memory=None, errors={"memory": "Read denied"})
    monkeypatch.setattr(status_command, "collect_system_status", lambda: partial)

    result = runner.invoke(app, ["status", "--json"] if json_output else ["status"])

    assert result.exit_code == 1
    if json_output:
        assert json.loads(result.stdout) == asdict(partial)
        assert result.stderr == ""
        assert "\x1b" not in result.stdout
    else:
        assert "Unavailable" in result.output
        assert "Read denied" in result.output
        assert "42.5%" in result.output


def test_panel_colors_and_literal_hostname(snapshot: SystemStatus) -> None:
    output = StringIO()
    console = Console(
        file=output, force_terminal=True, color_system="standard", no_color=False, width=140
    )

    status_command.render_status(replace(snapshot, hostname="[red]test-host[/red]"), console)

    rendered = output.getvalue()
    for code in ("\x1b[32m", "\x1b[33m", "\x1b[31m"):
        assert code in rendered
    assert "[red]test-host[/red]" in rendered


def test_status_help_does_not_collect(snapshot: SystemStatus) -> None:
    result = runner.invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    assert "--json" in result.output
    status_command.collect_system_status.assert_not_called()


@pytest.mark.parametrize("json_output", [False, True])
def test_all_metrics_unavailable(monkeypatch: pytest.MonkeyPatch, json_output: bool) -> None:
    fields = ("operating_system", "hostname", "uptime_seconds", "cpu_percent", "memory", "disk")
    failed = SystemStatus(**dict.fromkeys(fields), errors=dict.fromkeys(fields, "Read denied"))
    monkeypatch.setattr(status_command, "collect_system_status", lambda: failed)

    result = runner.invoke(app, ["status", "--json"] if json_output else ["status"])

    assert result.exit_code == 1
    if json_output:
        assert json.loads(result.stdout) == asdict(failed)
    else:
        assert "Unavailable" in result.output
        assert "0.0%" not in result.output
