#!/usr/bin/env python3
"""Block B end-to-end smoke: REST/service -> task -> worker -> lance index.

Mirrors scripts/local_e2e_smoke.py (TTL Block A) but for INDEX_BUILD:

  phase4a: plant a tiny lance dataset on MinIO with a synthetic
           512-d vector column so create_index has something real
           to index.
  phase3 : create the dataset metadata row + call
           index_service.create_index, which now (Block B) also
           enqueues an INDEX_BUILD task.  Service-layer direct call
           (same bypass as the TTL smoke) -- real MySQL + RLS still run.
  phase4b: drive the worker for one iteration; assert the INDEX_BUILD
           task came back SUCCEEDED with mode='real'.
  phase5 : re-open the lance dataset from MinIO and assert
           ds.list_indices() reports the new index.

Run with the local k3s stack already up (MinIO 30900 / MySQL 30307);
no LCP API restart required since we go through the service layer.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Match start_lcp_api_local.sh exactly so the planner / worker / lance_io
# all see the same MinIO + MySQL we just smoke-tested in Block A.
os.environ.setdefault(
    "LCP_DB_DSN",
    "mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp",
)
os.environ.setdefault(
    "LCP_LANCE_STORAGE_ENDPOINT", "http://127.0.0.1:30900",
)
os.environ.setdefault("LCP_LANCE_STORAGE_ACCESS_KEY", "minioadmin")
os.environ.setdefault("LCP_LANCE_STORAGE_SECRET_KEY", "minioadmin")
os.environ.setdefault("LCP_LANCE_STORAGE_REGION", "us-east-1")
os.environ.setdefault("LCP_LANCE_STORAGE_ALLOW_HTTP", "true")
os.environ.setdefault("LCP_LANCE_STORAGE_PATH_STYLE", "true")
os.environ.setdefault("LCP_AUTH_DISABLE_MTLS", "true")
os.environ.setdefault("LCP_RLS_ENABLED", "false")

import lance  # noqa: E402  -- order matters: env vars first
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from lcp.core.tenant import (  # noqa: E402
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.data_plane import lance_io  # noqa: E402
from lcp.db.rls import install_rls_listener  # noqa: E402
from lcp.schemas.dataset import DatasetRegisterRequest  # noqa: E402
from lcp.services import dataset_service, index_service  # noqa: E402
from lcp.workers.lifecycle_worker_service import WorkerConfig, run_iteration  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Use a unique table name per run so we don't collide with prior smoke runs.
RUN_ID = uuid.uuid4().hex[:8]
TABLE_NAME = f"index_build_smoke_{RUN_ID}"
BUCKET = "lcp-lance"
LANCE_URI = f"s3://{BUCKET}/smoke/{TABLE_NAME}.lance"
INDEX_NAME = "embedding_idx"
COLUMN = "embedding"
TENANT_ID = f"tenant-{RUN_ID}"


# ---------------------------------------------------------------------------
# Phase 4a: plant a lance dataset with a real vector column on MinIO
# ---------------------------------------------------------------------------


def phase_4a_plant_lance_table() -> None:
    """Write a tiny lance dataset (256 rows x 32-d) on MinIO.

    Why 32-d: lance IVF_PQ requires num_sub_vectors to divide the
    dimension; 32 / 8 = 4 sub-vectors keeps the test fast.  Why 256
    rows: enough for IVF to actually pick centroids without lance
    rejecting num_partitions.

    Pre-flight: ensure the MinIO bucket exists; we want a clear error
    if it doesn't, not a deep lance LanceError.
    """

    import boto3  # local import: only this script needs it

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["LCP_LANCE_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["LCP_LANCE_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["LCP_LANCE_STORAGE_SECRET_KEY"],
        region_name=os.environ.get("LCP_LANCE_STORAGE_REGION", "us-east-1"),
    )
    existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if BUCKET not in existing:
        s3.create_bucket(Bucket=BUCKET)
        print(f"[phase4a] created bucket {BUCKET}")

    storage_options = lance_io.build_storage_options()
    n_rows, dim = 256, 32

    rng = np.random.default_rng(seed=42)
    vectors = rng.standard_normal((n_rows, dim)).astype("float32")
    table = pa.table({
        "id": pa.array(list(range(n_rows)), type=pa.int64()),
        COLUMN: pa.FixedSizeListArray.from_arrays(
            pa.array(vectors.reshape(-1), type=pa.float32()),
            list_size=dim,
        ),
    })
    lance.write_dataset(table, LANCE_URI, storage_options=storage_options, mode="overwrite")
    print(f"[phase4a] wrote {n_rows} rows x {dim}-d to {LANCE_URI}")


# ---------------------------------------------------------------------------
# DB session factory
# ---------------------------------------------------------------------------


def _make_session_factory() -> async_sessionmaker:
    engine = create_async_engine(
        os.environ["LCP_DB_DSN"],
        future=True,
        pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Phase 3: create dataset + index (which now also enqueues INDEX_BUILD task)
# ---------------------------------------------------------------------------


async def phase_3_register_and_create_index(factory: async_sessionmaker) -> str:
    """Register the dataset and call index_service.create_index.

    Returns the dataset_uuid so phase 4b can locate the task.
    """

    principal = TenantPrincipal(
        tenant_id=TENANT_ID,
        subject="smoke",
        auth_method="oidc",
        is_system=False,
    )
    token = set_current_tenant(principal)
    try:
        async with factory() as session:
            req = DatasetRegisterRequest.model_validate(
                {
                    "catalog": "lance",
                    "schema": "public",
                    "table": TABLE_NAME,
                    "storage_uri": LANCE_URI,
                    "owner": "smoke",
                    "description": "block-b smoke",
                },
            )
            ds = await dataset_service.create_dataset(session, payload=req)
            print(f"[phase3] dataset_uuid={ds.dataset_uuid}")

            idx = await index_service.create_index(
                session,
                dataset_uuid=ds.dataset_uuid,
                index_name=INDEX_NAME,
                column_name=COLUMN,
                index_type="IVF_PQ",
                params={"num_partitions": 4, "num_sub_vectors": 8},
            )
            print(
                f"[phase3] index row id={idx.id} status={idx.status} "
                f"(expect BUILDING; worker will flip to READY)",
            )
            assert idx.status == "BUILDING", f"unexpected status: {idx.status}"
            return ds.dataset_uuid
    finally:
        reset_current_tenant(token)


# ---------------------------------------------------------------------------
# Phase 4b: register a worker, run iteration, drain INDEX_BUILD
# ---------------------------------------------------------------------------


async def phase_4b_drive_worker(
    factory: async_sessionmaker, dataset_uuid: str,
) -> dict:
    """Run worker iterations under system context until INDEX_BUILD finishes.

    Mirrors the Block A smoke loop: register the worker once, then
    iterate ``run_iteration`` with ``with_system_context`` until two
    idle ticks in a row.  Read the ``result`` column straight off the
    INDEX_BUILD task row to verify mode='real'.
    """

    from sqlalchemy import select

    from lcp.db.models import Task
    from lcp.services import scheduler_service

    worker_id = f"smoke-worker-{RUN_ID}"

    async with factory() as reg_sess:
        with with_system_context():
            worker = await scheduler_service.register_worker(
                reg_sess, worker_id=worker_id, worker_type="lifecycle-smoke",
            )
    cfg = WorkerConfig(
        worker_id=worker.worker_id, lease_id=worker.lease_id, task_type=None,
    )

    idle_in_a_row = 0
    saw_index_build = False
    for i in range(10):
        async with factory() as session:
            with with_system_context():
                outcome = await run_iteration(session, config=cfg)
        if outcome.claimed:
            line = (
                f"[phase4b] iter={i} task={outcome.task_uuid} "
                f"type={outcome.task_type} -> {outcome.final_status}"
            )
            if outcome.error:
                line += f" err={outcome.error}"
            print(line)
            if outcome.task_type == "INDEX_BUILD":
                saw_index_build = True
                if outcome.final_status != "SUCCEEDED":
                    raise RuntimeError(
                        f"INDEX_BUILD ended in {outcome.final_status}: "
                        f"{outcome.error}",
                    )
            idle_in_a_row = 0
        else:
            idle_in_a_row += 1
            if idle_in_a_row >= 2 and saw_index_build:
                break

    if not saw_index_build:
        raise RuntimeError("worker drained queue without running INDEX_BUILD")

    # Read the INDEX_BUILD task row (stable filter on dataset_uuid + type).
    with with_system_context():
        async with factory() as session:
            stmt = (
                select(Task)
                .where(Task.dataset_uuid == dataset_uuid)
                .where(Task.task_type == "INDEX_BUILD")
                .order_by(Task.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one()
    payload = row.result or {}
    print(
        f"[phase4b] INDEX_BUILD payload mode={payload.get('mode')} "
        f"index={payload.get('index_name')}",
    )
    if payload.get("mode") != "real":
        raise RuntimeError(f"expected mode='real', got {payload!r}")
    return payload


# ---------------------------------------------------------------------------
# Phase 5: read lance back, confirm the index exists on disk
# ---------------------------------------------------------------------------


def phase_5_verify_index_on_disk() -> None:
    storage_options = lance_io.build_storage_options()
    ds = lance.dataset(LANCE_URI, storage_options=storage_options)
    indices = list(ds.list_indices())
    print(f"[phase5] ds.list_indices() returned {len(indices)} entries")
    for entry in indices:
        # ``entry`` is a dict in modern lance; print key bits.
        print(f"  - {entry}")
    assert len(indices) >= 1, "expected at least one index on disk"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    print(f"=== Block B smoke run id={RUN_ID} ===")
    phase_4a_plant_lance_table()

    factory = _make_session_factory()

    dataset_uuid = await phase_3_register_and_create_index(factory)
    print(f"[main] dataset_uuid={dataset_uuid}")

    payload = await phase_4b_drive_worker(factory, dataset_uuid)
    assert payload["dataset_uuid"] == dataset_uuid

    phase_5_verify_index_on_disk()
    print("=== Block B smoke OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
