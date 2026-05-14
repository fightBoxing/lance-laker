"""B.1-B.4 in-cluster smoke test (run inside lcp-api pod).

Goal (karpathy rule 4): prove the 4 commits 41465af / 15681c8 / 0a60e55 /
15804ce are alive against the real MySQL + the real lcp-worker poll loop,
without any mocks.  Bypasses the REST/auth layer because the cluster has
no IdP -- the REST surface is already covered by 331 unit tests.

Steps and verification:
  1. set_current_tenant(t-smoke-vec)
  2. dataset_service.create_dataset                -> dataset_uuid
  3. vectorization_service.create_rule(target=v)   -> rule.id, enabled=True
  4. vectorization_service.submit_vectorize_task   -> task.task_uuid, type=VECTORIZE
  5. poll task_service.get_task until terminal     -> expect SUCCEEDED
  6. set_enabled(False) + submit_vectorize_task    -> expect RuleDisabledError

Anything that prints "FAIL" is a real-cluster regression in slice 3.
"""

from __future__ import annotations

import asyncio
import time

from lcp.core.tenant import TenantPrincipal, set_current_tenant
from lcp.db.session import get_session_factory
from lcp.schemas.dataset import DatasetRegisterRequest
from lcp.schemas.vectorization import RuleCreateRequest
from lcp.services import dataset_service, task_service, vectorization_service


TENANT = "t-smoke-vec"


def _step(n: int, msg: str) -> None:
    print(f"\n=== STEP {n}: {msg} ===", flush=True)


async def main() -> int:
    set_current_tenant(
        TenantPrincipal(tenant_id=TENANT, subject="smoke", auth_method="dev"),
    )
    factory = get_session_factory()

    # ---- step 2: create dataset ----------------------------------------
    _step(2, "create dataset")
    suffix = str(int(time.time()))
    async with factory() as session:
        ds = await dataset_service.create_dataset(
            session,
            DatasetRegisterRequest.model_validate({
                "catalog": "smoke",
                "schema": "vec",
                "table": f"t_{suffix}",
                "storage_uri": f"s3://lcp-lance/smoke/vec/t_{suffix}",
                "owner": "smoke",
            }),
        )
        ds_uuid = ds.dataset_uuid
        print(f"OK dataset_uuid={ds_uuid}", flush=True)

    # ---- step 3: create vectorization_rule -----------------------------
    _step(3, "create vectorization_rule")
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

    # ---- step 4: submit_vectorize_task ---------------------------------
    _step(4, "submit_vectorize_task (B.4 service entry)")
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

    # ---- step 5: idempotent replay -------------------------------------
    _step(5, "submit_vectorize_task again -> idempotent")
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

    # ---- step 6: wait for worker to drain ------------------------------
    _step(6, "poll task until terminal (worker should pick it up)")
    # 180s instead of 60s: the real-model path (slice 4) downloads the
    # sentence-transformers model from the Hugging Face hub on first run
    # (~90 MB for MiniLM-L6-v2) before encode() can return.  Subsequent
    # runs in the same pod hit the on-disk cache + the in-process model
    # cache and finish in <2s, but the cold-cache path needs the bigger
    # budget.  Leaving 180s also covers slow egress through colima.
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
        print("FAIL task did not reach terminal in 60s", flush=True)
        return 1
    print(
        f"OK final_status={final_status} result={final_result} "
        f"error={final_error}",
        flush=True,
    )
    if final_status != "SUCCEEDED":
        print("FAIL final_status != SUCCEEDED", flush=True)
        return 1

    # ---- step 7: disabled rule must be rejected ------------------------
    _step(7, "disable rule then submit_vectorize_task -> RuleDisabledError")
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
