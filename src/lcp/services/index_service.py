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
``create_index`` inserts a ``BUILDING`` row AND enqueues an
``INDEX_BUILD`` task so a worker can actually build the on-disk ANN
index; the worker flips the row to ``READY`` once lance succeeds.

``BUILDING -> READY``  (INDEX_BUILD worker)
``READY -> OPTIMIZING -> READY``
``READY -> MERGING -> READY``
``* -> FAILED``        (worker; preserves error_message)
``* -> DROPPED``       (terminal, soft-delete)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.time import utcnow_naive
from lcp.db.models import Index
from lcp.services import dataset_service, task_service

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


# ``utcnow_naive`` is imported from ``lcp.core.time`` (shared utility).


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

    # Async half of the build flow: hand a task to the worker so it
    # actually issues lance.create_index and promotes BUILDING -> READY.
    # We purposely DO this AFTER the index row commits so the row always
    # exists before any worker could see the task.  Idempotency key:
    # ``build:<dataset>/<name>:<id>`` — the row ``id`` is stable and guards
    # against replays from retrying HTTP clients.
    #
    # Safety net: if task submission fails the index row is already
    # committed in BUILDING state with no worker coming.  Catch any
    # exception, flip the row to FAILED so callers can see what happened,
    # and re-raise so the HTTP response is still an error.
    try:
        await task_service.submit_task(
            session,
            task_type="INDEX_BUILD",
            dataset_uuid=dataset_uuid,
            params={
                "index_name": index_name,
                "column_name": column_name,
                "index_type": index_type,
                "params": params or {},
            },
            idempotency_key=f"build:{dataset_uuid}/{index_name}:{obj.id}",
        )
    except Exception as exc:
        obj.status = "FAILED"
        obj.error_message = f"task submission failed: {exc}"
        await session.commit()
        raise
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

    # No pagination: a single dataset rarely hosts hundreds of indexes;
    # ``len(items)`` avoids an extra DB round-trip for COUNT.
    return items, len(items)


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
    """Move a READY index to OPTIMIZING and enqueue an INDEX_OPTIMIZE task.

    The state flip is synchronous so concurrent callers cannot overlap; the
    actual lance ``optimize_indices`` call runs asynchronously in the worker
    that picks up the enqueued task and flips the row back to READY.
    """

    obj = await get_index(session, dataset_uuid, index_name)
    if obj.status not in _OPTIMIZABLE_STATES:
        raise IndexTransitionError(
            f"index {index_name!r} is in state {obj.status!r}; only "
            f"{sorted(_OPTIMIZABLE_STATES)} states allow optimize",
        )
    obj.status = "OPTIMIZING"
    obj.last_optimized_at = utcnow_naive()
    await session.commit()
    await session.refresh(obj)

    # Async half: enqueue an INDEX_OPTIMIZE task so the worker actually runs
    # ``lance.optimize_indices`` and promotes OPTIMIZING -> READY.  Submit
    # AFTER the state flip commits so the task can never see a stale READY
    # row.  Idempotency key keys off the row id AND the optimize stamp so a
    # second optimize of the same index after it goes READY again gets a
    # fresh task; HTTP retries within the same flip dedupe.
    #
    # Safety net: if task submission fails the index is stuck in OPTIMIZING.
    # Roll it back to READY and re-raise so the HTTP response is still an
    # error and the caller can retry cleanly.
    try:
        await task_service.submit_task(
            session,
            task_type="INDEX_OPTIMIZE",
            dataset_uuid=dataset_uuid,
            params={"index_name": index_name},
            idempotency_key=(
                f"optimize:{dataset_uuid}/{index_name}:"
                f"{obj.id}:{obj.last_optimized_at.isoformat()}"
            ),
        )
    except Exception as exc:
        obj.status = "READY"
        obj.error_message = f"task submission failed: {exc}"
        await session.commit()
        raise
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
    obj.last_merged_at = utcnow_naive()
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
