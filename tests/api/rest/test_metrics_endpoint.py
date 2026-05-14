"""Tests for the REST API ``/metrics`` endpoint.

Two contracts the endpoint must keep:

1. **Public**: scrape works without an Authorization header (Prometheus
   does not present one).
2. **Format**: Content-Type is the Prometheus text-format media type
   so a regression to ``application/json`` is caught immediately.

Why ASGITransport rather than ``fastapi.testclient.TestClient``:
    The repo-wide convention (see :mod:`tests.api.rest.test_auth`) is
    to drive the app through ``httpx.ASGITransport`` so we stay on a
    single anyio event loop and avoid the resource-warning noise that
    TestClient's worker-thread teardown produces under
    ``filterwarnings=["error"]``.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from lcp.api.rest.main import create_app
from lcp.observability import (
    CONTENT_TYPE_LATEST,
    WATCHER_PASS_TOTAL,
    reset_registry,
)


async def test_metrics_endpoint_anonymous_scrape_returns_200() -> None:
    """No Authorization header -> 200 + prom text-format payload."""

    reset_registry()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    # Spec compliance: Prometheus expects exactly this text-format media type.
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


async def test_metrics_endpoint_includes_lcp_namespace_metrics() -> None:
    """The body must list our metric names so dashboards can query them."""

    reset_registry()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")

    body = response.text
    assert "lcp_build_info" in body
    assert "lcp_watcher_pass_total" in body
    assert "lcp_task_finished_total" in body
    assert "lcp_gravitino_request_duration_seconds" in body


async def test_metrics_endpoint_reflects_runtime_emissions() -> None:
    """A counter incremented at runtime must appear in /metrics output."""

    reset_registry()
    app = create_app()
    transport = ASGITransport(app=app)
    # Stamp a counter *before* the scrape so the body proves the
    # registry singleton is shared across the call site and the route.
    WATCHER_PASS_TOTAL().labels(result="success").inc()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")

    body = response.text
    assert 'lcp_watcher_pass_total{result="success"} 1.0' in body
    # Note: we deliberately do not assert ``component="rest_api"`` on
    # ``lcp_build_info`` here.  That label is set by the FastAPI
    # ``lifespan`` startup hook, which ASGITransport does not run by
    # default; verifying it would test transport plumbing, not the
    # ``/metrics`` endpoint contract.  The component label is exercised
    # by the live container's startup logs and by integration tests
    # that go through the real uvicorn entry-point.


async def test_metrics_endpoint_does_not_require_auth() -> None:
    """Even with the OIDC middleware enabled, /metrics must not 401.

    Cross-checks against a known-protected route to confirm the auth
    layer is wired and the bypass list actually applies.
    """

    reset_registry()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        protected = await client.get("/v1/datasets")
        scrape = await client.get("/metrics")

    assert protected.status_code == 401, (
        "sanity check: OIDC middleware should reject unauthenticated requests"
    )
    assert scrape.status_code == 200
