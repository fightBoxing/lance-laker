"""Unit tests for ``lifecycle_worker_service.run_iteration``.

Covers the dispatch-and-finalize logic: success, idle (no task), missing
dataset, unknown task type, and executor exception.

These tests stay at the SQLite level by reusing the project's pattern
(see ``test_scheduler_service.py``).  Real-MySQL coverage is in the
matching integration test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.core.tenant import with_system_context
from lcp.db.models import Base, Dataset, Task
from lcp.services import scheduler_service
from lcp.workers.lifecycle_executors import (
    ExecutorResult,
    LifecycleExecutor,
    TtlDeleteExecutor,
)
from lcp.workers.lifecycle_worker_service import (
    WorkerConfig,
    run_iteration,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def system_principal() -> Iterator[None]:
    with with_system_context():
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_dataset(session: AsyncSession) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id="acme",
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


async def _seed_pending_task(
    session: AsyncSession,
    *,
    dataset: Dataset,
    task_type: str = "TTL_DELETE",
    params: dict | None = None,
) -> Task:
    if params is None:
        params = {"ttl_days": 30, "policy_name": "p1"}
    task = Task(
        task_uuid=str(uuid.uuid4()),
        task_type=task_type,
        dataset_uuid=dataset.dataset_uuid,
        tenant_id=dataset.tenant_id,
        status="PENDING",
        priority=5,
        params=params,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _register(session: AsyncSession) -> tuple[str, str]:
    worker = await scheduler_service.register_worker(
        session,
        worker_id=f"unit-w-{uuid.uuid4().hex[:6]}",
        worker_type="lifecycle",
    )
    return worker.worker_id, worker.lease_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestWorkerHappyPath:

    async def test_claims_executes_and_completes(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_pending_task(session, dataset=ds, task_type="TTL_DELETE")
        wid, lid = await _register(session)

        outcome = await run_iteration(
            session,
            config=WorkerConfig(
                worker_id=wid, lease_id=lid, task_type="TTL_DELETE",
            ),
        )

        assert outcome.claimed is True
        assert outcome.final_status == "SUCCEEDED"
        assert outcome.task_type == "TTL_DELETE"

        # Verify DB-side state.
        stmt = select(Task).where(Task.task_uuid == outcome.task_uuid)
        task = (await session.execute(stmt)).scalar_one()
        assert task.status == "SUCCEEDED"
        assert task.result is not None
        assert task.result["executor"] == "TtlDeleteExecutor"

    async def test_no_task_returns_idle_outcome(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        wid, lid = await _register(session)

        outcome = await run_iteration(
            session,
            config=WorkerConfig(worker_id=wid, lease_id=lid),
        )

        assert outcome.claimed is False
        assert outcome.task_uuid is None
        assert outcome.final_status is None


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestWorkerFailurePaths:

    async def test_missing_dataset_fails_task_with_DatasetMissing(  # noqa: N802
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        """Dataset row deleted between planner emission and worker dispatch."""

        ds = await _seed_dataset(session)
        task = await _seed_pending_task(session, dataset=ds)

        # Simulate dataset deletion before worker picks up.
        await session.delete(ds)
        await session.commit()

        wid, lid = await _register(session)

        outcome = await run_iteration(
            session,
            config=WorkerConfig(worker_id=wid, lease_id=lid),
        )

        assert outcome.final_status == "FAILED"
        assert outcome.error == "DatasetMissing"

        stmt = select(Task).where(Task.task_uuid == task.task_uuid)
        refreshed = (await session.execute(stmt)).scalar_one()
        assert refreshed.status == "FAILED"
        assert refreshed.error_code == "DatasetMissing"

    async def test_unknown_task_type_fails_with_UnknownTaskType(  # noqa: N802
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_pending_task(session, dataset=ds, task_type="BOGUS")
        wid, lid = await _register(session)

        # Pass a registry that does NOT contain BOGUS.
        outcome = await run_iteration(
            session,
            config=WorkerConfig(worker_id=wid, lease_id=lid),
            registry={"TTL_DELETE": TtlDeleteExecutor()},
        )

        assert outcome.final_status == "FAILED"
        assert outcome.error == "UnknownTaskType"

    async def test_executor_exception_translates_to_fail_task(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_pending_task(session, dataset=ds, task_type="TTL_DELETE")
        wid, lid = await _register(session)

        class BoomExecutor(LifecycleExecutor):
            task_type = "TTL_DELETE"

            async def execute(self, session, *, task, dataset) -> ExecutorResult:
                raise RuntimeError("kaboom")

        outcome = await run_iteration(
            session,
            config=WorkerConfig(worker_id=wid, lease_id=lid),
            registry={"TTL_DELETE": BoomExecutor()},
        )

        assert outcome.final_status == "FAILED"
        assert outcome.error == "RuntimeError"
        # The task row must reflect the failure.
        stmt = select(Task).where(Task.task_uuid == outcome.task_uuid)
        task = (await session.execute(stmt)).scalar_one()
        assert task.status == "FAILED"
        assert task.error_code == "RuntimeError"
        assert task.error_message == "kaboom"
