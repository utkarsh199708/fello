"""Small UTC date helpers for the generator.

Inside the generator an instant is an integer count of seconds since the Unix epoch. Integers
sort and subtract without float or timezone surprises, and they format the same on every OS.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime


def add_months(dt: datetime, months: int) -> datetime:
    """Move `dt` by whole months, clamping the day (31 Jan + 1 month = 28 or 29 Feb)."""
    year, month0 = divmod(dt.year * 12 + dt.month - 1 + months, 12)
    day = min(dt.day, calendar.monthrange(year, month0 + 1)[1])
    return dt.replace(year=year, month=month0 + 1, day=day)


def month_floor(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=UTC)


def iso(seconds: int) -> str:
    """The timestamp format used in every generated file."""
    return from_epoch(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")
