"""INDEX_BUILD executor: builds an ANN index on a dataset column.

Why this executor exists
------------------------
:func:`lcp.services.index_service.create_index` only inserts the
``vector_index`` row in ``BUILDING`` state; it does not itself issue the
expensive lance ``create_index`` call (that can take minutes on large
datasets and must not block an HTTP request).  This executor is the
async half of that workflow: it runs lance's ``create_index`` against
the on-disk lance table and flips the row ``BUILDING -> READY``.

Task params contract
--------------------
The planner / service layer puts the following keys into ``task.params``:

- ``index_name`` (required)  -- identifies which ``vector_index`` row to promote
- ``column_name`` (required) -- column to index; must exist in the lance schema
- ``index_type`` (required)  -- lance vocabulary (``IVF_PQ``, ``IVF_HNSW_PQ``, ...)
- ``params`` (optional)      -- forwarded verbatim as ``**kwargs`` to lance

Missing required keys raise ``ValueError`` so the worker surfaces a
clear FAILED cause instead of a deep lance stack trace.

Stub vs real
------------
Mirrors the sibling executors: when ``Settings.lance_storage_endpoint``
is empty we return a stub payload (unit tests never touch lance);
otherwise we call :func:`lcp.data_plane.lance_io.create_index` on a
worker thread and include its descriptor in the payload.

Scoping (Block B deliberate choice)
-----------------------------------
Unlike :class:`IndexOptimizeExecutor`, which scans EVERY ``OPTIMIZING``
row for the dataset, this executor promotes ONLY the one index named in
``task.params``.  Two reasons:

1. ``create_index`` is per-column; a dataset-wide scan would be the
   wrong semantics.
2. Keeping scope to a single row makes concurrent INDEX_BUILD tasks on
   the same dataset safe without any new locking.

If the named index row is missing (e.g. someone dropped it between
submit and execute), we fail loudly -- silently no-op would hide real
bugs.
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

_REQUIRED_PARAMS: tuple[str, ...] = ("index_name", "column_name", "index_type")


class IndexBuildExecutor(LifecycleExecutor):
    """Build an ANN index, then promote the ``BUILDING`` row to ``READY``."""

    task_type = "INDEX_BUILD"

    async def execute(
        self,
        session: AsyncSession,
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        params = task.params or {}
        missing = [k for k in _REQUIRED_PARAMS if not params.get(k)]
        if missing:
            raise ValueError(
                f"INDEX_BUILD task is missing required params: {missing}",
            )

        index_name: str = params["index_name"]
        column_name: str = params["column_name"]
        index_type: str = params["index_type"]
        # ``index_params`` is the nested dict forwarded to lance; absent ->
        # lance picks its defaults.  We never inject a dict of our own so
        # operators keep full control.
        index_params: dict[str, object] = params.get("params") or {}

        # Look up the target row up-front: we want a clear "index row gone"
        # error BEFORE we burn minutes on the lance build.
        stmt = select(Index).where(
            Index.dataset_uuid == dataset.dataset_uuid,
            Index.index_name == index_name,
        )
        index_row = (await session.execute(stmt)).scalar_one_or_none()
        if index_row is None:
            raise ValueError(
                f"INDEX_BUILD target index not found: "
                f"{dataset.dataset_uuid}/{index_name}",
            )

        now = utcnow_naive()
        settings = get_settings()

        # Stub mode: keep every unit test green without installing pylance.
        # We still flip BUILDING -> READY because the LCP state-machine is
        # real work regardless of whether lance ran; tests and operators
        # assume the row progresses.
        if not settings.lance_storage_endpoint:
            index_row.status = "READY"
            index_row.last_optimized_at = now
            # Best-effort Gravitino mirror; never fails the task (Step 6).
            # Stub-mode datasets still benefit -- it lets integration tests
            # exercise the property-push wiring without lance configured.
            pushed = await push_index_properties(
                settings=settings,
                schema=dataset.db_schema,
                table=dataset.table_name,
                index_name=index_name,
                state="READY",
                column=column_name,
                last_optimized_at=now,
            )
            return ExecutorResult(
                payload={
                    "executor": "IndexBuildExecutor",
                    "mode": "stub",
                    "dataset_uuid": dataset.dataset_uuid,
                    "storage_uri": dataset.storage_uri,
                    "index_name": index_name,
                    "column_name": column_name,
                    "index_type": index_type,
                    "index_params": index_params,
                    "would_call": "lance.LanceDataset.create_index",
                    "executed_at": now.isoformat(),
                    "gravitino_property_pushed": pushed,
                },
            )

        # Real mode: push the sync lance call onto a worker thread.  If it
        # raises, we let the exception propagate; the worker's error path
        # rolls back the session (so status stays BUILDING) and marks the
        # task FAILED.  Invariant: BUILDING -> READY only when lance
        # succeeded.
        storage_options = lance_io.build_storage_options(settings)
        descriptor = await asyncio.to_thread(
            lance_io.create_index,
            dataset.storage_uri,
            column=column_name,
            index_type=index_type,
            storage_options=storage_options,
            replace=True,
            **index_params,
        )

        # State-machine bookkeeping is inside the worker's transaction; a
        # crash after lance commit but before session.commit will replay:
        # ``replace=True`` above makes the retry idempotent on the lance side.
        index_row.status = "READY"
        index_row.last_optimized_at = now

        # Best-effort Gravitino property mirror (Step 6).  If this throws
        # despite the helper swallowing GravitinoError -- e.g. an unexpected
        # error type -- we still let the executor's exception path roll
        # back the lance-side state-machine.  But the helper is documented
        # to never raise; this is defence-in-depth only.
        pushed = await push_index_properties(
            settings=settings,
            schema=dataset.db_schema,
            table=dataset.table_name,
            index_name=index_name,
            state="READY",
            column=column_name,
            last_optimized_at=now,
        )

        return ExecutorResult(
            payload={
                "executor": "IndexBuildExecutor",
                "mode": "real",
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "index_name": index_name,
                "column_name": column_name,
                "index_type": index_type,
                "index_params": index_params,
                "lance_descriptor": descriptor,
                "executed_at": now.isoformat(),
                "gravitino_property_pushed": pushed,
            },
        )
