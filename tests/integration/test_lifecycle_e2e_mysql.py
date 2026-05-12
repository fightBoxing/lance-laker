"""End-to-end integration tests for the lifecycle planner against MySQL.

This is the *first* test in the project that exercises the full business
loop: a user-authored ``lifecycle_policy`` row is turned into a ``task``
row by the planner and then dispatched + completed by a worker via the
``scheduler_service`` primitives.

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
from lcp.db.models import Dataset, LifecyclePolicy, Task
from lcp.db.rls import install_rls_listener
from lcp.services import lifecycle_planner_service, scheduler_service

pytestmark = pytest.mark.integration


DEFAULT_DSN = "mysql+aiomysql://lcp:lcp_dev_pwd@127.0.0.1:30306/lcp"


def _mysql_reachable(host: str, port: int) -> bool:
    """Cheap pre-flight to skip the suite when MySQL is offline."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


if not _mysql_reachable("127.0.0.1", 30306):
    pytest.skip(
        "MySQL on 127.0.0.1:30306 is not reachable; "
        "start the Colima k3s cluster or set LCP_TEST_MYSQL_DSN.",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def mysql_dsn() -> str:
    return os.environ.get("LCP_TEST_MYSQL_DSN", DEFAULT_DSN)


@pytest.fixture
async def mysql_session(mysql_dsn: str) -> AsyncIterator[AsyncSession]:
    """Yield a system-context AsyncSession bound to real MySQL."""

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
    """Per-test tenant id used purely for test data isolation."""

    return f"itest-lp-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _purge(session: AsyncSession, tenant_id: str) -> None:
    """Remove tenant data + helper worker rows."""

    await session.execute(
        text("DELETE FROM task WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.execute(
        text("DELETE FROM dataset WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.execute(
        text(
            "DELETE FROM worker_registry WHERE worker_id LIKE :p",
        ).bindparams(p=f"itest-{tenant_id}%"),
    )
    await session.commit()


async def _seed_dataset_and_policy(
    session: AsyncSession,
    *,
    tenant_id: str,
    ttl_days: int | None = 30,
    compaction_threshold: dict | None = None,
    index_optimize_cron: str | None = None,
) -> tuple[Dataset, LifecyclePolicy]:
    """Insert one dataset + one policy as if a user had registered them."""

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

    policy = LifecyclePolicy(
        dataset_uuid=ds.dataset_uuid,
        policy_name="default",
        ttl_days=ttl_days,
        compaction_threshold=compaction_threshold,
        index_optimize_cron=index_optimize_cron,
        enabled=True,
    )
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return ds, policy


async def _consume_one_task(
    session: AsyncSession,
    *,
    worker_id: str,
    task_type: str | None = None,
) -> Task | None:
    """Demo worker: register, claim one task, mark it SUCCEEDED.

    Returns the completed task (refreshed) so assertions can inspect it.
    """

    worker = await scheduler_service.register_worker(
        session, worker_id=worker_id, worker_type="lifecycle",
    )
    claimed = await scheduler_service.claim_next_task(
        session,
        worker_id=worker.worker_id,
        lease_id=worker.lease_id,
        task_type=task_type,
    )
    if claimed is None:
        return None
    return await scheduler_service.complete_task(
        session,
        worker_id=worker.worker_id,
        task_uuid=claimed.task_uuid,
        result={"executed_by": "demo-worker"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifecycleEndToEnd:

    async def test_ttl_policy_creates_task_consumed_by_worker(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """Full loop: policy -> planner -> task -> worker -> SUCCEEDED."""

        try:
            ds, policy = await _seed_dataset_and_policy(
                mysql_session, tenant_id=isolated_tenant_id, ttl_days=14,
            )

            tick = await lifecycle_planner_service.plan_once(mysql_session)
            assert len(tick.emitted_tasks) == 1

            task_uuid = tick.emitted_tasks[0]

            # Confirm the task row has all the fields we promised.
            stmt = select(Task).where(Task.task_uuid == task_uuid)
            task = (await mysql_session.execute(stmt)).scalar_one()
            assert task.task_type == "TTL_DELETE"
            assert task.status == "PENDING"
            assert task.tenant_id == isolated_tenant_id
            assert task.dataset_uuid == ds.dataset_uuid
            assert task.params == {"ttl_days": 14, "policy_name": "default"}

            # Worker side: claim + complete.
            done = await _consume_one_task(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w1",
                task_type="TTL_DELETE",
            )
            assert done is not None
            assert done.task_uuid == task_uuid
            assert done.status == "SUCCEEDED"
            assert done.result == {"executed_by": "demo-worker"}

            # last_run_at on the policy should now be set.
            await mysql_session.refresh(policy)
            assert policy.last_run_at is not None
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_planner_idempotent_on_real_mysql(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """Two plan_once() calls in the same day produce exactly one task.

        Real-MySQL test of the UNIQUE(idempotency_key) contract.
        """

        try:
            await _seed_dataset_and_policy(
                mysql_session, tenant_id=isolated_tenant_id, ttl_days=7,
            )

            first = await lifecycle_planner_service.plan_once(mysql_session)
            second = await lifecycle_planner_service.plan_once(mysql_session)

            assert len(first.emitted_tasks) == 1
            assert second.emitted_tasks == []
            assert second.skipped_duplicates == 1

            # Exactly one task in the database.
            cnt = await mysql_session.execute(
                text(
                    "SELECT COUNT(*) FROM task WHERE tenant_id = :t",
                ).bindparams(t=isolated_tenant_id),
            )
            assert cnt.scalar_one() == 1
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_full_policy_emits_three_distinct_tasks(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        """A fully-configured policy yields three task types in one tick."""

        try:
            await _seed_dataset_and_policy(
                mysql_session,
                tenant_id=isolated_tenant_id,
                ttl_days=30,
                compaction_threshold={"small_files": 100},
                index_optimize_cron="0 2 * * *",
            )

            tick = await lifecycle_planner_service.plan_once(mysql_session)
            assert len(tick.emitted_tasks) == 3

            stmt = select(Task).where(Task.tenant_id == isolated_tenant_id)
            tasks = (await mysql_session.execute(stmt)).scalars().all()
            types = sorted(t.task_type for t in tasks)
            assert types == ["COMPACTION", "INDEX_OPTIMIZE", "TTL_DELETE"]
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_disabled_policy_emits_no_task(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        try:
            ds = Dataset(
                dataset_uuid=str(uuid.uuid4()),
                tenant_id=isolated_tenant_id,
                catalog="lance",
                db_schema="public",
                table_name=f"t_{uuid.uuid4().hex[:6]}",
                storage_uri="s3://bucket/path",
                status="READY",
            )
            mysql_session.add(ds)
            await mysql_session.commit()
            await mysql_session.refresh(ds)
            policy = LifecyclePolicy(
                dataset_uuid=ds.dataset_uuid,
                policy_name="off",
                ttl_days=10,
                enabled=False,
            )
            mysql_session.add(policy)
            await mysql_session.commit()

            tick = await lifecycle_planner_service.plan_once(mysql_session)
            assert tick.scanned_policies == 0
            assert tick.emitted_tasks == []
        finally:
            await _purge(mysql_session, isolated_tenant_id)
