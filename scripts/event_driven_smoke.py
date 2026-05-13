"""End-to-end smoke for the event-driven index watcher.

What this script PROVES (the user's original question):

    "If I write to lance directly (bypassing LCP), will LCP auto-build
     indices over the new data?"

YES -- and this script exhibits the loop end-to-end against a real
MinIO + MySQL + lance setup, with no LCP REST API involved on the
data path:

    Phase 0: clean slate -- drop+recreate dataset row in MySQL, drop
             any prior lance dataset on MinIO.
    Phase 1: seed 800 vectors via ``lance.write_dataset`` and create a
             ``vector_index`` (BUILDING -> READY) so the watcher has
             something to watch.
    Phase 2: register a lifecycle policy with ``index_watch_enabled=True``
             and ``index_watch_min_unindexed_rows=200``.
    Phase 3: append 400 fresh vectors via PURE ``lance.write_dataset``
             -- NO LCP API call.  This is the moment a normal user
             "bypasses" LCP.
    Phase 4: run ONE watcher pass + drain the worker pool.
    Phase 5: assert:
        - a ``watcher:*:INDEX_OPTIMIZE:v*:unindexed_rows`` task landed
          in SUCCEEDED state;
        - lance reports the new rows are now ``num_indexed_rows``;
        - the index row's ``last_seen_version`` matches lance latest;
        - the index row's status is back to READY.

How to run::

    # MinIO + MySQL must be up (deploy/k8s).
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
    LCP_LANCE_STORAGE_ENDPOINT=http://127.0.0.1:30900 \\
    LCP_DB_DSN='mysql+aiomysql://lcp:lcp@127.0.0.1:30306/lcp' \\
        python scripts/event_driven_smoke.py

Exit codes
----------
- ``0``  PASS
- ``>0`` FAIL with a one-line reason printed to stderr
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import lance
import numpy as np
import pyarrow as pa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings
from lcp.core.tenant import with_system_context
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Index, LifecyclePolicy, Task
from lcp.db.rls import install_rls_listener
from lcp.services import index_watcher_service, scheduler_service
from lcp.workers.lifecycle_executors import build_default_registry
from lcp.workers.lifecycle_worker_service import (
    WorkerConfig,
    run_iteration,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("LANCE_MINIO_BUCKET", "lcp-smoke")
TABLE_KEY = os.environ.get(
    "EVENT_SMOKE_TABLE", "event_driven_smoke.lance",
)
SEED_ROWS = 800
APPEND_ROWS = 400
DIM = 8
TENANT_ID = "smoke-tenant"
INDEX_NAME = "vec_idx"
POLICY_NAME = "watch-policy"


# ---------------------------------------------------------------------------
# Lance helpers
# ---------------------------------------------------------------------------


def _make_arrow_table(start: int, count: int) -> pa.Table:
    """Build ``count`` fixed-shape vectors starting at id ``start``."""

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


def _storage_uri() -> str:
    return f"s3://{BUCKET}/{TABLE_KEY}"


def _storage_options() -> dict[str, str]:
    return lance_io.build_storage_options()


# ---------------------------------------------------------------------------
# Phase 0 helpers
# ---------------------------------------------------------------------------


async def _purge_dataset_metadata(session: AsyncSession) -> None:
    """Remove any dataset row from a previous run so we start clean."""

    stmt = select(Dataset).where(Dataset.storage_uri == _storage_uri())
    existing = (await session.execute(stmt)).scalars().all()
    for ds in existing:
        # Cascade deletes the index + policy + tasks via the FK constraints.
        await session.delete(ds)
    await session.commit()


def _purge_lance_dataset() -> None:
    """Drop any pre-existing lance dataset under our URI.

    Tolerates "does not exist".  We do this in the test harness, not by
    calling LCP, because the dataset row is what LCP cares about.
    """

    try:
        ds = lance.dataset(_storage_uri(), storage_options=_storage_options())
    except Exception:  # noqa: BLE001 -- "missing" looks like many errors
        return
    try:
        ds.delete("1=1")
    except Exception:  # noqa: BLE001 -- best effort
        pass


# ---------------------------------------------------------------------------
# Phase 1 + 2: seed
# ---------------------------------------------------------------------------


async def _seed_dataset_and_policy(
    session: AsyncSession,
) -> tuple[Dataset, Index, LifecyclePolicy]:
    """Insert dataset + index + policy rows mirroring real /v1/* calls.

    We avoid hitting the REST API to keep this script independent from
    the API server lifecycle (the user might run the watcher locally
    without bringing up the API).
    """

    dataset = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        catalog="lance",
        db_schema="smoke",
        table_name="event_driven",
        storage_uri=_storage_uri(),
        status="ACTIVE",
        latest_version=0,
    )
    session.add(dataset)
    await session.flush()

    # Vector index in READY state -- the watcher only acts on READY.
    # In a "real" run, INDEX_BUILD worker would have flipped this; we
    # short-cut for the smoke since we just ran ``create_index`` below.
    index = Index(
        dataset_uuid=dataset.dataset_uuid,
        index_name=INDEX_NAME,
        column_name="vec",
        index_type="IVF_PQ",
        params={"num_partitions": 4, "num_sub_vectors": 2},
        status="READY",
        coverage=Decimal("1.0000"),
        last_seen_version=None,
        last_optimized_at=None,
    )
    session.add(index)

    policy = LifecyclePolicy(
        dataset_uuid=dataset.dataset_uuid,
        policy_name=POLICY_NAME,
        index_watch_enabled=True,
        # Any append > 200 rows triggers an INDEX_OPTIMIZE.  Picking a
        # threshold strictly less than APPEND_ROWS makes the smoke
        # deterministic.
        index_watch_min_unindexed_rows=200,
        index_watch_min_version_drift=1,
        index_watch_stale_minutes=60,
        enabled=True,
    )
    session.add(policy)

    await session.commit()
    await session.refresh(dataset)
    await session.refresh(index)
    await session.refresh(policy)
    return dataset, index, policy


def _build_lance_index() -> None:
    """Phase 1 lance work: write seed rows, then create the vector index."""

    table = _make_arrow_table(start=0, count=SEED_ROWS)
    lance.write_dataset(
        table, _storage_uri(),
        mode="overwrite",
        storage_options=_storage_options(),
    )
    ds = lance.dataset(_storage_uri(), storage_options=_storage_options())
    ds.create_index(
        "vec",
        index_type="IVF_PQ",
        num_partitions=4,
        num_sub_vectors=2,
        replace=True,
    )


def _append_lance_rows() -> int:
    """Phase 3: pure lance append, no LCP API.  Returns new latest version."""

    table = _make_arrow_table(start=SEED_ROWS, count=APPEND_ROWS)
    lance.write_dataset(
        table, _storage_uri(),
        mode="append",
        storage_options=_storage_options(),
    )
    ds = lance.dataset(_storage_uri(), storage_options=_storage_options())
    return int(ds.latest_version)


# ---------------------------------------------------------------------------
# Phase 4: drive watcher + worker
# ---------------------------------------------------------------------------


async def _drive_watcher_and_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    lease_id: str,
    max_seconds: float = 30.0,
) -> tuple[int, int]:
    """Run watcher passes interleaved with worker iterations.

    Returns ``(emitted_count, succeeded_count)`` so the caller can
    assert end-state without re-querying.

    Implementation: the watcher and worker are two cooperating loops.
    We drive them in series (one pass each) until either we see a
    SUCCEEDED watcher task or ``max_seconds`` elapses.  This mirrors
    what the two k8s Deployments do, just within a single process.
    """

    settings = get_settings()
    deadline = time.monotonic() + max_seconds
    registry = build_default_registry()
    emitted = 0
    succeeded = 0

    while time.monotonic() < deadline:
        # --- watcher tick ---
        async with factory() as session:
            report = await index_watcher_service.run_watch_pass(
                session, settings=settings,
            )
            emitted += report.enqueued_tasks

        # --- worker tick (drain any pending) ---
        for _ in range(5):  # cap inner loop
            async with factory() as session:
                outcome = await run_iteration(
                    session,
                    config=WorkerConfig(
                        worker_id=worker_id,
                        lease_id=lease_id,
                        task_type="INDEX_OPTIMIZE",
                    ),
                    registry=registry,
                )
            if not outcome.claimed:
                break
            if outcome.final_status == "SUCCEEDED":
                succeeded += 1

        if succeeded > 0 and emitted > 0:
            return emitted, succeeded

        await asyncio.sleep(0.5)

    return emitted, succeeded


# ---------------------------------------------------------------------------
# Phase 5: assertions
# ---------------------------------------------------------------------------


async def _assert_post_state(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    expected_version: int,
) -> tuple[bool, str]:
    """Return ``(ok, message)`` summarising the post-state."""

    # Index row state
    idx = (
        await session.execute(
            select(Index).where(Index.dataset_uuid == dataset_uuid),
        )
    ).scalar_one()
    if idx.status != "READY":
        return False, f"index status is {idx.status!r}, expected READY"
    if idx.last_seen_version != expected_version:
        return (
            False,
            f"last_seen_version={idx.last_seen_version}, expected "
            f"{expected_version}",
        )

    # Watcher-emitted task
    stmt = (
        select(Task)
        .where(Task.dataset_uuid == dataset_uuid)
        .where(Task.task_type == "INDEX_OPTIMIZE")
        .order_by(Task.id.desc())
    )
    task = (await session.execute(stmt)).scalars().first()
    if task is None:
        return False, "no INDEX_OPTIMIZE task in DB"
    if task.idempotency_key is None or not task.idempotency_key.startswith(
        "watcher:",
    ):
        return (
            False,
            f"task idempotency_key {task.idempotency_key!r} is not "
            "from the watcher",
        )
    if task.status != "SUCCEEDED":
        return False, f"task status is {task.status!r}, expected SUCCEEDED"

    # Lance-side: the new rows must be indexed now.
    stats = lance_io.read_index_stats(
        _storage_uri(),
        INDEX_NAME,
        storage_options=_storage_options(),
    )
    if stats is None:
        return False, "lance index_statistics returned None post-optimize"
    if stats.num_unindexed_rows is None:
        return False, "lance did not expose num_unindexed_rows"
    if stats.num_unindexed_rows != 0:
        return (
            False,
            f"lance reports {stats.num_unindexed_rows} unindexed rows; "
            "expected 0 after auto-optimize",
        )

    return (
        True,
        f"index ready, version={idx.last_seen_version}, "
        f"task={task.task_uuid}, watcher_key={task.idempotency_key}",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _run() -> int:
    print("phase 0: connecting to MySQL + cleaning prior state")
    settings = get_settings()
    engine = create_async_engine(settings.db_dsn, future=True, echo=False)
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        with with_system_context():
            async with factory() as session:
                await _purge_dataset_metadata(session)

            print("phase 0: cleaning prior lance dataset")
            _purge_lance_dataset()

            print(f"phase 1: writing {SEED_ROWS} rows + creating IVF_PQ index")
            _build_lance_index()

            print("phase 2: seeding LCP dataset/index/policy rows")
            async with factory() as session:
                dataset, _index, _policy = await _seed_dataset_and_policy(session)
                dataset_uuid = dataset.dataset_uuid

            print(f"phase 3: appending {APPEND_ROWS} rows via direct lance.write")
            new_version = _append_lance_rows()
            print(f"phase 3: lance latest_version is now {new_version}")

            # Register a worker so the inner-loop ``run_iteration`` can
            # claim tasks.  The watcher is *not* a registered worker.
            worker_id = f"smoke-worker-{uuid.uuid4().hex[:6]}"
            async with factory() as session:
                worker = await scheduler_service.register_worker(
                    session,
                    worker_id=worker_id,
                    worker_type="lifecycle",
                )
                lease_id = worker.lease_id

            print(
                "phase 4: driving watcher + worker until INDEX_OPTIMIZE "
                "succeeds (timeout=30s)",
            )
            emitted, succeeded = await _drive_watcher_and_worker(
                factory,
                worker_id=worker_id,
                lease_id=lease_id,
            )
            print(
                f"phase 4: watcher emitted={emitted} task(s); worker "
                f"succeeded={succeeded} task(s)",
            )

            if emitted == 0:
                print(
                    "FAIL: watcher did not emit any INDEX_OPTIMIZE task "
                    "(check lifecycle policy thresholds and lance stats)",
                    file=sys.stderr,
                )
                return 1
            if succeeded == 0:
                print(
                    "FAIL: watcher emitted but worker never reached "
                    "SUCCEEDED (check executor logs)",
                    file=sys.stderr,
                )
                return 2

            print("phase 5: asserting post-state")
            async with factory() as session:
                ok, msg = await _assert_post_state(
                    session,
                    dataset_uuid=dataset_uuid,
                    expected_version=new_version,
                )
            if not ok:
                print(f"FAIL: {msg}", file=sys.stderr)
                return 3
            print(f"PASS: {msg}")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    """Entry-point usable from CLI and from tests."""

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
