"""TTL_DELETE executor: prunes rows older than a TTL window.

Behaviour
---------
- When :class:`Settings` carries no ``lance_storage_endpoint`` (the
  unit-test default), the executor stays in **stub** mode and returns
  the same payload shape the original stub did.  This keeps every
  existing unit test green without touching them.
- When an endpoint is configured, the executor calls
  :func:`lcp.data_plane.lance_io.delete_rows` with a ``created_at <
  cutoff`` predicate and includes the post-delete row count in the
  payload.

The ``ttl_days`` parameter is sourced from ``task.params``; the planner
guarantees it is set, but the executor still validates so a malformed
task surfaces a clear ``ValueError`` instead of a stack trace deep
inside lance.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Task
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)


class TtlDeleteExecutor(LifecycleExecutor):
    """Delete rows older than ``ttl_days`` from the dataset's lance table."""

    task_type = "TTL_DELETE"

    async def execute(
        self,
        session: AsyncSession,  # noqa: ARG002 -- session reserved for future audit writes
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

        now = utcnow_naive().replace(microsecond=0)
        cutoff = now - timedelta(days=int(ttl_days))
        # Use a SQL-friendly literal: lance accepts ANSI-style timestamp
        # literals via DataFusion.  The planner-side preview keeps the
        # human-readable INTERVAL form for the audit trail.
        predicate = f"created_at < timestamp '{cutoff.isoformat()}'"
        predicate_preview = f"created_at < (now - INTERVAL {ttl_days} DAY)"

        settings = get_settings()
        # Stub mode preserves the legacy payload shape so old tests pass
        # unchanged; real mode adds row counts.
        if not settings.lance_storage_endpoint:
            return ExecutorResult(
                payload={
                    "executor": "TtlDeleteExecutor",
                    "mode": "stub",
                    "dataset_uuid": dataset.dataset_uuid,
                    "storage_uri": dataset.storage_uri,
                    "ttl_days": ttl_days,
                    "predicate_preview": predicate_preview,
                    "would_call": "lance.LanceDataset.delete",
                    "executed_at": now.isoformat(),
                },
            )

        # Real mode: lance is sync, so push it onto a worker thread to
        # keep the asyncio event loop responsive.
        storage_options = lance_io.build_storage_options(settings)
        rows_after = await asyncio.to_thread(
            lance_io.delete_rows,
            dataset.storage_uri,
            predicate,
            storage_options=storage_options,
        )

        return ExecutorResult(
            payload={
                "executor": "TtlDeleteExecutor",
                "mode": "real",
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "ttl_days": ttl_days,
                "predicate": predicate,
                "predicate_preview": predicate_preview,
                "rows_after": rows_after,
                "executed_at": now.isoformat(),
            },
        )
