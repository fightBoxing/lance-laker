"""Integration tests for the scheduler primitives against real MySQL.

These tests are the real proof that ``SELECT ... FOR UPDATE SKIP LOCKED``
works the way the unit tests assume it does:

- Two concurrent ``claim_next_task`` calls on the same row must not
  return the same task -- one must see the row locked and skip it.
- The reaper must surface stale workers and re-queue their orphan tasks
  in a clean state that a healthy worker can pick up immediately.

Auto-skipped when MySQL is unreachable so CI never blocks when the local
cluster is offline.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.db.models import Task
from lcp.db.rls import install_rls_listener
from lcp.services import scheduler_service

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
    """Yield an AsyncSession bound to real MySQL.

    The RLS listener is installed for parity with production; under the
    system principal it short-circuits and does not inject WHERE clauses.
    """

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

    return f"itest-sched-{uuid.uuid4().hex[:8]}"


async def _purge(session: AsyncSession, tenant_id: str) -> None:
    """Remove tenant data + worker rows the test inserted."""

    await session.execute(
        text("DELETE FROM task WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.execute(
        text("DELETE FROM worker_registry WHERE worker_id LIKE :p").bindparams(
            p=f"itest-{tenant_id}%",
        ),
    )
    await session.commit()


async def _seed_pending_task(
    session: AsyncSession,
    *,
    tenant_id: str,
    priority: int = 5,
) -> str:
    """Insert one PENDING task; return its task_uuid."""

    task = Task(
        task_uuid=str(uuid.uuid4()),
        task_type="COMPACTION",
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        status="PENDING",
        priority=priority,
        max_attempts=3,
    )
    session.add(task)
    await session.commit()
    return task.task_uuid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchedulerMysqlIntegration:

    async def test_register_claim_complete_round_trip(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        try:
            worker = await scheduler_service.register_worker(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w1",
                worker_type="vdw",
                capacity=2,
            )
            task_uuid = await _seed_pending_task(
                mysql_session, tenant_id=isolated_tenant_id,
            )

            claimed = await scheduler_service.claim_next_task(
                mysql_session,
                worker_id=worker.worker_id,
                lease_id=worker.lease_id,
            )
            assert claimed is not None
            assert claimed.task_uuid == task_uuid
            assert claimed.status == "RUNNING"
            assert claimed.tenant_id == isolated_tenant_id  # preserved!

            done = await scheduler_service.complete_task(
                mysql_session,
                worker_id=worker.worker_id,
                task_uuid=task_uuid,
                result={"rows": 42},
            )
            assert done.status == "SUCCEEDED"
            assert done.tenant_id == isolated_tenant_id
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_concurrent_workers_do_not_double_claim(
        self,
        mysql_dsn: str,
        isolated_tenant_id: str,
    ) -> None:
        """Two parallel claims on the same row must not both succeed.

        Uses two independent engines so each gets its own connection and
        the row lock is observable; aborts everything via teardown.
        """

        async def _do_claim(worker_label: str, task_uuid: str) -> Task | None:
            engine = create_async_engine(mysql_dsn, future=True, pool_pre_ping=True)
            install_rls_listener(engine.sync_engine)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            factory = async_sessionmaker(
                engine, expire_on_commit=False, class_=AsyncSession,
            )
            try:
                with with_system_context():
                    async with factory() as session:
                        worker = await scheduler_service.register_worker(
                            session,
                            worker_id=f"itest-{isolated_tenant_id}-{worker_label}",
                            worker_type="vdw",
                        )
                        return await scheduler_service.claim_next_task(
                            session,
                            worker_id=worker.worker_id,
                            lease_id=worker.lease_id,
                        )
            finally:
                await engine.dispose()

        # Seed exactly ONE task on a third connection.
        seed_engine = create_async_engine(mysql_dsn, future=True)
        install_rls_listener(seed_engine.sync_engine)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        seed_factory = async_sessionmaker(
            seed_engine, expire_on_commit=False, class_=AsyncSession,
        )
        with with_system_context():
            async with seed_factory() as seed_session:
                task_uuid = await _seed_pending_task(
                    seed_session, tenant_id=isolated_tenant_id,
                )
        await seed_engine.dispose()

        try:
            results = await asyncio.gather(
                _do_claim("a", task_uuid),
                _do_claim("b", task_uuid),
            )
            claimed = [r for r in results if r is not None]
            # Exactly one of the two parallel claims must win.
            assert len(claimed) == 1
            assert claimed[0].task_uuid == task_uuid
        finally:
            cleanup_engine = create_async_engine(mysql_dsn, future=True)
            install_rls_listener(cleanup_engine.sync_engine)
            cleanup_factory = async_sessionmaker(
                cleanup_engine, expire_on_commit=False, class_=AsyncSession,
            )
            with with_system_context():
                async with cleanup_factory() as s:
                    await _purge(s, isolated_tenant_id)
            await cleanup_engine.dispose()

    async def test_reaper_requeues_orphan_tasks(
        self,
        mysql_session: AsyncSession,
        system_principal: None,
        isolated_tenant_id: str,
    ) -> None:
        try:
            worker = await scheduler_service.register_worker(
                mysql_session,
                worker_id=f"itest-{isolated_tenant_id}-w1",
                worker_type="vdw",
            )
            task_uuid = await _seed_pending_task(
                mysql_session, tenant_id=isolated_tenant_id,
            )
            claimed = await scheduler_service.claim_next_task(
                mysql_session,
                worker_id=worker.worker_id,
                lease_id=worker.lease_id,
            )
            assert claimed is not None

            # Backdate the heartbeat directly on the DB so the reaper sees
            # this worker as stale.  Use a raw UPDATE so the ORM cache does
            # not silently override us on the next refresh.
            await mysql_session.execute(
                text(
                    "UPDATE worker_registry "
                    "SET last_heartbeat_at = NOW() - INTERVAL 5 MINUTE "
                    "WHERE worker_id = :w"
                ).bindparams(w=worker.worker_id),
            )
            await mysql_session.commit()

            reaped, requeued = await scheduler_service.reap_dead_workers(
                mysql_session, timeout=timedelta(seconds=30),
            )
            assert worker.worker_id in reaped
            assert task_uuid in requeued

            await mysql_session.refresh(claimed)
            assert claimed.status == "PENDING"
            assert claimed.worker_id is None
            # Tenancy must still be intact after the reaper's writes.
            assert claimed.tenant_id == isolated_tenant_id
        finally:
            await _purge(mysql_session, isolated_tenant_id)

    async def test_system_context_required(
        self,
        mysql_session: AsyncSession,
        isolated_tenant_id: str,
    ) -> None:
        """Sanity: scheduler primitives fail closed without system principal."""

        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            with pytest.raises(scheduler_service.SchedulerNotSystemError):
                await scheduler_service.register_worker(
                    mysql_session,
                    worker_id=f"itest-{isolated_tenant_id}-noop",
                    worker_type="vdw",
                )
        finally:
            reset_current_tenant(token)
