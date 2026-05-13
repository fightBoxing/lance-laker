"""One-off ALTER TABLE migration to add the watcher fields.

Run once before exercising scripts/event_driven_smoke.py against an
existing MySQL with the pre-watcher schema.  Idempotent: skips columns
that already exist.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DDL = [
    (
        "ALTER TABLE vector_index ADD COLUMN last_seen_version "
        "BIGINT UNSIGNED DEFAULT NULL COMMENT 'lance latest_version "
        "last observed by event-driven watcher; NULL = never observed' "
        "AFTER last_merged_at"
    ),
    (
        "ALTER TABLE lifecycle_policy ADD COLUMN index_watch_enabled "
        "TINYINT(1) NOT NULL DEFAULT 0 AFTER index_optimize_cron"
    ),
    (
        "ALTER TABLE lifecycle_policy ADD COLUMN index_watch_min_unindexed_rows "
        "INT UNSIGNED DEFAULT 1000 COMMENT 'unindexed-rows threshold; "
        "NULL disables this signal' AFTER index_watch_enabled"
    ),
    (
        "ALTER TABLE lifecycle_policy ADD COLUMN index_watch_min_version_drift "
        "INT UNSIGNED DEFAULT 1 COMMENT 'lance version-drift threshold; "
        "NULL disables this signal' AFTER index_watch_min_unindexed_rows"
    ),
    (
        "ALTER TABLE lifecycle_policy ADD COLUMN index_watch_stale_minutes "
        "INT UNSIGNED DEFAULT 30 COMMENT 'force optimise after N minutes; "
        "NULL disables stale fallback' AFTER index_watch_min_version_drift"
    ),
]


async def _run() -> int:
    dsn = os.environ.get("LCP_DB_DSN")
    if not dsn:
        print("LCP_DB_DSN not set", file=sys.stderr)
        return 1
    engine = create_async_engine(dsn, future=True)
    try:
        async with engine.begin() as conn:
            for stmt in DDL:
                try:
                    await conn.execute(text(stmt))
                    print(f"OK   {stmt[:90]}...", flush=True)
                except Exception as exc:  # noqa: BLE001
                    if "Duplicate column name" in str(exc):
                        print(f"SKIP {stmt[:90]}...", flush=True)
                    else:
                        print(f"ERR  {stmt[:90]}... -> {exc}", flush=True)
                        return 2
    finally:
        await engine.dispose()
    print("migration complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
