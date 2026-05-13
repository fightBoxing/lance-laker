"""Lance + MinIO smoke test -- proves the lance data plane is operational.

Independent of LCP control plane.  Writes a tiny lance dataset to a
MinIO bucket via S3-compatible API, reads it back, deletes a row, and
asserts the row count went down.

This script intentionally does NOT live under ``src/lcp/``: it is a
*proof* of the storage stack, not a part of the control plane.  The
control plane's executors (TTL_DELETE / COMPACTION / INDEX_OPTIMIZE)
will plug into a similar code path in a follow-up.

Usage::

    # MinIO must be reachable on 127.0.0.1:30900 (Colima NodePort).
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        python scripts/lance_minio_smoke.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import lance
import pyarrow as pa


# Defaults match the buckets created in step 1 of the deployment dry-run.
ENDPOINT = os.environ.get("LANCE_MINIO_ENDPOINT", "http://127.0.0.1:30900")
BUCKET = os.environ.get("LANCE_MINIO_BUCKET", "lcp-smoke")
TABLE_KEY = os.environ.get("LANCE_MINIO_TABLE", "lance_smoke_table.lance")
ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin")
SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin")


def _storage_options() -> dict[str, str]:
    """Return the lance ``storage_options`` dict for MinIO via S3 API.

    ``allow_http=true`` is required because MinIO in this dev cluster
    is plain HTTP (no TLS); on a real deployment we would terminate
    TLS in front of MinIO and drop this flag.
    """

    return {
        "endpoint": ENDPOINT,
        "aws_access_key_id": ACCESS_KEY,
        "aws_secret_access_key": SECRET_KEY,
        "aws_region": "us-east-1",
        "allow_http": "true",
        # Path-style requests: MinIO does not implement virtual-hosted
        # style bucket subdomains.
        "virtual_hosted_style_request": "false",
    }


def _build_table() -> pa.Table:
    """Three-row arrow table -- enough to round-trip and delete."""

    return pa.table(
        {
            "id": pa.array([1, 2, 3], type=pa.int64()),
            "label": pa.array(["alpha", "beta", "gamma"], type=pa.string()),
            "created_at": pa.array(
                [datetime.now(timezone.utc).replace(microsecond=0)] * 3,
                type=pa.timestamp("us", tz="UTC"),
            ),
        },
    )


def main() -> int:
    uri = f"s3://{BUCKET}/{TABLE_KEY}"
    print(f"target uri = {uri}")
    print(f"endpoint   = {ENDPOINT}")

    storage = _storage_options()

    # 1. Write -- mode=overwrite so reruns of the smoke test are clean.
    table = _build_table()
    lance.write_dataset(
        table,
        uri,
        mode="overwrite",
        storage_options=storage,
    )
    print(f"wrote {table.num_rows} rows")

    # 2. Read back -- verify lance can roundtrip via MinIO.
    ds = lance.dataset(uri, storage_options=storage)
    rows = ds.count_rows()
    print(f"read back {rows} rows")
    if rows != table.num_rows:
        print(
            f"FAIL: round-trip rowcount mismatch (wrote={table.num_rows}, "
            f"read={rows})",
            file=sys.stderr,
        )
        return 1

    # 3. Delete one row, re-open, confirm count dropped by exactly 1.
    ds.delete("id = 2")
    ds_after = lance.dataset(uri, storage_options=storage)
    rows_after = ds_after.count_rows()
    print(f"after delete: {rows_after} rows")
    if rows_after != rows - 1:
        print(
            f"FAIL: expected {rows - 1} rows after delete, got {rows_after}",
            file=sys.stderr,
        )
        return 2

    # 4. Show the version history -- proves lance is keeping the
    # transaction log on MinIO, not just relying on local state.
    versions = ds_after.versions()
    print(f"version count = {len(versions)} (expected >= 2)")
    if len(versions) < 2:
        print("FAIL: expected at least 2 versions after write+delete", file=sys.stderr)
        return 3

    print("PASS: lance + MinIO smoke succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
