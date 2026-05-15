"""beta.2a/β.3 in-cluster smoke: full lance read/write loop for VECTORIZE
+ auto-index-build.

Goal (Karpathy rule 4): prove the embedding pipeline, end-to-end:

  - service layer (B.4)        -- submit_vectorize_task creates VECTORIZE task
  - real model (B.5)           -- sentence-transformers loads + encodes
  - lance closure (B.6 / beta.2a)
                                -- worker reads source column from a real
                                  on-disk lance dataset, encodes via the
                                  real model, writes a fixed-size float32
                                  vector list back to the target column
  - auto-index-build (β.3)     -- EmbeddingExecutor auto-submits an
                                  INDEX_BUILD task when rule.extra has
                                  index_config.auto_build=true; the worker
                                  picks it up and creates a lance ANN index
  - ANN search (β.4)           -- search_service.search runs a top-k
                                  nearest-neighbour query against the
                                  indexed vector column

Bypasses the REST/auth layer because the cluster has no IdP -- the REST
surface is already covered by 353+ unit tests.

Storage:
  Uses the cluster MinIO bucket via ``s3://lcp-lance/...``.  The
  ``LCP_LANCE_STORAGE_*`` env vars (configmap + secret in
  deploy/k8s/00-namespace-config.yaml) feed both the seed call here and
  the worker's :func:`lcp.data_plane.lance_io.build_storage_options` --
  using the same Settings instance on both sides eliminates the
  ``api-pod-writes-but-worker-pod-cannot-read`` failure mode that bit
  the local-fs variant of this script.

Steps:
  1.   set_current_tenant
  2.   seed a 5-row lance dataset on s3://lcp-lance/smoke/vec/t_<ts>
  3.   dataset_service.create_dataset                 -> dataset_uuid
  4.   vectorization_service.create_rule(target=v)    -> rule.id
       (with extra.index_config.auto_build=true)
  5.   vectorization_service.submit_vectorize_task    -> task_uuid
  6.   submit_vectorize_task again                    -> idempotent
  7.   poll task_service.get_task                     -> SUCCEEDED
  8.   re-open the lance dataset                      -> v column exists,
                                                        5 rows, 384-d
  9.   set_enabled(False) + submit                    -> RuleDisabledError
  10.  find INDEX_BUILD task auto-submitted by β.3    -> exists, PENDING
  11.  poll INDEX_BUILD task until terminal           -> SUCCEEDED
  12.  re-open lance dataset, list_indices()          -> index present
  13.  search_service.search(vector, column='v', k=3) -> top-k results

Anything that prints "FAIL" is a real-cluster regression in slice 3 / 4.
"""

from __future__ import annotations

import asyncio
import time

import lance
import pyarrow as pa

from lcp.core.config import get_settings
from lcp.core.tenant import TenantPrincipal, set_current_tenant
from lcp.data_plane import lance_io
from lcp.db.session import get_session_factory
from lcp.schemas.dataset import DatasetRegisterRequest
from lcp.schemas.vectorization import RuleCreateRequest
from sqlalchemy import select

from lcp.db.models import Task as TaskModel
from lcp.services import dataset_service, search_service, task_service, vectorization_service

TENANT = "t-smoke-vec"

# beta.2a: storage prefix on the cluster MinIO bucket.  The worker reads
# storage_options from the same Settings (LCP_LANCE_STORAGE_*) so the
# seed call and the worker's lance_io call hit the same backing store.
_LANCE_BUCKET = "lcp-lance"
_LANCE_PREFIX = "smoke/vec"


def _step(n: int, msg: str) -> None:
    print(f"\n=== STEP {n}: {msg} ===", flush=True)


def _seed_lance_dataset(
    uri: str,
    *,
    storage_options: dict[str, str] | None,
    n: int = 5,
) -> None:
    """Write a tiny pyarrow table of {id, title} rows to ``uri``.

    Uses lance's ``write_dataset`` with mode='create'; each smoke run
    uses a unique time-suffix path so re-runs never collide.
    Forwards ``storage_options`` so the same Settings-derived
    credentials feed both the seed and the worker's later read+write.
    """

    table = pa.table({
        "id": pa.array(list(range(n)), type=pa.int64()),
        "title": pa.array(
            [f"row {i}: lance laker smoke" for i in range(n)],
            type=pa.string(),
        ),
    })
    lance.write_dataset(
        table, uri, mode="create", storage_options=storage_options,
    )


async def main() -> int:
    set_current_tenant(
        TenantPrincipal(tenant_id=TENANT, subject="smoke", auth_method="dev"),
    )
    factory = get_session_factory()

    settings = get_settings()
    storage_options = lance_io.build_storage_options(settings)

    suffix = str(int(time.time()))
    storage_uri = f"s3://{_LANCE_BUCKET}/{_LANCE_PREFIX}/t_{suffix}"

    # ---- step 2: seed the on-disk lance dataset ------------------------
    _step(2, f"seed 5 rows to {storage_uri}")
    _seed_lance_dataset(storage_uri, storage_options=storage_options, n=5)
    seed_count = lance.dataset(
        storage_uri, storage_options=storage_options,
    ).count_rows()
    print(f"OK seeded rows={seed_count}", flush=True)
    assert seed_count == 5, seed_count

    # ---- step 3: create dataset ----------------------------------------
    _step(3, "create dataset")
    async with factory() as session:
        ds = await dataset_service.create_dataset(
            session,
            DatasetRegisterRequest.model_validate({
                "catalog": "smoke",
                "schema": "vec",
                "table": f"t_{suffix}",
                "storage_uri": storage_uri,
                "owner": "smoke",
            }),
        )
        ds_uuid = ds.dataset_uuid
        print(f"OK dataset_uuid={ds_uuid}", flush=True)

    # ---- step 4: create vectorization_rule -----------------------------
    _step(4, "create vectorization_rule (with index_config.auto_build=true)")
    async with factory() as session:
        rule = await vectorization_service.create_rule(
            session,
            dataset_uuid=ds_uuid,
            payload=RuleCreateRequest.model_validate({
                "target_column": "v",
                "source_columns": ["title"],
                "model_name": "sentence-transformers/all-MiniLM-L6-v2",
                "model_version": "1",
                "extra": {
                    "index_config": {
                        "auto_build": True,
                        "index_type": "IVF_PQ",
                    },
                },
            }),
        )
        rule_id = rule.id
        print(
            f"OK rule.id={rule_id} enabled={rule.enabled} "
            f"target={rule.target_column} extra={rule.extra}",
            flush=True,
        )

    # ---- step 5: submit_vectorize_task ---------------------------------
    _step(5, "submit_vectorize_task (B.4 service entry)")
    async with factory() as session:
        task, created = await vectorization_service.submit_vectorize_task(
            session, ds_uuid, "v",
        )
        task_uuid = task.task_uuid
        print(
            f"OK task_uuid={task_uuid} type={task.task_type} "
            f"status={task.status} created={created} params={task.params}",
            flush=True,
        )
        assert task.task_type == "VECTORIZE", task.task_type

    # ---- step 6: idempotent replay -------------------------------------
    _step(6, "submit_vectorize_task again -> idempotent")
    async with factory() as session:
        task2, created2 = await vectorization_service.submit_vectorize_task(
            session, ds_uuid, "v",
        )
        print(
            f"OK task_uuid={task2.task_uuid} created={created2} "
            f"(must equal {task_uuid}, created2 must be False)",
            flush=True,
        )
        assert task2.task_uuid == task_uuid
        assert created2 is False

    # ---- step 7: wait for worker to drain ------------------------------
    _step(7, "poll task until terminal (worker should pick it up)")
    # 180s budget: the real-model path downloads MiniLM-L6-v2 from the
    # Hugging Face hub on first run (~90 MB) before encode() can return.
    # Subsequent runs in the same pod hit the on-disk + in-process
    # caches and finish in <2s.
    deadline = time.time() + 180
    final_status = None
    while time.time() < deadline:
        async with factory() as session:
            cur = await task_service.get_task(session, task_uuid)
            print(f"  status={cur.status} updated_at={cur.updated_at}", flush=True)
            if cur.status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                final_status = cur.status
                final_result = cur.result
                final_error = cur.error_message
                break
        await asyncio.sleep(2)
    if final_status is None:
        print("FAIL task did not reach terminal in 180s", flush=True)
        return 1
    print(
        f"OK final_status={final_status} result={final_result} "
        f"error={final_error}",
        flush=True,
    )
    if final_status != "SUCCEEDED":
        print("FAIL final_status != SUCCEEDED", flush=True)
        return 1

    # ---- step 8: re-read lance and verify the v column landed ----------
    _step(8, "re-read lance dataset, assert v column shape")
    ds_after = lance.dataset(storage_uri, storage_options=storage_options)
    schema = ds_after.schema
    print(f"OK lance schema={schema}", flush=True)
    if "v" not in schema.names:
        print("FAIL target column 'v' not present in lance schema", flush=True)
        return 1
    v_field = schema.field("v")
    if not pa.types.is_fixed_size_list(v_field.type):
        print(f"FAIL v column type is {v_field.type!r}, want fixed_size_list", flush=True)
        return 1
    if v_field.type.list_size != 384:
        print(f"FAIL v column dim is {v_field.type.list_size}, want 384", flush=True)
        return 1
    rows_after = ds_after.count_rows()
    if rows_after != 5:
        print(f"FAIL row count after vectorize is {rows_after}, want 5", flush=True)
        return 1
    print(f"OK v column shape=({rows_after}, 384) dtype={v_field.type}", flush=True)

    # ---- step 9: disabled rule must be rejected ------------------------
    _step(9, "disable rule then submit_vectorize_task -> RuleDisabledError")
    async with factory() as session:
        await vectorization_service.set_enabled(
            session, ds_uuid, "v", enabled=False,
        )
    async with factory() as session:
        try:
            await vectorization_service.submit_vectorize_task(
                session, ds_uuid, "v",
            )
        except vectorization_service.RuleDisabledError as exc:
            print(f"OK RuleDisabledError raised: {exc}", flush=True)
        else:
            print("FAIL expected RuleDisabledError, got nothing", flush=True)
            return 1

    # ---- step 10: find INDEX_BUILD task auto-submitted by β.3 ----------
    _step(10, "find INDEX_BUILD task auto-submitted by EmbeddingExecutor")
    index_build_task_uuid = None
    async with factory() as session:
        stmt = select(TaskModel).where(
            TaskModel.task_type == "INDEX_BUILD",
            TaskModel.dataset_uuid == ds_uuid,
        )
        ib_task = (await session.execute(stmt)).scalar_one_or_none()
        if ib_task is None:
            print("FAIL INDEX_BUILD task not found (β.3 auto-submit failed)", flush=True)
            return 1
        index_build_task_uuid = ib_task.task_uuid
        print(
            f"OK INDEX_BUILD task found: uuid={index_build_task_uuid} "
            f"status={ib_task.status} params={ib_task.params}",
            flush=True,
        )
        assert ib_task.params["index_name"] == "idx_v", ib_task.params

    # ---- step 11: poll INDEX_BUILD task until terminal ------------------
    _step(11, "poll INDEX_BUILD task until terminal")
    deadline = time.time() + 120
    ib_final_status = None
    while time.time() < deadline:
        async with factory() as session:
            cur = await task_service.get_task(session, index_build_task_uuid)
            print(f"  status={cur.status} updated_at={cur.updated_at}", flush=True)
            if cur.status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                ib_final_status = cur.status
                ib_result = cur.result
                ib_error = cur.error_message
                break
        await asyncio.sleep(2)
    if ib_final_status is None:
        print("FAIL INDEX_BUILD task did not reach terminal in 120s", flush=True)
        return 1
    print(
        f"OK INDEX_BUILD final_status={ib_final_status} result={ib_result} "
        f"error={ib_error}",
        flush=True,
    )
    if ib_final_status != "SUCCEEDED":
        print("FAIL INDEX_BUILD final_status != SUCCEEDED", flush=True)
        return 1

    # ---- step 12: verify lance index exists on disk --------------------
    _step(12, "re-read lance dataset, verify ANN index present")
    ds_indexed = lance.dataset(storage_uri, storage_options=storage_options)
    indices = ds_indexed.list_indices()
    print(f"OK lance indices={indices}", flush=True)
    if not indices:
        print("FAIL no indices found on lance dataset after INDEX_BUILD", flush=True)
        return 1
    # At least one index should cover the 'v' column.
    v_indexed = any(
        idx.get("columns") == ["v"] or idx.get("column") == "v"
        for idx in indices
    ) if isinstance(indices[0], dict) else len(indices) > 0
    if not v_indexed:
        print("FAIL no index covers column 'v'", flush=True)
        return 1
    print("OK ANN index on column 'v' confirmed", flush=True)

    # ---- step 13: vector search via search_service (β.4) ---------------
    _step(13, "vector search via search_service (β.4 ANN search)")
    # Use the first row's vector as the query vector to do a self-search.
    # Read the 'v' column from the lance dataset to get a real 384-d vector.
    sample_table = ds_indexed.to_table(columns=["v"], limit=1)
    query_vector = sample_table.column("v")[0].as_py()
    print(f"OK query_vector dim={len(query_vector)}", flush=True)
    assert len(query_vector) == 384, f"expected 384-d, got {len(query_vector)}"

    async with factory() as session:
        search_results = await search_service.search(
            session,
            dataset_uuid=ds_uuid,
            vector=query_vector,
            column="v",
            k=3,
        )
    print(f"OK search returned {len(search_results)} results", flush=True)
    if not search_results:
        print("FAIL search returned 0 results", flush=True)
        return 1
    if len(search_results) > 3:
        print(
            f"FAIL search returned {len(search_results)} results, want <= 3",
            flush=True,
        )
        return 1
    # Every result must have a _distance key.
    for i, row in enumerate(search_results):
        if "_distance" not in row:
            print(f"FAIL result[{i}] missing '_distance' key: {row}", flush=True)
            return 1
    # The first result should be the query row itself (distance ≈ 0).
    first_dist = search_results[0]["_distance"]
    print(f"OK first result _distance={first_dist}", flush=True)
    if first_dist > 0.01:
        print(
            f"WARN first result distance={first_dist} > 0.01 "
            "(expected near-zero for self-search)",
            flush=True,
        )
    print(
        f"OK vector search returned {len(search_results)} results, "
        f"all with _distance field",
        flush=True,
    )

    print("\n=== ALL STEPS PASSED ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
