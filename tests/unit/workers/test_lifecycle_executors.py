"""Unit tests for the three lifecycle executors.

Each executor is exercised against an in-memory SQLite session.  The
TTL/COMPACTION executors are stubs and only need contract / payload
checks; the INDEX_OPTIMIZE executor is real and gets a state-machine
verification.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.db.models import Base, Dataset, Index, Task
from lcp.workers.lifecycle_executors import (
    CompactionExecutor,
    ExecutorResult,
    IndexOptimizeExecutor,
    TtlDeleteExecutor,
    build_default_registry,
)

# ---------------------------------------------------------------------------
# Fixtures
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


async def _make_dataset(
    session: AsyncSession, *, tenant_id: str = "acme",
) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        catalog="lance",
        db_schema="public",
        table_name=f"t_{uuid.uuid4().hex[:6]}",
        storage_uri="s3://bucket/path",
        status="READY",
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


def _make_task(
    *,
    dataset_uuid: str,
    tenant_id: str,
    task_type: str,
    params: dict | None,
) -> Task:
    return Task(
        task_uuid=str(uuid.uuid4()),
        task_type=task_type,
        dataset_uuid=dataset_uuid,
        tenant_id=tenant_id,
        status="RUNNING",
        priority=5,
        progress=Decimal("0.0000"),
        attempt=1,
        max_attempts=3,
        params=params,
    )


# ---------------------------------------------------------------------------
# TTL_DELETE
# ---------------------------------------------------------------------------


class TestTtlDeleteExecutor:

    async def test_returns_stub_payload(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="TTL_DELETE",
            params={"ttl_days": 30, "policy_name": "p1"},
        )

        result = await TtlDeleteExecutor().execute(
            session, task=task, dataset=ds,
        )

        assert isinstance(result, ExecutorResult)
        assert result.payload["executor"] == "TtlDeleteExecutor"
        assert result.payload["mode"] == "stub"
        assert result.payload["ttl_days"] == 30
        assert result.payload["dataset_uuid"] == ds.dataset_uuid
        assert "lance.LanceDataset.delete" in result.payload["would_call"]
        # Predicate preview is informational; just ensure ttl_days lands.
        assert "30 DAY" in result.payload["predicate_preview"]

    async def test_missing_ttl_days_raises(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="TTL_DELETE",
            params={"policy_name": "p1"},
        )

        with pytest.raises(ValueError, match="ttl_days"):
            await TtlDeleteExecutor().execute(
                session, task=task, dataset=ds,
            )


# ---------------------------------------------------------------------------
# COMPACTION
# ---------------------------------------------------------------------------


class TestCompactionExecutor:

    async def test_returns_stub_payload_with_threshold(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="COMPACTION",
            params={"threshold": {"small_files": 100}, "policy_name": "p1"},
        )

        result = await CompactionExecutor().execute(
            session, task=task, dataset=ds,
        )

        assert result.payload["executor"] == "CompactionExecutor"
        assert result.payload["mode"] == "stub"
        assert result.payload["threshold"] == {"small_files": 100}
        assert "compact_files" in result.payload["would_call"]

    async def test_missing_threshold_uses_empty_dict(
        self, session: AsyncSession,
    ) -> None:
        """Defensive: planner always sets threshold but stub must not crash."""

        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="COMPACTION",
            params=None,
        )

        result = await CompactionExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert result.payload["threshold"] == {}


# ---------------------------------------------------------------------------
# INDEX_OPTIMIZE  (real LCP-internal state machine)
# ---------------------------------------------------------------------------


class TestIndexOptimizeExecutor:

    async def _seed_index(
        self,
        session: AsyncSession,
        *,
        dataset_uuid: str,
        index_name: str,
        status: str,
    ) -> Index:
        idx = Index(
            dataset_uuid=dataset_uuid,
            index_name=index_name,
            index_type="IVF_PQ",
            column_name="embedding",
            status=status,
        )
        session.add(idx)
        await session.commit()
        await session.refresh(idx)
        return idx

    async def test_promotes_optimizing_indexes_back_to_ready(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        idx_opt = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid,
            index_name="idx_optimizing", status="OPTIMIZING",
        )
        idx_ready = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid,
            index_name="idx_ready", status="READY",
        )

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={"cron": "0 2 * * *", "policy_name": "p1"},
        )
        result = await IndexOptimizeExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        assert result.payload["executor"] == "IndexOptimizeExecutor"
        assert result.payload["mode"] == "real"
        assert result.payload["promoted_count"] == 1
        assert "idx_optimizing" in result.payload["indexes_promoted"]

        # OPTIMIZING -> READY, last_optimized_at set.
        await session.refresh(idx_opt)
        assert idx_opt.status == "READY"
        assert idx_opt.last_optimized_at is not None
        assert isinstance(idx_opt.last_optimized_at, datetime)

        # READY index unchanged.
        await session.refresh(idx_ready)
        assert idx_ready.status == "READY"

    async def test_no_optimizing_indexes_returns_zero(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )
        result = await IndexOptimizeExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert result.payload["promoted_count"] == 0
        assert result.payload["indexes_promoted"] == []

    async def test_isolated_to_dataset(
        self, session: AsyncSession,
    ) -> None:
        """OPTIMIZING indexes on *other* datasets must not be touched."""

        ds_a = await _make_dataset(session)
        ds_b = await _make_dataset(session)
        idx_a = await self._seed_index(
            session, dataset_uuid=ds_a.dataset_uuid,
            index_name="idx_a", status="OPTIMIZING",
        )
        idx_b = await self._seed_index(
            session, dataset_uuid=ds_b.dataset_uuid,
            index_name="idx_b", status="OPTIMIZING",
        )

        task = _make_task(
            dataset_uuid=ds_a.dataset_uuid,
            tenant_id=ds_a.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )
        await IndexOptimizeExecutor().execute(
            session, task=task, dataset=ds_a,
        )
        await session.commit()

        await session.refresh(idx_a)
        await session.refresh(idx_b)
        assert idx_a.status == "READY"
        assert idx_b.status == "OPTIMIZING"  # unchanged


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


class TestDefaultRegistry:

    def test_registry_has_three_known_types(self) -> None:
        registry = build_default_registry()
        assert set(registry) == {
            "TTL_DELETE", "COMPACTION", "INDEX_OPTIMIZE",
        }

    def test_registry_returns_fresh_instances_each_call(self) -> None:
        """Two builds must not share executor instances (no global state)."""

        a = build_default_registry()
        b = build_default_registry()
        assert a["TTL_DELETE"] is not b["TTL_DELETE"]
