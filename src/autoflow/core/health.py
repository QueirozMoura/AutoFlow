"""Utilization thresholds without presentation or system dependencies."""

from typing import Literal

UsageLevel = Literal["normal", "warning", "critical"]


def classify_usage(percent: float) -> UsageLevel:
    """Classify the unrounded percentage using the 70% and 90% boundaries."""
    if percent >= 90:
        return "critical"
    if percent >= 70:
        return "warning"
    return "normal"
