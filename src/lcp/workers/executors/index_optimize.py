"""INDEX_OPTIMIZE executor: rebuilds delta indices, then promotes rows.

This executor was already partially real before the lance integration:
it walked every ``OPTIMIZING`` index for the dataset and flipped it back
to ``READY`` with a fresh ``last_optimized_at`` stamp.  That half is
control-plane bookkeeping and stays unchanged.

What is new
-----------
Before flipping the rows, we now (when lance is configured) actually
call :func:`lcp.data_plane.lance_io.optimize_indices` so the on-disk
delta indices are rebuilt.  In stub mode the executor behaves exactly
like before -- no lance side effect, only the LCP state-machine work.

Why we still flip rows in stub mode
-----------------------------------
The state-machine transition is part of the LCP contract documented in
``index_service.optimize_index`` (READY -> OPTIMIZING -> READY).  Tests
and operators expect the row to come back to READY after the worker
runs, regardless of whether real lance work happened.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Index, Task
from lcp.workers.executors._gravitino_push import push_index_properties
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)


class IndexOptimizeExecutor(LifecycleExecutor):
    """Rebuild delta indices, then promote ``OPTIMIZING`` rows to ``READY``."""

    task_type = "INDEX_OPTIMIZE"

    async def execute(
        self,
        session: AsyncSession,
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        settings = get_settings()
        # Real lance work first: if it raises, we let the worker catch
        # it and rollback before any row state changed.  This keeps the
        # invariant "OPTIMIZING -> READY only when lance succeeded".
        lance_index_count: int | None = None
        post_optimize_version: int | None = None
        if settings.lance_storage_endpoint:
            storage_options = lance_io.build_storage_options(settings)
            lance_index_count, post_optimize_version = await asyncio.to_thread(
                lance_io.optimize_indices,
                dataset.storage_uri,
                storage_options=storage_options,
            )

        # LCP-internal state-machine work: every OPTIMIZING index for
        # this dataset moves back to READY with a fresh stamp.
        stmt = (
            select(Index)
            .where(Index.dataset_uuid == dataset.dataset_uuid)
            .where(Index.status == "OPTIMIZING")
        )
        rows = list((await session.execute(stmt)).scalars().all())

        # Watcher integration: pin ``last_seen_version`` to the dataset
        # version that exists *after* lance committed the optimize, not
        # to the version observed when the watcher fired.  Without this,
        # every successful optimize bumps ``latest_version`` by one and
        # the next watcher pass sees ``drift >= 1`` again, triggering a
        # tight optimize-then-trigger loop on idle datasets.
        #
        # Fallback chain:
        #   1. real lance run -> ``post_optimize_version`` (preferred)
        #   2. watcher-emitted task with no real lance run (stub mode)
        #      -> ``task.params['lance_version']``
        #   3. neither -> leave the field untouched
        seen_version: int | None = post_optimize_version
        if seen_version is None:
            seen_version = _extract_lance_version(task)

        now = utcnow_naive()
        for index in rows:
            index.status = "READY"
            index.last_optimized_at = now
            if seen_version is not None:
                index.last_seen_version = seen_version

        # Best-effort Gravitino property mirror (Step 6): one property
        # push per promoted index.  We do these sequentially -- typical
        # OPTIMIZE batches are 1-3 indices and the cost vs network
        # round-tripping cost from a fan-out is not worth the extra
        # complexity (asyncio.gather + error aggregation).  If a future
        # workload promotes 50 indices at once, revisit.
        property_push_count = 0
        for index in rows:
            pushed = await push_index_properties(
                settings=settings,
                schema=dataset.db_schema,
                table=dataset.table_name,
                index_name=index.index_name,
                state="READY",
                column=index.column_name,
                last_optimized_at=now,
            )
            if pushed:
                property_push_count += 1

        # Caller (worker) commits; this executor stays inside the worker
        # transaction so a crash before commit re-runs cleanly.
        # ``mode`` is always "real": even without lance configured, the
        # OPTIMIZING -> READY state-machine transition IS real LCP work.
        # The optional ``lance_index_count`` key signals whether on-disk
        # index files were also rebuilt.
        payload = {
            "executor": "IndexOptimizeExecutor",
            "mode": "real",
            "dataset_uuid": dataset.dataset_uuid,
            "indexes_promoted": [r.index_name for r in rows],
            "promoted_count": len(rows),
            "executed_at": now.isoformat(),
            "gravitino_property_pushed_count": property_push_count,
        }
        if lance_index_count is not None:
            # Only present when lance was actually called; tests can
            # switch on this key to detect the real-lance code path.
            payload["lance_index_count"] = lance_index_count
        if post_optimize_version is not None:
            # Surface the version we wrote to ``last_seen_version`` so
            # operators can correlate watcher logs with executor runs.
            payload["post_optimize_version"] = post_optimize_version
        return ExecutorResult(payload=payload)


def _extract_lance_version(task: Task) -> int | None:
    """Best-effort extract ``lance_version`` from ``task.params``.

    Returns ``None`` when the params are absent / malformed / non-numeric.
    Watcher-emitted tasks always include this; planner / API tasks omit
    it and the executor leaves ``last_seen_version`` untouched.
    """

    params = task.params or {}
    value = params.get("lance_version")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
