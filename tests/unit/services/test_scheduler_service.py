"""Unit tests for the scheduler primitives.

These tests run against an in-memory SQLite database; they verify
correctness of the state machine but NOT real-world concurrency, which
relies on ``SELECT ... FOR UPDATE SKIP LOCKED`` and is exercised by
``tests/integration/test_scheduler_mysql.py`` against a live MySQL.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.db.models import Base, Task
from lcp.services import scheduler_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession bound to an in-memory SQLite database.

    StaticPool keeps the same connection across the test so the schema we
    create here is visible to subsequent queries.
    """

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
    """Bind the system principal for the duration of one test."""

    with with_system_context():
        yield


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed_pending_task(
    session: AsyncSession,
    *,
    tenant_id: str = "acme",
    task_type: str = "COMPACTION",
    priority: int = 5,
) -> Task:
    """Insert a fresh PENDING task as if a tenant had submitted it."""

    task = Task(
        task_uuid=str(uuid.uuid4()),
        task_type=task_type,
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        status="PENDING",
        priority=priority,
        max_attempts=3,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# System-principal guard
# ---------------------------------------------------------------------------


class TestSystemGuard:

    async def test_register_without_system_raises(
        self, session: AsyncSession,
    ) -> None:
        """Calling scheduler primitives outside with_system_context() must fail."""

        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            with pytest.raises(scheduler_service.SchedulerNotSystemError):
                await scheduler_service.register_worker(
                    session,
                    worker_id="w1", worker_type="vdw", capacity=2,
                )
        finally:
            reset_current_tenant(token)


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


class TestWorkerLifecycle:

    async def test_register_creates_alive_worker(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw", capacity=4,
        )
        assert worker.status == "ALIVE"
        assert worker.capacity == 4
        assert worker.in_flight == 0
        assert len(worker.lease_id) > 0

    async def test_re_register_rotates_lease(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        first = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        # SQLAlchemy's identity map returns the same Python object for the
        # second register call, so capture the lease_id as a string before
        # the in-place mutation.
        first_lease = first.lease_id
        second = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        assert first_lease != second.lease_id

    async def test_heartbeat_with_stale_lease_rejected(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        first = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        old_lease = first.lease_id
        # Re-register rotates the lease.
        await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        with pytest.raises(scheduler_service.WorkerLeaseMismatchError):
            await scheduler_service.heartbeat(
                session, worker_id="w1", lease_id=old_lease,
            )

    async def test_drain_marks_worker_draining(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        worker = await scheduler_service.drain_worker(session, worker_id="w1")
        assert worker.status == "DRAINING"


# ---------------------------------------------------------------------------
# Claim / complete / fail
# ---------------------------------------------------------------------------


class TestClaimAndComplete:

    async def test_claim_picks_pending_task_and_marks_running(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw", capacity=2,
        )
        task = await _seed_pending_task(session)

        claimed = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert claimed is not None
        assert claimed.task_uuid == task.task_uuid
        assert claimed.status == "RUNNING"
        assert claimed.worker_id == "w1"
        assert claimed.attempt == 1

        # Worker.in_flight must increment so capacity gating works.
        await session.refresh(worker)
        assert worker.in_flight == 1

    async def test_claim_returns_none_when_no_task_available(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        claimed = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert claimed is None

    async def test_claim_respects_capacity(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw", capacity=1,
        )
        await _seed_pending_task(session)
        await _seed_pending_task(session)

        first = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        second = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert first is not None
        # Capacity 1 is now full; second claim must back off.
        assert second is None

    async def test_claim_orders_by_priority_then_age(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw", capacity=3,
        )
        # Lower priority value means more urgent.
        low = await _seed_pending_task(session, priority=8)
        await asyncio.sleep(0.01)
        high = await _seed_pending_task(session, priority=2)

        first = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert first is not None
        assert first.task_uuid == high.task_uuid

        second = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert second is not None
        assert second.task_uuid == low.task_uuid

    async def test_complete_task_marks_succeeded_and_decrements_in_flight(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw", capacity=2,
        )
        await _seed_pending_task(session)

        task = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert task is not None

        completed = await scheduler_service.complete_task(
            session,
            worker_id="w1",
            task_uuid=task.task_uuid,
            result={"rows_processed": 100},
        )
        assert completed.status == "SUCCEEDED"
        assert completed.result == {"rows_processed": 100}
        assert completed.finished_at is not None

        await session.refresh(worker)
        assert worker.in_flight == 0

    async def test_fail_task_marks_failed_with_error(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        await _seed_pending_task(session)
        task = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert task is not None

        failed = await scheduler_service.fail_task(
            session,
            worker_id="w1",
            task_uuid=task.task_uuid,
            error_code="E_TIMEOUT",
            error_message="model server unreachable",
        )
        assert failed.status == "FAILED"
        assert failed.error_code == "E_TIMEOUT"
        assert failed.error_message == "model server unreachable"

    async def test_complete_by_wrong_worker_rejected(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker_a = await scheduler_service.register_worker(
            session, worker_id="wA", worker_type="vdw",
        )
        await scheduler_service.register_worker(
            session, worker_id="wB", worker_type="vdw",
        )
        await _seed_pending_task(session)
        task = await scheduler_service.claim_next_task(
            session, worker_id="wA", lease_id=worker_a.lease_id,
        )
        assert task is not None
        with pytest.raises(scheduler_service.TaskTransitionError):
            await scheduler_service.complete_task(
                session, worker_id="wB", task_uuid=task.task_uuid,
            )

    async def test_draining_worker_does_not_get_new_tasks(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w1", worker_type="vdw",
        )
        await _seed_pending_task(session)
        await scheduler_service.drain_worker(session, worker_id="w1")
        claimed = await scheduler_service.claim_next_task(
            session, worker_id="w1", lease_id=worker.lease_id,
        )
        assert claimed is None


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


class TestReaper:

    async def test_reaps_stale_workers_and_requeues_running_tasks(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w-stale", worker_type="vdw",
        )
        await _seed_pending_task(session)
        task = await scheduler_service.claim_next_task(
            session, worker_id="w-stale", lease_id=worker.lease_id,
        )
        assert task is not None
        assert task.status == "RUNNING"

        # Backdate the heartbeat so the reaper sees the worker as stale.
        worker.last_heartbeat_at = _utcnow() - timedelta(minutes=5)
        await session.commit()

        reaped, requeued = await scheduler_service.reap_dead_workers(
            session, timeout=timedelta(seconds=30),
        )
        assert reaped == ["w-stale"]
        assert task.task_uuid in requeued

        # The orphan task must be back to PENDING with worker_id cleared.
        await session.refresh(task)
        assert task.status == "PENDING"
        assert task.worker_id is None

    async def test_reaper_is_idempotent_on_dead_workers(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        worker = await scheduler_service.register_worker(
            session, worker_id="w-stale", worker_type="vdw",
        )
        worker.last_heartbeat_at = _utcnow() - timedelta(minutes=5)
        await session.commit()

        first_reaped, _ = await scheduler_service.reap_dead_workers(
            session, timeout=timedelta(seconds=30),
        )
        second_reaped, _ = await scheduler_service.reap_dead_workers(
            session, timeout=timedelta(seconds=30),
        )
        assert first_reaped == ["w-stale"]
        assert second_reaped == []
