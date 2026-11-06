"""Bounded, timeout-controlled journalctl access without a shell or elevation."""

import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

from autoflow.core.logs import LogEntry, LogsError


class JournalError(Exception):
    """The local journal could not be read safely."""


def parse_journal(line: str) -> LogEntry:
    """Retain only MESSAGE and PRIORITY; never propagate raw JSON in errors."""
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError
        message = record.get("MESSAGE", "")
        if isinstance(message, list):
            if not all(type(value) is int and 0 <= value <= 255 for value in message):
                raise ValueError
            message = bytes(message).decode("utf-8", errors="replace")
        if not isinstance(message, str):
            raise ValueError
        priority = record.get("PRIORITY")
        if isinstance(priority, str) and priority in tuple(str(n) for n in range(8)):
            priority = int(priority)
        elif type(priority) is not int or not 0 <= priority <= 7:
            priority = None
        return LogEntry(message, priority)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LogsError("Invalid journal JSON record (content omitted)") from exc


class JournalReader:
    """A readline interface that also drains bounded diagnostics until EOF."""

    def __init__(self, process: subprocess.Popen, timeout: float) -> None:
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.buffer = bytearray()
        self.diagnostic_bytes = 0
        self.finished = False
        self.selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        try:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
        except BaseException:
            self.selector.close()
            raise

    def readline(self, size: int) -> bytes:
        while True:
            newline = self.buffer.find(b"\n", 0, size)
            if newline >= 0:
                length = newline + 1
                result = bytes(self.buffer[:length])
                del self.buffer[:length]
                return result
            if len(self.buffer) >= size:
                result = bytes(self.buffer[:size])
                del self.buffer[:size]
                return result
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise JournalError("journalctl timed out; narrow --since or increase --timeout")
            if not self.selector.get_map():
                try:
                    code = self.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    raise JournalError("journalctl timed out") from exc
                self.finished = True
                if code:
                    raise JournalError(
                        f"journalctl failed (exit {code}); check journal permissions"
                    )
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            for key, _ in self.selector.select(remaining):
                amount = min(65536, size - len(self.buffer)) if key.data == "stdout" else 4096
                if amount <= 0:
                    continue
                try:
                    data = os.read(key.fd, amount)
                except BlockingIOError:
                    continue
                if not data:
                    self.selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    self.buffer.extend(data)
                else:
                    self.diagnostic_bytes += len(data)
                    if self.diagnostic_bytes > 65536:
                        raise JournalError("journalctl diagnostic output exceeded 64 KiB")

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass  # It exited between poll and kill.
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise JournalError("Could not reap journalctl after cancellation") from exc
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self.process.stderr is not None:
                self.process.stderr.close()


@contextmanager
def open_journal(since: str = "1h", timeout: float = 10.0) -> Iterator[JournalReader]:
    """Only relative, positive s/m/h/d/w intervals are accepted for --since."""
    if not re.fullmatch(r"[1-9]\d{0,5}[smhdw]", since):
        raise JournalError("--since must be a positive interval such as 30m, 1h, or 2d")
    if not 0 < timeout <= 3600:
        raise JournalError("Journal timeout must be positive and at most 3600 seconds")
    arguments = [
        "journalctl",
        "--no-pager",
        "--no-ask-password",
        "--all",
        "--output=json",
        "--output-fields=MESSAGE,PRIORITY",
        f"--since=-{since}",
    ]
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise JournalError("journalctl is not installed or is unavailable on PATH") from exc
    except OSError as exc:
        raise JournalError("Cannot start journalctl; check executable permissions") from exc
    reader = None
    try:
        reader = JournalReader(process, timeout)
        yield reader
    finally:
        if reader is not None:
            reader.close()
        else:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
