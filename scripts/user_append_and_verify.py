"""User-perspective validation: write to lance directly, watch LCP cover it.

This is the script you'd hand to a user who asks "if I just call
``lance.write_dataset``, will LCP auto-build the index?".  It uses the
existing watched dataset (created by ``event_driven_smoke.py``) and
appends N more rows via PURE lance API — no HTTP, no LCP imports — then
polls the lance index_statistics until ``num_unindexed_rows == 0``,
proving the watcher + worker chain auto-covered the new rows.

How to run::

    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        python scripts/user_append_and_verify.py

Exit codes
----------
- ``0``  PASS  -- new rows were auto-indexed within the timeout.
- ``>0`` FAIL  -- rows still un-indexed after the timeout (check that
        the watcher + worker daemons are running, and that the policy
        for this dataset has ``index_watch_enabled=true``).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import lance
import numpy as np
import pyarrow as pa

# ---------------------------------------------------------------------------
# Constants -- match the dataset created by event_driven_smoke.py.
# ---------------------------------------------------------------------------

ENDPOINT = os.environ.get("LANCE_MINIO_ENDPOINT", "http://127.0.0.1:30900")
BUCKET = os.environ.get("LANCE_MINIO_BUCKET", "lcp-smoke")
TABLE_KEY = os.environ.get(
    "EVENT_SMOKE_TABLE", "event_driven_smoke.lance",
)
INDEX_NAME = os.environ.get("EVENT_SMOKE_INDEX", "vec_idx")
APPEND_ROWS = int(os.environ.get("APPEND_ROWS", "500"))
TIMEOUT_SECONDS = float(os.environ.get("VERIFY_TIMEOUT", "60"))
DIM = 8


def _storage_options() -> dict[str, str]:
    return {
        "endpoint": ENDPOINT,
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        "aws_secret_access_key": os.environ.get(
            "AWS_SECRET_ACCESS_KEY", "minioadmin",
        ),
        "aws_region": "us-east-1",
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def _build_table(start: int, count: int) -> pa.Table:
    """Make ``count`` fresh vectors starting at id ``start``."""

    rng = np.random.default_rng(seed=start)
    vec_type = pa.list_(pa.float32(), DIM)
    vectors = rng.normal(size=(count, DIM)).astype(np.float32).tolist()
    return pa.table(
        {
            "id": pa.array(
                list(range(start, start + count)), type=pa.int64(),
            ),
            "vec": pa.array(vectors, type=vec_type),
            "created_at": pa.array(
                [datetime.now(timezone.utc).replace(microsecond=0)] * count,
                type=pa.timestamp("us", tz="UTC"),
            ),
        },
    )


def main() -> int:
    uri = f"s3://{BUCKET}/{TABLE_KEY}"
    storage = _storage_options()

    print(f"target uri = {uri}")

    # 1. Snapshot the BEFORE state so we know what changed.
    ds_before = lance.dataset(uri, storage_options=storage)
    rows_before = ds_before.count_rows()
    version_before = int(ds_before.latest_version)
    stats_before = ds_before.stats.index_stats(INDEX_NAME)
    print(
        f"BEFORE: rows={rows_before}  "
        f"version={version_before}  "
        f"indexed={stats_before['num_indexed_rows']}  "
        f"unindexed={stats_before['num_unindexed_rows']}",
    )

    # 2. Append APPEND_ROWS rows via PURE lance.write_dataset.
    #
    # NB: NO call to LCP REST API.  This is exactly what a user would
    # do if they were unaware of LCP, or had a non-LCP ingestion
    # pipeline writing into the lance dataset.
    table = _build_table(start=rows_before + 100_000, count=APPEND_ROWS)
    lance.write_dataset(table, uri, mode="append", storage_options=storage)

    ds_after_write = lance.dataset(uri, storage_options=storage)
    rows_after_write = ds_after_write.count_rows()
    version_after_write = int(ds_after_write.latest_version)
    stats_after_write = ds_after_write.stats.index_stats(INDEX_NAME)
    print(
        f"AFTER WRITE: rows={rows_after_write}  "
        f"version={version_after_write}  "
        f"indexed={stats_after_write['num_indexed_rows']}  "
        f"unindexed={stats_after_write['num_unindexed_rows']}",
    )

    if rows_after_write != rows_before + APPEND_ROWS:
        print("FAIL: row count after write is wrong", file=sys.stderr)
        return 1
    if stats_after_write["num_unindexed_rows"] < APPEND_ROWS:
        # Lance counts the new rows as un-indexed immediately on append;
        # if it doesn't, our trigger logic has nothing to react to.
        print(
            "FAIL: lance did not flag the new rows as un-indexed",
            file=sys.stderr,
        )
        return 2

    # 3. Poll lance until ``num_unindexed_rows`` drops back to 0.
    #    This is the watcher + worker doing the work, asynchronously.
    print(
        f"polling for auto-optimize (timeout={TIMEOUT_SECONDS}s) ...",
    )
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_unindexed: int | None = None
    while time.monotonic() < deadline:
        ds_now = lance.dataset(uri, storage_options=storage)
        stats_now = ds_now.stats.index_stats(INDEX_NAME)
        current_unindexed = int(stats_now["num_unindexed_rows"])
        current_version = int(ds_now.latest_version)
        if current_unindexed != last_unindexed:
            print(
                f"  t={int(time.monotonic())}s "
                f"version={current_version} "
                f"unindexed={current_unindexed} "
                f"indexed={stats_now['num_indexed_rows']}",
            )
            last_unindexed = current_unindexed
        if current_unindexed == 0:
            print(
                f"PASS: all {APPEND_ROWS} new rows were auto-indexed by LCP "
                f"(final version={current_version}, "
                f"indexed_rows={stats_now['num_indexed_rows']})",
            )
            return 0
        time.sleep(2.0)

    print(
        f"FAIL: still {last_unindexed} unindexed rows after "
        f"{TIMEOUT_SECONDS}s; is the watcher+worker pair running?",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
