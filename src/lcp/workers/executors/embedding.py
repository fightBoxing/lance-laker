"""VECTORIZE executor: computes vectors for a vectorization_rule.

Why this executor exists
------------------------
:mod:`lcp.services.vectorization_service` lets users create a
``vectorization_rule`` (target column + source columns + model).  This
executor is the async half: it consumes ``VECTORIZE`` tasks the REST
layer (or a future planner) enqueues against that rule and runs the
embedding model.

Slice 4 scoping
---------------
Slice 4 adds the **real model client** path on top of slice-3's mock:

* the mock path (:func:`lcp.embeddings.hash_embedding`) still runs when
  ``rule.model_name`` is the literal ``"mock"`` -- keeps unit tests
  and operator probes free of model downloads;
* any other ``model_name`` is forwarded to
  :func:`lcp.embeddings.st_embedding`, which loads the named model from
  the Hugging Face hub the first time and caches it process-wide.

What slice 4 still does NOT do (deliberate -- next slice):

* read source rows from the lance dataset.  We still feed a fixed
  probe list so the payload reports vector shape without needing a
  populated lance table.  Once the lance read loop lands, the probe
  list is replaced with a per-batch ``read_columns`` projection.
* call :func:`lcp.data_plane.lance_io.add_columns_from_func` to write
  the new column back; same reason -- pretending to write a fixed
  vector for every row would mask read-loop bugs later.

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
* Real-model load failure (e.g. unknown model_name, no network)
  surfaces the underlying ``sentence-transformers`` /
  :class:`SentenceTransformersNotInstalledError` -- the worker maps
  this to FAILED with the exception class as the error code so an
  operator can tell "wrong model id" from "wheel missing" at a glance.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Dataset, Task, VectorizationRule
from lcp.embeddings import hash_embedding, st_embedding
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)

# Reserved ``model_name`` value that bypasses the real-model path and
# uses the deterministic hash mock instead.  Kept as a literal (not an
# enum) because the value is also a wire-level identifier stored in the
# ``vectorization_rule.model_name`` column.
_MOCK_MODEL_NAME: str = "mock"

# Probe text the executor encodes so the payload reports vector shape on
# the same input regardless of underlying lance state.  Replaced by
# per-batch ``read_columns`` projection once the lance read loop lands.
_PROBE_TEXTS: tuple[str, ...] = ("__lcp_embedding_probe__",)


class EmbeddingExecutor(LifecycleExecutor):
    """Compute embeddings for a vectorization_rule.

    Slice 3 wired the call graph with a deterministic mock model.
    Slice 4 routes ``rule.model_name`` to either that mock or to a real
    sentence-transformers model so the rest of the pipeline (REST,
    task queue, worker, payload contract) keeps working unchanged.
    """

    task_type = "VECTORIZE"

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
                "VECTORIZE task is missing 'vectorization_rule_id' in params",
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
                f"VECTORIZE target vectorization_rule not found: "
                f"id={rule_id} dataset={dataset.dataset_uuid}",
            )
        if not rule.enabled:
            # Defensive: REST layer should already have rejected, but
            # an old enqueued task could outlive a disable.
            raise ValueError(
                f"VECTORIZE vectorization_rule is disabled: id={rule_id}",
            )

        # Route by model_name.  The mock path stays for unit tests and
        # operator probes; everything else hits the real client.  Any
        # exception from st_embedding (unknown model, missing wheel,
        # network failure) propagates so the worker maps it to a
        # FAILED task with a clear error_code.
        if rule.model_name == _MOCK_MODEL_NAME:
            mode = "mock"
            vectors = hash_embedding(_PROBE_TEXTS)
        else:
            mode = "real-model"
            vectors = st_embedding(
                _PROBE_TEXTS, model_name=rule.model_name,
            )
        vector_dim = len(vectors[0]) if vectors else 0

        now = utcnow_naive()

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
