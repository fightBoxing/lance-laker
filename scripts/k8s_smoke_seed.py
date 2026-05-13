"""End-to-end k8s smoke fixture: seed one dataset + lifecycle policy.

Runs inside the lcp-api Pod via ``kubectl exec``.  Using the service
layer directly bypasses OIDC (we don't ship a real IdP in the dev
cluster) while still exercising the full DB / state-machine path.

This script is *only* for the deployment dry-run; tests use proper
fixtures.  Idempotent: running twice does not double-create.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lcp.core.config import get_settings
from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)
from lcp.db.rls import install_rls_listener
from lcp.schemas.dataset import DatasetRegisterRequest
from lcp.schemas.lifecycle import PolicyCreateRequest
from lcp.services import dataset_service, lifecycle_service


TENANT = "smoke-tenant"
CATALOG = "smoke"
DB_SCHEMA = "smoke_ns"
TABLE_NAME = "k8s_smoke_dataset"
POLICY_NAME = "smoke_ttl"


def _principal() -> TenantPrincipal:
    """Build a synthetic OIDC-equivalent principal for the seed run."""

    return TenantPrincipal(
        tenant_id=TENANT,
        subject="k8s-smoke",
        auth_method="oidc",
        is_system=False,
    )


async def _find_existing_dataset(session) -> str | None:
    """Return dataset_uuid if already seeded, else None.  RLS-scoped."""

    items, _total = await dataset_service.list_datasets(
        session,
        catalog=CATALOG,
        db_schema=DB_SCHEMA,
        page=1,
        page_size=50,
    )
    for ds in items:
        if ds.table_name == TABLE_NAME:
            return ds.dataset_uuid
    return None


async def _seed() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.db_dsn, future=True, pool_pre_ping=True)
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    token = set_current_tenant(_principal())
    try:
        async with factory() as session:
            existing_uuid = await _find_existing_dataset(session)
            if existing_uuid is None:
                req = DatasetRegisterRequest.model_validate(
                    {
                        "catalog": CATALOG,
                        "schema": DB_SCHEMA,
                        "table": TABLE_NAME,
                        "storage_uri": (
                            "s3://lcp-lance/smoke/k8s_smoke_dataset"
                        ),
                        "owner": "k8s-smoke",
                        "description": "End-to-end k8s deployment dry-run.",
                    },
                )
                ds = await dataset_service.create_dataset(
                    session, payload=req,
                )
                dataset_uuid = ds.dataset_uuid
                print(f"created dataset {dataset_uuid}")
            else:
                dataset_uuid = existing_uuid
                print(f"reused dataset {dataset_uuid}")

            # Idempotency: skip policy creation if already there.
            try:
                existing_policy = await lifecycle_service.get_policy(
                    session, dataset_uuid, POLICY_NAME,
                )
                print(
                    f"reused policy {existing_policy.policy_name} "
                    f"(enabled={existing_policy.enabled})",
                )
            except lifecycle_service.PolicyNotFoundError:
                pol = PolicyCreateRequest(
                    policy_name=POLICY_NAME,
                    ttl_days=1,
                    enabled=True,
                )
                created = await lifecycle_service.create_policy(
                    session,
                    dataset_uuid=dataset_uuid,
                    payload=pol,
                )
                print(
                    f"created policy {created.policy_name} "
                    f"(enabled={created.enabled})",
                )
        return 0
    finally:
        reset_current_tenant(token)
        await engine.dispose()


def main() -> int:
    print(f"smoke seed run at {datetime.now(timezone.utc).isoformat()}")
    try:
        return asyncio.run(_seed())
    except Exception as exc:  # noqa: BLE001 -- top-level CLI
        print(f"seed failed: {exc!r}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
