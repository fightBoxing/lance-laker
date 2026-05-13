"""Dump the post-smoke state of the LCP DB (task + worker rows)."""

from __future__ import annotations

import pymysql


def main() -> int:
    conn = pymysql.connect(
        host="127.0.0.1",
        port=30306,
        user="lcp",
        password="lcp_dev_pwd",
        database="lcp",
    )
    cur = conn.cursor()

    print("=== latest 3 TTL_DELETE tasks ===")
    cur.execute(
        "SELECT task_uuid, task_type, status, worker_id, "
        "started_at, finished_at, "
        "JSON_UNQUOTE(JSON_EXTRACT(result, '$.mode')) AS mode, "
        "JSON_UNQUOTE(JSON_EXTRACT(result, '$.rows_after')) AS rows_after, "
        "JSON_UNQUOTE(JSON_EXTRACT(result, '$.predicate')) AS predicate "
        "FROM task WHERE task_type='TTL_DELETE' "
        "ORDER BY created_at DESC LIMIT 3",
    )
    rows = cur.fetchall()
    for r in rows:
        print(
            f"  task_uuid  = {r[0]}\n"
            f"  type       = {r[1]}\n"
            f"  status     = {r[2]}\n"
            f"  worker     = {r[3]}\n"
            f"  started    = {r[4]}\n"
            f"  finished   = {r[5]}\n"
            f"  mode       = {r[6]}\n"
            f"  rows_after = {r[7]}\n"
            f"  predicate  = {r[8]}\n"
        )
    print()

    print("=== latest 3 workers ===")
    cur.execute(
        "SELECT worker_id, worker_type, status, capacity, in_flight, "
        "last_heartbeat_at FROM worker_registry "
        "ORDER BY created_at DESC LIMIT 3",
    )
    for r in cur.fetchall():
        print(
            f"  worker_id      = {r[0]}\n"
            f"  type           = {r[1]}\n"
            f"  status         = {r[2]}\n"
            f"  capacity       = {r[3]}\n"
            f"  in_flight      = {r[4]}\n"
            f"  last_heartbeat = {r[5]}\n"
        )

    print("=== latest 3 lifecycle_policy rows ===")
    cur.execute(
        "SELECT policy_name, dataset_uuid, ttl_days, enabled, last_run_at "
        "FROM lifecycle_policy ORDER BY created_at DESC LIMIT 3",
    )
    for r in cur.fetchall():
        print(
            f"  policy     = {r[0]}\n"
            f"  dataset    = {r[1]}\n"
            f"  ttl_days   = {r[2]}\n"
            f"  enabled    = {r[3]}\n"
            f"  last_run   = {r[4]}\n"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
