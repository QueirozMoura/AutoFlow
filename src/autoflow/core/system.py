"""Read-only system snapshots, independent of terminal presentation."""

import platform
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psutil


@dataclass(frozen=True)
class OperatingSystem:
    name: str
    version: str


@dataclass(frozen=True)
class ResourceUsage:
    percent: float
    used_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class SystemStatus:
    operating_system: OperatingSystem | None
    hostname: str | None
    uptime_seconds: float | None
    cpu_percent: float | None
    memory: ResourceUsage | None
    disk: ResourceUsage | None
    disk_path: str = "/"
    errors: dict[str, str] = field(default_factory=dict)


def _operating_system() -> OperatingSystem:
    release = platform.freedesktop_os_release()
    if pretty_name := release.get("PRETTY_NAME"):
        # PRETTY_NAME is a complete display name; do not append a version twice.
        return OperatingSystem(name=pretty_name, version="")
    return OperatingSystem(
        name=release.get("NAME") or "Unknown operating system",
        version=release.get("VERSION_ID") or release.get("VERSION") or "",
    )


def _uptime() -> float:
    uptime = time.time() - psutil.boot_time()
    if uptime < 0:
        raise ValueError("Boot time is later than the current clock")
    return uptime


def _memory() -> ResourceUsage:
    memory = psutil.virtual_memory()
    # Match psutil's percentage: available memory includes reclaimable cache.
    return ResourceUsage(memory.percent, memory.total - memory.available, memory.total)


def _disk() -> ResourceUsage:
    disk = psutil.disk_usage("/")
    return ResourceUsage(disk.percent, disk.used, disk.total)


def collect_system_status() -> SystemStatus:
    """Collect each field independently; failed reads are null with an error."""
    errors: dict[str, str] = {}

    def read[T](name: str, collect: Callable[[], T]) -> T | None:
        try:
            return collect()
        except (OSError, psutil.Error, ValueError, KeyError) as exc:
            errors[name] = str(exc) or type(exc).__name__
            return None

    return SystemStatus(
        operating_system=read("operating_system", _operating_system),
        hostname=read("hostname", socket.gethostname),
        uptime_seconds=read("uptime_seconds", _uptime),
        cpu_percent=read("cpu_percent", lambda: psutil.cpu_percent(interval=0.1)),
        memory=read("memory", _memory),
        disk=read("disk", _disk),
        errors=errors,
    )
