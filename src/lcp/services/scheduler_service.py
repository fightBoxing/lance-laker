"""Scheduler primitives for worker registration and task dispatch.

This module is the *runtime* counterpart of the CRUD-only services elsewhere
in :mod:`lcp.services`.  It owns:

- worker registration / heartbeat / drain (``worker_registry`` table);
- atomic claim of the next pending task (``SELECT ... FOR UPDATE SKIP LOCKED``
  on MySQL; degrades to ordinary ``FOR UPDATE`` on SQLite during unit tests);
- worker-side state transitions ``QUEUED -> RUNNING``, ``RUNNING -> SUCCEEDED``
  and ``RUNNING -> FAILED``;
- reaping of dead workers and re-queueing of their orphan tasks.

System principal
----------------
Every public function here MUST be invoked under
:func:`lcp.core.tenant.with_system_context`; otherwise the RLS hook will
either fail-closed (no principal) or filter out cross-tenant rows the
scheduler legitimately needs to see.  Each helper raises
:class:`SchedulerNotSystemError` when it detects a non-system principal.

What this module does NOT do
----------------------------
- It does NOT run a daemon loop.  Long-process concerns (signal handling,
  graceful shutdown, log/metrics fan-out) are deferred; the user wires
  these primitives into a CLI / Job / supervisor of their choice.
- It does NOT export REST endpoints.  Workers are inside the trust boundary
  and are expected to reach the scheduler via gRPC or an in-process import.
- It does NOT use ``distributed_lock`` for leader election.  A single
  scheduler instance is the current model; multi-leader support is a
  separate iteration.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.tenant import get_current_tenant
from lcp.db.models import Task, WorkerRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Heartbeat older than this means the worker is presumed dead.  Picked to be
# ~3x a comfortable heartbeat interval; configurable per call so tests can
# inject very small values.
DEFAULT_HEARTBEAT_TIMEOUT = timedelta(seconds=30)


# Task states that participate in the scheduler's state machine.
_PENDING = "PENDING"
_QUEUED = "QUEUED"
_RUNNING = "RUNNING"
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"
_CANCELLED = "CANCELLED"

# Status enum on the worker_registry table.
_ALIVE = "ALIVE"
_DRAINING = "DRAINING"
_DEAD = "DEAD"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SchedulerNotSystemError(Exception):
    """Raised when scheduler primitives run without a system principal."""


class WorkerNotFoundError(Exception):
    """Raised when a heartbeat / drain references an unknown worker_id."""


class WorkerLeaseMismatchError(Exception):
    """Raised when an inbound heartbeat's lease_id no longer matches.

    A common cause is the worker restarting (and rotating its lease) while a
    stale heartbeat from the old lease is still in flight; the stale call
    must be rejected so the new lease stays the source of truth.
    """


class TaskTransitionError(Exception):
    """Raised when a worker - side task transition is not allowed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return naive UTC datetime that matches the DATETIME(3) columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_system() -> None:
    """Refuse to run unless the caller bound a system principal."""

    principal = get_current_tenant()
    if principal is None or not getattr(principal, "is_system", False):
        raise SchedulerNotSystemError(
            "scheduler primitives must run under with_system_context()",
        )


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


async def register_worker(
    session: AsyncSession,
    *,
    worker_id: str,
    worker_type: str,
    host: str | None = None,
    capacity: int = 1,
    labels: dict[str, Any] | None = None,
) -> WorkerRegistry:
    """Insert or refresh a worker row; rotates ``lease_id`` on every call.

    Re-registration is idempotent on ``worker_id``: the row's ``lease_id``,
    ``status`` and ``last_heartbeat_at`` are reset, and ``in_flight`` is
    cleared because a (re)registering worker has not yet been dispatched
    any task under the new lease.
    """

    _require_system()

    new_lease = str(uuid.uuid4())
    now = _utcnow()

    stmt: Select[Any] = select(WorkerRegistry).where(
        WorkerRegistry.worker_id == worker_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()

    if existing is not None:
        existing.worker_type = worker_type
        existing.host = host
        existing.lease_id = new_lease
        existing.capacity = capacity
        existing.labels = labels
        existing.in_flight = 0
        existing.status = _ALIVE
        existing.last_heartbeat_at = now
        await session.commit()
        await session.refresh(existing)
        return existing

    obj = WorkerRegistry(
        worker_id=worker_id,
        worker_type=worker_type,
        host=host,
        lease_id=new_lease,
        capacity=capacity,
        labels=labels,
        status=_ALIVE,
        last_heartbeat_at=now,
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError:
        # Race against another process registering the same worker_id;
        # rollback and re-issue as an upsert.
        await session.rollback()
        return await register_worker(
            session,
            worker_id=worker_id,
            worker_type=worker_type,
            host=host,
            capacity=capacity,
            labels=labels,
        )
    await session.commit()
    await session.refresh(obj)
    return obj


async def heartbeat(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_id: str,
) -> WorkerRegistry:
    """Refresh the heartbeat timestamp; rejects stale leases."""

    _require_system()

    stmt: Select[Any] = select(WorkerRegistry).where(
        WorkerRegistry.worker_id == worker_id,
    )
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise WorkerNotFoundError(worker_id)
    if obj.lease_id != lease_id:
        raise WorkerLeaseMismatchError(
            f"worker {worker_id!r} lease mismatch (expected {obj.lease_id!r})",
        )
    obj.last_heartbeat_at = _utcnow()
    if obj.status == _DEAD:
        # A worker reviving itself before the reaper got to it -- accept and
        # mark ALIVE again; tasks it lost are already back in PENDING.
        obj.status = _ALIVE
    await session.commit()
    await session.refresh(obj)
    return obj


async def drain_worker(
    session: AsyncSession,
    *,
    worker_id: str,
) -> WorkerRegistry:
    """Mark a worker as DRAINING; the dispatcher stops handing it tasks.

    The worker keeps finishing its in-flight tasks; a separate
    :func:`reap_dead_workers` call clears DEAD rows.  Idempotent.
    """

    _require_system()

    stmt: Select[Any] = select(WorkerRegistry).where(
        WorkerRegistry.worker_id == worker_id,
    )
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise WorkerNotFoundError(worker_id)
    if obj.status != _DEAD:
        obj.status = _DRAINING
        await session.commit()
        await session.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Task dispatch + worker-side transitions
# ---------------------------------------------------------------------------


async def claim_next_task(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_id: str,
    task_type: str | None = None,
) -> Task | None:
    """Atomically pull the next dispatchable task for ``worker_id``.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers never
    pick the same row.  On SQLite the locking clause is silently ignored,
    which is fine for unit tests that do not exercise concurrency.

    Returns ``None`` when no task is available so the caller can back off.
    """

    _require_system()

    # Validate worker before claiming so a stale lease never gets work.
    worker = await _get_worker(session, worker_id)
    if worker.lease_id != lease_id:
        raise WorkerLeaseMismatchError(
            f"worker {worker_id!r} lease mismatch",
        )
    if worker.status != _ALIVE:
        # DRAINING / DEAD workers must not receive new work.
        return None
    if worker.in_flight >= worker.capacity:
        return None

    # Dispatchable tasks are PENDING (fresh) or QUEUED (a previous claim
    # that did not transition to RUNNING for any reason).  We sort by
    # priority (lower = more urgent) then by created_at so older tasks
    # don't starve.
    base_stmt: Select[Any] = (
        select(Task)
        .where(
            or_(Task.status == _PENDING, Task.status == _QUEUED),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if task_type is not None:
        base_stmt = base_stmt.where(Task.task_type == task_type)

    task = (await session.execute(base_stmt)).scalar_one_or_none()
    if task is None:
        return None

    # Move to RUNNING under the same transaction so the row lock guarantees
    # at-most-once dispatch.
    now = _utcnow()
    task.status = _RUNNING
    task.worker_id = worker_id
    task.started_at = now
    if task.attempt == 0:
        task.attempt = 1

    worker.in_flight += 1

    await session.commit()
    await session.refresh(task)
    return task


async def complete_task(
    session: AsyncSession,
    *,
    worker_id: str,
    task_uuid: str,
    result: dict[str, Any] | None = None,
) -> Task:
    """Mark a RUNNING task as SUCCEEDED.

    The worker must own the task (``task.worker_id == worker_id``) and the
    task must be in RUNNING; otherwise :class:`TaskTransitionError`.
    """

    _require_system()

    task = await _get_task(session, task_uuid)
    if task.worker_id != worker_id:
        raise TaskTransitionError(
            f"task {task_uuid} is owned by {task.worker_id!r}, not {worker_id!r}",
        )
    if task.status != _RUNNING:
        raise TaskTransitionError(
            f"task {task_uuid} is in state {task.status!r}; expected RUNNING",
        )
    task.status = _SUCCEEDED
    task.finished_at = _utcnow()
    task.result = result
    task.error_code = None
    task.error_message = None

    await _decrement_in_flight(session, worker_id)
    await session.commit()
    await session.refresh(task)
    return task


async def fail_task(
    session: AsyncSession,
    *,
    worker_id: str,
    task_uuid: str,
    error_code: str,
    error_message: str,
) -> Task:
    """Mark a RUNNING task as FAILED; consumes one ``attempt``.

    The retry policy itself (re-queueing for another attempt) is not run
    here -- the API ``POST /tasks/{uuid}/retry`` already handles that and
    we want to keep the worker-side transition narrow.
    """

    _require_system()

    task = await _get_task(session, task_uuid)
    if task.worker_id != worker_id:
        raise TaskTransitionError(
            f"task {task_uuid} is owned by {task.worker_id!r}, not {worker_id!r}",
        )
    if task.status != _RUNNING:
        raise TaskTransitionError(
            f"task {task_uuid} is in state {task.status!r}; expected RUNNING",
        )
    task.status = _FAILED
    task.finished_at = _utcnow()
    task.error_code = error_code
    task.error_message = error_message

    await _decrement_in_flight(session, worker_id)
    await session.commit()
    await session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Failure recovery
# ---------------------------------------------------------------------------


async def reap_dead_workers(
    session: AsyncSession,
    *,
    timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> tuple[Sequence[str], Sequence[str]]:
    """Mark stale workers DEAD and re-queue their RUNNING tasks.

    Returns ``(reaped_worker_ids, requeued_task_uuids)`` so callers can
    surface metrics.  Safe to run repeatedly; idempotent on workers that
    are already DEAD.
    """

    _require_system()

    cutoff = _utcnow() - timeout

    # 1) Find candidates: ALIVE / DRAINING workers whose last heartbeat
    #    predates the cutoff.  DEAD workers are skipped (already reaped).
    stale_stmt: Select[Any] = select(WorkerRegistry).where(
        and_(
            WorkerRegistry.status != _DEAD,
            WorkerRegistry.last_heartbeat_at < cutoff,
        ),
    )
    stale_workers = (await session.execute(stale_stmt)).scalars().all()
    reaped_ids: list[str] = []
    requeued: list[str] = []

    for worker in stale_workers:
        # 2) Move RUNNING tasks owned by this worker back to PENDING so a
        #    healthy worker can pick them up.  ``attempt`` is left as-is:
        #    consuming it would punish workers that crash through no
        #    fault of their own, and the retry/cancellation policy is the
        #    user's job (not the reaper's).
        running_stmt: Select[Any] = select(Task).where(
            and_(Task.worker_id == worker.worker_id, Task.status == _RUNNING),
        )
        running_tasks = (await session.execute(running_stmt)).scalars().all()
        for task in running_tasks:
            task.status = _PENDING
            task.worker_id = None
            task.started_at = None
            requeued.append(task.task_uuid)

        worker.status = _DEAD
        worker.in_flight = 0
        reaped_ids.append(worker.worker_id)

    if reaped_ids:
        await session.commit()
    return reaped_ids, requeued


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_worker(
    session: AsyncSession,
    worker_id: str,
) -> WorkerRegistry:
    stmt: Select[Any] = select(WorkerRegistry).where(
        WorkerRegistry.worker_id == worker_id,
    )
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise WorkerNotFoundError(worker_id)
    return obj


async def _get_task(session: AsyncSession, task_uuid: str) -> Task:
    stmt: Select[Any] = select(Task).where(Task.task_uuid == task_uuid)
    obj = (await session.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise TaskTransitionError(f"task {task_uuid} not found")
    return obj


async def _decrement_in_flight(session: AsyncSession, worker_id: str) -> None:
    """Atomically decrement ``in_flight`` (clamped at zero)."""

    await session.execute(
        update(WorkerRegistry)
        .where(WorkerRegistry.worker_id == worker_id)
        .values(in_flight=WorkerRegistry.in_flight - 1)
        .execution_options(synchronize_session=False),
    )
