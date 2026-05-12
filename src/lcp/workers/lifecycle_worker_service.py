"""Lifecycle worker: bridge between scheduler primitives and executors.

The single entry point for one work iteration is :func:`run_iteration`.
It performs:

1. Claim one task (RLS bypass via :func:`with_system_context` is the
   caller's job; the worker raises if the principal is not system).
2. Look up the parent dataset (workers always need it; failing this
   counts as a permanent task failure -- the dataset was deleted).
3. Dispatch to the executor registered for ``task.task_type``.
4. Complete the task with the executor's result, or fail it with the
   exception class name + message.

Why a separate "service" instead of putting this in the CLI module
------------------------------------------------------------------
- Keeps the CLI thin: ``lcp.workers.lifecycle_worker`` only owns the
  asyncio main loop, signal handling, and engine bootstrap.
- Lets unit / integration tests drive the worker one tick at a time,
  without spawning a daemon.

Concurrency
-----------
The current worker pulls tasks one at a time (``capacity=1`` per
``register_worker`` default).  That is intentional for the first
iteration: it removes any "in-flight bookkeeping" concerns from the
critical path while the executors themselves are stubs.  When real
lance executors land, ``capacity`` can be lifted by changing one
parameter -- :func:`scheduler_service.claim_next_task` already enforces
``in_flight < capacity`` atomically.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Dataset, Task
from lcp.services import scheduler_service
from lcp.workers.lifecycle_executors import (
    LifecycleExecutor,
    build_default_registry,
)

__all__ = [
    "TickOutcome",
    "run_iteration",
    "WorkerConfig",
]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerConfig:
    """Static config the CLI passes through to :func:`run_iteration`.

    ``task_type`` is None for a generic worker; in production we run
    one process per task type so a slow TTL_DELETE never blocks
    INDEX_OPTIMIZE.
    """

    worker_id: str
    lease_id: str
    task_type: str | None = None


@dataclass(frozen=True)
class TickOutcome:
    """Return value of one iteration -- helps tests assert without re-querying."""

    claimed: bool
    task_uuid: str | None
    task_type: str | None
    final_status: str | None  # "SUCCEEDED" | "FAILED" | None (no claim)
    error: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_iteration(
    session: AsyncSession,
    *,
    config: WorkerConfig,
    registry: dict[str, LifecycleExecutor] | None = None,
) -> TickOutcome:
    """Run one claim/execute/complete cycle.

    Returns ``TickOutcome(claimed=False, ...)`` when no task was
    available so the CLI loop knows to back off.

    Any uncaught exception inside the executor is captured and turned
    into ``fail_task``; this method itself does not propagate executor
    errors back to the caller, only infrastructure errors (DB unavailable,
    etc.) propagate.
    """

    executors = registry if registry is not None else build_default_registry()

    claimed = await scheduler_service.claim_next_task(
        session,
        worker_id=config.worker_id,
        lease_id=config.lease_id,
        task_type=config.task_type,
    )
    if claimed is None:
        return TickOutcome(
            claimed=False, task_uuid=None,
            task_type=None, final_status=None,
        )

    return await _dispatch_and_finalize(
        session,
        worker_id=config.worker_id,
        task=claimed,
        executors=executors,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _dispatch_and_finalize(
    session: AsyncSession,
    *,
    worker_id: str,
    task: Task,
    executors: dict[str, LifecycleExecutor],
) -> TickOutcome:
    """Dispatch one already-claimed task; always finalize via scheduler."""

    # Resolve dataset (worker side: dataset_service is tenant-scoped, but
    # the worker is in system context so we hit the table directly).
    stmt = select(Dataset).where(Dataset.dataset_uuid == task.dataset_uuid)
    dataset = (await session.execute(stmt)).scalar_one_or_none()
    if dataset is None:
        # The parent dataset disappeared between planner emission and
        # worker dispatch.  This is permanent (no retry will help), but
        # we still go through fail_task to keep state-machine invariants.
        await scheduler_service.fail_task(
            session,
            worker_id=worker_id,
            task_uuid=task.task_uuid,
            error_code="DatasetMissing",
            error_message=(
                f"dataset {task.dataset_uuid} no longer exists; "
                "task is orphaned"
            ),
        )
        return TickOutcome(
            claimed=True,
            task_uuid=task.task_uuid,
            task_type=task.task_type,
            final_status="FAILED",
            error="DatasetMissing",
        )

    executor = executors.get(task.task_type)
    if executor is None:
        # Unknown task type for this worker.  Fail it so it does not
        # spin.  An operator can wire a new executor and POST /retry.
        await scheduler_service.fail_task(
            session,
            worker_id=worker_id,
            task_uuid=task.task_uuid,
            error_code="UnknownTaskType",
            error_message=f"no executor registered for {task.task_type!r}",
        )
        return TickOutcome(
            claimed=True,
            task_uuid=task.task_uuid,
            task_type=task.task_type,
            final_status="FAILED",
            error="UnknownTaskType",
        )

    try:
        result = await executor.execute(session, task=task, dataset=dataset)
    except Exception as exc:  # noqa: BLE001 -- we deliberately catch all
        # Snapshot the identifiers BEFORE rollback: rollback expires every
        # attribute on the ORM-attached ``task`` instance, after which any
        # ``task.task_uuid`` access triggers a lazy SELECT -- and on
        # aiosqlite that lazy SELECT crashes with ``MissingGreenlet``
        # because we are no longer in the original greenlet context.
        task_uuid = task.task_uuid
        task_type = task.task_type
        # Roll back any partial executor mutations so fail_task starts on
        # a clean slate.  Rollback errors are swallowed: their failure
        # must never mask the original executor error.
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 -- defensive, see comment above
            pass
        await scheduler_service.fail_task(
            session,
            worker_id=worker_id,
            task_uuid=task_uuid,
            error_code=type(exc).__name__,
            error_message=str(exc)[:2048],
        )
        return TickOutcome(
            claimed=True,
            task_uuid=task_uuid,
            task_type=task_type,
            final_status="FAILED",
            error=type(exc).__name__,
        )

    await scheduler_service.complete_task(
        session,
        worker_id=worker_id,
        task_uuid=task.task_uuid,
        result=result.payload,
    )
    return TickOutcome(
        claimed=True,
        task_uuid=task.task_uuid,
        task_type=task.task_type,
        final_status="SUCCEEDED",
    )
