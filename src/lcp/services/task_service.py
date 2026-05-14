"""Business logic for the ``task`` table.

Responsibilities:
- Translate OpenAPI shapes to ORM rows.
- Enforce the task state machine (PENDING -> QUEUED -> RUNNING -> SUCCEEDED |
  FAILED | CANCELLED) on every transition; only ``submit`` / ``cancel`` /
  ``retry`` are exposed at the API layer for now — the worker-side
  transitions (``QUEUED``, ``RUNNING``, ``SUCCEEDED``, ``FAILED``) will be
  driven by the scheduler in a future module.
- Honour ``idempotency_key`` so retried submissions return the existing row
  instead of creating a duplicate (per OpenAPI guidance).

The RLS hook in :mod:`lcp.db.rls` automatically filters every SELECT/UPDATE/
DELETE by ``tenant_id``; we still set ``tenant_id`` on INSERT explicitly
because the hook only rewrites read/modify statements.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.tenant import require_current_tenant
from lcp.core.time import utcnow_naive
from lcp.db.models import Task

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Terminal states cannot be cancelled or transitioned further.
_TERMINAL_STATES: frozenset[str] = frozenset({"SUCCEEDED", "CANCELLED"})

# Cancellation is a no-op when already cancelled and rejected from terminal
# success / failed states (failed tasks should be retried, not cancelled).
_CANCELLABLE_STATES: frozenset[str] = frozenset({"PENDING", "QUEUED", "RUNNING"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskNotFoundError(Exception):
    """Raised when a task_uuid cannot be resolved in the current tenant."""


class TaskTransitionError(Exception):
    """Raised when a state transition is not allowed by the state machine."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ``utcnow_naive`` is imported from ``lcp.core.time`` (shared utility).


# ---------------------------------------------------------------------------
# CRUD + state-machine operations
# ---------------------------------------------------------------------------


async def submit_task(
    session: AsyncSession,
    *,
    task_type: str,
    dataset_uuid: str,
    priority: int = 5,
    params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> tuple[Task, bool]:
    """Insert a new task in ``PENDING`` state.

    Returns ``(task, created)`` so the router can reply ``202 Accepted`` for a
    fresh submission and ``200 OK`` for an idempotent replay (same key).
    The flag mirrors the ``Created? bool`` pattern used by Kubernetes ``apply``.
    """

    principal = require_current_tenant()

    # Idempotency check: if a task with the same key already exists, return it
    # (and do NOT insert a duplicate).  We do this *before* the INSERT to give
    # callers a clean ``200 OK`` path; we still rely on the unique constraint
    # as the last line of defence in case of a race.
    if idempotency_key is not None:
        existing_stmt: Select[Any] = select(Task).where(
            Task.idempotency_key == idempotency_key,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            return existing, False

    obj = Task(
        task_uuid=str(uuid.uuid4()),
        task_type=task_type,
        dataset_uuid=dataset_uuid,
        tenant_id=principal.tenant_id,
        status="PENDING",
        priority=priority,
        params=params,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError:
        # Race: a concurrent submitter inserted the same idempotency_key
        # between the SELECT above and our INSERT.  Re-read and return the
        # winning row instead of surfacing a 500.
        await session.rollback()
        if idempotency_key is None:  # pragma: no cover - DDL has no other unique
            raise
        retry_stmt: Select[Any] = select(Task).where(
            Task.idempotency_key == idempotency_key,
        )
        winner = (await session.execute(retry_stmt)).scalar_one_or_none()
        if winner is None:  # pragma: no cover - extremely unlikely
            raise
        return winner, False

    await session.commit()
    await session.refresh(obj)
    return obj, True


async def get_task(session: AsyncSession, task_uuid: str) -> Task:
    """Return a single task; RLS hook adds the tenant predicate."""

    stmt: Select[Any] = select(Task).where(Task.task_uuid == task_uuid)
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise TaskNotFoundError(task_uuid)
    return obj


def _apply_task_filters(
    stmt: Select[Any],
    *,
    dataset_uuid: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
) -> Select[Any]:
    """Append optional WHERE predicates shared by list and count queries.

    Extracted so the two queries stay in sync when filters are added.
    """

    if dataset_uuid is not None:
        stmt = stmt.where(Task.dataset_uuid == dataset_uuid)
    if task_type is not None:
        stmt = stmt.where(Task.task_type == task_type)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    return stmt


async def list_tasks(
    session: AsyncSession,
    *,
    dataset_uuid: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Task], int]:
    """Return ``(items, total)`` filtered by tenant + optional facets."""

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 20
    if page_size > 200:
        page_size = 200

    filters = dict(dataset_uuid=dataset_uuid, task_type=task_type, status=status)

    # Direct count off the Task table so the RLS hook can match the leaf and
    # inject ``tenant_id``; wrapping in a subquery would hide the table name.
    count_stmt = _apply_task_filters(select(func.count(Task.id)), **filters)
    total = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        _apply_task_filters(select(Task), **filters)
        .order_by(Task.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await session.execute(items_stmt)).scalars().all()
    return items, int(total)


async def cancel_task(session: AsyncSession, task_uuid: str) -> Task:
    """Move a non-terminal task to ``CANCELLED``.

    Idempotent: cancelling an already-cancelled task is a no-op (returns the
    same row).  Cancelling a SUCCEEDED / FAILED task raises
    :class:`TaskTransitionError` so the router can return 409.
    """

    obj = await get_task(session, task_uuid)
    if obj.status == "CANCELLED":
        return obj
    if obj.status not in _CANCELLABLE_STATES:
        raise TaskTransitionError(
            f"task {task_uuid} is in terminal state {obj.status!r} and cannot be cancelled",
        )
    obj.status = "CANCELLED"
    obj.finished_at = utcnow_naive()
    await session.commit()
    await session.refresh(obj)
    return obj


async def retry_task(session: AsyncSession, task_uuid: str) -> Task:
    """Re-queue a ``FAILED`` task as ``PENDING``.

    Resets ``error_*`` fields and increments ``attempt`` so callers can see
    how many retries have been issued.  Refusing to retry a non-FAILED task
    keeps the worker from accidentally re-running a successful job.
    """

    obj = await get_task(session, task_uuid)
    if obj.status != "FAILED":
        raise TaskTransitionError(
            f"task {task_uuid} is in state {obj.status!r}; only FAILED tasks can be retried",
        )
    if obj.attempt >= obj.max_attempts:
        raise TaskTransitionError(
            f"task {task_uuid} has exhausted retries ({obj.attempt}/{obj.max_attempts})",
        )
    obj.status = "PENDING"
    obj.attempt += 1
    obj.error_code = None
    obj.error_message = None
    obj.started_at = None
    obj.finished_at = None
    await session.commit()
    await session.refresh(obj)
    return obj
