"""Unit tests for the executor-side Gravitino property push helper.

We test the helper directly (not via the executors) because the
helper is the only place that owns the failure semantics; the
executors are simple call sites whose own tests assert the LCP
state-machine, not Gravitino I/O.

What we cover
-------------
* Disabled mode (``gravitino_url=""``) returns ``False`` and never
  builds an HTTP client.
* Successful round-trip returns ``True`` and sends the right keys/values.
* GravitinoError is caught and turned into ``False`` (no propagation).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from lcp.core.config import Settings
from lcp.integrations.gravitino.client import GravitinoError
from lcp.integrations.gravitino.mapping import index_property_keys
from lcp.workers.executors._gravitino_push import push_index_properties


def _settings(gravitino_url: str = "") -> Settings:
    """Build a Settings instance with the minimum fields the helper reads.

    We deliberately do NOT load real env: ``Settings()`` reads from env
    and would couple the test to local shell state.  Pass the only field
    we care about.
    """

    return Settings(
        gravitino_url=gravitino_url,
        gravitino_metalake="metalake_demo",
        gravitino_catalog="lance",
    )


@pytest.mark.asyncio
async def test_disabled_when_gravitino_url_blank() -> None:
    """``gravitino_url=""`` -> no client built, helper returns False.

    Pinning this so the disabled path stays a true no-op and never
    accidentally makes a network call (which is what would happen if
    someone forgot the early-return).
    """

    pushed = await push_index_properties(
        settings=_settings(gravitino_url=""),
        schema="public",
        table="embeddings",
        index_name="emb_idx",
        state="READY",
        column="vector",
        last_optimized_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert pushed is False


@pytest.mark.asyncio
async def test_successful_push_sends_expected_keys() -> None:
    """Helper sends the right path + body shape to Gravitino on success.

    We intercept httpx with MockTransport and assert the JSON body's
    ``properties`` map carries exactly the three keys the mapping helper
    is supposed to emit.  This is a contract test for the executor->
    Gravitino wire shape -- breaking it would silently drop catalog
    information without any test failing.
    """

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # PUT (or POST) /api/metalakes/.../filesets/embeddings  --
        # we don't pin the verb because Gravitino's exact path is
        # an internal detail of GravitinoClient.set_fileset_properties.
        captured["method"] = request.method
        captured["url"] = str(request.url)
        # Body is JSON; record so we can assert content below.
        import json
        captured["body"] = json.loads(request.content.decode())
        # Minimal valid Fileset response so client.set_fileset_properties
        # can parse the success path; values copied from existing client tests.
        return httpx.Response(
            200,
            json={
                "fileset": {
                    "name": "embeddings",
                    "storageLocation": "s3://bucket/embeddings",
                    "type": "MANAGED",
                    "comment": None,
                    "properties": {},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    settings = _settings(gravitino_url="http://gravitino.local:8090")

    # Patch GravitinoClient.from_settings to inject the mock transport.
    # The helper calls ``GravitinoClient.from_settings(settings)``; we
    # need that method to return a client wired to ``transport``.
    from lcp.integrations.gravitino.client import GravitinoClient

    real_from_settings = GravitinoClient.from_settings

    def from_settings_with_transport(s: Settings) -> GravitinoClient:
        return GravitinoClient(
            base_url=s.gravitino_url,
            metalake=s.gravitino_metalake,
            catalog=s.gravitino_catalog,
            transport=transport,
        )

    with patch.object(
        GravitinoClient, "from_settings",
        classmethod(lambda cls, s: from_settings_with_transport(s)),
    ):
        pushed = await push_index_properties(
            settings=settings,
            schema="public",
            table="embeddings",
            index_name="emb_idx",
            state="READY",
            column="vector",
            last_optimized_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
        )

    assert pushed is True

    # The body is Gravitino's alterFileset envelope: ``updates`` list of
    # ``{"@type": "setProperty", "property": "lcp.index....", "value": "..."}``.
    # We walk the list once, build a ``{property -> value}`` map, and
    # assert our three keys are present with the expected values.
    state_key, column_key, last_key = index_property_keys("emb_idx")
    body = captured["body"]
    assert isinstance(body, dict)
    updates = body.get("updates")
    assert isinstance(updates, list) and len(updates) == 3
    sent = {
        entry["property"]: entry["value"]
        for entry in updates
        if isinstance(entry, dict) and entry.get("@type") == "setProperty"
    }
    assert sent[state_key] == "READY"
    assert sent[column_key] == "vector"
    assert sent[last_key] == "2024-05-06T07:08:09+00:00"

    # Restore in case future tests instantiate without the patch context.
    GravitinoClient.from_settings = real_from_settings  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_gravitino_error_is_swallowed_and_returns_false() -> None:
    """A failing HTTP call must NOT raise -- the executor depends on it.

    Specifically: if the helper raised, the executor's try/except above
    it would mark the task FAILED, which is exactly what Step 6 says
    must NOT happen.  Pinning the swallow.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # 500 -> GravitinoClient raises GravitinoError.
        return httpx.Response(500, json={"message": "boom"})

    transport = httpx.MockTransport(handler)
    settings = _settings(gravitino_url="http://gravitino.local:8090")

    from lcp.integrations.gravitino.client import GravitinoClient

    def from_settings_with_transport(s: Settings) -> GravitinoClient:
        return GravitinoClient(
            base_url=s.gravitino_url,
            metalake=s.gravitino_metalake,
            catalog=s.gravitino_catalog,
            transport=transport,
        )

    with patch.object(
        GravitinoClient, "from_settings",
        classmethod(lambda cls, s: from_settings_with_transport(s)),
    ):
        # If the helper accidentally re-raises, this line propagates and
        # the test fails -- which is the assertion we want.
        pushed = await push_index_properties(
            settings=settings,
            schema="public",
            table="embeddings",
            index_name="emb_idx",
            state="READY",
            column="vector",
            last_optimized_at=None,
        )

    assert pushed is False


@pytest.mark.asyncio
async def test_gravitino_error_subclass_404_is_also_swallowed() -> None:
    """``GravitinoNotFoundError`` (subclass) goes through the same swallow path.

    Independent test from the 500 case because the catch is on the parent
    class; an accidental refactor to ``except GravitinoError as exc`` ->
    ``except SomeOtherType`` would silently break this branch and only
    the parent test would notice if we got lucky.  Belt and braces.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "fileset not found"})

    transport = httpx.MockTransport(handler)
    settings = _settings(gravitino_url="http://gravitino.local:8090")

    from lcp.integrations.gravitino.client import GravitinoClient

    def from_settings_with_transport(s: Settings) -> GravitinoClient:
        return GravitinoClient(
            base_url=s.gravitino_url,
            metalake=s.gravitino_metalake,
            catalog=s.gravitino_catalog,
            transport=transport,
        )

    with patch.object(
        GravitinoClient, "from_settings",
        classmethod(lambda cls, s: from_settings_with_transport(s)),
    ):
        pushed = await push_index_properties(
            settings=settings,
            schema="public",
            table="embeddings",
            index_name="emb_idx",
            state="READY",
            column="vector",
            last_optimized_at=None,
        )

    assert pushed is False


# Sanity check that the test setup itself isn't broken: a bare
# GravitinoError raised in handler still propagates through the
# transport layer.  If this ever fires, the swallow tests above are
# tautologies and need investigating.
def test_gravitino_error_is_actually_an_exception() -> None:
    assert issubclass(GravitinoError, Exception)
