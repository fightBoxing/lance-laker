"""Unit tests for :mod:`lcp.services.meta_sync_service`.

Strategy:
    * Use an in-memory sqlite session (matches existing service tests).
    * Mock the Gravitino client with a hand-rolled stub that only implements
      the three async methods the service calls.  Avoids the brittle
      bookkeeping of ``unittest.mock.AsyncMock(spec=...)`` for protocol
      methods.

Coverage targets:
    * Happy path: Gravitino has the fileset, LCP gets patched, properties
      pushed, ``Dataset.extra['gravitino']`` stamped.
    * Idempotency: a second pass with no changes still stamps ``last_synced_at``
      but ``changed`` is False.
    * Negative: 404 maps to ``not_in_gravitino`` outcome and does not write
      LCP.
    * Negative: get_fileset transport error -> ``error`` outcome.
    * Property push error: LCP patch persists even if property write fails
      (we report the failure but do not roll back the LCP patch).
    * ``push_properties=False`` skips the write-back.
    * ``reconcile_all`` aggregates outcomes correctly.
    * ``discover_unknown_filesets`` returns Gravitino-only names.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.db.models import Base, Dataset
from lcp.integrations.gravitino.client import (
    Fileset,
    GravitinoError,
    GravitinoNotFoundError,
)
from lcp.services import meta_sync_service
from lcp.services.meta_sync_service import (
    EXTRA_KEY,
    DatasetSyncOutcome,
    SyncReport,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory sqlite, single-connection so transactions are visible."""

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed(
    session: AsyncSession,
    *,
    table: str = "embeddings",
    storage_uri: str = "s3://bucket/old",
    description: str | None = "old",
) -> Dataset:
    """Insert a dataset row and return it."""

    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        catalog="lance",
        db_schema="public",
        table_name=table,
        storage_uri=storage_uri,
        tenant_id="acme",
        status="ACTIVE",
        row_count=10,
        fragment_count=2,
        index_coverage=Decimal("0.5000"),
        latest_version=3,
        description=description,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


def _fileset(
    *,
    name: str = "embeddings",
    storage_location: str = "s3://bucket/new",
    comment: str | None = "fresh",
    properties: dict[str, str] | None = None,
) -> Fileset:
    raw: dict[str, Any] = {
        "name": name,
        "storageLocation": storage_location,
        "type": "MANAGED",
        "comment": comment,
        "properties": properties or {},
    }
    return Fileset(
        name=name,
        storage_location=storage_location,
        fileset_type="MANAGED",
        comment=comment,
        properties=properties or {},
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Stub client (lightweight, no AsyncMock pyramid)
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal stand-in for :class:`GravitinoClient`.

    Tests configure ``get_response`` (Fileset or Exception) and
    ``set_response`` (Fileset or Exception) and read back ``calls`` to
    assert what the service did.
    """

    def __init__(self) -> None:
        self.get_response: Fileset | Exception | None = None
        self.set_response: Fileset | Exception | None = None
        self.list_response: list[str] = []
        self.calls: list[tuple[str, str, str]] = []

    async def get_fileset(self, schema: str, name: str) -> Fileset:
        self.calls.append(("get", schema, name))
        if isinstance(self.get_response, Exception):
            raise self.get_response
        assert self.get_response is not None, "get_response not configured"
        return self.get_response

    async def list_filesets(self, schema: str) -> list[str]:
        self.calls.append(("list", schema, ""))
        return list(self.list_response)

    async def set_fileset_properties(
        self, schema: str, name: str, properties: dict[str, str],
    ) -> Fileset:
        # Stash ``properties`` so a test can assert the wire shape.
        self.calls.append(("set", schema, name))
        self.last_set_properties = properties
        if isinstance(self.set_response, Exception):
            raise self.set_response
        # Echo the get_response (or a synthetic) so the call returns a Fileset.
        if isinstance(self.set_response, Fileset):
            return self.set_response
        return _fileset(name=name, properties=properties)


# ---------------------------------------------------------------------------
# reconcile_dataset
# ---------------------------------------------------------------------------


class TestReconcileDataset:

    async def test_happy_path_patches_lcp_and_pushes_properties(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session, storage_uri="s3://old", description="old")
        client = _StubClient()
        client.get_response = _fileset(
            storage_location="s3://new", comment="fresh",
        )
        client.set_response = client.get_response

        fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        outcome = await meta_sync_service.reconcile_dataset(
            session, client, ds, now=fixed,
        )

        assert outcome.status == "synced"
        assert outcome.changed is True
        assert outcome.pushed_properties is True

        # LCP was patched.
        await session.refresh(ds)
        assert ds.storage_uri == "s3://new"
        assert ds.description == "fresh"

        # Sync metadata stamped into ``extra``.
        assert ds.extra is not None
        sync_meta = ds.extra[EXTRA_KEY]
        assert sync_meta["last_storage_location"] == "s3://new"
        assert sync_meta["last_pushed_properties"] is True
        assert sync_meta["last_synced_at"] == "2024-01-02T03:04:05+00:00"

        # Properties were pushed; assert key shape.
        assert {c[0] for c in client.calls} == {"get", "set"}
        from lcp.integrations.gravitino.mapping import (
            PROP_DATASET_UUID,
            PROP_SYNCED_AT,
        )
        assert PROP_DATASET_UUID in client.last_set_properties
        assert PROP_SYNCED_AT in client.last_set_properties

    async def test_second_pass_is_idempotent_when_already_in_sync(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session, storage_uri="s3://same", description="same")
        client = _StubClient()
        client.get_response = _fileset(
            storage_location="s3://same", comment="same",
        )

        first = await meta_sync_service.reconcile_dataset(session, client, ds)
        second = await meta_sync_service.reconcile_dataset(session, client, ds)

        assert first.status == "synced"
        # No catalog-side change because storage and comment matched.
        assert first.changed is False
        assert second.changed is False
        # But properties are pushed every time so Gravitino UI's
        # ``last_synced_at`` keeps advancing.
        assert first.pushed_properties is True
        assert second.pushed_properties is True

    async def test_404_marks_dataset_as_not_in_gravitino(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session)
        client = _StubClient()
        client.get_response = GravitinoNotFoundError("missing", status=404)

        outcome = await meta_sync_service.reconcile_dataset(session, client, ds)

        assert outcome.status == "not_in_gravitino"
        assert outcome.changed is False
        assert outcome.pushed_properties is False
        # No property push attempted -> only one call (the GET).
        assert [c[0] for c in client.calls] == ["get"]
        # LCP row untouched.
        await session.refresh(ds)
        assert ds.storage_uri == "s3://bucket/old"
        # ``extra`` was not stamped because we never reached that point.
        assert ds.extra is None

    async def test_transport_error_on_get_yields_error_outcome(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session)
        client = _StubClient()
        client.get_response = GravitinoError("network down", status=None)

        outcome = await meta_sync_service.reconcile_dataset(session, client, ds)

        assert outcome.status == "error"
        assert "network down" in (outcome.error or "")
        # No property push attempted.
        assert [c[0] for c in client.calls] == ["get"]

    async def test_property_push_failure_keeps_lcp_patch(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session, storage_uri="s3://old")
        client = _StubClient()
        client.get_response = _fileset(storage_location="s3://new")
        client.set_response = GravitinoError("push failed", status=500)

        outcome = await meta_sync_service.reconcile_dataset(session, client, ds)

        # We surface the error but did NOT roll back the LCP patch.
        assert outcome.status == "error"
        assert outcome.changed is True
        assert outcome.pushed_properties is False
        # LCP did get the new storage_uri.
        await session.refresh(ds)
        assert ds.storage_uri == "s3://new"

    async def test_push_properties_false_skips_set(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session)
        client = _StubClient()
        client.get_response = _fileset(storage_location="s3://new")

        outcome = await meta_sync_service.reconcile_dataset(
            session, client, ds, push_properties=False,
        )

        assert outcome.status == "synced"
        assert outcome.pushed_properties is False
        assert [c[0] for c in client.calls] == ["get"]


# ---------------------------------------------------------------------------
# reconcile_all
# ---------------------------------------------------------------------------


class TestReconcileAll:

    async def test_aggregates_outcomes(self, session: AsyncSession) -> None:
        # Seed 3 datasets in two schemas.  We'll filter to one schema and
        # arrange the stub so one is synced, one missing, one errors out.
        ds_ok = await _seed(session, table="ok")
        ds_missing = await _seed(session, table="missing")
        ds_err = await _seed(session, table="boom")

        # Stub returns different responses per call.  ``_StubClient`` only
        # supports one response at a time, so we route via a side effect.
        class _RoutingClient(_StubClient):
            def __init__(self) -> None:
                super().__init__()
                # Map (schema, name) -> response
                self.routing: dict[tuple[str, str], Fileset | Exception] = {}

            async def get_fileset(self, schema: str, name: str) -> Fileset:
                self.calls.append(("get", schema, name))
                resp = self.routing[(schema, name)]
                if isinstance(resp, Exception):
                    raise resp
                return resp

        client = _RoutingClient()
        client.routing[("public", "ok")] = _fileset(
            name="ok", storage_location="s3://bucket/ok", comment="x",
        )
        client.routing[("public", "missing")] = GravitinoNotFoundError(
            "missing", status=404,
        )
        client.routing[("public", "boom")] = GravitinoError(
            "boom", status=500,
        )

        report: SyncReport = await meta_sync_service.reconcile_all(
            session, client, schema="public",
        )

        assert report.total == 3
        assert report.synced == 1
        assert report.missing == 1
        assert report.errors == 1
        # Items preserve insertion order (id ASC).
        ordered = [(o.dataset_uuid, o.status) for o in report.items]
        assert [s for _, s in ordered] == ["synced", "not_in_gravitino", "error"]
        # Sanity: the synced one is ``ds_ok``.
        assert ordered[0][0] == ds_ok.dataset_uuid
        assert ordered[1][0] == ds_missing.dataset_uuid
        assert ordered[2][0] == ds_err.dataset_uuid


# ---------------------------------------------------------------------------
# discover_unknown_filesets
# ---------------------------------------------------------------------------


class TestDiscoverUnknownFilesets:

    async def test_returns_only_filesets_not_in_lcp(
        self, session: AsyncSession,
    ) -> None:
        await _seed(session, table="known")
        client = _StubClient()
        client.list_response = ["known", "unknown_a", "unknown_b"]

        names = await meta_sync_service.discover_unknown_filesets(
            client, session, schema="public",
        )

        assert names == ["unknown_a", "unknown_b"]

    async def test_empty_gravitino_returns_empty_without_db_query(
        self, session: AsyncSession,
    ) -> None:
        await _seed(session, table="known")
        client = _StubClient()
        client.list_response = []

        names = await meta_sync_service.discover_unknown_filesets(
            client, session, schema="public",
        )

        assert names == []
        # Only the list call happened.
        assert [c[0] for c in client.calls] == ["list"]


# ---------------------------------------------------------------------------
# Counter shape regression
# ---------------------------------------------------------------------------


class TestSyncReport:
    """Lock the report counter rules so the REST schema can rely on them."""

    def test_record_increments_correct_buckets(self) -> None:
        report = SyncReport()
        report.record(DatasetSyncOutcome("a", "synced", changed=True, pushed_properties=True))
        report.record(DatasetSyncOutcome("b", "synced", changed=False, pushed_properties=True))
        report.record(DatasetSyncOutcome("c", "not_in_gravitino"))
        report.record(DatasetSyncOutcome("d", "error", error="boom"))

        assert report.total == 4
        assert report.synced == 2
        assert report.changed == 1
        assert report.pushed == 2
        assert report.missing == 1
        assert report.errors == 1
