"""VECTORIZE executor: computes vectors for a vectorization_rule.

Why this executor exists
------------------------
:mod:`lcp.services.vectorization_service` lets users create a
``vectorization_rule`` (target column + source columns + model).  This
executor is the async half: it consumes ``VECTORIZE`` tasks the REST
layer (or a future planner) enqueues against that rule and runs the
embedding model against the lance dataset.

Slice 4 / B.6 (beta.2a) scoping
-------------------------------
beta.2a closes the lance read/write loop with **single source column**
support:

* read the named ``source_column`` from the dataset (one projected
  column, no multi-column concat);
* run :func:`lcp.embeddings.hash_embedding` (mock) or
  :func:`lcp.embeddings.st_embedding` (real model) per record batch;
* write the resulting fixed-size vector list back via
  :func:`lcp.data_plane.lance_io.add_columns_from_func` to the
  ``target_column``.

What beta.2a deliberately does NOT do
-------------------------------------
* multi-column concat (``len(rule.source_columns) > 1``).  Concat
  semantics (separator, NULL handling) need a contract before we
  silently pick one; until then we raise ``NotImplementedError`` so
  callers see this is missing rather than getting wrong vectors.
* batch streaming control / progress reporting.  ``add_columns_from_func``
  iterates fragments internally; we do not yet expose per-batch
  progress.  Adequate for the smoke harness (~5 rows); revisit when
  10K+ row datasets land.
* incremental / partial re-embedding.  Today the call rebuilds the
  whole column; lance ``add_columns`` itself is idempotent against a
  fresh column name but the executor does not yet support "embed only
  rows where target_column IS NULL".

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
* Multi-column rule -> ``NotImplementedError`` (deferred to beta.2b).
* Real-model load failure (e.g. unknown model_name, no network)
  surfaces the underlying ``sentence-transformers`` /
  :class:`SentenceTransformersNotInstalledError`.
* Lance not installed -> :class:`LanceNotInstalledError` from
  :mod:`lcp.data_plane.lance_io` -- the worker maps this to FAILED so
  an operator can tell "missing wheel" from "wrong model id" at a
  glance.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
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


def _build_batch_transform(
    *,
    source_column: str,
    target_column: str,
    model_name: str,
    vector_dim: int,
) -> Any:
    """Return a lance ``AddColumnsUDF`` that fills ``target_column``.

    Lance 0.6+ requires :func:`lance.add_columns` callables to be wrapped
    with :func:`lance.batch_udf` (raw callables are interpreted as
    pandas UDFs and crash without pandas installed -- which we
    deliberately do NOT pull into the worker image).  We wrap here so
    ``lance_io.add_columns_from_func`` stays generic.

    The UDF input is a ``pa.RecordBatch`` projected to
    ``[source_column]``; the output is a ``pa.RecordBatch`` carrying
    only the new ``target_column``.  ``output_schema`` is built up
    front so lance can validate the very first batch and we get a
    clear error if the encoder ever returns the wrong dim.

    The pyarrow / lance imports are local so this module loads cleanly
    when the ``[lance]`` extras are absent -- the wrap only happens at
    task-dispatch time inside the worker thread.
    """

    import lance
    import pyarrow as pa

    is_mock = model_name == _MOCK_MODEL_NAME

    # Fixed-size list of float32 is the conventional lance shape for
    # vector indexes (IVF_PQ etc. expect it).  float32 is half the
    # storage of float64 with negligible accuracy loss for retrieval.
    out_type = pa.list_(pa.float32(), vector_dim)
    output_schema = pa.schema([pa.field(target_column, out_type)])

    def _transform(batch: pa.RecordBatch) -> pa.RecordBatch:
        # Pull the source column as Python strings.  ``to_pylist`` is
        # the fastest path for small batches and avoids forcing
        # callers to know about pyarrow types.
        texts = batch.column(source_column).to_pylist()
        # Lance may pass NULLs through; coerce defensively so the
        # encoder never sees ``None``.  Real-model encoders crash on
        # None; mock encoder also requires ``str``.
        materialised = [t if isinstance(t, str) else "" for t in texts]
        if is_mock:
            vectors = hash_embedding(materialised)
        else:
            vectors = st_embedding(materialised, model_name=model_name)
        out_array = pa.array(vectors, type=out_type)
        return pa.RecordBatch.from_arrays(
            [out_array], names=[target_column],
        )

    return lance.batch_udf(output_schema=output_schema)(_transform)


class EmbeddingExecutor(LifecycleExecutor):
    """Compute embeddings for a vectorization_rule.

    Slice 3 wired the call graph with a deterministic mock model.
    Slice 4 / B.5 added the real sentence-transformers backend.
    Slice 4 / B.6 (beta.2a) closes the lance read/write loop so the
    target column is materialised into the on-disk dataset.
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

        source_columns = list(rule.source_columns)
        if len(source_columns) != 1:
            # beta.2a: single-column only.  Multi-column concat needs a
            # separator + NULL-handling contract before we pick one
            # silently.  Tracked as beta.2b.
            raise NotImplementedError(
                "VECTORIZE multi-column concat is deferred to beta.2b; "
                f"got source_columns={source_columns!r}",
            )
        source_column = source_columns[0]

        # Probe the encoder once up front to learn the vector dim --
        # we need it to build ``output_schema`` for the lance UDF
        # wrapper, and surfacing the same value into the payload below
        # keeps the contract self-consistent (one source of truth, no
        # second probe call after the lance loop).
        if rule.model_name == _MOCK_MODEL_NAME:
            probe_vectors = hash_embedding(["__lcp_probe__"])
        else:
            probe_vectors = st_embedding(
                ["__lcp_probe__"], model_name=rule.model_name,
            )
        vector_dim = len(probe_vectors[0]) if probe_vectors else 0

        # Build the per-batch UDF; this does NOT load the model again
        # (sentence-transformers caches per process), nor does it
        # touch lance yet -- the call site below dispatches it on a
        # worker thread.
        transform = _build_batch_transform(
            source_column=source_column,
            target_column=rule.target_column,
            model_name=rule.model_name,
            vector_dim=vector_dim,
        )
        mode = "mock" if rule.model_name == _MOCK_MODEL_NAME else "real-model"

        # Lance is sync; push to a worker thread so the asyncio loop
        # stays responsive (mirrors ttl_delete / index_build).  Any
        # lance / model exception propagates so the worker maps it to
        # FAILED with a clear error_code.
        settings = get_settings()
        storage_options = lance_io.build_storage_options(settings)
        rows_after = await asyncio.to_thread(
            lance_io.add_columns_from_func,
            dataset.storage_uri,
            transforms=transform,
            read_columns=[source_column],
            storage_options=storage_options,
        )

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
                "source_columns": source_columns,
                "batch_size": rule.batch_size,
                "vector_dim": vector_dim,
                "vector_count": int(rows_after),
                "executed_at": now.isoformat(),
            },
        )
