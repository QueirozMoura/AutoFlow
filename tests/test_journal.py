"""Simulate journalctl with synthetic subprocesses; never query the real journal."""

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from autoflow.core.logs import LogsError, analyze_stream
from autoflow.integrations import journal


def fake_journal(monkeypatch: pytest.MonkeyPatch, code: str):
    real_popen = subprocess.Popen
    launched = []

    def launch(arguments, **kwargs):
        assert arguments[0] == "journalctl" and kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        process = real_popen([sys.executable, "-c", code], **kwargs)
        launched.append((arguments, process))
        return process

    monkeypatch.setattr(journal.subprocess, "Popen", launch)
    return launched


def test_stream_json_arguments_and_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = fake_journal(monkeypatch, 'print(\'{"MESSAGE":"unknown event", "PRIORITY":"3"}\')')
    with journal.open_journal("1h") as stream:
        result = analyze_stream(stream, source="journal", parser=journal.parse_journal)
        assert stream.finished
    assert result.severity_counts["ERROR"] == 1
    assert "--since=-1h" in launched[0][0] and "--no-pager" in launched[0][0]
    assert "--output-fields=MESSAGE,PRIORITY" in launched[0][0]
    assert launched[0][1].poll() == 0


@pytest.mark.parametrize(
    "priority,expected", [("0", 0), ("7", 7), (4, 4), ("9", None), (True, None)]
)
def test_record_priority(priority, expected) -> None:
    record = journal.parse_journal(json.dumps({"MESSAGE": "event", "PRIORITY": priority}))
    assert record.priority == expected


def test_binary_message() -> None:
    record = journal.parse_journal('{"MESSAGE":[69,82,82,79,82,255],"PRIORITY":"3"}')
    assert record.message == "ERROR\ufffd" and record.priority == 3


@pytest.mark.parametrize(
    "data", ["not JSON private-secret", "[]", '{"MESSAGE":{}}', '{"MESSAGE":[999]}']
)
def test_invalid_records_do_not_echo_data(data: str) -> None:
    with pytest.raises(LogsError, match="content omitted") as error:
        journal.parse_journal(data)
    assert data not in str(error.value)


@pytest.mark.parametrize(
    "since", ["--help", "1h; touch /tmp/private", "0h", "-1h", "yesterday", "1h\n"]
)
def test_since_validation_never_starts_process(since: str, monkeypatch: pytest.MonkeyPatch) -> None:
    popen = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(journal.subprocess, "Popen", popen)
    with pytest.raises(journal.JournalError, match="since"):
        with journal.open_journal(since):
            pass
    popen.assert_not_called()


def test_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journal.subprocess, "Popen", Mock(side_effect=FileNotFoundError))
    with pytest.raises(journal.JournalError, match="not installed"):
        with journal.open_journal():
            pass


def test_permission_failure_omits_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = fake_journal(
        monkeypatch, 'import sys; sys.stderr.write("private permission denied"); sys.exit(1)'
    )
    with pytest.raises(journal.JournalError, match="permissions") as error:
        with journal.open_journal() as stream:
            analyze_stream(stream, source="journal", parser=journal.parse_journal)
    assert "private" not in str(error.value)
    assert launched[0][1].poll() == 1


def test_timeout_kills_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = fake_journal(monkeypatch, "import time; time.sleep(30)")
    with pytest.raises(journal.JournalError, match="timed out"):
        with journal.open_journal(timeout=0.05) as stream:
            stream.readline(1024)
    assert launched[0][1].poll() is not None


def test_early_limit_cancels_child(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = fake_journal(
        monkeypatch, 'import time; print("ERROR synthetic", flush=True); time.sleep(30)'
    )
    with journal.open_journal() as stream:
        result = analyze_stream(stream, source="journal", max_lines=1)
        assert result.limited and not stream.finished
    assert launched[0][1].poll() is not None


def test_stderr_drained_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = fake_journal(
        monkeypatch, 'import sys; sys.stderr.write("x"*70000); sys.stderr.flush()'
    )
    with pytest.raises(journal.JournalError, match="diagnostic output exceeded"):
        with journal.open_journal() as stream:
            stream.readline(1024)
    assert launched[0][1].poll() is not None


def test_diagnostics_with_success_are_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_journal(
        monkeypatch,
        'import sys; sys.stderr.write("restricted journals"); print(\'{"MESSAGE":"ready"}\')',
    )
    with journal.open_journal() as stream:
        result = analyze_stream(stream, source="journal", parser=journal.parse_journal)
        assert result.total_lines == 1 and stream.diagnostic_bytes > 0


def test_empty_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_journal(monkeypatch, "pass")
    with journal.open_journal() as stream:
        assert analyze_stream(stream, source="journal").total_lines == 0
