"""Quick inspector for the watcher's effects: dump the most recent
``watcher:*`` tasks and the index row state.

Read-only.  Useful as a "did it work?" pulse check before running the
e2e smoke or while tailing the watcher log.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine


async def _run() -> int:
    dsn = os.environ.get("LCP_DB_DSN")
    if not dsn:
        print("LCP_DB_DSN not set", file=sys.stderr)
        return 1
    engine = create_async_engine(dsn, future=True)
    try:
        async with engine.connect() as conn:
            print("=" * 70)
            print("RECENT vector_index ROWS")
            print("=" * 70)
            rows = (await conn.execute(text(
                "SELECT id, dataset_uuid, index_name, status, "
                "last_seen_version, last_optimized_at "
                "FROM vector_index ORDER BY id DESC LIMIT 10"
            ))).fetchall()
            for r in rows:
                print(f"  id={r[0]}  ds={r[1][:8]}...  name={r[2]}  "
                      f"status={r[3]}  seen_v={r[4]}  "
                      f"last_opt={r[5]}")

            print("")
            print("=" * 70)
            print("RECENT watcher:* TASKS (top 15)")
            print("=" * 70)
            tasks = (await conn.execute(text(
                "SELECT task_uuid, task_type, status, idempotency_key, "
                "JSON_EXTRACT(params, '$.trigger_reason') as reason, "
                "JSON_EXTRACT(params, '$.lance_version') as v, "
                "JSON_EXTRACT(params, '$.unindexed_rows') as urows, "
                "created_at "
                "FROM task "
                "WHERE idempotency_key LIKE 'watcher:%' "
                "ORDER BY id DESC LIMIT 15"
            ))).fetchall()
            for t in tasks:
                print(f"  uuid={t[0][:8]}  status={t[2]:9s}  "
                      f"reason={t[4]}  v={t[5]}  urows={t[6]}  "
                      f"key={t[3]}")

            print("")
            print("=" * 70)
            print("watcher:* TASK COUNT BY STATUS")
            print("=" * 70)
            counts = (await conn.execute(text(
                "SELECT status, COUNT(*) FROM task "
                "WHERE idempotency_key LIKE 'watcher:%' "
                "GROUP BY status"
            ))).fetchall()
            for c in counts:
                print(f"  {c[0]:12s}  {c[1]}")

    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
