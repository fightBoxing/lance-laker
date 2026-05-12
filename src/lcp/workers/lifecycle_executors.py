"""Pluggable executors that turn a ``task`` row into actual work.

Why an abstraction
------------------
The LCP control plane lives entirely above the lance data plane: every
``dataset`` row is just metadata that *points at* a lance table on object
storage (``storage_uri``).  Until lance is wired in (a separate project
of its own -- ``pylance`` dependency, S3/GCS credentials, real lance
datasets to test against), the worker still needs to be operational so
the rest of the control plane can be exercised end-to-end.

This module solves that by introducing a small ``LifecycleExecutor``
contract; every task type binds to one executor.  Today three concrete
implementations exist:

- :class:`TtlDeleteExecutor` -- stub.  Records the predicate that *would*
  be passed to ``lance.LanceDataset.delete()``.
- :class:`CompactionExecutor` -- stub.  Records the threshold that *would*
  be passed to ``lance.LanceDataset.compact_files()``.
- :class:`IndexOptimizeExecutor` -- partially real.  Walks every
  ``OPTIMIZING`` index that belongs to the task's dataset and flips it
  back to ``READY`` with a fresh ``last_optimized_at`` timestamp.  This
  is real LCP-internal work, not a stub, but it does NOT call lance to
  rebuild the actual ANN index files yet.

Replacing a stub with a lance-backed implementation in the future is a
local change: subclass / instantiate ``LifecycleExecutor`` and register
it in the worker's executor map.  No changes to the worker loop or the
scheduler primitives are required.

Style
-----
Executors are pure ``await`` coroutines that take the SQLAlchemy session
already bound by the worker.  They MUST be idempotent at the row level:
the worker may crash mid-execution and the task will be retried.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Dataset, Index, Task

__all__ = [
    "ExecutorResult",
    "LifecycleExecutor",
    "TtlDeleteExecutor",
    "CompactionExecutor",
    "IndexOptimizeExecutor",
    "build_default_registry",
]


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorResult:
    """Outcome of one ``execute()`` call.

    The worker hands ``payload`` straight to ``scheduler_service.complete_task``
    as the task ``result`` JSON column, so callers can read it back via the
    REST API without touching the worker's logs.
    """

    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class LifecycleExecutor(ABC):
    """One executor per ``task.task_type``.

    Concrete subclasses MUST set :attr:`task_type` to the string the
    scheduler will route on.  Multiple instances of the same task type
    are not currently supported -- the worker keeps a flat ``{type:
    executor}`` map.
    """

    task_type: str = ""

    @abstractmethod
    async def execute(
        self,
        session: AsyncSession,
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        """Run the work.  Return ``ExecutorResult`` on success.

        Raise any exception to signal failure; the worker will translate
        it into ``fail_task`` with the exception class name as the error
        code.
        """


def _utcnow() -> datetime:
    """Return naive UTC -- matches the DDL ``DATETIME(3)`` columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# TTL_DELETE -- stub
# ---------------------------------------------------------------------------


class TtlDeleteExecutor(LifecycleExecutor):
    """Stub TTL deletion.

    A real implementation would:

    1. ``ds = lance.dataset(dataset.storage_uri)``
    2. ``cutoff = utcnow() - timedelta(days=task.params['ttl_days'])``
    3. ``ds.delete(predicate=f"created_at < timestamp '{cutoff.isoformat()}'")``
    4. Return version + row counts in the result.

    We do not have a lance dependency yet; this stub records what *would*
    have been deleted so users / tests can verify the planner-to-task
    pipeline.  See :mod:`lcp.workers.lifecycle_executors` docstring for
    the substitution path.
    """

    task_type = "TTL_DELETE"

    async def execute(
        self,
        session: AsyncSession,  # noqa: ARG002 -- session reserved for future real impl
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        params = task.params or {}
        ttl_days = params.get("ttl_days")
        if ttl_days is None:
            # Defensive: the planner always sets this, but a malformed
            # task that came in via another path must surface clearly.
            raise ValueError("TTL_DELETE task is missing 'ttl_days' in params")

        cutoff = _utcnow().replace(microsecond=0)
        return ExecutorResult(
            payload={
                "executor": "TtlDeleteExecutor",
                "mode": "stub",
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "ttl_days": ttl_days,
                "predicate_preview": (
                    f"created_at < (now - INTERVAL {ttl_days} DAY)"
                ),
                "would_call": "lance.LanceDataset.delete",
                "executed_at": cutoff.isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# COMPACTION -- stub
# ---------------------------------------------------------------------------


class CompactionExecutor(LifecycleExecutor):
    """Stub small-file compaction.

    A real implementation would call ``ds.optimize.compact_files(...)``
    using the threshold dict from ``task.params``.  Today we only echo
    the threshold so operators can confirm the planner picked it up.
    """

    task_type = "COMPACTION"

    async def execute(
        self,
        session: AsyncSession,  # noqa: ARG002 -- session reserved for future real impl
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        params = task.params or {}
        threshold = params.get("threshold") or {}
        return ExecutorResult(
            payload={
                "executor": "CompactionExecutor",
                "mode": "stub",
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "threshold": threshold,
                "would_call": "lance.LanceDataset.optimize.compact_files",
                "executed_at": _utcnow().isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# INDEX_OPTIMIZE -- partially real (LCP-internal state machine)
# ---------------------------------------------------------------------------


class IndexOptimizeExecutor(LifecycleExecutor):
    """Real LCP-internal index-optimize executor.

    Unlike the other two, this one does *real* control-plane work: every
    ``vector_index`` row that belongs to ``dataset`` and is currently in
    ``OPTIMIZING`` state is flipped back to ``READY`` with a fresh
    ``last_optimized_at`` timestamp.  This is the OPTIMIZING -> READY
    transition that the state-machine docstring in
    :mod:`lcp.services.index_service` promises but does not yet expose
    as a service function.

    What is NOT done here (yet)
    ---------------------------
    - We do not call lance to rebuild the on-disk ANN index.  That is
      the lance integration that lives outside the LCP control plane.
    - We do not move READY -> OPTIMIZING here; the user-facing
      ``index_service.optimize_index`` already does that.

    NOTE for future contributors: when ``index_service`` grows a proper
    ``finish_optimize_index`` function, replace the inline UPDATE below
    with a call to it.  Today the inline UPDATE is the smaller, more
    surgical change.
    """

    task_type = "INDEX_OPTIMIZE"

    async def execute(
        self,
        session: AsyncSession,
        *,
        task: Task,  # noqa: ARG002 -- params not consumed yet
        dataset: Dataset,
    ) -> ExecutorResult:
        stmt = (
            select(Index)
            .where(Index.dataset_uuid == dataset.dataset_uuid)
            .where(Index.status == "OPTIMIZING")
        )
        rows = list((await session.execute(stmt)).scalars().all())

        now = _utcnow()
        for index in rows:
            index.status = "READY"
            index.last_optimized_at = now

        # Caller (worker) commits; this executor stays inside the worker
        # transaction so a crash before commit re-runs cleanly.
        return ExecutorResult(
            payload={
                "executor": "IndexOptimizeExecutor",
                "mode": "real",
                "dataset_uuid": dataset.dataset_uuid,
                "indexes_promoted": [r.index_name for r in rows],
                "promoted_count": len(rows),
                "executed_at": now.isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def build_default_registry() -> dict[str, LifecycleExecutor]:
    """Return the executor map the production worker uses.

    Tests can build a custom registry to inject failing executors for
    error-path coverage.
    """

    return {
        TtlDeleteExecutor.task_type: TtlDeleteExecutor(),
        CompactionExecutor.task_type: CompactionExecutor(),
        IndexOptimizeExecutor.task_type: IndexOptimizeExecutor(),
    }
