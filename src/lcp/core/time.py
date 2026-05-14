"""Shared time utilities for the LCP codebase.

All DDL columns use ``DATETIME(3)`` (timezone-naive, millisecond precision).
Centralising the "UTC now, stripped of tzinfo" pattern avoids five separate
copies spread across services, schedulers, and executors.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return the current UTC time as a timezone-naive ``datetime``.

    Matches the ``DATETIME(3)`` column type used throughout the DDL: MySQL
    stores naive timestamps, and SQLite's ``CURRENT_TIMESTAMP`` is also
    naive.  Stripping ``tzinfo`` keeps equality checks in tests working
    when the other side comes from ``datetime.utcnow()`` or ``func.now()``.
    """

    return datetime.now(timezone.utc).replace(tzinfo=None)
