"""Integration tests for the index service against real MySQL.

Indexes are tenant-isolated indirectly through their parent dataset, so each
test creates both the dataset and the index, then cleans up via the dataset
cascade (``ON DELETE CASCADE`` on ``fk_index_dataset``) plus an explicit
DELETE on the dataset row.

Tests are auto-skipped when MySQL is unreachable.
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
from lcp.services import dataset_service, index_service

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

    tenant_id = f"itest-idx-{uuid.uuid4().hex[:8]}"
    token = set_current_tenant(
        TenantPrincipal(tenant_id=tenant_id, subject="itest", auth_method="oidc"),
    )
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


async def _purge_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Remove all dataset rows for the tenant; CASCADE removes the indexes."""

    await session.execute(
        text("DELETE FROM dataset WHERE tenant_id = :t").bindparams(t=tenant_id),
    )
    await session.commit()


async def _create_dataset(session: AsyncSession) -> str:
    """Helper: create a dataset under the current tenant; return its uuid."""

    payload = DatasetRegisterRequest.model_validate(
        {
            "catalog": "lance_catalog",
            "schema": "media",
            "table": f"images_{uuid.uuid4().hex[:6]}",
            "storage_uri": "s3://bucket/images",
        },
    )
    obj = await dataset_service.create_dataset(session, payload)
    return obj.dataset_uuid


# ---------------------------------------------------------------------------
# Round-trip + state-machine on real MySQL
# ---------------------------------------------------------------------------


class TestIndexServiceMysqlIntegration:

    async def test_create_get_list_drop_round_trip(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)

            created = await index_service.create_index(
                mysql_session,
                dataset_uuid=ds_uuid,
                index_name="image_vector_hnsw",
                column_name="image_vector",
                index_type="HNSW",
                params={"m": 16, "ef_construction": 200},
            )
            assert created.status == "BUILDING"
            assert created.dataset_uuid == ds_uuid

            fetched = await index_service.get_index(
                mysql_session, ds_uuid, "image_vector_hnsw",
            )
            assert fetched.column_name == "image_vector"

            items, total = await index_service.list_indexes(mysql_session, ds_uuid)
            assert total == 1
            assert items[0].index_name == "image_vector_hnsw"

            await index_service.drop_index(
                mysql_session, ds_uuid, "image_vector_hnsw",
            )
            after = await index_service.get_index(
                mysql_session, ds_uuid, "image_vector_hnsw",
            )
            assert after.status == "DROPPED"
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_duplicate_index_raises(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)
            await index_service.create_index(
                mysql_session,
                dataset_uuid=ds_uuid,
                index_name="dup_idx",
                column_name="vec",
                index_type="IVF_FLAT",
            )
            with pytest.raises(index_service.IndexAlreadyExistsError):
                await index_service.create_index(
                    mysql_session,
                    dataset_uuid=ds_uuid,
                    index_name="dup_idx",
                    column_name="vec",
                    index_type="IVF_FLAT",
                )
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_optimize_only_on_ready(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)
            await index_service.create_index(
                mysql_session,
                dataset_uuid=ds_uuid,
                index_name="opt_idx",
                column_name="vec",
                index_type="HNSW",
            )
            # Fresh index is BUILDING -> optimize must be rejected.
            with pytest.raises(index_service.IndexTransitionError):
                await index_service.optimize_index(
                    mysql_session, ds_uuid, "opt_idx",
                )

            # Simulate a worker finishing the build.
            obj = await index_service.get_index(mysql_session, ds_uuid, "opt_idx")
            obj.status = "READY"
            await mysql_session.commit()

            optimized = await index_service.optimize_index(
                mysql_session, ds_uuid, "opt_idx",
            )
            assert optimized.status == "OPTIMIZING"
            assert optimized.last_optimized_at is not None
        finally:
            await _purge_tenant(mysql_session, tenant_id)
