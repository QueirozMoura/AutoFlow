"""Exercise backup commands against temporary data, including piped JSON."""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import backup as command
from autoflow.core.backup import BackupError

runner = CliRunner()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source = tmp_path / "input [data]"
    source.mkdir()
    (source / "file.txt").write_text("temporary backup fixture\n")
    return source


def test_backup_help() -> None:
    result = runner.invoke(app, ["backup", "--help"])
    assert result.exit_code == 0
    assert "--output" in result.output
    assert "--dry-run" in result.output
    assert "--json" in result.output


def test_human_backup(source: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["backup", str(source), "--output", str(tmp_path / "backups")])
    assert result.exit_code == 0, result.output
    assert "Backup completed" in result.stdout
    assert "Original size" in result.stdout
    assert "Compressed size" in result.stdout
    assert "Duration" in result.stdout
    archives = list((tmp_path / "backups").glob("*.tar.gz"))
    assert len(archives) == 1
    # Long generated names must remain visible even on narrow terminals.
    assert archives[0].name in "".join(result.stdout.replace("│", "").split())


def test_json_result(source: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["backup", str(source), "--output", str(tmp_path / "backups"), "--json"],
        color=True,
        env={"FORCE_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["dry_run"] is False
    assert data["file_count"] == 1
    assert data["original_bytes"] == 25
    assert data["compressed_bytes"] == Path(data["archive"]).stat().st_size
    assert data["files"] == ["input [data]/file.txt"]
    assert result.stderr == "" and "\x1b" not in result.stdout


@pytest.mark.parametrize("json_output", [False, True])
def test_dry_run(source: Path, tmp_path: Path, json_output: bool) -> None:
    before = sorted(tmp_path.rglob("*"))
    args = ["backup", str(source), "--output", str(tmp_path / "missing" / "backups"), "--dry-run"]
    result = runner.invoke(app, args + (["--json"] if json_output else []))
    assert result.exit_code == 0, result.output
    assert sorted(tmp_path.rglob("*")) == before
    if json_output:
        data = json.loads(result.stdout)
        assert data["dry_run"] is True and data["archive"] is None
        assert data["file_count"] == 1 and data["original_bytes"] == 25
        assert result.stderr == ""
    else:
        assert "no files created" in result.stdout
        assert "file.txt" in result.stdout
        assert "25 bytes" in result.stdout


@pytest.mark.parametrize("json_output", [False, True])
def test_missing_source_error(tmp_path: Path, json_output: bool) -> None:
    args = ["backup", str(tmp_path / "absent"), "--output", str(tmp_path / "backups")]
    result = runner.invoke(app, args + (["--json"] if json_output else []))
    assert result.exit_code == 1
    assert not (tmp_path / "backups").exists()
    if json_output:
        data = json.loads(result.stdout)
        assert data["success"] is False and data["error"]
        assert result.stderr == "" and "\x1b" not in result.stdout
    else:
        assert "Backup failed" in result.stderr
        assert "Backup completed" not in result.stdout


def test_json_permission_error(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        command, "create_backup", Mock(side_effect=BackupError("Permission denied"))
    )
    result = runner.invoke(app, ["backup", str(source), "-o", str(tmp_path / "backups"), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "Permission denied"
    assert result.stderr == ""


def test_json_integration_through_pipe(source: Path, tmp_path: Path) -> None:
    # Console entry point of this environment, with an actual non-terminal stdout.
    executable = Path(sys.executable).parent / "autoflow"
    result = subprocess.run(
        [str(executable), "backup", str(source), "-o", str(tmp_path / "backups"), "--json"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert result.stderr == "" and "\x1b" not in result.stdout
    with tarfile.open(data["archive"]) as archive:
        assert archive.getnames() == ["input [data]", "input [data]/file.txt"]
        assert archive.extractfile("input [data]/file.txt").read() == b"temporary backup fixture\n"


def test_exclusions_are_visible(source: Path, tmp_path: Path) -> None:
    (source / "link").symlink_to(source / "file.txt")
    result = runner.invoke(
        app, ["backup", str(source), "-o", str(tmp_path / "backups"), "--dry-run"]
    )
    assert result.exit_code == 0
    assert "Excluded by safety policy" in result.stdout
    assert "symbolic link" in result.stdout


def test_main_help_lists_backup() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "backup" in result.output
