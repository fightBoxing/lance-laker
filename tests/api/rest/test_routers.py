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

    async def test_list_indexes_accepts_dataset_id_filter(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx")
        resp = await http_client.get(
            "/v1/indexes?dataset_id=d1",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["filter"]["dataset_id"] == "d1"

    async def test_create_index_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-create")
        resp = await http_client.post(
            "/v1/indexes",
            headers=_auth(token),
            json={"type": "hnsw"},
        )
        assert resp.status_code == 501

    async def test_optimize_index_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-opt")
        resp = await http_client.post(
            "/v1/indexes/idx-1/optimize",
            headers=_auth(token),
        )
        assert resp.status_code == 501

    async def test_drop_index_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-idx-drop")
        resp = await http_client.delete(
            "/v1/indexes/idx-1",
            headers=_auth(token),
        )
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


class TestMetaRouter:

    async def test_trigger_meta_sync_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-meta")
        resp = await http_client.post(
            "/v1/meta/sync",
            headers=_auth(token),
            json={"dry_run": True},
        )
        assert resp.status_code == 501

    async def test_get_meta_sync_status_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-meta-status")
        resp = await http_client.get(
            "/v1/meta/sync/run-1",
            headers=_auth(token),
        )
        assert resp.status_code == 501

    async def test_get_dataset_snapshot_returns_501(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(tenant_id="t-meta-snap")
        resp = await http_client.get(
            "/v1/meta/datasets/d-1/snapshot",
            headers=_auth(token),
        )
        assert resp.status_code == 501


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
            "/v1/indexes",
            "/v1/indexes/{index_id}",
            "/v1/indexes/{index_id}/optimize",
            "/v1/meta/sync",
            "/v1/meta/sync/{run_id}",
            "/v1/meta/datasets/{dataset_id}/snapshot",
            "/healthz",
        }
        missing = expected_subset - paths
        assert not missing, f"routes missing from app: {missing}"
