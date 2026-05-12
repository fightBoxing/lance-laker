"""Business logic for the ``vector_index`` table.

Tenant isolation strategy
-------------------------
The DDL deliberately omits ``tenant_id`` from ``vector_index`` to avoid
duplicating data that already lives on the parent ``dataset``.  Instead, every
operation in this module first calls
:func:`lcp.services.dataset_service.get_dataset` which:

1. Goes through the RLS hook and matches by ``dataset.tenant_id``.
2. Raises :class:`lcp.services.dataset_service.DatasetNotFoundError` when the
   caller does not own the dataset (cross-tenant access -> 404).

So a malicious caller cannot read another tenant's index even though
``vector_index`` itself is not in the RLS registry.

State machine
-------------
``BUILDING -> READY``  (worker)
``READY -> OPTIMIZING -> READY``
``READY -> MERGING -> READY``
``* -> FAILED``        (worker; preserves error_message)
``* -> DROPPED``       (terminal, soft-delete)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Index
from lcp.services import dataset_service

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IndexNotFoundError(Exception):
    """Raised when an index name cannot be resolved within a dataset."""


class IndexAlreadyExistsError(Exception):
    """Raised when ``(dataset_uuid, index_name)`` already exists."""


class IndexTransitionError(Exception):
    """Raised when an index state transition is not allowed."""


# ---------------------------------------------------------------------------
# State sets
# ---------------------------------------------------------------------------


# Operations that mutate state are only allowed when the index is in a
# stable state (READY); BUILDING / OPTIMIZING / MERGING tasks are exclusive.
_OPTIMIZABLE_STATES: frozenset[str] = frozenset({"READY"})
_MERGEABLE_STATES: frozenset[str] = frozenset({"READY"})

# Drop is allowed from any non-terminal state; calling drop on an already
# dropped index is a no-op (idempotent).
_TERMINAL_STATES: frozenset[str] = frozenset({"DROPPED"})


def _utcnow() -> datetime:
    """Return naive UTC ``datetime`` matching the DATETIME(3) column type."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


async def create_index(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    index_name: str,
    column_name: str,
    index_type: str,
    params: dict[str, Any] | None = None,
    replace_if_exists: bool = False,
) -> Index:
    """Insert a new index row in ``BUILDING`` state.

    The actual index build runs asynchronously in a worker; this method only
    creates the metadata row.  ``replace_if_exists=True`` drops the existing
    index before inserting (useful for re-creates with new params).
    """

    # Tenant guard: dataset_service.get_dataset is RLS-filtered, so a wrong
    # tenant gets DatasetNotFoundError -> the router renders it as 404.
    await dataset_service.get_dataset(session, dataset_uuid)

    if replace_if_exists:
        existing = await _maybe_get_index(session, dataset_uuid, index_name)
        if existing is not None:
            await session.delete(existing)
            await session.flush()

    obj = Index(
        dataset_uuid=dataset_uuid,
        index_name=index_name,
        column_name=column_name,
        index_type=index_type,
        params=params,
        status="BUILDING",
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise IndexAlreadyExistsError(
            f"index {index_name!r} already exists on dataset {dataset_uuid}",
        ) from exc
    await session.commit()
    await session.refresh(obj)
    return obj


async def get_index(
    session: AsyncSession,
    dataset_uuid: str,
    index_name: str,
) -> Index:
    """Return one index row; cross-tenant access yields 404 via dataset guard."""

    await dataset_service.get_dataset(session, dataset_uuid)
    obj = await _maybe_get_index(session, dataset_uuid, index_name)
    if obj is None:
        raise IndexNotFoundError(f"{dataset_uuid}/{index_name}")
    return obj


async def list_indexes(
    session: AsyncSession,
    dataset_uuid: str,
) -> tuple[Sequence[Index], int]:
    """Return ``(items, total)`` for the dataset's indexes.

    No paging at this level: a single dataset is unlikely to host hundreds of
    indexes.  Add it later if real workloads demand it.
    """

    await dataset_service.get_dataset(session, dataset_uuid)

    base: Select[Any] = select(Index).where(Index.dataset_uuid == dataset_uuid)
    items = (
        await session.execute(base.order_by(Index.created_at.desc()))
    ).scalars().all()

    count_stmt: Select[Any] = select(func.count(Index.id)).where(
        Index.dataset_uuid == dataset_uuid,
    )
    total = (await session.execute(count_stmt)).scalar_one()
    return items, int(total)


async def drop_index(
    session: AsyncSession,
    dataset_uuid: str,
    index_name: str,
) -> None:
    """Soft-delete: flip status to DROPPED.

    Hard removal happens later in the lifecycle worker so administrators can
    audit the operation.
    """

    obj = await get_index(session, dataset_uuid, index_name)
    if obj.status in _TERMINAL_STATES:
        return  # already dropped, idempotent
    obj.status = "DROPPED"
    await session.commit()


async def optimize_index(
    session: AsyncSession,
    dataset_uuid: str,
    index_name: str,
) -> Index:
    """Move a READY index to OPTIMIZING and stamp ``last_optimized_at``.

    Real optimisation work happens asynchronously; this method only flips the
    state so concurrent callers cannot trigger overlapping optimisations.
    """

    obj = await get_index(session, dataset_uuid, index_name)
    if obj.status not in _OPTIMIZABLE_STATES:
        raise IndexTransitionError(
            f"index {index_name!r} is in state {obj.status!r}; only "
            f"{sorted(_OPTIMIZABLE_STATES)} states allow optimize",
        )
    obj.status = "OPTIMIZING"
    obj.last_optimized_at = _utcnow()
    await session.commit()
    await session.refresh(obj)
    return obj


async def merge_index(
    session: AsyncSession,
    dataset_uuid: str,
    index_name: str,
) -> Index:
    """Move a READY index to MERGING and stamp ``last_merged_at``."""

    obj = await get_index(session, dataset_uuid, index_name)
    if obj.status not in _MERGEABLE_STATES:
        raise IndexTransitionError(
            f"index {index_name!r} is in state {obj.status!r}; only "
            f"{sorted(_MERGEABLE_STATES)} states allow merge",
        )
    obj.status = "MERGING"
    obj.last_merged_at = _utcnow()
    await session.commit()
    await session.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _maybe_get_index(
    session: AsyncSession,
    dataset_uuid: str,
    index_name: str,
) -> Index | None:
    """Return the index row or None; assumes the dataset guard already ran."""

    stmt: Select[Any] = select(Index).where(
        Index.dataset_uuid == dataset_uuid,
        Index.index_name == index_name,
    )
    return (await session.execute(stmt)).scalar_one_or_none()
