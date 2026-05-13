"""Executor base classes and the default registry factory.

Why this is its own module
--------------------------
Splitting ``lifecycle_executors.py`` into one-module-per-executor lets
new executors (e.g. ``IndexBuildExecutor``) land without re-touching the
file every other executor lives in.  The base contract belongs here so
each concrete executor only imports the abstraction, not its siblings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Dataset, Task

__all__ = [
    "ExecutorResult",
    "LifecycleExecutor",
    "build_default_registry",
    "utcnow_naive",
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


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def utcnow_naive() -> datetime:
    """Return naive UTC -- matches the DDL ``DATETIME(3)`` columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def build_default_registry() -> dict[str, LifecycleExecutor]:
    """Return the executor map the production worker uses.

    Tests can build a custom registry to inject failing executors for
    error-path coverage.

    The import is local to avoid a circular dependency: each concrete
    executor imports :class:`LifecycleExecutor` from this module.
    """

    # Local import: each concrete executor lives in its own module and
    # itself imports from this base module.  Importing them at module
    # top level would create an import cycle.
    from lcp.workers.executors.compaction import CompactionExecutor
    from lcp.workers.executors.index_optimize import IndexOptimizeExecutor
    from lcp.workers.executors.ttl_delete import TtlDeleteExecutor

    return {
        TtlDeleteExecutor.task_type: TtlDeleteExecutor(),
        CompactionExecutor.task_type: CompactionExecutor(),
        IndexOptimizeExecutor.task_type: IndexOptimizeExecutor(),
    }
