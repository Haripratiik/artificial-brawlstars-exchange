"""Simulated time.

Nanoseconds as integers, for the same reason prices are ticks as integers:
exactness. A simulation that compares floating-point timestamps can order two
events differently on two machines, which would make a "reproducible" seeded run
reproducible only by luck. Integer nanoseconds compare exactly and cover roughly
292 years in a signed 64-bit value, which is more than any experiment needs.

Nanoseconds specifically, rather than microseconds, because the latency
differences this project studies are small. A co-located market maker and a
retail agent may differ by five orders of magnitude, and rounding the fast end to
microseconds would erase the distinction the experiment is about.
"""

from __future__ import annotations

from typing import NewType

__all__ = [
    "Timestamp",
    "Duration",
    "NANOS_PER_MICRO",
    "NANOS_PER_MILLI",
    "NANOS_PER_SECOND",
    "micros",
    "millis",
    "seconds",
    "minutes",
    "format_timestamp",
]

Timestamp = NewType("Timestamp", int)
Duration = NewType("Duration", int)

NANOS_PER_MICRO = 1_000
NANOS_PER_MILLI = 1_000_000
NANOS_PER_SECOND = 1_000_000_000


def micros(value: float) -> Duration:
    return Duration(int(value * NANOS_PER_MICRO))


def millis(value: float) -> Duration:
    return Duration(int(value * NANOS_PER_MILLI))


def seconds(value: float) -> Duration:
    return Duration(int(value * NANOS_PER_SECOND))


def minutes(value: float) -> Duration:
    return Duration(int(value * 60 * NANOS_PER_SECOND))


def format_timestamp(value: Timestamp) -> str:
    """Human-readable elapsed time, for logs and traces."""
    total_seconds, nanos = divmod(int(value), NANOS_PER_SECOND)
    hours, remainder = divmod(total_seconds, 3600)
    mins, secs = divmod(remainder, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}.{nanos // 1_000_000:03d}"
