"""COMPACTION executor: merges small lance fragments into bigger ones.

Behaviour mirrors :mod:`ttl_delete`: stub fallback when no endpoint is
configured, real ``lance_io.compact_files`` call otherwise.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Task
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)

# Keys we accept inside ``task.params['threshold']``.  Anything else is
# kept in the payload (audit) but NOT forwarded to lance, so a future
# threshold key does not silently break the worker.
_LANCE_COMPACT_KWARGS: frozenset[str] = frozenset({
    "target_rows_per_fragment",
    "materialize_deletions",
})


class CompactionExecutor(LifecycleExecutor):
    """Run ``ds.optimize.compact_files`` against the dataset's lance table."""

    task_type = "COMPACTION"

    async def execute(
        self,
        session: AsyncSession,  # noqa: ARG002 -- session reserved for future audit writes
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        params = task.params or {}
        threshold: dict[str, Any] = params.get("threshold") or {}
        now = utcnow_naive()

        settings = get_settings()
        if not settings.lance_storage_endpoint:
            return ExecutorResult(
                payload={
                    "executor": "CompactionExecutor",
                    "mode": "stub",
                    "dataset_uuid": dataset.dataset_uuid,
                    "storage_uri": dataset.storage_uri,
                    "threshold": threshold,
                    "would_call": "lance.LanceDataset.optimize.compact_files",
                    "executed_at": now.isoformat(),
                },
            )

        # Filter threshold dict to only the keys lance_io.compact_files
        # actually accepts; ignored keys are still surfaced in the
        # payload so operators can see what the planner sent.
        compact_kwargs = {
            k: v for k, v in threshold.items() if k in _LANCE_COMPACT_KWARGS
        }
        ignored_keys = sorted(set(threshold) - _LANCE_COMPACT_KWARGS)

        storage_options = lance_io.build_storage_options(settings)
        stats = await asyncio.to_thread(
            lance_io.compact_files,
            dataset.storage_uri,
            storage_options=storage_options,
            **compact_kwargs,
        )

        return ExecutorResult(
            payload={
                "executor": "CompactionExecutor",
                "mode": "real",
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "threshold": threshold,
                "ignored_threshold_keys": ignored_keys,
                "stats": {
                    "fragments_removed": stats.fragments_removed,
                    "fragments_added": stats.fragments_added,
                    "files_removed": stats.files_removed,
                    "files_added": stats.files_added,
                },
                "executed_at": now.isoformat(),
            },
        )
