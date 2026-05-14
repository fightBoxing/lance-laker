"""beta.2a in-cluster smoke: full lance read/write loop for VECTORIZE.

Goal (Karpathy rule 4): prove the embedding pipeline, end-to-end:

  - service layer (B.4)        -- submit_vectorize_task creates VECTORIZE task
  - real model (B.5)           -- sentence-transformers loads + encodes
  - lance closure (B.6 / beta.2a)
                                -- worker reads source column from a real
                                  on-disk lance dataset, encodes via the
                                  real model, writes a fixed-size float32
                                  vector list back to the target column

Bypasses the REST/auth layer because the cluster has no IdP -- the REST
surface is already covered by 342 unit tests.

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
  5.   vectorization_service.submit_vectorize_task    -> task_uuid
  6.   submit_vectorize_task again                    -> idempotent
  7.   poll task_service.get_task                     -> SUCCEEDED
  8.   re-open the lance dataset                      -> v column exists,
                                                        5 rows, 384-d
  9.   set_enabled(False) + submit                    -> RuleDisabledError

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
from lcp.services import dataset_service, task_service, vectorization_service

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
    _step(4, "create vectorization_rule")
    async with factory() as session:
        rule = await vectorization_service.create_rule(
            session,
            dataset_uuid=ds_uuid,
            payload=RuleCreateRequest.model_validate({
                "target_column": "v",
                "source_columns": ["title"],
                "model_name": "sentence-transformers/all-MiniLM-L6-v2",
                "model_version": "1",
            }),
        )
        rule_id = rule.id
        print(
            f"OK rule.id={rule_id} enabled={rule.enabled} "
            f"target={rule.target_column}",
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

    print("\n=== ALL STEPS PASSED ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
