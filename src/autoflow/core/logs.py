"""Streaming log interpretation, privacy reduction, and bounded aggregation."""

import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from autoflow.core.backup import _FILE_FLAGS, BackupError, _absolute, _directory

DEFAULT_MAX_BYTES = 10 * 1024**2
DEFAULT_MAX_LINES = 100_000
DEFAULT_MAX_LINE_BYTES = 64 * 1024
DEFAULT_MAX_GROUPS = 1_000
SEVERITIES = ("ERROR", "WARNING", "INFO", "DEBUG", "UNKNOWN")
_LEVEL = re.compile(
    r"\b(ERROR|ERR|CRITICAL|CRIT|FATAL|PANIC|ALERT|EMERG(?:ENCY)?|SEVERE|EXCEPTION|"
    r"FAIL(?:ED|URE)?|WARNING|WARN|INFO(?:RMATIONAL)?|NOTICE|DEBUG|TRACE)\b",
    re.I,
)
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?\]?\s*")
_SYSLOG = re.compile(r"^\s*[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+")
_SECRET = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"""
    r"""refresh[_-]?token|authorization|cookie|set-cookie|session[_-]?id)["']?\s*[:=]\s*"""
    r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+(?:\s+[^\s,;]+)?)"""
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+")
_URL = re.compile(r"(?i)\b(?:https?|ftp)://[^\s]+")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![0-9a-fA-F:])[0-9a-fA-F]*:[0-9a-fA-F:.]+")
_PATH = re.compile(r"(?<!\w)/[^\s,;]+")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_ID = re.compile(r"(?i)\b(request[_-]?id|trace[_-]?id|transaction[_-]?id)[\"']?\s*[:=]\s*[\w-]+")
_NUMBER = re.compile(r"\b(?:0x[0-9a-fA-F]+|\d+)\b")


class LogsError(Exception):
    """Log analysis could not be completed."""


@dataclass(frozen=True)
class LogEntry:
    message: str
    priority: int | None = None


@dataclass(frozen=True)
class MessageGroup:
    severity: str
    message: str
    count: int


@dataclass(frozen=True)
class LogsResult:
    success: bool
    source_kind: str
    source: str
    total_lines: int
    bytes_read: int
    severity_counts: dict[str, int]
    selected_lines: int
    groups: list[MessageGroup]
    limited: bool
    limit_reasons: list[str]
    ungrouped_lines: int
    errors_only: bool
    top: int
    max_lines: int
    max_bytes: int
    max_line_bytes: int
    max_groups: int
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


def severity(entry: LogEntry) -> str:
    """Prefer structured priority, then syslog PRI, then the first level token."""
    priority = entry.priority
    message = _ANSI.sub("", entry.message)
    if priority is None and (match := re.match(r"^<(\d{1,3})>", message)):
        value = int(match[1])
        if value <= 191:
            priority = value % 8
    if priority is not None and 0 <= priority <= 7:
        return (
            "ERROR"
            if priority <= 3
            else ("WARNING" if priority == 4 else "INFO" if priority <= 6 else "DEBUG")
        )
    match = _LEVEL.search(message)
    if not match:
        return "UNKNOWN"
    value = match[1].upper()
    if value in ("WARN", "WARNING"):
        return "WARNING"
    if value in ("INFO", "INFORMATIONAL", "NOTICE"):
        return "INFO"
    if value in ("DEBUG", "TRACE"):
        return "DEBUG"
    return "ERROR"


def normalize_message(message: str) -> str:
    """Return a sanitized template, never a stored raw example."""
    message = _ANSI.sub("", message)
    message = "".join(
        c if unicodedata.category(c) not in ("Cc", "Cf", "Cs") else " " for c in message
    )
    message = re.sub(r"^<\d{1,3}>", "", message)
    message = _TIMESTAMP.sub("", message)
    message = _SYSLOG.sub("<host> ", message)
    message = _SECRET.sub(lambda match: f"{match[1].lower()}=<redacted>", message)
    message = _BEARER.sub("<redacted-auth>", message)
    message = _URL.sub("<url>", message)
    message = _EMAIL.sub("<email>", message)
    message = _IP.sub("<ip>", message)
    message = _IPV6.sub(lambda match: "<ip>" if match[0].count(":") >= 2 else match[0], message)
    message = _PATH.sub("<path>", message)
    message = _UUID.sub("<id>", message)
    message = _ID.sub(lambda match: f"{match[1].lower()}=<id>", message)
    message = _NUMBER.sub("<n>", message)
    return " ".join(message.split())[:512] or "<empty>"


def analyze_stream(
    stream: BinaryIO,
    *,
    source: str,
    source_kind: str = "file",
    parser: Callable[[str], LogEntry] = LogEntry,
    errors_only: bool = False,
    top: int = 10,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_groups: int = DEFAULT_MAX_GROUPS,
) -> LogsResult:
    """Analyze a bounded prefix, keeping exact counts for analyzed lines only."""
    started = time.monotonic()
    if min(top, max_lines, max_bytes, max_line_bytes, max_groups) < 1:
        raise LogsError("All analysis limits and top must be positive")
    counts = dict.fromkeys(SEVERITIES, 0)
    groups: dict[tuple[str, str], int] = {}
    total_lines = bytes_read = selected = ungrouped = 0
    reasons = []
    try:
        while total_lines < max_lines and bytes_read < max_bytes:
            remaining = max_bytes - bytes_read
            data = stream.readline(min(max_line_bytes + 1, remaining))
            if not data:
                break
            bytes_read += len(data)
            if len(data) > max_line_bytes:
                reasons.append("max_line_bytes")
                break
            if not data.endswith(b"\n") and bytes_read == max_bytes:
                reasons.append("max_bytes")
                break  # Never classify a possibly incomplete line.
            entry = parser(data.decode("utf-8", errors="replace").rstrip("\r\n"))
            level = severity(entry)
            total_lines += 1
            counts[level] += 1
            if errors_only and level != "ERROR":
                continue
            selected += 1
            key = (level, normalize_message(entry.message))
            if key in groups:
                groups[key] += 1
            elif len(groups) < max_groups:
                groups[key] = 1
            else:
                ungrouped += 1
        if total_lines == max_lines:
            reasons.append("max_lines")
        if bytes_read == max_bytes and "max_bytes" not in reasons:
            reasons.append("max_bytes")
    except OSError as exc:
        raise LogsError(f"Log read failed: {exc.strerror or type(exc).__name__}") from exc
    if ungrouped:
        reasons.append("max_groups")
    ranked = sorted(
        groups.items(), key=lambda item: (-item[1], SEVERITIES.index(item[0][0]), item[0][1])
    )
    return LogsResult(
        True,
        source_kind,
        source,
        total_lines,
        bytes_read,
        counts,
        selected,
        [MessageGroup(level, message, count) for (level, message), count in ranked[:top]],
        bool(reasons),
        reasons,
        ungrouped,
        errors_only,
        top,
        max_lines,
        max_bytes,
        max_line_bytes,
        max_groups,
        time.monotonic() - started,
    )


def analyze_file(path: Path, **options: object) -> LogsResult:
    """Open only regular files, without following symbolic path components."""
    try:
        path = _absolute(path)
        with _directory(path.parent) as parent:
            assert parent is not None
            descriptor = os.open(path.name, _FILE_FLAGS, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise LogsError("Log source must be a regular file")
                return analyze_stream(stream, source=str(path), **options)
    except (OSError, BackupError, ValueError, RuntimeError) as exc:
        raise LogsError(f"Cannot open log file: {exc}") from exc
