"""Streaming analysis and privacy tests with synthetic logs only."""

import io
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoflow.core import logs as core


def analyze(data: bytes, **options) -> core.LogsResult:
    return core.analyze_stream(io.BytesIO(data), source="synthetic", **options)


@pytest.mark.parametrize(
    "token,expected",
    [
        ("error", "ERROR"),
        ("ERR", "ERROR"),
        ("fatal", "ERROR"),
        ("CRITICAL", "ERROR"),
        ("panic", "ERROR"),
        ("exception", "ERROR"),
        ("failed", "ERROR"),
        ("warn", "WARNING"),
        ("WARNING", "WARNING"),
        ("notice", "INFO"),
        ("INFORMATIONAL", "INFO"),
        ("info", "INFO"),
        ("trace", "DEBUG"),
        ("DEBUG", "DEBUG"),
        ("no level here", "UNKNOWN"),
    ],
)
def test_severity_variants(token: str, expected: str) -> None:
    assert core.severity(core.LogEntry(f"[{token}] message")) == expected


def test_first_token_and_structured_priority() -> None:
    assert core.severity(core.LogEntry("INFO previous ERROR resolved")) == "INFO"
    assert core.severity(core.LogEntry("<4> message")) == "WARNING"
    assert core.severity(core.LogEntry("ERROR text", priority=6)) == "INFO"
    assert core.severity(core.LogEntry("message", priority=0)) == "ERROR"
    assert core.severity(core.LogEntry("message", priority=7)) == "DEBUG"
    assert core.severity(core.LogEntry("<999> message")) == "UNKNOWN"


def test_summary_filter_and_top() -> None:
    data = b"ERROR failure 1\nWARNING disk\nINFO ready\nDEBUG value\nno severity\nERROR failure 2\n"
    result = analyze(data, errors_only=True, top=1)
    assert result.total_lines == 6 and result.bytes_read == len(data)
    assert result.severity_counts == {"ERROR": 2, "WARNING": 1, "INFO": 1, "DEBUG": 1, "UNKNOWN": 1}
    assert result.selected_lines == 2
    assert result.groups == [core.MessageGroup("ERROR", "ERROR failure <n>", 2)]
    assert not result.limited


def test_timestamp_and_dynamic_values_grouped() -> None:
    result = analyze(
        b"2026-11-06T10:00:00Z ERROR worker[123] request_id=abc failed at 10.0.0.1\n"
        b"2026-11-06T10:01:01Z ERROR worker[456] request_id=xyz failed at 10.0.0.2\n"
    )
    assert len(result.groups) == 1 and result.groups[0].count == 2
    assert "2026" not in result.groups[0].message and "abc" not in result.groups[0].message


@pytest.mark.parametrize(
    "message,secret",
    [
        ("ERROR password=hunter2", "hunter2"),
        ('ERROR "password": "private secret"', "private secret"),
        ("ERROR api_key=private-key", "private-key"),
        ("ERROR Authorization: Bearer private-token", "private-token"),
        ("ERROR Bearer opaque-token", "opaque-token"),
        ("ERROR email alice@example.com", "alice@example.com"),
        ("ERROR server 192.168.1.23", "192.168.1.23"),
        ("ERROR server 2001:db8::1234", "2001:db8::1234"),
        ("ERROR https://alice:secret@example.com/path?token=private", "alice"),
        ("ERROR reading /home/alice/private.log", "/home/alice/private.log"),
    ],
)
def test_sensitive_data_not_retained(message: str, secret: str) -> None:
    result = analyze((message + "\n").encode())
    assert secret not in result.groups[0].message
    assert not hasattr(result.groups[0], "raw_message")


def test_invalid_encoding_and_terminal_controls() -> None:
    result = analyze(b"\x1b[31mERROR\x1b[0m invalid\xff\x00\x07\r\n")
    assert result.severity_counts["ERROR"] == 1
    assert "\ufffd" in result.groups[0].message
    assert all(ord(c) >= 32 for c in result.groups[0].message)
    assert "\x1b" not in result.groups[0].message


def test_empty_and_blank_lines() -> None:
    assert analyze(b"").total_lines == 0
    result = analyze(b"\n\nplain without newline")
    assert result.severity_counts["UNKNOWN"] == 3
    assert any(group.message == "<empty>" and group.count == 2 for group in result.groups)


@pytest.mark.parametrize(
    "limit,options",
    [
        ("max_lines", {"max_lines": 2}),
        ("max_bytes", {"max_bytes": 12}),
        ("max_line_bytes", {"max_line_bytes": 3}),
    ],
)
def test_read_limits_report_partial_results(limit: str, options: dict) -> None:
    stream = io.BytesIO(b"ERROR one\nINFO two\nWARNING three\n")
    result = core.analyze_stream(stream, source="synthetic", **options)
    assert result.limited and limit in result.limit_reasons
    if limit == "max_bytes":
        assert stream.tell() <= 12 and result.total_lines == 1
    elif limit == "max_lines":
        assert result.total_lines == 2 and stream.tell() == len(b"ERROR one\nINFO two\n")
    else:
        assert result.total_lines == 0 and stream.tell() == 4


def test_group_memory_bound_and_deterministic_ranking() -> None:
    result = analyze(b"ERROR apple\nERROR banana\nERROR cherry\nERROR apple\n", max_groups=2)
    assert result.total_lines == 4 and result.selected_lines == 4
    assert result.ungrouped_lines == 1 and result.limit_reasons == ["max_groups"]
    assert [group.count for group in result.groups] == [2, 1]
    assert result.groups[0].message == "ERROR apple"


def test_regular_file_read_only(tmp_path: Path) -> None:
    path = tmp_path / "system.log"
    path.write_bytes(b"ERROR synthetic\nINFO ok\n")
    before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
    result = core.analyze_file(path)
    assert result.total_lines == 2 and result.source == str(path)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)


def test_large_file_bounded_prefix(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    with path.open("wb") as stream:
        for _ in range(10000):
            stream.write(b"ERROR synthetic repeated\n" * 10)
    result = core.analyze_file(path, max_bytes=4096)
    assert result.bytes_read == 4096 and result.limited
    assert result.total_lines < 100000 and len(result.groups) == 1


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(core.LogsError, match="Cannot open"):
        core.analyze_file(tmp_path / "missing")


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink", "parent_link"])
def test_nonregular_and_link_paths_rejected(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "log"
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("ERROR private")
        source.symlink_to(target)
    else:
        source.symlink_to(tmp_path, target_is_directory=True)
        source = source / "target"
    with pytest.raises(core.LogsError):
        core.analyze_file(source)


def test_permission_and_midread_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core.os, "open", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(core.LogsError):
        core.analyze_file(tmp_path / "log")
    stream = Mock()
    stream.readline.side_effect = [b"ERROR first\n", OSError("sensitive raw content")]
    with pytest.raises(core.LogsError, match="Log read failed") as error:
        core.analyze_stream(stream, source="synthetic")
    assert "sensitive raw content" not in str(error.value)


@pytest.mark.parametrize(
    "option", ["top", "max_bytes", "max_lines", "max_line_bytes", "max_groups"]
)
def test_invalid_limits(option: str) -> None:
    with pytest.raises(core.LogsError, match="positive"):
        analyze(b"", **{option: 0})


def test_report_templates_bounded() -> None:
    result = analyze(b"ERROR " + b"a" * 2000 + b"\n")
    assert len(result.groups[0].message) == 512


def test_large_hex_line_and_unicode_display_controls() -> None:
    result = analyze(b"ERROR " + b"a" * 60000 + b"\n")
    assert result.total_lines == 1 and len(result.groups[0].message) == 512
    assert "\u202e" not in core.normalize_message("ERROR \u202ereversed")
