"""REST auth middleware — interface tests.

This is **the** H-2 regression: the pure-ASGI middleware must set the
tenant contextvar in a scope that propagates into the route handler.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI


pytestmark = pytest.mark.api


# ---------------------------------------------------------------------------
# Public-path bypass
# ---------------------------------------------------------------------------


class TestPublicPaths:

    async def test_healthz_is_public(self, http_client: Any) -> None:
        resp = await http_client.get("/healthz")
        assert resp.status_code == 200

    async def test_docs_is_public(
        self,
        configured_settings: None,
        patch_jwks: None,
    ) -> None:
        """M-2 regression: /docs and its static assets must not 401."""

        del configured_settings, patch_jwks
        from httpx import ASGITransport, AsyncClient

        from lcp.api.rest.auth import OIDCAuthMiddleware

        app = FastAPI(title="t")
        app.add_middleware(OIDCAuthMiddleware)

        # Hit the built-in OpenAPI schema endpoint, which lives under /openapi.json
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.get("/openapi.json")
            assert r1.status_code == 200


# ---------------------------------------------------------------------------
# Missing / malformed credentials
# ---------------------------------------------------------------------------


class TestUnauthenticated:

    async def test_returns_401_when_header_missing(self, http_client: Any) -> None:
        resp = await http_client.get("/v1/datasets")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert resp.json()["detail"] == "Missing Bearer token"

    async def test_returns_401_for_non_bearer_scheme(self, http_client: Any) -> None:
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": "Basic abc"},
        )
        assert resp.status_code == 401

    async def test_returns_401_for_malformed_token(self, http_client: Any) -> None:
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": "Bearer garbage.not.jwt"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Valid token -> contextvar propagation (H-2 regression)
# ---------------------------------------------------------------------------


class TestContextvarPropagation:

    async def test_valid_token_binds_tenant_for_handler(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        """If contextvar did not propagate, the handler would have no tenant."""

        token = issue_token(tenant_id="acme-corp")
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token}"},
        )
        # datasets.list_datasets echoes the tenant in the payload.
        assert resp.status_code == 200
        assert resp.json()["tenant"] == "acme-corp"

    async def test_different_tenants_isolated(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token_a = issue_token(tenant_id="tenant-a")
        token_b = issue_token(tenant_id="tenant-b")

        resp_a = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        resp_b = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp_a.json()["tenant"] == "tenant-a"
        assert resp_b.json()["tenant"] == "tenant-b"


# ---------------------------------------------------------------------------
# Expired / wrong-issuer tokens
# ---------------------------------------------------------------------------


class TestTokenValidation:

    async def test_expired_token_rejected(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(exp_offset=-3600)
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    async def test_wrong_issuer_rejected(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        token = issue_token(issuer="https://evil.test")
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    async def test_token_without_tenant_claim_rejected(
        self,
        http_client: Any,
        issue_token: Any,
    ) -> None:
        """M-1 regression: a JWT with only 'sub' must not be accepted."""

        # Empty tenant_id is skipped by extract_tenant_from_claims, which
        # then refuses to fall back to 'sub' (no-sub-fallback contract).
        token = issue_token(tenant_id="")
        resp = await http_client.get(
            "/v1/datasets",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
