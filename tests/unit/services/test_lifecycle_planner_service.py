"""Unit tests for the lifecycle planner against in-memory SQLite.

These cover the policy-to-task mapping rules and the idempotency contract
(repeat invocations within the same time bucket must NOT emit duplicates).
Real-MySQL concurrency is the integration test's job.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
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
from lcp.db.models import Base, Dataset, LifecyclePolicy, Task
from lcp.services import lifecycle_planner_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session sharing one connection via StaticPool."""

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
    """Bind a system principal for one test."""

    with with_system_context():
        yield


async def _seed_dataset(
    session: AsyncSession,
    *,
    tenant_id: str = "acme",
    table: str = "t1",
) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        catalog="lance",
        db_schema="public",
        table_name=table,
        storage_uri="s3://bucket/path",
        status="READY",
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


async def _seed_policy(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    name: str = "p1",
    ttl_days: int | None = None,
    compaction_threshold: dict | None = None,
    index_optimize_cron: str | None = None,
    enabled: bool = True,
) -> LifecyclePolicy:
    policy = LifecyclePolicy(
        dataset_uuid=dataset_uuid,
        policy_name=name,
        ttl_days=ttl_days,
        compaction_threshold=compaction_threshold,
        index_optimize_cron=index_optimize_cron,
        enabled=enabled,
    )
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return policy


async def _all_tasks(session: AsyncSession) -> list[Task]:
    stmt = select(Task).order_by(Task.id.asc())
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# System guard
# ---------------------------------------------------------------------------


class TestPlannerSystemGuard:

    async def test_plan_without_system_raises(
        self, session: AsyncSession,
    ) -> None:
        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            with pytest.raises(lifecycle_planner_service.PlannerNotSystemError):
                await lifecycle_planner_service.plan_once(session)
        finally:
            reset_current_tenant(token)


# ---------------------------------------------------------------------------
# Policy -> task mapping
# ---------------------------------------------------------------------------


class TestPlannerEmission:

    async def test_ttl_only_policy_emits_one_task(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        policy = await _seed_policy(session, dataset_uuid=ds.dataset_uuid, ttl_days=30)

        tick = await lifecycle_planner_service.plan_once(session)

        assert tick.scanned_policies == 1
        assert len(tick.emitted_tasks) == 1
        tasks = await _all_tasks(session)
        assert len(tasks) == 1
        assert tasks[0].task_type == "TTL_DELETE"
        assert tasks[0].tenant_id == ds.tenant_id  # tenancy preserved!
        assert tasks[0].dataset_uuid == ds.dataset_uuid
        assert tasks[0].status == "PENDING"
        assert tasks[0].params == {
            "ttl_days": 30,
            "policy_name": policy.policy_name,
        }
        # idempotency_key must encode policy.id + day bucket.
        assert tasks[0].idempotency_key is not None
        assert tasks[0].idempotency_key.startswith(
            f"acme:lifecycle:{policy.id}:TTL_DELETE:",
        )

    async def test_full_policy_emits_three_tasks(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(
            session,
            dataset_uuid=ds.dataset_uuid,
            ttl_days=30,
            compaction_threshold={"small_files": 100},
            index_optimize_cron="0 2 * * *",
        )

        tick = await lifecycle_planner_service.plan_once(session)

        assert len(tick.emitted_tasks) == 3
        tasks = await _all_tasks(session)
        types = sorted(t.task_type for t in tasks)
        assert types == ["COMPACTION", "INDEX_OPTIMIZE", "TTL_DELETE"]

    async def test_disabled_policy_emits_nothing(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(
            session, dataset_uuid=ds.dataset_uuid, ttl_days=30, enabled=False,
        )

        tick = await lifecycle_planner_service.plan_once(session)

        assert tick.scanned_policies == 0
        assert tick.emitted_tasks == []

    async def test_orphan_policy_silently_skipped(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        """Policy whose dataset_uuid does not resolve must not crash planner."""

        await _seed_policy(
            session, dataset_uuid="missing-uuid", ttl_days=30,
        )
        tick = await lifecycle_planner_service.plan_once(session)
        assert tick.emitted_tasks == []
        # Scanned count still counts the row even though it was orphaned.
        assert tick.scanned_policies == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestPlannerIdempotency:

    async def test_same_day_replan_skips_duplicates(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid, ttl_days=7)

        first = await lifecycle_planner_service.plan_once(session)
        second = await lifecycle_planner_service.plan_once(session)

        assert len(first.emitted_tasks) == 1
        assert second.emitted_tasks == []
        assert second.skipped_duplicates == 1
        # Only one task row exists.
        tasks = await _all_tasks(session)
        assert len(tasks) == 1

    async def test_next_day_emits_new_task(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid, ttl_days=7)

        day1 = datetime(2026, 1, 1, 12, 0, 0)
        day2 = datetime(2026, 1, 2, 12, 0, 0)
        first = await lifecycle_planner_service.plan_once(session, now=day1)
        second = await lifecycle_planner_service.plan_once(session, now=day2)

        assert len(first.emitted_tasks) == 1
        assert len(second.emitted_tasks) == 1
        tasks = await _all_tasks(session)
        assert len(tasks) == 2

    async def test_index_optimize_uses_hour_bucket(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        """INDEX_OPTIMIZE tasks should be re-emitted across hours."""

        ds = await _seed_dataset(session)
        await _seed_policy(
            session,
            dataset_uuid=ds.dataset_uuid,
            index_optimize_cron="0 * * * *",
        )

        h1 = datetime(2026, 1, 1, 10, 0, 0)
        h2 = h1 + timedelta(hours=1)
        await lifecycle_planner_service.plan_once(session, now=h1)
        # Same hour: no new task.
        same = await lifecycle_planner_service.plan_once(session, now=h1)
        # Next hour: new task.
        next_hour = await lifecycle_planner_service.plan_once(session, now=h2)

        assert same.emitted_tasks == []
        assert len(next_hour.emitted_tasks) == 1


# ---------------------------------------------------------------------------
# last_run_at bookkeeping
# ---------------------------------------------------------------------------


class TestPlannerBookkeeping:

    async def test_last_run_at_is_set(
        self, session: AsyncSession, system_principal: None,
    ) -> None:
        ds = await _seed_dataset(session)
        policy = await _seed_policy(
            session, dataset_uuid=ds.dataset_uuid, ttl_days=30,
        )
        assert policy.last_run_at is None

        when = datetime(2026, 1, 1, 12, 0, 0)
        await lifecycle_planner_service.plan_once(session, now=when)

        # Re-load the policy and confirm last_run_at landed.
        refreshed = await session.get(LifecyclePolicy, policy.id)
        assert refreshed is not None
        assert refreshed.last_run_at == when
