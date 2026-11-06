"""Deterministic collection and threshold tests without host metric reads."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from autoflow.core import system
from autoflow.core.health import classify_usage


@pytest.fixture
def metric_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    reads = {
        "operating_system": Mock(return_value={"NAME": "Fedora Linux", "VERSION_ID": "44"}),
        "hostname": Mock(return_value="test-host"),
        "uptime_seconds": Mock(return_value=9939.0),
        "cpu_percent": Mock(return_value=42.5),
        "memory": Mock(
            return_value=SimpleNamespace(total=16 * 1024**3, available=6 * 1024**3, percent=62.5)
        ),
        "disk": Mock(
            return_value=SimpleNamespace(total=100 * 1024**3, used=92 * 1024**3, percent=92.0)
        ),
    }
    monkeypatch.setattr(system.platform, "freedesktop_os_release", reads["operating_system"])
    monkeypatch.setattr(system.socket, "gethostname", reads["hostname"])
    monkeypatch.setattr(system.time, "time", lambda: 100000.0)
    for attribute, field in (
        ("boot_time", "uptime_seconds"),
        ("cpu_percent", "cpu_percent"),
        ("virtual_memory", "memory"),
        ("disk_usage", "disk"),
    ):
        monkeypatch.setattr(system.psutil, attribute, reads[field])
    return reads


def test_collect_snapshot(metric_reads: dict[str, Mock]) -> None:
    snapshot = system.collect_system_status()

    assert snapshot.operating_system == system.OperatingSystem("Fedora Linux", "44")
    assert snapshot.hostname == "test-host"
    assert snapshot.uptime_seconds == 90061.0
    assert snapshot.cpu_percent == 42.5
    assert snapshot.memory == system.ResourceUsage(62.5, 10 * 1024**3, 16 * 1024**3)
    assert snapshot.disk == system.ResourceUsage(92.0, 92 * 1024**3, 100 * 1024**3)
    assert snapshot.disk_path == "/"
    assert snapshot.errors == {}
    metric_reads["cpu_percent"].assert_called_once_with(interval=0.1)
    metric_reads["disk"].assert_called_once_with("/")


@pytest.mark.parametrize(
    "field",
    ["operating_system", "hostname", "uptime_seconds", "cpu_percent", "memory", "disk"],
)
def test_failed_read_preserves_other_fields(field: str, metric_reads: dict[str, Mock]) -> None:
    metric_reads[field].side_effect = PermissionError("Read denied")

    snapshot = system.collect_system_status()

    assert getattr(snapshot, field) is None
    assert snapshot.errors == {field: "Read denied"}
    for other in metric_reads.keys() - {field}:
        assert getattr(snapshot, other) is not None


def test_all_reads_fail(metric_reads: dict[str, Mock]) -> None:
    for read in metric_reads.values():
        read.side_effect = OSError("Unavailable")

    snapshot = system.collect_system_status()

    assert set(snapshot.errors) == set(metric_reads)
    assert all(getattr(snapshot, field) is None for field in metric_reads)


def test_psutil_error(metric_reads: dict[str, Mock]) -> None:
    metric_reads["cpu_percent"].side_effect = psutil.AccessDenied()

    snapshot = system.collect_system_status()

    assert snapshot.cpu_percent is None
    assert "cpu_percent" in snapshot.errors


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (
            {"PRETTY_NAME": "Example Linux Rolling", "NAME": "Example Linux", "VERSION": "Rolling"},
            system.OperatingSystem("Example Linux Rolling", ""),
        ),
        (
            {"PRETTY_NAME": "Example Linux 1 (Stable)", "NAME": "Example Linux", "VERSION_ID": "1"},
            system.OperatingSystem("Example Linux 1 (Stable)", ""),
        ),
        ({"PRETTY_NAME": "Example Linux"}, system.OperatingSystem("Example Linux", "")),
        (
            {"NAME": "Example Linux", "VERSION": "Rolling"},
            system.OperatingSystem("Example Linux", "Rolling"),
        ),
        (
            {"NAME": "Example Linux", "VERSION_ID": "1", "VERSION": "1 (Stable)"},
            system.OperatingSystem("Example Linux", "1"),
        ),
        ({"NAME": "Linux"}, system.OperatingSystem("Linux", "")),
        (
            {"PRETTY_NAME": "", "NAME": "Example Linux", "VERSION_ID": "", "VERSION": "Rolling"},
            system.OperatingSystem("Example Linux", "Rolling"),
        ),
        ({}, system.OperatingSystem("Unknown operating system", "")),
        (
            {"PRETTY_NAME": "", "NAME": "", "VERSION_ID": "", "VERSION": ""},
            system.OperatingSystem("Unknown operating system", ""),
        ),
        ({"VERSION": "Rolling"}, system.OperatingSystem("Unknown operating system", "Rolling")),
    ],
)
def test_os_identification_fallback_preserves_metrics(
    metric_reads: dict[str, Mock], release: dict[str, str], expected: system.OperatingSystem
) -> None:
    baseline = system.collect_system_status()
    metric_reads["operating_system"].return_value = release

    snapshot = system.collect_system_status()

    assert snapshot == replace(baseline, operating_system=expected)
    assert snapshot.errors == {}


def test_future_boot_time(metric_reads: dict[str, Mock]) -> None:
    metric_reads["uptime_seconds"].return_value = 100001.0

    snapshot = system.collect_system_status()

    assert snapshot.uptime_seconds is None
    assert "uptime_seconds" in snapshot.errors


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        (0, "normal"),
        (69.99, "normal"),
        (70, "warning"),
        (89.99, "warning"),
        (90, "critical"),
        (100, "critical"),
    ],
)
def test_thresholds(percent: float, expected: str) -> None:
    assert classify_usage(percent) == expected
