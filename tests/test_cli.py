"""Tests for the public CLI interface."""

from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from autoflow.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "AutoFlow CLI" in result.output
    assert "--help" in result.output
    assert "--version" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"AutoFlow CLI {version('autoflow-cli')}"
    assert "Usage:" not in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--version" in result.output


@pytest.mark.parametrize("argument", ["--unknown-option", "unknown-command"])
def test_invalid_input(argument: str) -> None:
    result = runner.invoke(app, [argument])

    assert result.exit_code == 2
    assert "Error" in result.output
