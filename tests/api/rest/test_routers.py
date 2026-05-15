"""Interface tests for the REST routers (smoke + contract level).

These tests assert:
- the route exists at the advertised path
- the middleware has bound the tenant
- stub handlers return the documented status codes (200/201/202/204/501)

Each test uses a freshly-signed token per call so that tenant isolation
can be verified from the outside.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.api


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------


class TestDatasetsRouter:

    async def test_list_datasets_ok(self, http_client: Any, issue_token: Any) -> None:
        token = issue_token(tenant_id="t-list")
        resp = await http_client.get("/v1/datasets", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["page"] == 1
        assert body["page_size"] == 20

    async def test_create_dataset_returns_201(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-create")
        resp = await http_client.post(
            "/v1/datasets",
            headers=_auth(token),
            json={
                "catalog": "c1",
                "schema": "s1",
                "table": "t1",
                "storage_uri": "s3://bucket/path",
                "owner": "alice",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["catalog"] == "c1"
        assert body["schema"] == "s1"
        assert body["table"] == "t1"
        assert body["status"] == "ACTIVE"
        assert body["tenant_id"] == "t-create"
        assert "dataset_uuid" in body

    async def test_create_then_get_returns_same_dataset(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-roundtrip")
        created = (
            await http_client.post(
                "/v1/datasets",
                headers=_auth(token),
                json={
                    "catalog": "c",
                    "schema": "s",
                    "table": "t",
                    "storage_uri": "s3://b/p",
                },
            )
        ).json()
        uuid = created["dataset_uuid"]

        resp = await http_client.get(f"/v1/datasets/{uuid}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["dataset_uuid"] == uuid

    async def test_get_unknown_dataset_returns_404(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-get")
        resp = await http_client.get("/v1/datasets/no-such-uuid", headers=_auth(token))
        assert resp.status_code == 404

    async def test_delete_existing_returns_204(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-del")
        created = (
            await http_client.post(
                "/v1/datasets",
                headers=_auth(token),
                json={
                    "catalog": "c",
                    "schema": "s",
                    "table": "t",
                    "storage_uri": "s3://b/p",
                },
            )
        ).json()
        uuid = created["dataset_uuid"]

        resp = await http_client.delete(f"/v1/datasets/{uuid}", headers=_auth(token))
        assert resp.status_code == 204

    async def test_delete_unknown_returns_404(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-del")
        resp = await http_client.delete("/v1/datasets/no-such-uuid", headers=_auth(token))
        assert resp.status_code == 404

    async def test_tenant_isolation_on_list(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        """RLS regression: a dataset created under tenant A is invisible to B."""

        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")
        await http_client.post(
            "/v1/datasets",
            headers=_auth(token_a),
            json={
                "catalog": "c",
                "schema": "s",
                "table": "t-a",
                "storage_uri": "s3://b/a",
            },
        )
        # Tenant B must see zero datasets even though one row exists in the DB.
        body_b = (await http_client.get("/v1/datasets", headers=_auth(token_b))).json()
        assert body_b["total"] == 0
        # Tenant A should see exactly its own row.
        body_a = (await http_client.get("/v1/datasets", headers=_auth(token_a))).json()
        assert body_a["total"] == 1


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


class TestTasksRouter:

    async def test_list_tasks_ok_empty(self, http_client: Any, issue_token: Any) -> None:
        token = issue_token(tenant_id="t-tasks")
        resp = await http_client.get(
            "/v1/tasks?status=RUNNING",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["page"] == 1
        assert body["page_size"] == 20

    async def test_submit_task_returns_202(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-sub")
        resp = await http_client.post(
            "/v1/tasks",
            headers=_auth(token),
            json={
                "type": "COMPACTION",
                "dataset_uuid": "00000000-0000-0000-0000-000000000001",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["type"] == "COMPACTION"
        assert body["status"] == "PENDING"
        assert body["tenant_id"] == "t-sub"
        assert body["priority"] == 5
        assert "task_uuid" in body

    async def test_submit_task_idempotency_returns_200_on_replay(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idem")
        payload = {
            "type": "VECTORIZE",
            "dataset_uuid": "00000000-0000-0000-0000-000000000002",
            "idempotency_key": "my-key-1",
        }
        first = await http_client.post(
            "/v1/tasks", headers=_auth(token), json=payload,
        )
        second = await http_client.post(
            "/v1/tasks", headers=_auth(token), json=payload,
        )
        assert first.status_code == 202
        assert second.status_code == 200
        # Same row returned -> same task_uuid.
        assert first.json()["task_uuid"] == second.json()["task_uuid"]

    async def test_get_task_round_trip(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-get")
        created = (
            await http_client.post(
                "/v1/tasks",
                headers=_auth(token),
                json={
                    "type": "INDEX_BUILD",
                    "dataset_uuid": "d-1",
                },
            )
        ).json()
        uuid_ = created["task_uuid"]
        resp = await http_client.get(f"/v1/tasks/{uuid_}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["task_uuid"] == uuid_

    async def test_get_unknown_task_returns_404(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-get-missing")
        resp = await http_client.get("/v1/tasks/no-such-uuid", headers=_auth(token))
        assert resp.status_code == 404

    async def test_cancel_pending_task_returns_200(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-cancel")
        created = (
            await http_client.post(
                "/v1/tasks",
                headers=_auth(token),
                json={"type": "COMPACTION", "dataset_uuid": "d-1"},
            )
        ).json()
        uuid_ = created["task_uuid"]
        resp = await http_client.post(f"/v1/tasks/{uuid_}/cancel", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"

    async def test_retry_non_failed_task_returns_409(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-retry")
        created = (
            await http_client.post(
                "/v1/tasks",
                headers=_auth(token),
                json={"type": "COMPACTION", "dataset_uuid": "d-1"},
            )
        ).json()
        uuid_ = created["task_uuid"]
        # Fresh task is PENDING, retry must be rejected.
        resp = await http_client.post(f"/v1/tasks/{uuid_}/retry", headers=_auth(token))
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "INVALID_TRANSITION"

    async def test_tenant_isolation_on_task_list(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        """RLS regression: tasks from tenant A must be invisible to tenant B."""

        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")
        await http_client.post(
            "/v1/tasks",
            headers=_auth(token_a),
            json={"type": "COMPACTION", "dataset_uuid": "d-1"},
        )
        body_b = (await http_client.get("/v1/tasks", headers=_auth(token_b))).json()
        assert body_b["total"] == 0
        body_a = (await http_client.get("/v1/tasks", headers=_auth(token_a))).json()
        assert body_a["total"] == 1


# ---------------------------------------------------------------------------
# indexes
# ---------------------------------------------------------------------------


class TestIndexesRouter:

    async def _create_dataset(
        self, http_client: Any, token: str, *, table: str = "t1",
    ) -> str:
        """Helper: create a dataset and return its uuid."""

        resp = await http_client.post(
            "/v1/datasets",
            headers=_auth(token),
            json={
                "catalog": "c",
                "schema": "s",
                "table": table,
                "storage_uri": "s3://b/p",
            },
        )
        return resp.json()["dataset_uuid"]

    async def test_list_indexes_empty(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-list")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/indexes", headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    async def test_list_indexes_unknown_dataset_returns_404(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-404")
        resp = await http_client.get(
            "/v1/datasets/no-such-uuid/indexes", headers=_auth(token),
        )
        assert resp.status_code == 404

    async def test_create_index_returns_202(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-create")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token),
            json={
                "name": "image_vector_hnsw",
                "column": "image_vector",
                "type": "HNSW",
                "params": {"m": 16, "ef_construction": 200},
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["index_name"] == "image_vector_hnsw"
        assert body["type"] == "HNSW"
        assert body["status"] == "BUILDING"

    async def test_create_duplicate_index_returns_409(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-dup")
        ds_uuid = await self._create_dataset(http_client, token)
        payload = {
            "name": "dup_idx", "column": "vec", "type": "IVF_FLAT",
        }
        first = await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token), json=payload,
        )
        second = await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token), json=payload,
        )
        assert first.status_code == 202
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "ALREADY_EXISTS"

    async def test_get_index_round_trip(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-get")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token),
            json={"name": "my_idx", "column": "c", "type": "BTREE"},
        )
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/indexes/my_idx", headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["index_name"] == "my_idx"

    async def test_optimize_building_index_returns_409(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """A freshly-created index is BUILDING; optimize must be rejected."""

        token = issue_token(tenant_id="t-idx-opt")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token),
            json={"name": "opt_idx", "column": "c", "type": "HNSW"},
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes/opt_idx/optimize",
            headers=_auth(token),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "INVALID_TRANSITION"

    async def test_drop_index_returns_204(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-drop")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token),
            json={"name": "drop_idx", "column": "c", "type": "BTREE"},
        )
        resp = await http_client.delete(
            f"/v1/datasets/{ds_uuid}/indexes/drop_idx", headers=_auth(token),
        )
        assert resp.status_code == 204
        # Verify state moved to DROPPED.
        get_resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/indexes/drop_idx", headers=_auth(token),
        )
        assert get_resp.json()["status"] == "DROPPED"

    async def test_tenant_isolation_via_parent_dataset(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Tenant B must not see tenant A's index even via direct URL."""

        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")
        ds_uuid = await self._create_dataset(http_client, token_a, table="isolated")
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/indexes",
            headers=_auth(token_a),
            json={"name": "secret_idx", "column": "c", "type": "HNSW"},
        )
        # Tenant B hits the same URL: the parent dataset lookup hits RLS
        # and 404s before the index is even checked.
        resp_b = await http_client.get(
            f"/v1/datasets/{ds_uuid}/indexes/secret_idx", headers=_auth(token_b),
        )
        assert resp_b.status_code == 404
        # Sanity: the owner can still see it.
        resp_a = await http_client.get(
            f"/v1/datasets/{ds_uuid}/indexes/secret_idx", headers=_auth(token_a),
        )
        assert resp_a.status_code == 200


# ---------------------------------------------------------------------------
# lifecycle policies
# ---------------------------------------------------------------------------


class TestLifecycleRouter:

    async def _create_dataset(
        self, http_client: Any, token: str, *, table: str = "t1",
    ) -> str:
        """Helper: create a dataset and return its uuid."""

        resp = await http_client.post(
            "/v1/datasets",
            headers=_auth(token),
            json={
                "catalog": "c",
                "schema": "s",
                "table": table,
                "storage_uri": "s3://b/p",
            },
        )
        return resp.json()["dataset_uuid"]

    async def test_list_policies_empty(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-list")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    async def test_list_policies_unknown_dataset_returns_404(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-404")
        resp = await http_client.get(
            "/v1/datasets/no-such/lifecycle-policies", headers=_auth(token),
        )
        assert resp.status_code == 404

    async def test_create_policy_returns_201(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-create")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
            json={
                "policy_name": "standard-90d",
                "ttl_days": 90,
                "tier_rules": {"hot_to_warm_days": 30},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["policy_name"] == "standard-90d"
        assert body["ttl_days"] == 90
        assert body["enabled"] is True

    async def test_create_duplicate_policy_returns_409(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-dup")
        ds_uuid = await self._create_dataset(http_client, token)
        payload = {"policy_name": "dup"}
        first = await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token), json=payload,
        )
        second = await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token), json=payload,
        )
        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "ALREADY_EXISTS"

    async def test_get_policy_round_trip(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-get")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
            json={"policy_name": "my-pol", "ttl_days": 30},
        )
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/my-pol",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["policy_name"] == "my-pol"

    async def test_patch_policy_only_supplied_fields(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-patch")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
            json={
                "policy_name": "p",
                "ttl_days": 30,
                "index_optimize_cron": "0 1 * * *",
            },
        )
        resp = await http_client.patch(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/p",
            headers=_auth(token),
            json={"ttl_days": 60},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ttl_days"] == 60
        # Field not in PATCH payload must keep its prior value.
        assert body["index_optimize_cron"] == "0 1 * * *"

    async def test_disable_then_enable_policy(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-toggle")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
            json={"policy_name": "p"},
        )
        disabled = await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/p/disable",
            headers=_auth(token),
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        enabled = await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/p/enable",
            headers=_auth(token),
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

    async def test_delete_policy_returns_204(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-pol-del")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token),
            json={"policy_name": "p"},
        )
        resp = await http_client.delete(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/p",
            headers=_auth(token),
        )
        assert resp.status_code == 204
        # Subsequent GET must be 404.
        get_resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/p",
            headers=_auth(token),
        )
        assert get_resp.status_code == 404

    async def test_tenant_isolation_via_parent_dataset(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Tenant B must not see tenant A's policy via direct URL."""

        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")
        ds_uuid = await self._create_dataset(http_client, token_a, table="isolated")
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies",
            headers=_auth(token_a),
            json={"policy_name": "secret"},
        )
        resp_b = await http_client.get(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/secret",
            headers=_auth(token_b),
        )
        assert resp_b.status_code == 404
        resp_a = await http_client.get(
            f"/v1/datasets/{ds_uuid}/lifecycle-policies/secret",
            headers=_auth(token_a),
        )
        assert resp_a.status_code == 200


# ---------------------------------------------------------------------------
# vectorization rules
# ---------------------------------------------------------------------------


class TestVectorizationRouter:

    async def _create_dataset(
        self, http_client: Any, token: str, *, table: str = "t1",
    ) -> str:
        """Helper: create a dataset and return its uuid."""

        resp = await http_client.post(
            "/v1/datasets",
            headers=_auth(token),
            json={
                "catalog": "c",
                "schema": "s",
                "table": table,
                "storage_uri": "s3://b/p",
            },
        )
        return resp.json()["dataset_uuid"]

    async def test_list_rules_empty(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-list")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    async def test_create_rule_returns_201(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-create")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "image_vector",
                "source_columns": ["image_url"],
                "model_name": "clip-vit-large",
                "model_version": "1.0",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_column"] == "image_vector"
        assert body["trigger_type"] == "ON_INSERT"
        assert body["enabled"] is True

    async def test_scheduled_rule_without_cron_returns_422(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Pydantic must reject SCHEDULED + missing cron at request validation."""

        token = issue_token(tenant_id="t-vec-cron")
        ds_uuid = await self._create_dataset(http_client, token)
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
                "trigger_type": "SCHEDULED",
            },
        )
        assert resp.status_code == 422

    async def test_create_duplicate_rule_returns_409(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-dup")
        ds_uuid = await self._create_dataset(http_client, token)
        payload = {
            "target_column": "v",
            "source_columns": ["a"],
            "model_name": "m",
            "model_version": "1",
        }
        first = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token), json=payload,
        )
        second = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token), json=payload,
        )
        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "ALREADY_EXISTS"

    async def test_get_rule_round_trip(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-get")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a", "b"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        resp = await http_client.get(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_columns"] == ["a", "b"]

    async def test_patch_promoting_to_scheduled_without_cron_returns_400(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Service-layer merged-state validation rejects PATCH that breaks invariant."""

        token = issue_token(tenant_id="t-vec-patch-bad")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        # Existing rule is ON_INSERT, no cron; flipping to SCHEDULED alone
        # would leave it non-runnable -> service must 400.
        resp = await http_client.patch(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v",
            headers=_auth(token),
            json={"trigger_type": "SCHEDULED"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "INVALID_ARGUMENT"

    async def test_patch_promoting_to_scheduled_with_cron_returns_200(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-patch-good")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        resp = await http_client.patch(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v",
            headers=_auth(token),
            json={"trigger_type": "SCHEDULED", "cron_expr": "0 2 * * *"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["trigger_type"] == "SCHEDULED"
        assert body["cron_expr"] == "0 2 * * *"

    async def test_disable_then_enable_rule(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-toggle")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        disabled = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/disable",
            headers=_auth(token),
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        enabled = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/enable",
            headers=_auth(token),
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

    async def test_tenant_isolation_via_parent_dataset(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Tenant B must not see tenant A's rule via direct URL."""

        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")
        ds_uuid = await self._create_dataset(http_client, token_a, table="isolated")
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token_a),
            json={
                "target_column": "secret",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        resp_b = await http_client.get(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/secret",
            headers=_auth(token_b),
        )
        assert resp_b.status_code == 404
        resp_a = await http_client.get(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/secret",
            headers=_auth(token_a),
        )
        assert resp_a.status_code == 200

    async def test_vectorize_now_creates_task(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """POST .../vectorize-now enqueues a VECTORIZE task."""

        token = issue_token(tenant_id="t-vec-now")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/vectorize-now",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Task wire shape uses ``type`` (not ``task_type``); validates
        # the executor's task_type matches schemas/task.py Literal.
        assert body["type"] == "VECTORIZE"
        assert body["status"] == "PENDING"
        assert body["dataset_uuid"] == ds_uuid
        # Params must carry the rule's primary key so the worker can
        # look the rule back up under tenant scope.
        assert "vectorization_rule_id" in body["params"]

    async def test_vectorize_now_idempotent_on_replay(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Two rapid posts return the same task_uuid (idempotency_key)."""

        token = issue_token(tenant_id="t-vec-idem")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        first = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/vectorize-now",
            headers=_auth(token),
        )
        second = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/vectorize-now",
            headers=_auth(token),
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["task_uuid"] == second.json()["task_uuid"]

    async def test_vectorize_now_404_when_rule_missing(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-vec-missing")
        ds_uuid = await self._create_dataset(http_client, token)
        # No rule created -> 404.
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/no-such/vectorize-now",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    async def test_vectorize_now_409_when_rule_disabled(
        self, http_client: Any, issue_token: Any,
    ) -> None:
        """Running a disabled rule would surprise operators; 409 instead."""

        token = issue_token(tenant_id="t-vec-disabled")
        ds_uuid = await self._create_dataset(http_client, token)
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules",
            headers=_auth(token),
            json={
                "target_column": "v",
                "source_columns": ["a"],
                "model_name": "m",
                "model_version": "1",
            },
        )
        await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/disable",
            headers=_auth(token),
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/vectorization-rules/v/vectorize-now",
            headers=_auth(token),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "RULE_DISABLED"


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


class TestMetaRouter:

    async def test_trigger_meta_sync_503_when_gravitino_unconfigured(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        # Default test settings leave LCP_GRAVITINO_URL empty, so the
        # handler must refuse the request loudly instead of attempting
        # a connection to a URL that does not exist.
        token = issue_token(tenant_id="t-meta")
        resp = await http_client.post(
            "/v1/meta/sync",
            headers=_auth(token),
            json={"scope": "ALL"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "GRAVITINO_NOT_CONFIGURED"

    async def test_trigger_meta_sync_runs_when_configured(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: Any,
    ) -> None:
        # Point the client at a fake URL and patch the underlying
        # GravitinoClient so the handler reconciles 0 datasets and
        # returns a clean SUCCEEDED report.  This is a router-level
        # integration check; service-level paths are covered exhaustively
        # in tests/unit/services/test_meta_sync_service.py.
        from lcp.api.rest.routers import meta as meta_router

        monkeypatch.setenv(
            "LCP_GRAVITINO_URL", "http://gravitino.test:8090",
        )
        # Bust the cached settings so the new env var is read.
        from lcp.core.config import get_settings
        get_settings.cache_clear()

        # Replace GravitinoClient.from_settings with a stub that yields
        # a no-op async context manager; the handler's reconcile_all
        # then sees zero datasets and returns immediately.
        class _NullClient:
            async def __aenter__(self) -> "_NullClient":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        monkeypatch.setattr(
            meta_router.GravitinoClient,
            "from_settings",
            classmethod(lambda cls, settings=None: _NullClient()),
        )

        token = issue_token(tenant_id="t-meta-ok")
        resp = await http_client.post(
            "/v1/meta/sync",
            headers=_auth(token),
            json={"scope": "ALL"},
        )

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "SUCCEEDED"
        assert body["scanned"] == 0
        assert body["sync_id"] == "sync-inline"

    async def test_trigger_meta_sync_rejects_catalog_scope(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: Any,
    ) -> None:
        # CATALOG is reserved in the OpenAPI but not implemented; the
        # router refuses with 400 instead of silently downgrading.
        monkeypatch.setenv(
            "LCP_GRAVITINO_URL", "http://gravitino.test:8090",
        )
        from lcp.core.config import get_settings
        get_settings.cache_clear()

        token = issue_token(tenant_id="t-meta-catalog")
        resp = await http_client.post(
            "/v1/meta/sync",
            headers=_auth(token),
            json={"scope": "CATALOG"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "SCOPE_NOT_SUPPORTED"

    async def test_get_meta_sync_status_still_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        # Async run-id storage is not yet implemented; assert the
        # handler emits the documented machine-readable code so clients
        # can switch behaviour when the feature lands.
        token = issue_token(tenant_id="t-meta-status")
        resp = await http_client.get(
            "/v1/meta/sync/run-1",
            headers=_auth(token),
        )
        assert resp.status_code == 501
        assert resp.json()["detail"]["code"] == "ASYNC_RUNS_NOT_IMPLEMENTED"

    async def test_get_dataset_snapshot_returns_404_when_unknown(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        # Snapshot endpoint now actually queries the dataset; verifying
        # the 404 path is enough for router coverage (the happy path
        # exercises the same RLS lookup as the datasets router).
        token = issue_token(tenant_id="t-meta-snap")
        resp = await http_client.get(
            "/v1/meta/datasets/does-not-exist/snapshot",
            headers=_auth(token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Route-table completeness (guardrail against accidental deletion)
# ---------------------------------------------------------------------------


class TestRouteTableContract:

    async def test_all_declared_routes_present(self, rest_app: Any) -> None:
        """Routes registered in the app must match the router contract."""

        paths = {r.path for r in rest_app.routes}  # type: ignore[attr-defined]

        expected_subset = {
            "/v1/datasets",
            "/v1/datasets/{dataset_uuid}",
            "/v1/tasks",
            "/v1/tasks/{task_uuid}",
            "/v1/tasks/{task_uuid}/cancel",
            "/v1/tasks/{task_uuid}/retry",
            "/v1/datasets/{dataset_uuid}/indexes",
            "/v1/datasets/{dataset_uuid}/indexes/{index_name}",
            "/v1/datasets/{dataset_uuid}/indexes/{index_name}/optimize",
            "/v1/datasets/{dataset_uuid}/indexes/{index_name}/merge",
            "/v1/datasets/{dataset_uuid}/lifecycle-policies",
            "/v1/datasets/{dataset_uuid}/lifecycle-policies/{policy_name}",
            "/v1/datasets/{dataset_uuid}/lifecycle-policies/{policy_name}/enable",
            "/v1/datasets/{dataset_uuid}/lifecycle-policies/{policy_name}/disable",
            "/v1/datasets/{dataset_uuid}/vectorization-rules",
            "/v1/datasets/{dataset_uuid}/vectorization-rules/{target_column}",
            "/v1/datasets/{dataset_uuid}/vectorization-rules/{target_column}/enable",
            "/v1/datasets/{dataset_uuid}/vectorization-rules/{target_column}/disable",
            "/v1/datasets/{dataset_uuid}/vectorization-rules/{target_column}/vectorize-now",
            "/v1/meta/sync",
            "/v1/meta/sync/{run_id}",
            "/v1/meta/datasets/{dataset_id}/snapshot",
            "/v1/datasets/{dataset_uuid}/search",
            "/healthz",
        }
        missing = expected_subset - paths
        assert not missing, f"routes missing from app: {missing}"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearchRouter:
    """Integration tests for ``POST /v1/datasets/{uuid}/search``.

    The lance layer (search_service.search) is monkey-patched so these
    tests exercise the HTTP contract without needing a real lance dataset.
    """

    _SEARCH_RESULTS = [
        {"_distance": 0.05, "id": 1, "title": "hello"},
        {"_distance": 0.42, "id": 2, "title": "world"},
    ]

    async def _create_dataset(
        self, http_client: Any, token: str, *, table: str = "t_search",
    ) -> str:
        resp = await http_client.post(
            "/v1/datasets",
            headers=_auth(token),
            json={
                "catalog": "c",
                "schema": "s",
                "table": table,
                "storage_uri": "s3://b/p",
            },
        )
        return resp.json()["dataset_uuid"]

    # -- happy path --------------------------------------------------------

    async def test_search_returns_200(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = issue_token(tenant_id="t-search-ok")
        ds_uuid = await self._create_dataset(http_client, token)

        async def _fake_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return list(self._SEARCH_RESULTS)

        monkeypatch.setattr(
            "lcp.services.search_service.search", _fake_search,
        )

        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1, 0.2, 0.3], "column": "v", "k": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["column"] == "v"
        assert body["k"] == 5
        assert len(body["results"]) == 2
        assert body["results"][0]["_distance"] == 0.05

    # -- dataset not found -------------------------------------------------

    async def test_search_unknown_dataset_returns_404(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lcp.services import dataset_service

        token = issue_token(tenant_id="t-search-404")

        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise dataset_service.DatasetNotFoundError("not found")

        monkeypatch.setattr(
            "lcp.services.search_service.search", _raise,
        )

        resp = await http_client.post(
            "/v1/datasets/no-such-uuid/search",
            headers=_auth(token),
            json={"vector": [0.1], "column": "v"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "NOT_FOUND"

    # -- column not found --------------------------------------------------

    async def test_search_bad_column_returns_400(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lcp.services import search_service as ss

        token = issue_token(tenant_id="t-search-col")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_col",
        )

        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise ss.ColumnNotFoundError("column 'bad' not found")

        monkeypatch.setattr(
            "lcp.services.search_service.search", _raise,
        )

        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1], "column": "bad"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "COLUMN_NOT_FOUND"

    # -- dimension mismatch ------------------------------------------------

    async def test_search_dim_mismatch_returns_400(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lcp.services import search_service as ss

        token = issue_token(tenant_id="t-search-dim")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_dim",
        )

        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise ss.DimensionMismatchError("expected 384, got 3")

        monkeypatch.setattr(
            "lcp.services.search_service.search", _raise,
        )

        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1, 0.2, 0.3], "column": "v"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "DIMENSION_MISMATCH"

    # -- request validation ------------------------------------------------

    async def test_search_empty_vector_returns_422(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-search-val")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_val",
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [], "column": "v"},
        )
        assert resp.status_code == 422

    async def test_search_missing_column_returns_422(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-search-val2")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_val2",
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1]},
        )
        assert resp.status_code == 422

    async def test_search_k_out_of_range_returns_422(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-search-val3")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_val3",
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1], "column": "v", "k": 0},
        )
        assert resp.status_code == 422

    async def test_search_extra_field_returns_422(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-search-val4")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_val4",
        )
        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={"vector": [0.1], "column": "v", "bogus": True},
        )
        assert resp.status_code == 422

    # -- optional params forwarded -----------------------------------------

    async def test_search_with_all_optional_params(
        self,
        http_client: Any,
        issue_token: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = issue_token(tenant_id="t-search-opts")
        ds_uuid = await self._create_dataset(
            http_client, token, table="t_search_opts",
        )

        captured: dict[str, Any] = {}

        async def _capture(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return list(self._SEARCH_RESULTS)

        monkeypatch.setattr(
            "lcp.services.search_service.search", _capture,
        )

        resp = await http_client.post(
            f"/v1/datasets/{ds_uuid}/search",
            headers=_auth(token),
            json={
                "vector": [0.1, 0.2],
                "column": "v",
                "k": 20,
                "filter": "category = 'tech'",
                "select": ["id", "title"],
                "nprobes": 32,
                "refine_factor": 5,
            },
        )
        assert resp.status_code == 200
        assert captured["column"] == "v"
        assert captured["k"] == 20
        assert captured["filter_expr"] == "category = 'tech'"
        assert captured["select_columns"] == ["id", "title"]
        assert captured["nprobes"] == 32
        assert captured["refine_factor"] == 5

    # -- auth required -----------------------------------------------------

    async def test_search_without_auth_returns_401(
        self, http_client: Any,
    ) -> None:
        resp = await http_client.post(
            "/v1/datasets/any-uuid/search",
            json={"vector": [0.1], "column": "v"},
        )
        assert resp.status_code == 401
