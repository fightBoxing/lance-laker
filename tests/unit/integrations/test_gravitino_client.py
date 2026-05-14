"""Unit tests for :mod:`lcp.integrations.gravitino.client`.

Strategy: drive the async client through ``httpx.MockTransport`` so the
suite stays hermetic — no live Gravitino, no localhost binding.

Coverage targets:

1. URL composition matches the Gravitino REST contract.
2. JSON parsing is tolerant of both the wrapped and flat response shapes.
3. HTTP status codes translate to the typed exception hierarchy.
4. ``set_fileset_properties`` emits the ``setProperty`` update list shape.
5. Auth header is attached for ``bearer`` and absent for ``none``.
6. Misconfiguration (empty url / unsupported auth / empty token) fails
   loudly at construction time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lcp.integrations.gravitino.client import (
    Fileset,
    GravitinoAuthError,
    GravitinoClient,
    GravitinoError,
    GravitinoNotFoundError,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    auth_type: str = "none",
    token: str = "",
) -> GravitinoClient:
    """Return a client wired to a ``MockTransport`` that delegates to ``handler``."""

    return GravitinoClient(
        base_url="http://gravitino.test:8090",
        metalake="lance_laker",
        catalog="lance_oss",
        auth_type=auth_type,
        token=token,
        timeout_seconds=2.0,
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# Construction-time validation
# ---------------------------------------------------------------------------


async def test_constructor_rejects_empty_base_url() -> None:
    with pytest.raises(GravitinoError):
        GravitinoClient(
            base_url="",
            metalake="lance_laker",
            catalog="lance_oss",
        )


async def test_constructor_rejects_unsupported_auth() -> None:
    with pytest.raises(NotImplementedError):
        GravitinoClient(
            base_url="http://x",
            metalake="m",
            catalog="c",
            auth_type="basic",
        )


async def test_constructor_rejects_bearer_without_token() -> None:
    with pytest.raises(GravitinoError):
        GravitinoClient(
            base_url="http://x",
            metalake="m",
            catalog="c",
            auth_type="bearer",
            token="",
        )


# ---------------------------------------------------------------------------
# URL + auth composition
# ---------------------------------------------------------------------------


async def test_list_filesets_hits_expected_path_and_returns_names() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "identifiers": [
                    {"namespace": ["lance_laker", "lance_oss", "public"], "name": "alpha"},
                    {"namespace": ["lance_laker", "lance_oss", "public"], "name": "beta"},
                ],
            },
        )

    async with _make_client(handler) as client:
        names = await client.list_filesets("public")

    assert names == ["alpha", "beta"]
    assert captured["url"].endswith(
        "/api/metalakes/lance_laker/catalogs/lance_oss/schemas/public/filesets",
    )
    # auth_type=none should not attach an Authorization header
    assert captured["auth"] is None


async def test_bearer_auth_attaches_authorization_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"identifiers": []})

    async with _make_client(handler, auth_type="bearer", token="s3cr3t") as client:
        await client.list_filesets("public")

    assert captured["auth"] == "Bearer s3cr3t"


# ---------------------------------------------------------------------------
# Parsing — wrapped vs flat shapes
# ---------------------------------------------------------------------------


def _wrapped_fileset_payload() -> dict[str, Any]:
    return {
        "fileset": {
            "name": "embeddings",
            "type": "MANAGED",
            "comment": "demo dataset",
            "storageLocation": "s3://lance/embeddings",
            "properties": {"owner": "alice", "lcp.synced_at": "2024-01-01T00:00:00Z"},
            # Future-proofing: an unknown top-level field must not break us.
            "auditInfo": {"creator": "bob"},
        },
    }


async def test_get_fileset_parses_wrapped_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_wrapped_fileset_payload())

    async with _make_client(handler) as client:
        fs = await client.get_fileset("public", "embeddings")

    assert isinstance(fs, Fileset)
    assert fs.name == "embeddings"
    assert fs.fileset_type == "MANAGED"
    assert fs.comment == "demo dataset"
    assert fs.storage_location == "s3://lance/embeddings"
    assert fs.properties["owner"] == "alice"
    # Raw payload retained for callers that need fields we have not surfaced.
    assert "auditInfo" in fs.raw


async def test_get_fileset_parses_flat_response() -> None:
    flat = _wrapped_fileset_payload()["fileset"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=flat)

    async with _make_client(handler) as client:
        fs = await client.get_fileset("public", "embeddings")

    assert fs.storage_location == "s3://lance/embeddings"


# ---------------------------------------------------------------------------
# Status code -> exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "exc_type"),
    [
        (404, GravitinoNotFoundError),
        (401, GravitinoAuthError),
        (403, GravitinoAuthError),
        (500, GravitinoError),
    ],
)
async def test_status_codes_translate_to_typed_exceptions(
    status_code: int, exc_type: type[GravitinoError],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="boom")

    async with _make_client(handler) as client:
        with pytest.raises(exc_type) as excinfo:
            await client.get_fileset("public", "missing")

    # All specialised exceptions retain the original status for caller logic.
    assert excinfo.value.status == status_code


async def test_transport_error_wraps_into_gravitino_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(GravitinoError) as excinfo:
            await client.list_filesets("public")

    # Transport-level failures have no HTTP status to attach.
    assert excinfo.value.status is None


# ---------------------------------------------------------------------------
# set_fileset_properties — request body shape
# ---------------------------------------------------------------------------


async def test_set_fileset_properties_emits_set_property_updates() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        # Echo back a fresh fileset reflecting the new properties.
        return httpx.Response(
            200,
            json={
                "fileset": {
                    "name": "embeddings",
                    "type": "MANAGED",
                    "comment": None,
                    "storageLocation": "s3://lance/embeddings",
                    "properties": {
                        "lcp.index.state": "READY",
                        "lcp.synced_at": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )

    async with _make_client(handler) as client:
        fs = await client.set_fileset_properties(
            "public",
            "embeddings",
            {
                "lcp.index.state": "READY",
                "lcp.synced_at": "2024-01-02T00:00:00Z",
            },
        )

    import json as _json
    body = _json.loads(captured["body"])

    assert captured["method"] == "PUT"
    assert "/filesets/embeddings" in captured["url"]
    # One ``setProperty`` entry per key, in declaration order.
    assert len(body["updates"]) == 2
    for entry in body["updates"]:
        assert entry["@type"] == "setProperty"
        assert "property" in entry
        assert "value" in entry
    assert fs.properties["lcp.index.state"] == "READY"


async def test_set_fileset_properties_rejects_empty_dict() -> None:
    # ``handler`` should never be reached.
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP must not be issued for empty patch")

    async with _make_client(handler) as client:
        with pytest.raises(GravitinoError):
            await client.set_fileset_properties("public", "embeddings", {})
