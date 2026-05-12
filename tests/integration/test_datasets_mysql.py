"""Integration tests against the real MySQL instance running inside the local
Colima k3s cluster (NodePort ``127.0.0.1:30306``).

The DB schema must already be loaded; see
``docs/architecture/ddl/lcp_state_schema.sql`` and the bootstrap commands in
the README.  Tests are auto-skipped when the MySQL endpoint is not reachable
so CI never blocks if the developer's K8s is offline.

Each test gets a fresh tenant id and cleans its rows on teardown to keep the
DB usable across reruns without manual TRUNCATE.
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
from lcp.schemas.dataset import DatasetRegisterRequest
from lcp.services import dataset_service

pytestmark = pytest.mark.integration


# Default DSN matches the local Colima NodePort layout; can be overridden via
# ``LCP_TEST_MYSQL_DSN`` so the same suite runs against any reachable MySQL.
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
    """Yield a tenant-scoped AsyncSession bound to real MySQL.

    The RLS listener is installed on the engine so the queries behave exactly
    like they do under uvicorn.
    """

    engine = create_async_engine(mysql_dsn, future=True, pool_pre_ping=True)
    install_rls_listener(engine.sync_engine)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def isolated_tenant() -> Iterator[str]:
    """Bind a fresh tenant principal for the test and reset it afterwards."""

    tenant_id = f"itest-{uuid.uuid4().hex[:8]}"
    token = set_current_tenant(
        TenantPrincipal(tenant_id=tenant_id, subject="itest", auth_method="oidc"),
    )
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


async def _purge_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Remove every row this test created so reruns stay deterministic."""

    await session.execute(
        text("DELETE FROM dataset WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.commit()


# ---------------------------------------------------------------------------
# CRUD round-trip on real MySQL
# ---------------------------------------------------------------------------


class TestDatasetServiceMysqlIntegration:

    async def test_create_get_list_delete_round_trip(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            payload = DatasetRegisterRequest.model_validate(
                {
                    "catalog": "lance_catalog",
                    "schema": "media",
                    "table": f"images_{uuid.uuid4().hex[:6]}",
                    "storage_uri": "s3://bucket/images",
                    "owner": "itest",
                },
            )
            created = await dataset_service.create_dataset(mysql_session, payload)
            assert created.tenant_id == tenant_id
            assert created.status == "ACTIVE"

            fetched = await dataset_service.get_dataset(
                mysql_session, created.dataset_uuid,
            )
            assert fetched.dataset_uuid == created.dataset_uuid

            items, total = await dataset_service.list_datasets(mysql_session)
            assert total == 1
            assert items[0].dataset_uuid == created.dataset_uuid

            await dataset_service.delete_dataset(
                mysql_session, created.dataset_uuid,
            )
            after = await dataset_service.get_dataset(
                mysql_session, created.dataset_uuid,
            )
            assert after.status == "DELETED"
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_unique_catalog_schema_table_violates(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            tbl = f"dup_{uuid.uuid4().hex[:6]}"
            payload = DatasetRegisterRequest.model_validate(
                {
                    "catalog": "c",
                    "schema": "s",
                    "table": tbl,
                    "storage_uri": "s3://b/p",
                },
            )
            await dataset_service.create_dataset(mysql_session, payload)
            with pytest.raises(dataset_service.DatasetAlreadyExistsError):
                await dataset_service.create_dataset(mysql_session, payload)
        finally:
            await _purge_tenant(mysql_session, tenant_id)
