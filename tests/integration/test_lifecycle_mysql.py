"""Integration tests for the lifecycle policy service against real MySQL.

Same shape as the dataset/task/index integration suites: target the local
Colima k3s ``mysql-cdc`` Pod (NodePort 30306) and auto-skip when the
endpoint is unreachable.

Each test isolates itself with a unique tenant_id and purges via the parent
dataset (``ON DELETE CASCADE`` on ``fk_policy_dataset``).
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
from lcp.schemas.lifecycle import PolicyCreateRequest, PolicyUpdateRequest
from lcp.services import dataset_service, lifecycle_service

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

    tenant_id = f"itest-pol-{uuid.uuid4().hex[:8]}"
    token = set_current_tenant(
        TenantPrincipal(tenant_id=tenant_id, subject="itest", auth_method="oidc"),
    )
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


async def _purge_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Delete the tenant's datasets; CASCADE removes attached policies."""

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
# Round-trip + edge cases on real MySQL
# ---------------------------------------------------------------------------


class TestLifecycleServiceMysqlIntegration:

    async def test_crud_round_trip(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)

            created = await lifecycle_service.create_policy(
                mysql_session,
                dataset_uuid=ds_uuid,
                payload=PolicyCreateRequest(
                    policy_name="standard-90d",
                    ttl_days=90,
                    tier_rules={"hot_to_warm_days": 30},
                ),
            )
            assert created.enabled is True
            assert created.ttl_days == 90

            fetched = await lifecycle_service.get_policy(
                mysql_session, ds_uuid, "standard-90d",
            )
            assert fetched.tier_rules == {"hot_to_warm_days": 30}

            items, total = await lifecycle_service.list_policies(
                mysql_session, ds_uuid,
            )
            assert total == 1
            assert items[0].policy_name == "standard-90d"

            patched = await lifecycle_service.update_policy(
                mysql_session, ds_uuid, "standard-90d",
                PolicyUpdateRequest(ttl_days=180),
            )
            assert patched.ttl_days == 180
            # Field omitted from PATCH must keep its prior value.
            assert patched.tier_rules == {"hot_to_warm_days": 30}

            await lifecycle_service.delete_policy(
                mysql_session, ds_uuid, "standard-90d",
            )
            with pytest.raises(lifecycle_service.PolicyNotFoundError):
                await lifecycle_service.get_policy(
                    mysql_session, ds_uuid, "standard-90d",
                )
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_duplicate_policy_raises(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)
            await lifecycle_service.create_policy(
                mysql_session,
                dataset_uuid=ds_uuid,
                payload=PolicyCreateRequest(policy_name="dup"),
            )
            with pytest.raises(lifecycle_service.PolicyAlreadyExistsError):
                await lifecycle_service.create_policy(
                    mysql_session,
                    dataset_uuid=ds_uuid,
                    payload=PolicyCreateRequest(policy_name="dup"),
                )
        finally:
            await _purge_tenant(mysql_session, tenant_id)

    async def test_enable_disable_toggle(
        self,
        mysql_session: AsyncSession,
        isolated_tenant: str,
    ) -> None:
        tenant_id = isolated_tenant
        try:
            ds_uuid = await _create_dataset(mysql_session)
            await lifecycle_service.create_policy(
                mysql_session,
                dataset_uuid=ds_uuid,
                payload=PolicyCreateRequest(policy_name="p"),
            )
            disabled = await lifecycle_service.set_enabled(
                mysql_session, ds_uuid, "p", enabled=False,
            )
            assert disabled.enabled is False
            # Idempotent: disabling an already-disabled policy is a no-op.
            again = await lifecycle_service.set_enabled(
                mysql_session, ds_uuid, "p", enabled=False,
            )
            assert again.enabled is False
            enabled = await lifecycle_service.set_enabled(
                mysql_session, ds_uuid, "p", enabled=True,
            )
            assert enabled.enabled is True
        finally:
            await _purge_tenant(mysql_session, tenant_id)
