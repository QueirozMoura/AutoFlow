"""Organization CLI previews and execution use temporary fixtures exclusively."""

import re
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoflow.cli import app
from autoflow.commands import organize as command
from autoflow.core.organizer import OrganizationResult, OrganizerError, PlanIssue

runner = CliRunner()


def test_help_and_registration(monkeypatch) -> None:
    executor = Mock(side_effect=AssertionError("help must never execute"))
    monkeypatch.setattr(command, "execute_organization", executor)
    result = runner.invoke(app, ["organize", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout and "required" in result.stdout.lower()
    assert "--execute" in result.stdout and "--yes" in result.stdout
    help_result = runner.invoke(app, ["--help"])
    assert "organize" in help_result.stdout
    for existing in ("backup", "restore", "logs", "status"):
        assert existing in help_result.stdout
    executor.assert_not_called()


def test_mode_required_before_inspecting_source(tmp_path: Path, monkeypatch) -> None:
    planner = Mock(side_effect=AssertionError("must not inspect without an explicit mode"))
    monkeypatch.setattr(command, "plan_organization", planner)
    result = runner.invoke(app, ["organize", str(tmp_path)])
    assert result.exit_code == 2
    assert "--dry-run" in result.output
    planner.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_preview_table_and_summary(tmp_path: Path) -> None:
    for name in ("a.txt", "b.TXT", "photo.jpg", "README"):
        (tmp_path / name).write_text("keep")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "not-planned.log").write_text("nested")
    (tmp_path / "ignored-link.txt").symlink_to(tmp_path / "a.txt")

    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "Original file" in result.stdout and "Proposed destination" in result.stdout
    for destination in ("txt/a.txt", "txt/b.TXT", "jpg/photo.jpg", "no_extension/README"):
        assert destination in result.stdout
    assert "Summary by extension" in result.stdout
    assert re.search(r"txt\s*│\s*2", result.stdout)
    assert re.search(r"no_extension\s*│\s*1", result.stdout)
    assert "Files planned: 4" in result.stdout
    assert "not-planned" not in result.stdout and "ignored-link" not in result.stdout
    assert "no files changed" in result.stdout


def test_empty_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "No regular files to organize" in result.stdout
    assert "Files planned: 0" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["existing_file", "existing_directory", "blocked_category"])
def test_collision_warnings_and_nonzero_exit(tmp_path: Path, kind: str) -> None:
    (tmp_path / "report.txt").write_text("original")
    category = tmp_path / "txt"
    if kind == "blocked_category":
        category.write_text("keep category file")
        reason = "not a real directory"
    else:
        category.mkdir()
        target = category / "report.txt"
        if kind == "existing_file":
            target.write_text("keep existing")
        else:
            target.mkdir()
        reason = "already exists"

    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])

    assert result.exit_code == 1
    assert "Organization preview" in result.stdout
    assert "Warning:" in result.stderr and reason in result.stderr
    assert "Invalid plan" in result.stderr
    assert (tmp_path / "report.txt").read_text() == "original"
    if kind == "existing_file":
        assert (category / "report.txt").read_text() == "keep existing"


def test_invalid_path_warning_is_display_safe(tmp_path: Path) -> None:
    original = tmp_path / "bad\x1b[31m\nname.txt"
    original.write_text("keep")
    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])
    assert result.exit_code == 1
    assert "Invalid destination path" in result.stderr
    assert "\\x1b" in result.stdout and "\\n" in result.stdout
    assert "\x1b" not in result.stdout + result.stderr
    assert original.read_text() == "keep"


@pytest.mark.parametrize("kind", ["missing", "regular_file", "symlink"])
def test_invalid_source(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    if kind == "regular_file":
        source.write_text("keep")
    elif kind == "symlink":
        source.symlink_to(tmp_path, target_is_directory=True)
    result = runner.invoke(app, ["organize", str(source), "--dry-run"])
    assert result.exit_code == 1
    assert result.stderr and "Organization preview" not in result.stdout


def test_planner_error_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        command, "plan_organization", Mock(side_effect=OrganizerError("inspection denied"))
    )
    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])
    assert result.exit_code == 1
    assert "inspection denied" in result.stderr and result.stdout == ""


def test_rich_markup_in_names_stays_literal(tmp_path: Path) -> None:
    (tmp_path / "[red]note.txt").touch()
    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "[red]note.txt" in result.stdout


def test_preview_preserves_source_and_existing_destination(tmp_path: Path) -> None:
    (tmp_path / "report.txt").write_bytes(b"keep original")
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt/report.txt").write_bytes(b"keep existing")
    (tmp_path / "empty").mkdir()
    paths = [tmp_path, *sorted(tmp_path.rglob("*"))]
    metadata = {path: path.stat() for path in paths}
    contents = {path: path.read_bytes() for path in paths if path.is_file()}

    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])

    assert result.exit_code == 1  # Existing destination invalidates only the plan.
    assert [tmp_path, *sorted(tmp_path.rglob("*"))] == paths
    for path, before in metadata.items():
        after = path.stat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
            before.st_ino,
            before.st_mode,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
    assert {path: path.read_bytes() for path in contents} == contents


@pytest.mark.parametrize("extra", [["--json"], ["--no-dry-run"], ["--dry-run=false"]])
def test_unsupported_modes_rejected(tmp_path: Path, extra: list[str]) -> None:
    result = runner.invoke(app, ["organize", str(tmp_path), *extra])
    assert result.exit_code == 2
    assert list(tmp_path.iterdir()) == []


def test_dry_run_never_calls_execution_or_confirmation(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").write_text("keep")
    executor = Mock(side_effect=AssertionError("dry-run must never execute"))
    confirm = Mock(side_effect=AssertionError("dry-run must not prompt"))
    monkeypatch.setattr(command, "execute_organization", executor)
    monkeypatch.setattr(command.typer, "confirm", confirm)
    result = runner.invoke(app, ["organize", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0 and "no files changed" in result.stdout
    executor.assert_not_called()
    confirm.assert_not_called()
    assert (tmp_path / "report.txt").read_text() == "keep"
    assert not (tmp_path / "txt").exists()


def test_execute_confirmed_shows_plan_before_prompt_and_moves(tmp_path: Path) -> None:
    (tmp_path / "report.txt").write_bytes(b"original")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/keep.txt").write_bytes(b"nested original")

    result = runner.invoke(app, ["organize", str(tmp_path), "--execute"], input="y\n")

    assert result.exit_code == 0, result.output
    assert result.stdout.index("Proposed destination") < result.stdout.index("Execute this")
    assert "Organization preview (execute)" in result.stdout
    assert "Organization completed" in result.stdout and "Moved files" in result.stdout
    assert "Ignored / not moved" in result.stdout and "Errors" in result.stdout
    assert "Directory or special file" in result.stdout
    assert "no automatic rollback" in result.stdout
    assert "Dry-run only" not in result.stdout and result.stderr == ""
    assert not (tmp_path / "report.txt").exists()
    assert (tmp_path / "txt/report.txt").read_bytes() == b"original"
    assert (tmp_path / "nested/keep.txt").read_bytes() == b"nested original"


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_cancel_does_not_call_execution_or_modify_files(
    tmp_path: Path, monkeypatch, answer
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"keep original")
    before = source.stat()
    executor = Mock(side_effect=AssertionError("cancelled execution must not run"))
    monkeypatch.setattr(command, "execute_organization", executor)
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute"], input=answer)
    assert result.exit_code == 0 and "cancelled" in result.stdout
    executor.assert_not_called()
    assert source.read_bytes() == b"keep original"
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert source.stat().st_ino == before.st_ino
    assert list(tmp_path.iterdir()) == [source]


def test_confirmation_eof_never_executes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").touch()
    executor = Mock(side_effect=AssertionError("EOF is not confirmation"))
    monkeypatch.setattr(command, "execute_organization", executor)
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute"], input="")
    assert result.exit_code == 1 and "Confirmation unavailable" in result.stdout
    executor.assert_not_called()
    assert not (tmp_path / "txt").exists()


def test_yes_skips_only_confirmation_and_delegates_to_core(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").write_text("original")
    real_execute = command.execute_organization
    executor = Mock(wraps=real_execute)
    confirm = Mock(side_effect=AssertionError("--yes must skip the prompt"))
    monkeypatch.setattr(command, "execute_organization", executor)
    monkeypatch.setattr(command.typer, "confirm", confirm)
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute", "--yes"])
    assert result.exit_code == 0, result.output
    executor.assert_called_once_with(tmp_path)
    confirm.assert_not_called()
    assert "Proposed destination" in result.stdout and "no automatic rollback" in result.stdout
    assert "Execute this organization plan?" not in result.stdout
    assert (tmp_path / "txt/report.txt").read_text() == "original"


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--yes"],
        ["--dry-run", "--yes"],
        ["--execute", "--dry-run"],
        ["--execute", "--dry-run", "--yes"],
    ],
)
def test_invalid_combinations_fail_before_planning(tmp_path: Path, monkeypatch, options) -> None:
    planner = Mock(side_effect=AssertionError("invalid options must not inspect paths"))
    executor = Mock(side_effect=AssertionError("invalid options must not execute"))
    monkeypatch.setattr(command, "plan_organization", planner)
    monkeypatch.setattr(command, "execute_organization", executor)
    result = runner.invoke(app, ["organize", str(tmp_path), *options])
    assert result.exit_code == 2
    planner.assert_not_called()
    executor.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("yes", [False, True])
def test_invalid_plan_cannot_be_confirmed_or_bypassed(
    tmp_path: Path, monkeypatch, yes: bool
) -> None:
    (tmp_path / "report.txt").write_text("original")
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt/report.txt").write_text("existing")
    executor = Mock(side_effect=AssertionError("invalid plan must not execute"))
    confirm = Mock(side_effect=AssertionError("invalid plan must not prompt"))
    monkeypatch.setattr(command, "execute_organization", executor)
    monkeypatch.setattr(command.typer, "confirm", confirm)
    result = runner.invoke(
        app, ["organize", str(tmp_path), "--execute", *(["--yes"] if yes else [])]
    )
    assert result.exit_code == 1 and "Invalid plan" in result.stderr
    executor.assert_not_called()
    confirm.assert_not_called()
    assert (tmp_path / "report.txt").read_text() == "original"
    assert (tmp_path / "txt/report.txt").read_text() == "existing"


def test_changed_plan_after_confirmation_requires_review(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").touch()

    def changed(*args, **kwargs):
        (tmp_path / "new.txt").write_text("not authorized by preview")
        return True

    executor = Mock(side_effect=AssertionError("changed plan must not execute"))
    monkeypatch.setattr(command.typer, "confirm", changed)
    monkeypatch.setattr(command, "execute_organization", executor)
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute"])
    assert result.exit_code == 1 and "Plan changed" in result.stdout
    executor.assert_not_called()
    assert (tmp_path / "report.txt").exists() and (tmp_path / "new.txt").exists()
    assert not (tmp_path / "txt").exists()


def test_execution_failure_summary_and_exit_code(tmp_path: Path, monkeypatch) -> None:
    original = tmp_path / "report.txt"
    original.write_text("keep")
    failed = OrganizationResult(
        success=False,
        source=tmp_path,
        moved=(),
        skipped=(PlanIssue(original, "Not moved: organization stopped"),),
        errors=(PlanIssue(original, "Permission denied"),),
        created_directories=(),
    )
    monkeypatch.setattr(command, "execute_organization", Mock(return_value=failed))
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute", "--yes"])
    assert result.exit_code == 1
    assert "Organization failed" in result.stdout
    assert "Ignored / not moved" in result.stdout and "Errors" in result.stdout
    assert "Permission denied" in result.stderr and "No automatic rollback" in result.stderr
    assert "Organization completed" not in result.stdout
    assert original.read_text() == "keep"


def test_real_partial_execution_is_reported_without_rollback(tmp_path: Path, monkeypatch) -> None:
    from autoflow.core import organizer as core

    (tmp_path / "a.txt").write_text("first original")
    (tmp_path / "b.txt").write_text("second original")
    real_move = core._move_noreplace
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("second move denied")
        return real_move(*args)

    monkeypatch.setattr(core, "_move_noreplace", fail_second)
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute", "--yes"])
    assert result.exit_code == 1
    assert "Organization partially completed" in result.stdout
    assert "Moved files" in result.stdout and "Ignored / not moved" in result.stdout
    assert "second move denied" in result.stderr and "No automatic rollback" in result.stderr
    assert (tmp_path / "txt/a.txt").read_text() == "first original"
    assert (tmp_path / "b.txt").read_text() == "second original"
    assert not (tmp_path / "a.txt").exists()


def test_execution_exception_is_displayed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "report.txt").touch()
    monkeypatch.setattr(
        command, "execute_organization", Mock(side_effect=OrganizerError("execution unavailable"))
    )
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute", "--yes"])
    assert result.exit_code == 1 and "execution unavailable" in result.stderr
    assert (tmp_path / "report.txt").exists() and not (tmp_path / "txt").exists()


def test_empty_execution_summary(tmp_path: Path) -> None:
    result = runner.invoke(app, ["organize", str(tmp_path), "--execute", "--yes"])
    assert result.exit_code == 0
    assert "Organization completed" in result.stdout and "Files planned: 0" in result.stdout
    assert list(tmp_path.iterdir()) == []
