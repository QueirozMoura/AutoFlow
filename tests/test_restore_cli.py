"""CLI restore contract and subprocess integration with temporary data."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.core.backup import create_backup

runner = CliRunner()


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    source = tmp_path / "source [data]"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "nested/file.txt").write_text("restore integration\n")
    return Path(create_backup(source, tmp_path / "backups").archive)


def test_restore_help() -> None:
    result = runner.invoke(app, ["restore", "--help"])
    assert result.exit_code == 0
    for option in ["--output", "--dry-run", "--json", "--max-files", "--max-bytes"]:
        assert option in result.stdout
    assert "restore" in runner.invoke(app, ["--help"]).stdout


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_restore_output(backup: Path, tmp_path: Path, json_output: bool, dry_run: bool) -> None:
    output = tmp_path / "restored"
    args = ["restore", str(backup), "-o", str(output)]
    if dry_run:
        args.append("--dry-run")
    if json_output:
        args.append("--json")
    result = runner.invoke(app, args, color=True, env={"FORCE_COLOR": "1"})
    assert result.exit_code == 0, result.output
    if json_output:
        data = json.loads(result.stdout)
        assert data["success"] is True and data["dry_run"] == dry_run
        assert data["file_count"] == 1 and data["total_bytes"] == 20
        assert data["files"] == ["source [data]/nested/file.txt"]
        assert data["archive"] == str(backup) and data["output"] == str(output)
        assert result.stderr == "" and "\x1b" not in result.stdout
    else:
        assert ("no files created" if dry_run else "Restore completed") in result.stdout
        assert "Uncompressed size" in result.stdout
    assert output.exists() != dry_run
    if dry_run and not json_output:
        assert "file.txt" in result.stdout


@pytest.mark.parametrize("problem", ["missing", "corrupt", "exists", "limit"])
@pytest.mark.parametrize("json_output", [False, True])
def test_restore_errors(backup: Path, tmp_path: Path, problem: str, json_output: bool) -> None:
    output = tmp_path / "restored"
    extra = []
    if problem == "missing":
        backup = tmp_path / "missing"
    elif problem == "corrupt":
        backup.write_bytes(b"corrupt")
    elif problem == "exists":
        output.mkdir()
    else:
        extra = ["--max-bytes", "1"]
    result = runner.invoke(
        app,
        ["restore", str(backup), "-o", str(output), *extra, *(["--json"] if json_output else [])],
    )
    assert result.exit_code == 1
    if json_output:
        data = json.loads(result.stdout)
        assert data["success"] is False and data["error"]
        assert result.stderr == "" and "\x1b" not in result.stdout
    else:
        assert result.stderr and "Restore completed" not in result.stdout
    assert not list(tmp_path.glob(".autoflow-restore-*"))


def test_integration_piped_json(backup: Path, tmp_path: Path) -> None:
    original = backup.read_bytes()
    output = tmp_path / "restored"
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "autoflow"),
            "restore",
            str(backup),
            "-o",
            str(output),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["success"] and data["file_count"] == 1
    assert result.stderr == "" and "\x1b" not in result.stdout
    assert (output / "source [data]/nested/file.txt").read_text() == "restore integration\n"
    assert (output / "source [data]/empty").is_dir()
    assert (tmp_path / "source [data]/nested/file.txt").read_text() == "restore integration\n"
    assert backup.read_bytes() == original


@pytest.mark.parametrize("option,value", [("--max-files", "0"), ("--max-bytes", "-1")])
def test_invalid_limits(backup: Path, tmp_path: Path, option: str, value: str) -> None:
    result = runner.invoke(
        app, ["restore", str(backup), "-o", str(tmp_path / "restored"), option, value]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "restored").exists()
