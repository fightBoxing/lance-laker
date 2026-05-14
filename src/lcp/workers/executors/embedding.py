"""VECTORIZE executor: computes vectors for a vectorization_rule.

Why this executor exists
------------------------
:mod:`lcp.services.vectorization_service` lets users create a
``vectorization_rule`` (target column + source columns + model).  This
executor is the async half: it consumes ``VECTORIZE`` tasks the REST
layer (or a future planner) enqueues against that rule and runs the
embedding model against the lance dataset.

Slice 4 / B.6 (beta.2a -> beta.2b) scoping
------------------------------------------
beta.2a closed the lance read/write loop with **single source column**
support.  beta.2b extends that to **multi-column concat**:

* read every ``source_column`` from the dataset in the order the rule
  declares them;
* concat the projected fields per row with a configurable separator,
  skipping NULL fields (so ``title=None, body="hi"`` becomes ``"hi"``,
  not ``" hi"``); the all-NULL row degrades to ``""`` exactly like the
  single-column NULL case;
* run :func:`lcp.embeddings.hash_embedding` (mock) or
  :func:`lcp.embeddings.st_embedding` (real model) per record batch;
* write the resulting fixed-size vector list back via
  :func:`lcp.data_plane.lance_io.add_columns_from_func` to the
  ``target_column``.

The separator defaults to a single space and can be overridden per-rule
via ``rule.extra["concat_separator"]``; we deliberately do NOT add a
first-class column on ``vectorization_rule`` for it -- the JSON ``extra``
bag is the existing escape hatch and graduating the field is reversible
when a real consumer asks for it.

Schema validation
-----------------
Before the lance loop starts we read the dataset schema once and assert
*every* ``source_column`` exists and is utf8-string.  This trades one
cheap roundtrip for a *clear* business error (FAILED with
``ValueError``) instead of letting lance crash mid-batch with a less
actionable message.

What this executor deliberately does NOT do
-------------------------------------------
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
* Source column missing or non-string  -> ``ValueError`` (raised by
  the schema validator before any model probe / lance write).
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
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Index, Task, VectorizationRule
from lcp.embeddings import hash_embedding, st_embedding
from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    utcnow_naive,
)

_logger = logging.getLogger(__name__)

# Reserved ``model_name`` value that bypasses the real-model path and
# uses the deterministic hash mock instead.  Kept as a literal (not an
# enum) because the value is also a wire-level identifier stored in the
# ``vectorization_rule.model_name`` column.
_MOCK_MODEL_NAME: str = "mock"

# Default separator inserted between source columns when concatenating
# multiple fields into the encoder input.  Single ASCII space is the
# least surprising choice for natural-language fields; rules that need
# something else set ``rule.extra["concat_separator"]``.  Surfaced as a
# constant so the executor and the payload report the same value.
_DEFAULT_CONCAT_SEPARATOR: str = " "

# Key under ``rule.extra`` used to override the concat separator.
# Single source of truth so a future REST schema change can promote
# this field to a first-class column without grepping for the literal.
_EXTRA_KEY_CONCAT_SEPARATOR: str = "concat_separator"

# Key under ``rule.extra`` for the auto-index-build configuration.
# Structure: {"auto_build": true, "index_name": ..., "index_type": ..., "params": ...}
_EXTRA_KEY_INDEX_CONFIG: str = "index_config"

# Default index type when not specified in index_config.
_DEFAULT_INDEX_TYPE: str = "IVF_PQ"


def _build_batch_transform(
    *,
    source_columns: list[str],
    target_column: str,
    model_name: str,
    vector_dim: int,
    separator: str,
) -> Any:
    """Return a lance ``AddColumnsUDF`` that fills ``target_column``.

    Lance 0.6+ requires :func:`lance.add_columns` callables to be wrapped
    with :func:`lance.batch_udf` (raw callables are interpreted as
    pandas UDFs and crash without pandas installed -- which we
    deliberately do NOT pull into the worker image).  We wrap here so
    ``lance_io.add_columns_from_func`` stays generic.

    The UDF input is a ``pa.RecordBatch`` projected to
    ``source_columns``; the output is a ``pa.RecordBatch`` carrying
    only the new ``target_column``.  ``output_schema`` is built up
    front so lance can validate the very first batch and we get a
    clear error if the encoder ever returns the wrong dim.

    Multi-column behaviour (beta.2b): per row, we project each named
    column, drop NULL fields, and join the surviving values with
    ``separator``.  Single-column rules are just the degenerate case
    of this loop -- one column, no separator ever inserted -- so we do
    NOT branch on ``len(source_columns) == 1`` (Karpathy rule 2: same
    code path beats two near-identical ones).

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
        # Materialise every source column as a Python list once;
        # ``to_pylist`` is the fastest path for small batches and
        # avoids forcing callers to know about pyarrow types.  All
        # projected columns share the batch's row count, so we can
        # zip them straight away.
        per_column = [
            batch.column(name).to_pylist() for name in source_columns
        ]
        materialised: list[str] = []
        for row in zip(*per_column, strict=True):
            # Skip NULL fields per the beta.2b contract; encoders
            # crash on ``None``, and including a sentinel like ""
            # would inject spurious separators (``"foo" + " " + ""``
            # -> trailing space -> different embedding from ``"foo"``).
            parts = [v for v in row if isinstance(v, str)]
            materialised.append(separator.join(parts))
        if is_mock:
            vectors = hash_embedding(materialised)
        else:
            vectors = st_embedding(materialised, model_name=model_name)
        out_array = pa.array(vectors, type=out_type)
        return pa.RecordBatch.from_arrays(
            [out_array], names=[target_column],
        )

    return lance.batch_udf(output_schema=output_schema)(_transform)


def _validate_source_columns(
    *,
    storage_uri: str,
    source_columns: list[str],
    storage_options: dict[str, str],
) -> None:
    """Assert every ``source_column`` exists and is utf8-string typed.

    Reads the dataset schema via :func:`lance_io.read_dataset_schema`
    so unit tests can monkey-patch a single seam instead of standing
    up a real lance dataset.  Raises :class:`ValueError` with the
    offending column name(s) -- the worker maps that to FAILED with
    ``error_code="ValueError"``, which is the same surface every other
    business-precondition violation in this executor uses.

    Why string-only: real / mock embedding backends both consume
    ``list[str]``.  Numeric or struct columns would silently get
    ``str(value)`` if we stringified, embedding e.g. user_ids -- which
    is almost never what the operator wanted.  Failing loud forces a
    deliberate decision rather than a quiet mis-vectorisation.
    """

    import pyarrow as pa

    schema = lance_io.read_dataset_schema(
        storage_uri, storage_options=storage_options,
    )
    schema_names = set(schema.names)
    missing = [c for c in source_columns if c not in schema_names]
    if missing:
        raise ValueError(
            f"VECTORIZE source_columns not found in dataset schema: "
            f"missing={missing!r} available={sorted(schema_names)!r}",
        )
    non_string = [
        c for c in source_columns
        # ``pa.types.is_string`` matches utf8 (the lance default for
        # text); large_string would also be safe to embed but we keep
        # the contract narrow until a real caller asks for it.
        if not pa.types.is_string(schema.field(c).type)
    ]
    if non_string:
        types = {c: str(schema.field(c).type) for c in non_string}
        raise ValueError(
            f"VECTORIZE source_columns must be utf8 string type; "
            f"non-string columns: {types!r}",
        )


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
        if not source_columns:
            # The REST layer rejects empty source_columns at create
            # time; this is defensive in case an old row predates that
            # validation -- failing loud beats embedding nothing.
            raise ValueError(
                f"VECTORIZE rule has empty source_columns: id={rule_id}",
            )

        # Per-rule separator override; default kept in a module-level
        # constant so the payload (below) and the transform agree on
        # the same value.
        extra = rule.extra or {}
        separator = str(
            extra.get(_EXTRA_KEY_CONCAT_SEPARATOR, _DEFAULT_CONCAT_SEPARATOR),
        )

        # Validate schema BEFORE probing the model: if the dataset
        # does not even have the columns the rule asks for, no amount
        # of model loading will help and we want the failure cheap.
        settings = get_settings()
        storage_options = lance_io.build_storage_options(settings)
        await asyncio.to_thread(
            _validate_source_columns,
            storage_uri=dataset.storage_uri,
            source_columns=source_columns,
            storage_options=storage_options,
        )

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
            source_columns=source_columns,
            target_column=rule.target_column,
            model_name=rule.model_name,
            vector_dim=vector_dim,
            separator=separator,
        )
        mode = "mock" if rule.model_name == _MOCK_MODEL_NAME else "real-model"

        # Lance is sync; push to a worker thread so the asyncio loop
        # stays responsive (mirrors ttl_delete / index_build).  Any
        # lance / model exception propagates so the worker maps it to
        # FAILED with a clear error_code.
        rows_after = await asyncio.to_thread(
            lance_io.add_columns_from_func,
            dataset.storage_uri,
            transforms=transform,
            read_columns=source_columns,
            storage_options=storage_options,
        )

        now = utcnow_naive()

        # β.3: best-effort auto-index-build.  If the rule carries an
        # index_config with auto_build=true, we insert an Index row +
        # INDEX_BUILD task into the session so the worker's outer
        # commit picks them up.  Failures here are logged but never
        # propagate -- the vectorization itself already succeeded.
        index_build_submitted = await _maybe_submit_index_build(
            session,
            dataset=dataset,
            target_column=rule.target_column,
            extra=extra,
        )

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
                # Surfaced so operators can see at a glance which sep
                # the encoder actually used for this run -- handy when
                # rule.extra was edited between runs.
                "concat_separator": separator,
                "batch_size": rule.batch_size,
                "vector_dim": vector_dim,
                "vector_count": int(rows_after),
                "index_build_submitted": index_build_submitted,
                "executed_at": now.isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# β.3: auto-index-build helper
# ---------------------------------------------------------------------------


async def _maybe_submit_index_build(
    session: AsyncSession,
    *,
    dataset: Dataset,
    target_column: str,
    extra: dict[str, Any],
) -> bool | None:
    """Best-effort: insert Index + INDEX_BUILD Task if configured.

    Returns:
        ``True``  -- index build task was submitted.
        ``False`` -- index already exists (idempotent skip).
        ``None``  -- no ``index_config`` or ``auto_build`` is falsy.

    Never raises: any unexpected error is logged and swallowed so the
    vectorization result is not lost.
    """

    index_config = extra.get(_EXTRA_KEY_INDEX_CONFIG)
    if not index_config or not index_config.get("auto_build"):
        return None

    try:
        index_name: str = index_config.get(
            "index_name", f"idx_{target_column}",
        )
        index_type: str = index_config.get("index_type", _DEFAULT_INDEX_TYPE)
        index_params: dict[str, Any] = index_config.get("params") or {}

        # Idempotent: skip if the index already exists (any status).
        stmt = select(Index).where(
            Index.dataset_uuid == dataset.dataset_uuid,
            Index.index_name == index_name,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            _logger.info(
                "β.3 auto-index: index %r already exists (status=%s), skipping",
                index_name,
                existing.status,
            )
            return False

        # Insert the Index row in BUILDING state.
        idx = Index(
            dataset_uuid=dataset.dataset_uuid,
            index_name=index_name,
            column_name=target_column,
            index_type=index_type,
            params=index_params or None,
            status="BUILDING",
        )
        session.add(idx)
        await session.flush()  # get idx.id for the idempotency key

        # Insert the INDEX_BUILD task so a worker picks it up.
        task = Task(
            task_uuid=str(uuid.uuid4()),
            task_type="INDEX_BUILD",
            dataset_uuid=dataset.dataset_uuid,
            tenant_id=dataset.tenant_id,
            status="PENDING",
            priority=5,
            params={
                "index_name": index_name,
                "column_name": target_column,
                "index_type": index_type,
                "params": index_params,
            },
            idempotency_key=f"auto_build:{dataset.dataset_uuid}/{index_name}:{idx.id}",
            max_attempts=3,
        )
        session.add(task)
        await session.flush()

        _logger.info(
            "β.3 auto-index: submitted INDEX_BUILD task %s for index %r",
            task.task_uuid,
            index_name,
        )
        return True

    except Exception:
        _logger.warning(
            "β.3 auto-index: failed to submit INDEX_BUILD for %s/%s, "
            "vectorization result is preserved",
            dataset.dataset_uuid,
            target_column,
            exc_info=True,
        )
        return None
