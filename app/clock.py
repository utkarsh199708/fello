"""The one place in ``app/`` that reads the wall clock.

Everything else takes time as an argument (``as_of``) or from a config file, which is what makes a
rerun of any night reproducible. Only the worker calls ``now_utc``, once per scheduled fire, and
passes the result down as ``as_of``.

``CLOCK_OVERRIDE`` pins "now" to a fixed ISO 8601 instant, for demos and backtests: the worker
then behaves as if every fire happened at that instant, so every fire after the first finds the
night already done and is a ``no_op``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from app.config import parse_utc


def now_utc() -> datetime:
    """``CLOCK_OVERRIDE`` when it is set and non-empty, otherwise the wall clock, aware in UTC."""
    override = os.environ.get("CLOCK_OVERRIDE", "").strip()
    if override:
        return parse_utc(override)
    return datetime.now(UTC)
