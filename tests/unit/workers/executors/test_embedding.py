"""Unit tests for ``EmbeddingExecutor`` (slice 3 / B.3).

Mirrors the in-memory SQLite + handcrafted ORM rows pattern used by
:mod:`tests.unit.workers.test_lifecycle_executors`.  The executor is
mock-only in slice 3, so we focus on:

* the contract param check (``vectorization_rule_id``);
* DB lookup error paths (rule missing, rule disabled);
* payload contract (every field a downstream consumer relies on);
* registry wiring (``build_default_registry`` includes ``"VECTORIZE"``).
"""

from __future__ import annotations

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

from lcp.db.models import Base, Dataset, Task, VectorizationRule
from lcp.workers.executors.base import (
    ExecutorResult,
    build_default_registry,
)
from lcp.workers.executors.embedding import EmbeddingExecutor

pytestmark = pytest.mark.unit


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
    factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession,
    )
    async with factory() as s:
        yield s
    await engine.dispose()


async def _make_dataset(session: AsyncSession) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id="acme",
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


async def _make_rule(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    enabled: bool = True,
    target_column: str = "embedding",
) -> VectorizationRule:
    rule = VectorizationRule(
        dataset_uuid=dataset_uuid,
        target_column=target_column,
        source_columns=["title", "body"],
        model_name="mock",
        model_version="v1",
        batch_size=64,
        trigger_type="ON_INSERT",
        enabled=enabled,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


def _make_task(
    *,
    dataset_uuid: str,
    params: dict | None,
) -> Task:
    return Task(
        task_uuid=str(uuid.uuid4()),
        task_type="VECTORIZE",
        dataset_uuid=dataset_uuid,
        tenant_id="acme",
        status="RUNNING",
        priority=5,
        progress=Decimal("0.0000"),
        attempt=1,
        max_attempts=3,
        params=params,
    )


# ---------------------------------------------------------------------------
# Param contract
# ---------------------------------------------------------------------------


class TestParams:

    async def test_missing_rule_id_raises(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(dataset_uuid=ds.dataset_uuid, params={})
        with pytest.raises(ValueError, match="vectorization_rule_id"):
            await EmbeddingExecutor().execute(session, task=task, dataset=ds)

    async def test_none_params_raises(
        self, session: AsyncSession,
    ) -> None:
        # ``task.params`` is JSON-nullable; the executor must treat
        # None the same as {} and surface a clear error.
        ds = await _make_dataset(session)
        task = _make_task(dataset_uuid=ds.dataset_uuid, params=None)
        with pytest.raises(ValueError, match="vectorization_rule_id"):
            await EmbeddingExecutor().execute(session, task=task, dataset=ds)


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------


class TestRuleLookup:

    async def test_missing_rule_raises(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        # No rule inserted; rule_id 999 must surface a clear error
        # rather than e.g. a NoneType attribute access deeper down.
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": 999},
        )
        with pytest.raises(ValueError, match="not found"):
            await EmbeddingExecutor().execute(session, task=task, dataset=ds)

    async def test_disabled_rule_raises(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        rule = await _make_rule(
            session, dataset_uuid=ds.dataset_uuid, enabled=False,
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        with pytest.raises(ValueError, match="disabled"):
            await EmbeddingExecutor().execute(session, task=task, dataset=ds)

    async def test_rule_belonging_to_other_dataset_not_used(
        self, session: AsyncSession,
    ) -> None:
        # Defence in depth: the executor scopes the rule lookup by
        # dataset_uuid; a rule whose id matches but whose dataset
        # differs must not be picked up.
        ds_a = await _make_dataset(session)
        ds_b = await _make_dataset(session)
        rule = await _make_rule(session, dataset_uuid=ds_b.dataset_uuid)
        task = _make_task(
            dataset_uuid=ds_a.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        with pytest.raises(ValueError, match="not found"):
            await EmbeddingExecutor().execute(
                session, task=task, dataset=ds_a,
            )


# ---------------------------------------------------------------------------
# Payload contract
# ---------------------------------------------------------------------------


class TestPayload:

    async def test_stub_mode_payload(
        self, session: AsyncSession,
    ) -> None:
        ds = await _make_dataset(session)
        rule = await _make_rule(session, dataset_uuid=ds.dataset_uuid)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )

        assert isinstance(result, ExecutorResult)
        p = result.payload
        # Standard executor fields.
        assert p["executor"] == "EmbeddingExecutor"
        # No lance_storage_endpoint configured in unit-test settings ->
        # mode must be "stub".
        assert p["mode"] == "stub"
        assert p["dataset_uuid"] == ds.dataset_uuid
        assert p["storage_uri"] == ds.storage_uri
        # Rule fan-out: every field downstream consumers may want must
        # be present and copied from the rule, not from task.params.
        assert p["vectorization_rule_id"] == rule.id
        assert p["model_name"] == "mock"
        assert p["model_version"] == "v1"
        assert p["target_column"] == "embedding"
        assert p["source_columns"] == ["title", "body"]
        assert p["batch_size"] == 64
        # Mock-model contract: default dim is 384 floats.
        assert p["vector_dim"] == 384
        # Documents the next-slice work without faking it now.
        assert p["would_call"] == "lance_io.add_columns_from_func"
        # Sanity: timestamp is ISO-formatted.
        assert "T" in p["executed_at"]


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistry:

    def test_default_registry_includes_embedding(self) -> None:
        registry = build_default_registry()
        assert "VECTORIZE" in registry
        # And the registered instance must be an EmbeddingExecutor.
        assert isinstance(registry["VECTORIZE"], EmbeddingExecutor)
