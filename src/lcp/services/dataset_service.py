"""CRUD business logic for the ``dataset`` table.

Why a separate service layer (and not a fat router):
- Routers stay HTTP-only and easy to swap for gRPC.
- Services are unit-testable with an in-memory sqlite session.
- The RLS hook in :mod:`lcp.db.rls` already injects ``tenant_id``; this layer
  must NOT add a redundant ``WHERE tenant_id = ...`` clause or it would
  collide with the hook on join rewrites.

Error contract:
- ``DatasetAlreadyExistsError`` -> router returns 409.
- ``DatasetNotFoundError``      -> router returns 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.tenant import require_current_tenant
from lcp.db.models import Dataset
from lcp.schemas.dataset import DatasetRegisterRequest


class DatasetAlreadyExistsError(Exception):
    """Raised when ``(catalog, schema, table)`` already exists for the tenant."""


class DatasetNotFoundError(Exception):
    """Raised when a dataset_uuid cannot be resolved in the current tenant."""


async def create_dataset(
    session: AsyncSession,
    payload: DatasetRegisterRequest,
) -> Dataset:
    """Insert a new dataset row.

    The RLS hook does not auto-inject ``tenant_id`` on INSERT (it only filters
    SELECT/UPDATE/DELETE); we therefore read the current principal explicitly.
    """

    principal = require_current_tenant()

    obj = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        catalog=payload.catalog,
        db_schema=payload.db_schema,
        table_name=payload.table_name,
        storage_uri=payload.storage_uri,
        tenant_id=principal.tenant_id,
        owner=payload.owner,
        description=payload.description,
        status="ACTIVE",
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DatasetAlreadyExistsError(
            f"dataset {payload.catalog}.{payload.db_schema}.{payload.table_name} already exists",
        ) from exc
    await session.commit()
    await session.refresh(obj)
    return obj


async def get_dataset(session: AsyncSession, dataset_uuid: str) -> Dataset:
    """Fetch one dataset by UUID; RLS hook adds the tenant filter."""

    stmt = select(Dataset).where(Dataset.dataset_uuid == dataset_uuid)
    result = await session.execute(stmt)
    obj = result.scalar_one_or_none()
    if obj is None:
        raise DatasetNotFoundError(dataset_uuid)
    return obj


def _apply_dataset_filters(
    stmt: Select[Any],
    *,
    catalog: str | None = None,
    db_schema: str | None = None,
) -> Select[Any]:
    """Append optional WHERE predicates shared by list and count queries.

    Extracted so the two queries stay in sync when filters are added.
    """

    if catalog is not None:
        stmt = stmt.where(Dataset.catalog == catalog)
    if db_schema is not None:
        stmt = stmt.where(Dataset.db_schema == db_schema)
    return stmt


async def list_datasets(
    session: AsyncSession,
    *,
    catalog: str | None = None,
    db_schema: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Dataset], int]:
    """Return ``(items, total)`` for the current tenant.

    Pagination uses 1-based ``page`` because the OpenAPI documents it that way.
    """

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 20
    if page_size > 200:
        page_size = 200

    # Count via a direct ``SELECT count(*) FROM dataset`` so the RLS hook can
    # match the ``dataset`` leaf and inject the tenant filter.  Wrapping
    # in a subquery would hide the table name behind an anonymous alias and
    # the predicate would silently be skipped, allowing cross-tenant count
    # leakage.
    count_stmt = _apply_dataset_filters(
        select(func.count(Dataset.id)), catalog=catalog, db_schema=db_schema,
    )
    total = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        _apply_dataset_filters(
            select(Dataset), catalog=catalog, db_schema=db_schema,
        )
        .order_by(Dataset.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await session.execute(items_stmt)).scalars().all()
    return items, int(total)


async def delete_dataset(session: AsyncSession, dataset_uuid: str) -> None:
    """Soft-delete: flip status to DELETED.

    Hard removal happens later in the lifecycle worker, so the row is still
    visible to administrators for audit purposes.
    """

    obj = await get_dataset(session, dataset_uuid)
    obj.status = "DELETED"
    await session.commit()
