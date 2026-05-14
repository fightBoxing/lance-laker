"""EMBEDDING executor: computes vectors for a vectorization_rule.

Why this executor exists
------------------------
:mod:`lcp.services.vectorization_service` lets users create a
``vectorization_rule`` (target column + source columns + model).  This
executor is the async half: it consumes ``EMBEDDING`` tasks the REST
layer (or a future planner) enqueues against that rule and runs the
embedding model.

Slice 3 scoping
---------------
Slice 3 ships the **mock model** + **call graph** only:

* model is :func:`lcp.embeddings.hash_embedding`, a deterministic
  hash-based vector generator (no torch / no model server);
* the executor records what it would write to lance in the task
  payload; it does NOT yet stream batches from lance and call
  :func:`lcp.data_plane.lance_io.add_columns_from_func` because that
  needs (a) a real source dataset with rows and (b) a real model
  client whose batch-shape we can pin.  Both are deliberate next-slice
  work; opening the lance write loop on top of mock data would only
  pretend to test something we haven't built yet.

Task params contract
--------------------
* ``vectorization_rule_id`` (required, int) -- primary-key of the
  ``vectorization_rule`` row to consume.  Mirrors how IndexBuildExecutor
  uses ``index_name`` as a single anchor and pulls the rest of the
  config from the DB row (Karpathy rule 3: don't duplicate state).

Anything else (model_name, source_columns, target_column, batch_size,
...) is read from the looked-up :class:`VectorizationRule`.

Failure modes
-------------
* Missing param  -> ``ValueError``  -> task FAILED with clear cause.
* Rule not found -> ``ValueError``  -> ditto (the rule could have been
  deleted between submit and execute; failing loud avoids silent
  no-ops).
* Disabled rule  -> ``ValueError``  -> matches the REST layer's
  expectation that ``enabled=False`` rules never run.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.db.models import Dataset, Task, VectorizationRule
from lcp.embeddings import hash_embedding
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)

# Sample texts the mock model hashes so the payload reports vector
# shape on the same input regardless of underlying lance state.  Keeps
# slice 3 deterministic; replaced by real per-row data once the lance
# read loop lands.
_PROBE_TEXTS: tuple[str, ...] = ("__lcp_embedding_probe__",)


class EmbeddingExecutor(LifecycleExecutor):
    """Compute embeddings for a vectorization_rule (mock-model slice)."""

    task_type = "EMBEDDING"

    async def execute(
        self,
        session: AsyncSession,
        *,
        task: Task,
        dataset: Dataset,
    ) -> ExecutorResult:
        params = task.params or {}
        rule_id = params.get("vectorization_rule_id")
        if rule_id is None:
            raise ValueError(
                "EMBEDDING task is missing 'vectorization_rule_id' in params",
            )

        # Single anchor; pull the rest from the rule row.  Same pattern
        # IndexBuildExecutor uses with index_name.
        stmt = select(VectorizationRule).where(
            VectorizationRule.id == int(rule_id),
            VectorizationRule.dataset_uuid == dataset.dataset_uuid,
        )
        rule = (await session.execute(stmt)).scalar_one_or_none()
        if rule is None:
            raise ValueError(
                f"EMBEDDING target vectorization_rule not found: "
                f"id={rule_id} dataset={dataset.dataset_uuid}",
            )
        if not rule.enabled:
            # Defensive: REST layer should already have rejected, but
            # an old enqueued task could outlive a disable.
            raise ValueError(
                f"EMBEDDING vectorization_rule is disabled: id={rule_id}",
            )

        # Compute a probe vector so the payload reports the model's
        # output shape on a fixed input.  This is the slice 3 stand-in
        # for "we ran the model on N rows": same call shape, just on a
        # canned input list.
        vectors = hash_embedding(_PROBE_TEXTS)
        vector_dim = len(vectors[0]) if vectors else 0

        now = utcnow_naive()
        settings = get_settings()
        # Real lance write-back lands in a follow-up slice; until we have
        # a source dataset with real rows AND a real model client we
        # would only be writing canned vectors, which is worse than not
        # writing anything (it would mask read-loop bugs later).
        mode = "real" if settings.lance_storage_endpoint else "stub"

        return ExecutorResult(
            payload={
                "executor": "EmbeddingExecutor",
                "mode": mode,
                "dataset_uuid": dataset.dataset_uuid,
                "storage_uri": dataset.storage_uri,
                "vectorization_rule_id": int(rule_id),
                "model_name": rule.model_name,
                "model_version": rule.model_version,
                "target_column": rule.target_column,
                "source_columns": list(rule.source_columns),
                "batch_size": rule.batch_size,
                "vector_dim": vector_dim,
                "would_call": "lance_io.add_columns_from_func",
                "executed_at": now.isoformat(),
            },
        )
