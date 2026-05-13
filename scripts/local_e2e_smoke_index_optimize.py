#!/usr/bin/env python3
"""Block C end-to-end smoke: optimize_index -> task -> worker -> lance.

Builds on Block B's smoke: re-uses the same lance dataset / index but
exercises the OPTIMIZE path:

  phase 0: register dataset (or reuse) and create index that lands as
           READY -- we plant the row directly to skip the build wait.
  phase 1: call index_service.optimize_index, which should now (Block C)
           flip the row to OPTIMIZING AND emit an INDEX_OPTIMIZE task.
  phase 2: drive the worker; assert INDEX_OPTIMIZE finished SUCCEEDED
           with mode='real'.
  phase 3: re-read the lance dataset and assert its ``versions()`` count
           grew (lance.optimize_indices commits a new manifest).
  phase 4: verify the vector_index row went back to READY.

Pre-req: scripts/local_e2e_smoke_index_build.py (Block B smoke) must
have been run at least once so the ``s3://lcp-lance/smoke/...`` lance
dataset + index exist.  We discover the most recent one rather than
hard-coding a UUID.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Match the Block A/B smokes so lance_io / DB / RLS line up.
os.environ.setdefault(
    "LCP_DB_DSN",
    "mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp",
)
os.environ.setdefault("LCP_LANCE_STORAGE_ENDPOINT", "http://127.0.0.1:30900")
os.environ.setdefault("LCP_LANCE_STORAGE_ACCESS_KEY", "minioadmin")
os.environ.setdefault("LCP_LANCE_STORAGE_SECRET_KEY", "minioadmin")
os.environ.setdefault("LCP_LANCE_STORAGE_REGION", "us-east-1")
os.environ.setdefault("LCP_LANCE_STORAGE_ALLOW_HTTP", "true")
os.environ.setdefault("LCP_LANCE_STORAGE_PATH_STYLE", "true")
os.environ.setdefault("LCP_AUTH_DISABLE_MTLS", "true")
os.environ.setdefault("LCP_RLS_ENABLED", "false")

import lance  # noqa: E402  -- env first
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from lcp.core.tenant import (  # noqa: E402
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.data_plane import lance_io  # noqa: E402
from lcp.db.models import Dataset, Index, Task  # noqa: E402
from lcp.db.rls import install_rls_listener  # noqa: E402
from lcp.services import index_service, scheduler_service  # noqa: E402
from lcp.workers.lifecycle_worker_service import WorkerConfig, run_iteration  # noqa: E402

RUN_ID = uuid.uuid4().hex[:8]
INDEX_NAME = "embedding_idx"  # matches Block B smoke


def _make_session_factory() -> async_sessionmaker:
    engine = create_async_engine(
        os.environ["LCP_DB_DSN"], future=True, pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _find_block_b_dataset(factory: async_sessionmaker) -> Dataset:
    """Pick the most recent Block-B-style dataset that still has its index."""

    with with_system_context():
        async with factory() as session:
            stmt = (
                select(Dataset)
                .where(Dataset.table_name.like("index_build_smoke_%"))
                .order_by(Dataset.created_at.desc())
                .limit(10)
            )
            datasets = (await session.execute(stmt)).scalars().all()
            for ds in datasets:
                idx_stmt = select(Index).where(
                    Index.dataset_uuid == ds.dataset_uuid,
                    Index.index_name == INDEX_NAME,
                    Index.status == "READY",
                )
                idx = (await session.execute(idx_stmt)).scalar_one_or_none()
                if idx is not None:
                    return ds
    raise RuntimeError(
        "no Block B smoke dataset with a READY index found; run "
        "scripts/local_e2e_smoke_index_build.py first",
    )


def _lance_versions_count(uri: str) -> int:
    storage_options = lance_io.build_storage_options()
    ds = lance.dataset(uri, storage_options=storage_options)
    return len(list(ds.versions()))


async def phase_1_call_optimize(
    factory: async_sessionmaker, ds: Dataset,
) -> str:
    """Service-layer call (REST 401 bypass, same as Block A/B smokes)."""

    principal = TenantPrincipal(
        tenant_id=ds.tenant_id,
        subject="smoke",
        auth_method="oidc",
        is_system=False,
    )
    token = set_current_tenant(principal)
    try:
        async with factory() as session:
            obj = await index_service.optimize_index(
                session, ds.dataset_uuid, INDEX_NAME,
            )
            assert obj.status == "OPTIMIZING", obj.status
            print(
                f"[phase1] index {INDEX_NAME} flipped to OPTIMIZING "
                f"last_optimized_at={obj.last_optimized_at}",
            )
            return obj.dataset_uuid
    finally:
        reset_current_tenant(token)


async def phase_2_drive_worker(
    factory: async_sessionmaker, dataset_uuid: str,
) -> dict:
    """Drain the queue until INDEX_OPTIMIZE on this dataset finishes."""

    worker_id = f"smoke-opt-worker-{RUN_ID}"
    async with factory() as reg_sess:
        with with_system_context():
            worker = await scheduler_service.register_worker(
                reg_sess, worker_id=worker_id, worker_type="block-c-smoke",
            )
    cfg = WorkerConfig(
        worker_id=worker.worker_id, lease_id=worker.lease_id, task_type=None,
    )

    saw_target = False
    idle = 0
    for i in range(15):
        async with factory() as session:
            with with_system_context():
                outcome = await run_iteration(session, config=cfg)
        if outcome.claimed:
            print(
                f"[phase2] iter={i} task={outcome.task_uuid} "
                f"type={outcome.task_type} -> {outcome.final_status}",
            )
            idle = 0
            if outcome.task_type == "INDEX_OPTIMIZE":
                # We only treat it as the target if it's for OUR dataset --
                # background OPTIMIZE tasks for other datasets may exist.
                async with factory() as session:
                    with with_system_context():
                        row = (await session.execute(
                            select(Task).where(Task.task_uuid == outcome.task_uuid),
                        )).scalar_one()
                if row.dataset_uuid == dataset_uuid:
                    saw_target = True
                    if outcome.final_status != "SUCCEEDED":
                        raise RuntimeError(
                            f"INDEX_OPTIMIZE failed: {outcome.error}",
                        )
                    payload = row.result or {}
                    return payload
        else:
            idle += 1
            if idle >= 2 and saw_target:
                break

    raise RuntimeError(
        f"worker drained without running INDEX_OPTIMIZE for {dataset_uuid}",
    )


async def phase_4_check_index_back_to_ready(
    factory: async_sessionmaker, ds: Dataset,
) -> None:
    with with_system_context():
        async with factory() as session:
            stmt = select(Index).where(
                Index.dataset_uuid == ds.dataset_uuid,
                Index.index_name == INDEX_NAME,
            )
            idx = (await session.execute(stmt)).scalar_one()
    print(
        f"[phase4] index status={idx.status} "
        f"last_optimized_at={idx.last_optimized_at}",
    )
    assert idx.status == "READY", idx.status


async def main() -> int:
    print(f"=== Block C smoke run id={RUN_ID} ===")
    factory = _make_session_factory()

    ds = await _find_block_b_dataset(factory)
    print(f"[phase0] reusing dataset {ds.dataset_uuid} uri={ds.storage_uri}")

    versions_before = _lance_versions_count(ds.storage_uri)
    print(f"[phase0] lance versions before optimize = {versions_before}")

    await phase_1_call_optimize(factory, ds)
    payload = await phase_2_drive_worker(factory, ds.dataset_uuid)
    print(
        f"[phase2] payload mode={payload.get('mode')} "
        f"lance_index_count={payload.get('lance_index_count')} "
        f"promoted_count={payload.get('promoted_count')}",
    )
    if payload.get("mode") != "real":
        raise RuntimeError(f"expected mode='real', got {payload!r}")

    versions_after = _lance_versions_count(ds.storage_uri)
    print(f"[phase3] lance versions after optimize  = {versions_after}")
    if versions_after <= versions_before:
        raise RuntimeError(
            f"lance versions did not grow: {versions_before} -> {versions_after}",
        )

    await phase_4_check_index_back_to_ready(factory, ds)
    print("=== Block C smoke OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
