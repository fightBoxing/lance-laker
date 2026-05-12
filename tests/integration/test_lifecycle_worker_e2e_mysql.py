"""End-to-end integration test: planner emits, real worker consumes.

This is the *first* test where a complete production-shape worker (the
``run_iteration`` primitive backed by real executors) drains tasks the
planner emitted on real MySQL.  It is the smoke test we will run before
shipping the lifecycle worker container.

Auto-skipped when MySQL is unreachable.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lcp.core.tenant import with_system_context
from lcp.db.models import Dataset, Index, LifecyclePolicy, Task
from lcp.db.rls import install_rls_listener
from lcp.services import lifecycle_planner_service, scheduler_service
from lcp.workers.lifecycle_worker_service import (
    WorkerConfig,
    run_iteration,
)

pytestmark = pytest.mark.integration


DEFAULT_DSN = "mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp"


def _mysql_reachable(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


if not _mysql_reachable("127.0.0.1", 30306):
    pytest.skip(
        "MySQL on 127.0.0.1:30306 is not reachable",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def mysql_dsn() -> str:
    return os.environ.get("LCP_TEST_MYSQL_DSN", DEFAULT_DSN)


@pytest.fixture
async def mysql_session(mysql_dsn: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(mysql_dsn, future=True, pool_pre_ping=True)
    install_rls_listener(engine.sync_engine)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def system_principal() -> Iterator[None]:
    with with_system_context():
        yield


@pytest.fixture
def isolated_tenant_id() -> str:
    return f"itest-lw-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _purge(session: AsyncSession, tenant_id: str) -> None:
    """Tear down all rows owned by the test tenant + the worker we used."""

    await session.execute(text(
        "DELETE FROM vector_index WHERE dataset_uuid IN "
        "(SELECT dataset_uuid FROM dataset WHERE tenant_id = :t)"
    ).bindparams(t=tenant_id))
    await session.execute(text(
        "DELETE FROM lifecycle_policy WHERE dataset_uuid IN "
        "(SELECT dataset_uuid FROM dataset WHERE tenant_id = :t)"
    ).bindparams(t=tenant_id))
    await session.execute(text(
        "DELETE FROM task WHERE tenant_id = :t"
    ).bindparams(t=tenant_id))
    await session.execute(text(
        "DELETE FROM dataset WHERE tenant_id = :t"
    ).bindparams(t=tenant_id))
    await session.execute(text(
        "DELETE FROM worker_registry WHERE worker_id LIKE :p"
    ).bindparams(p=f"itest-{tenant_id}%"))
    await session.commit()


async def _seed_dataset(
    session: AsyncSession, *, tenant_id: str,
) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        catalog="lance",
        db_schema="public",
        table_name=f"t_{uuid.uuid4().hex[:6]}",
        storage_uri="s3://bucket/path",
        status="READY",
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifecycleWorkerEndToEnd:

    async def test_ttl_task_drained_to_succeeded(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """Planner emits TTL_DELETE; real worker drains it to SUCCEEDED."""

        try:
            ds = await _seed_dataset(
                mysql_session, tenant_id=isolated_tenant_id,
            )
            policy = LifecyclePolicy(
                dataset_uuid=ds.dataset_uuid,
                policy_name="default",
                ttl_days=14,
                enabled=True,
            )
            mysql_session.add(policy)
            await mysql_session.commit()

            tick = await lifecycle_planner_service.plan_once(mysql_session)
            assert len(tick.emitted_tasks) == 1

            worker = await scheduler_service.register_worker(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w1",
                worker_type="lifecycle",
            )

            outcome = await run_iteration(
                mysql_session,
                config=WorkerConfig(
                    worker_id=worker.worker_id,
                    lease_id=worker.lease_id,
                    task_type="TTL_DELETE",
                ),
            )

            assert outcome.claimed is True
            assert outcome.final_status == "SUCCEEDED"
            assert outcome.task_type == "TTL_DELETE"

            stmt = select(Task).where(Task.task_uuid == outcome.task_uuid)
            task = (await mysql_session.execute(stmt)).scalar_one()
            assert task.status == "SUCCEEDED"
            assert task.result is not None
            assert task.result["executor"] == "TtlDeleteExecutor"
            assert task.result["mode"] == "stub"
            assert task.result["ttl_days"] == 14
            assert task.tenant_id == isolated_tenant_id
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_index_optimize_real_state_machine_on_mysql(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """INDEX_OPTIMIZE is the ONLY non-stub executor.

        Verify that on real MySQL, an OPTIMIZING vector_index row is
        flipped back to READY by the worker.  This is the single most
        important regression test of this iteration.
        """

        try:
            ds = await _seed_dataset(
                mysql_session, tenant_id=isolated_tenant_id,
            )
            policy = LifecyclePolicy(
                dataset_uuid=ds.dataset_uuid,
                policy_name="default",
                index_optimize_cron="0 2 * * *",
                enabled=True,
            )
            mysql_session.add(policy)
            # Pre-create an OPTIMIZING index so the executor has work to do.
            idx = Index(
                dataset_uuid=ds.dataset_uuid,
                index_name="my_idx",
                index_type="IVF_PQ",
                column_name="embedding",
                status="OPTIMIZING",
            )
            mysql_session.add(idx)
            await mysql_session.commit()

            tick = await lifecycle_planner_service.plan_once(mysql_session)
            assert len(tick.emitted_tasks) == 1

            worker = await scheduler_service.register_worker(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w2",
                worker_type="lifecycle",
            )

            outcome = await run_iteration(
                mysql_session,
                config=WorkerConfig(
                    worker_id=worker.worker_id,
                    lease_id=worker.lease_id,
                    task_type="INDEX_OPTIMIZE",
                ),
            )
            assert outcome.final_status == "SUCCEEDED"

            stmt = select(Task).where(Task.task_uuid == outcome.task_uuid)
            task = (await mysql_session.execute(stmt)).scalar_one()
            assert task.result["executor"] == "IndexOptimizeExecutor"
            assert task.result["mode"] == "real"
            assert task.result["promoted_count"] == 1
            assert task.result["indexes_promoted"] == ["my_idx"]

            # Real state machine assertion: index moved back to READY.
            await mysql_session.refresh(idx)
            assert idx.status == "READY"
            assert idx.last_optimized_at is not None
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_orphan_task_fails_with_DatasetMissing(  # noqa: N802
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """If a task survives its dataset, worker must fail it cleanly."""

        try:
            ds = await _seed_dataset(
                mysql_session, tenant_id=isolated_tenant_id,
            )
            # Manually craft a PENDING task, then drop the dataset to
            # simulate the race window between planner emission and
            # worker dispatch.
            task = Task(
                task_uuid=str(uuid.uuid4()),
                task_type="TTL_DELETE",
                dataset_uuid=ds.dataset_uuid,
                tenant_id=ds.tenant_id,
                status="PENDING",
                priority=5,
                idempotency_key=f"itest-orphan-{uuid.uuid4().hex[:8]}",
                params={"ttl_days": 1, "policy_name": "p"},
            )
            mysql_session.add(task)
            await mysql_session.commit()

            await mysql_session.delete(ds)
            await mysql_session.commit()

            worker = await scheduler_service.register_worker(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w3",
                worker_type="lifecycle",
            )
            outcome = await run_iteration(
                mysql_session,
                config=WorkerConfig(
                    worker_id=worker.worker_id,
                    lease_id=worker.lease_id,
                    task_type="TTL_DELETE",
                ),
            )

            assert outcome.final_status == "FAILED"
            assert outcome.error == "DatasetMissing"

            stmt = select(Task).where(Task.task_uuid == task.task_uuid)
            refreshed = (await mysql_session.execute(stmt)).scalar_one()
            assert refreshed.status == "FAILED"
            assert refreshed.error_code == "DatasetMissing"
        finally:
            await _purge(mysql_session, isolated_tenant_id)
