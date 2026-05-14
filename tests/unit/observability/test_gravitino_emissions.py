"""Tests verifying GravitinoClient emits request-duration metrics.

Why these live in tests/unit/observability rather than next to the
client tests:
    The contract being tested is "observability writes happen", not
    "the client behaves correctly".  Splitting keeps each test file
    responsible for one contract -- a regression in either fails the
    test next to its intent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lcp.integrations.gravitino import GravitinoClient
from lcp.integrations.gravitino.client import GravitinoAuthError, GravitinoNotFoundError
from lcp.observability import render_latest, reset_registry


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    reset_registry()


def _make_client(handler: Any) -> GravitinoClient:
    """Build a GravitinoClient backed by an httpx MockTransport."""

    transport = httpx.MockTransport(handler)
    return GravitinoClient(
        base_url="http://gravitino.test:8090",
        metalake="lance_laker",
        catalog="lance_oss",
        auth_type="none",
        token="",
        timeout_seconds=2.0,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


async def test_ping_records_success_observation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "catalog": {"name": "lance_oss"}},
        )

    async with _make_client(handler) as client:
        await client.ping()

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="ping",result="success"} 1.0'
        in body
    )


async def test_list_filesets_records_success_observation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"identifiers": []})

    async with _make_client(handler) as client:
        await client.list_filesets("public")

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="list_filesets",result="success"} 1.0'
        in body
    )


async def test_set_fileset_properties_records_success_observation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # The client tolerates wrapped + flat shapes; minimal valid body.
        return httpx.Response(
            200,
            json={
                "fileset": {
                    "name": "f",
                    "type": "managed",
                    "storageLocation": "s3://b/p",
                    "properties": {"k": "v"},
                },
            },
        )

    async with _make_client(handler) as client:
        await client.set_fileset_properties("public", "f", {"k": "v"})

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count'
        '{op="set_fileset_properties",result="success"} 1.0'
        in body
    )


# ---------------------------------------------------------------------------
# error paths -- result label must distinguish them
# ---------------------------------------------------------------------------


async def test_ping_404_records_not_found_label() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="catalog gone")

    async with _make_client(handler) as client:
        with pytest.raises(GravitinoNotFoundError):
            await client.ping()

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="ping",result="not_found"} 1.0'
        in body
    )


async def test_list_filesets_401_records_auth_label() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    async with _make_client(handler) as client:
        with pytest.raises(GravitinoAuthError):
            await client.list_filesets("public")

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="list_filesets",result="auth"} 1.0'
        in body
    )


async def test_get_fileset_500_records_error_label() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    from lcp.integrations.gravitino.client import GravitinoError

    async with _make_client(handler) as client:
        with pytest.raises(GravitinoError):
            await client.get_fileset("public", "f")

    body = render_latest().decode()
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="get_fileset",result="error"} 1.0'
        in body
    )
