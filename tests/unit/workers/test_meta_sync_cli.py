"""Unit tests for :mod:`lcp.workers.meta_sync_cli`.

We test three layers:

1. ``_build_parser`` and ``_validate_args``      — pure CLI arg parsing.
2. ``_reconcile``                                  — scope dispatch logic.
3. ``main``                                        — early-exit when
   ``LCP_GRAVITINO_URL`` is empty (the CronJob no-op contract).

Async ``_run`` end-to-end with a real DB + GravitinoClient is covered
indirectly by ``tests/unit/services/test_meta_sync_service.py`` plus
the router test; we do not duplicate that here.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.db.models import Base, Dataset
from lcp.integrations.gravitino.client import Fileset
from lcp.workers import meta_sync_cli

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
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


def _fileset(name: str = "t1") -> Fileset:
    raw = {
        "name": name,
        "storageLocation": "s3://bucket/" + name,
        "type": "MANAGED",
        "comment": None,
        "properties": {},
    }
    return Fileset(
        name=name,
        storage_location="s3://bucket/" + name,
        fileset_type="MANAGED",
        comment=None,
        properties={},
        raw=raw,
    )


class _StubClient:
    """Minimal stand-in for :class:`GravitinoClient` (sync-API parity).

    Reuses the shape from the meta_sync_service tests but is local here
    so the CLI test file is self-contained.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def get_fileset(self, schema: str, name: str) -> Fileset:
        self.calls.append(("get", schema, name))
        return _fileset(name)

    async def list_filesets(self, schema: str) -> list[str]:
        self.calls.append(("list", schema, ""))
        return []

    async def set_fileset_properties(
        self, schema: str, name: str, properties: dict[str, str],
    ) -> Fileset:
        self.calls.append(("set", schema, name))
        return _fileset(name)


async def _seed(session: AsyncSession, *, table: str = "t1") -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        catalog="lance",
        db_schema="public",
        table_name=table,
        storage_uri="s3://bucket/old",
        tenant_id="acme",
        status="ACTIVE",
        row_count=0,
        fragment_count=0,
        index_coverage=Decimal("0.0000"),
        latest_version=1,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


# ---------------------------------------------------------------------------
# argparse + validation
# ---------------------------------------------------------------------------


class TestArgs:

    def test_default_scope_is_all_with_push(self) -> None:
        args = meta_sync_cli._build_parser().parse_args([])
        assert args.scope == "ALL"
        assert args.push_properties is True

    def test_no_push_disables_property_writeback(self) -> None:
        args = meta_sync_cli._build_parser().parse_args(["--no-push"])
        assert args.push_properties is False

    def test_push_and_no_push_are_mutually_exclusive(self) -> None:
        # argparse exits with code 2 (the "argument error" convention)
        # when mutually exclusive flags collide.
        with pytest.raises(SystemExit) as exc:
            meta_sync_cli._build_parser().parse_args(["--push", "--no-push"])
        assert exc.value.code == 2

    def test_schema_scope_requires_schema_flag(self) -> None:
        args = meta_sync_cli._build_parser().parse_args(["--scope", "SCHEMA"])
        with pytest.raises(SystemExit):
            meta_sync_cli._validate_args(args)

    def test_dataset_scope_requires_uuid(self) -> None:
        args = meta_sync_cli._build_parser().parse_args(["--scope", "DATASET"])
        with pytest.raises(SystemExit):
            meta_sync_cli._validate_args(args)

    def test_invalid_scope_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit):
            meta_sync_cli._build_parser().parse_args(["--scope", "CATALOG"])


# ---------------------------------------------------------------------------
# _reconcile dispatch
# ---------------------------------------------------------------------------


class TestReconcileDispatch:

    async def test_scope_all_calls_reconcile_all(
        self, session: AsyncSession,
    ) -> None:
        await _seed(session, table="t1")
        client = _StubClient()

        args = argparse.Namespace(
            scope="ALL",
            schema=None,
            dataset_uuid=None,
            push_properties=True,
        )
        report = await meta_sync_cli._reconcile(session, client, args)

        assert report.total == 1
        assert report.synced == 1
        # ALL scope iterates every dataset (one in this fixture).
        assert any(c[0] == "get" for c in client.calls)

    async def test_scope_schema_filters(self, session: AsyncSession) -> None:
        await _seed(session, table="public_a")
        # Seed a row in a different schema we should NOT touch.
        other = await _seed(session, table="other_a")
        other.db_schema = "other"
        await session.commit()

        client = _StubClient()
        args = argparse.Namespace(
            scope="SCHEMA",
            schema="public",
            dataset_uuid=None,
            push_properties=True,
        )
        report = await meta_sync_cli._reconcile(session, client, args)

        assert report.total == 1
        # Sanity: no GET against the other schema's table.
        assert all(c[2] != "other_a" for c in client.calls if c[0] == "get")

    async def test_scope_dataset_unknown_yields_empty_report(
        self, session: AsyncSession,
    ) -> None:
        client = _StubClient()
        args = argparse.Namespace(
            scope="DATASET",
            schema=None,
            dataset_uuid="does-not-exist",
            push_properties=True,
        )
        report = await meta_sync_cli._reconcile(session, client, args)

        # Empty report (not an exception) keeps the cron job exit-0
        # contract intact when an operator mistypes a UUID.
        assert report.total == 0
        # No Gravitino calls because the LCP lookup failed first.
        assert client.calls == []

    async def test_scope_dataset_known_records_outcome(
        self, session: AsyncSession,
    ) -> None:
        ds = await _seed(session, table="t1")
        client = _StubClient()
        args = argparse.Namespace(
            scope="DATASET",
            schema=None,
            dataset_uuid=ds.dataset_uuid,
            push_properties=True,
        )
        report = await meta_sync_cli._reconcile(session, client, args)

        assert report.total == 1
        assert report.items[0].dataset_uuid == ds.dataset_uuid


# ---------------------------------------------------------------------------
# main: early-exit when Gravitino unconfigured
# ---------------------------------------------------------------------------


class TestMainEarlyExit:

    def test_main_exits_zero_when_gravitino_url_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Ensure no env override leaks in from a sibling test that set
        # LCP_GRAVITINO_URL via monkeypatch; we want the *default* empty.
        monkeypatch.delenv("LCP_GRAVITINO_URL", raising=False)
        from lcp.core.config import get_settings
        get_settings.cache_clear()

        rc = meta_sync_cli.main([])

        # Cron-friendly contract: empty URL must not crash-loop.
        assert rc == 0
