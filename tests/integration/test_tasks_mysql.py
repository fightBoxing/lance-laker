"""Integration tests for the task service against real MySQL.

Target the same Colima k3s ``mysql-cdc`` Pod (NodePort 30306) as the dataset
integration suite.  Each test isolates itself with a unique tenant_id and
purges its rows on teardown, so the suite is idempotent across reruns.

Tests are auto-skipped if MySQL is unreachable so CI never blocks when the
local cluster is offline.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)
from lcp.db.rls import install_rls_listener
from lcp.services import task_service

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
    """Yield a tenant-scoped AsyncSession bound to real MySQL."""

    engine = create_async_engine(mysql_dsn, future=True, pool_pre_ping=True)
    install_rls_listener(engine.sync_engine)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def isolated_tenant() -> Iterator[str]:
    """Bind a fresh tenant principal per test and reset it afterwards."""

    tenant_id = f"itest-task-{uuid.uuid4().hex[:8]}"
    token = set_current_tenant(
        TenantPrincipal(tenant_id=tenant_id, subject="itest", auth_method="oidc"),
    )
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


async def _purge_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Remove every task row this test created so reruns stay deterministic."""

    await session.execute(
        text("DELETE FROM task WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Round-trip + state-machine on real MySQL
# ---------------------------------------------------------------------------


class TestTaskServiceMysqlIntegration:

    async def test_submit_get_list_cancel_round_trip(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            obj, created = await task_service.submit_task(
                mysql_session,
                task_type="COMPACTION",
                dataset_uuid=str(uuid.uuid4()),
                priority=3,
            )
            assert created is True
            assert obj.tenant_id == tenant_id
            assert obj.status == "PENDING"

            fetched = await task_service.get_task(mysql_session, obj.task_uuid)
            assert fetched.task_uuid == obj.task_uuid

            items, total = await task_service.list_tasks(mysql_session)
            assert total == 1
            assert items[0].task_uuid == obj.task_uuid

            cancelled = await task_service.cancel_task(mysql_session, obj.task_uuid)
            assert cancelled.status == "CANCELLED"
            assert cancelled.finished_at is not None

            # Cancelling an already-cancelled task is idempotent.
            again = await task_service.cancel_task(mysql_session, obj.task_uuid)
            assert again.status == "CANCELLED"
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_idempotency_key_returns_same_row(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            key = f"itest-key-{uuid.uuid4().hex[:8]}"
            first, c1 = await task_service.submit_task(
                mysql_session,
                task_type="VECTORIZE",
                dataset_uuid=str(uuid.uuid4()),
                idempotency_key=key,
            )
            second, c2 = await task_service.submit_task(
                mysql_session,
                task_type="VECTORIZE",
                dataset_uuid=str(uuid.uuid4()),
                idempotency_key=key,
            )
            assert c1 is True
            assert c2 is False
            assert first.task_uuid == second.task_uuid
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_retry_only_on_failed(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            obj, _ = await task_service.submit_task(
                mysql_session,
                task_type="INDEX_BUILD",
                dataset_uuid=str(uuid.uuid4()),
            )
            # PENDING -> retry must be rejected.
            with pytest.raises(task_service.TaskTransitionError):
                await task_service.retry_task(mysql_session, obj.task_uuid)

            # Simulate a worker marking the task FAILED, then retry succeeds.
            obj.status = "FAILED"
            obj.error_code = "E_BOOM"
            obj.error_message = "simulated"
            await mysql_session.commit()

            retried = await task_service.retry_task(mysql_session, obj.task_uuid)
            assert retried.status == "PENDING"
            assert retried.attempt == 1
            assert retried.error_code is None
        finally:
            await _purge_tenant(mysql_session, tenant_id)
