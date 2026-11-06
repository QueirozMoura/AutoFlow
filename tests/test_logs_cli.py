"""Rich, JSON, filtering, and temporary-file integration for logs."""

import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import logs as command
from autoflow.integrations.journal import JournalError

runner = CliRunner()


@pytest.fixture
def logfile(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic [data].log"
    path.write_text(
        "ERROR task 10 failed password=private\nINFO ready\n"
        "ERROR task 20 failed password=private\nWARNING disk\nno severity\n"
    )
    return path


def test_help_and_registration() -> None:
    result = runner.invoke(app, ["logs", "--help"])
    assert result.exit_code == 0
    for option in [
        "--file",
        "--journal",
        "--since",
        "--errors",
        "--json",
        "--top",
        "--max-bytes",
        "--max-lines",
        "--max-line-bytes",
        "--max-groups",
        "--timeout",
    ]:
        assert option in result.stdout
    assert "logs" in runner.invoke(app, ["--help"]).stdout


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("errors", [False, True])
def test_file_output(logfile: Path, json_output: bool, errors: bool) -> None:
    result = runner.invoke(
        app,
        [
            "logs",
            "--file",
            str(logfile),
            "--top",
            "1",
            *(["--json"] if json_output else []),
            *(["--errors"] if errors else []),
        ],
        color=True,
        env={"FORCE_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "private" not in result.stdout
    if json_output:
        data = json.loads(result.stdout)
        assert data["success"] and data["total_lines"] == 5
        assert data["severity_counts"]["ERROR"] == 2
        assert data["selected_lines"] == (2 if errors else 5)
        assert data["groups"][0]["count"] == 2 and len(data["groups"]) == 1
        assert "\x1b" not in result.stdout and result.stderr == ""
    else:
        assert "Log analysis" in result.stdout and "Most frequent messages" in result.stdout
        assert "Analyzed lines" in result.stdout


@pytest.mark.parametrize(
    "arguments", [[], ["--journal", "--file", "unused"], ["--file", "unused", "--since", "1h"]]
)
def test_source_selection_errors_are_json(arguments: list[str]) -> None:
    result = runner.invoke(app, ["logs", *arguments, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["success"] is False
    assert result.stderr == ""


def test_missing_file_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["logs", "--file", str(tmp_path / "missing"), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]
    assert result.stderr == ""


def test_partial_summary(logfile: Path) -> None:
    result = runner.invoke(app, ["logs", "--file", str(logfile), "--max-lines", "1", "--json"])
    data = json.loads(result.stdout)
    assert result.exit_code == 0 and data["limited"]
    assert data["total_lines"] == 1 and data["limit_reasons"] == ["max_lines"]
    human = runner.invoke(app, ["logs", "--file", str(logfile), "--max-lines", "1"])
    assert "Partial analysis" in human.stdout


def test_no_matching_errors(tmp_path: Path) -> None:
    path = tmp_path / "info.log"
    path.write_text("INFO ready\n")
    result = runner.invoke(app, ["logs", "--file", str(path), "--errors"])
    assert result.exit_code == 0 and "No matching messages" in result.stdout


def test_journal_command_and_diagnostic_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def fake_open(since, timeout):
        assert since == "1h" and timeout == 10.0
        stream = io.BytesIO(b'{"MESSAGE":"not textual ERROR", "PRIORITY":"6"}\n')
        stream.finished = True
        stream.diagnostic_bytes = 100
        yield stream

    monkeypatch.setattr(command, "open_journal", fake_open)
    result = runner.invoke(app, ["logs", "--journal", "--since", "1h", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["source_kind"] == "journal" and data["severity_counts"]["INFO"] == 1
    assert data["warnings"] and result.stderr == ""


def test_journal_error_json(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def denied(*args):
        raise JournalError("journal permission denied")
        yield

    monkeypatch.setattr(command, "open_journal", denied)
    result = runner.invoke(app, ["logs", "--journal", "--json"])
    assert (
        result.exit_code == 1 and json.loads(result.stdout)["error"] == "journal permission denied"
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    "option", ["--max-lines", "--max-bytes", "--max-line-bytes", "--max-groups", "--top"]
)
def test_invalid_limits(logfile: Path, option: str) -> None:
    result = runner.invoke(app, ["logs", "--file", str(logfile), option, "0"])
    assert result.exit_code == 2


def test_subprocess_integration_piped_json(logfile: Path) -> None:
    before = logfile.read_bytes(), logfile.stat().st_mode, logfile.stat().st_mtime_ns
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "autoflow"),
            "logs",
            "--file",
            str(logfile),
            "--errors",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["total_lines"] == 5 and data["selected_lines"] == 2
    assert data["groups"][0]["count"] == 2
    assert "private" not in result.stdout and "\x1b" not in result.stdout
    assert result.stderr == ""
    assert before == (logfile.read_bytes(), logfile.stat().st_mode, logfile.stat().st_mtime_ns)
