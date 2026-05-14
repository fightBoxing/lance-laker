"""Unit tests for ``EmbeddingExecutor``.

Mirrors the in-memory SQLite + handcrafted ORM rows pattern used by
:mod:`tests.unit.workers.test_lifecycle_executors`.  Coverage layered
by slice:

* slice 3 (B.3): contract param check, DB lookup error paths, payload
  contract, registry wiring;
* slice 4 (B.5): real-model routing via ``rule.model_name``;
* slice 4 (B.6 / beta.2a): real lance read/write loop -- single source
  column only, multi-column raises ``NotImplementedError``.

Lance is mocked in every test by monkey-patching
``lance_io.add_columns_from_func``; we never load a real lance dataset
or pyarrow batch in unit tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.data_plane import lance_io
from lcp.db.models import Base, Dataset, Task, VectorizationRule
from lcp.workers.executors import embedding as embedding_module
from lcp.workers.executors.base import (
    ExecutorResult,
    build_default_registry,
)
from lcp.workers.executors.embedding import EmbeddingExecutor

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_lance_add_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Default fixture: stub lance_io.add_columns_from_func.

    Every executor test path that reaches the lance call must NOT hit a
    real bucket.  We capture the call so individual tests can assert
    on the transform / read_columns shape.  Returns the capture dict
    keyed by ``calls`` (list).

    Tests that need to drive the transform callable themselves
    overwrite this stub via their own monkeypatch.
    """

    capture: dict[str, Any] = {"calls": []}

    def _fake_add(
        uri: str,
        *,
        transforms: Any,
        read_columns: list[str] | None = None,
        storage_options: dict[str, str] | None = None,
    ) -> int:
        capture["calls"].append({
            "uri": uri,
            "transforms": transforms,
            "read_columns": read_columns,
            "storage_options": storage_options,
        })
        # Pretend the dataset has 5 rows after the column add; tests
        # that care about a different count override this stub.
        return 5

    monkeypatch.setattr(lance_io, "add_columns_from_func", _fake_add)
    return capture


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
    model_name: str = "mock",
    source_columns: list[str] | None = None,
) -> VectorizationRule:
    rule = VectorizationRule(
        dataset_uuid=dataset_uuid,
        target_column=target_column,
        # beta.2a: single-column default.  Tests that exercise the
        # multi-column NotImplementedError path pass an explicit list.
        source_columns=source_columns if source_columns is not None else ["title"],
        model_name=model_name,
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

    async def test_mock_mode_payload(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
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
        # mock-named rule -> mock path; ``mode`` reflects encoder
        # provenance, decoupled from lance config.
        assert p["mode"] == "mock"
        assert p["dataset_uuid"] == ds.dataset_uuid
        assert p["storage_uri"] == ds.storage_uri
        # Rule fan-out: every field downstream consumers may want must
        # be present and copied from the rule, not from task.params.
        assert p["vectorization_rule_id"] == rule.id
        assert p["model_name"] == "mock"
        assert p["model_version"] == "v1"
        assert p["target_column"] == "embedding"
        assert p["source_columns"] == ["title"]
        assert p["batch_size"] == 64
        # Mock-model contract: default dim is 384 floats.
        assert p["vector_dim"] == 384
        # beta.2a closes the loop: vector_count is the post-write row
        # count from lance, replacing the slice-3 ``would_call`` field.
        assert p["vector_count"] == 5
        assert "would_call" not in p
        # Sanity: timestamp is ISO-formatted.
        assert "T" in p["executed_at"]
        # Lance was actually called with the right shape: one read
        # column (the rule's single source) and a callable transform.
        assert len(stub_lance_add_columns["calls"]) == 1
        call = stub_lance_add_columns["calls"][0]
        assert call["uri"] == ds.storage_uri
        assert call["read_columns"] == ["title"]
        assert callable(call["transforms"])

    async def test_real_model_mode_payload(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch st_embedding so the real-model path is exercised
        # without loading any sentence-transformers wheels.  Captures
        # call args so we can assert the executor forwarded
        # rule.model_name verbatim.
        captured: dict[str, Any] = {}

        def _fake_st(texts: Any, *, model_name: str) -> list[list[float]]:
            captured["texts"] = list(texts)
            captured["model_name"] = model_name
            # Return a 768-d vector so we can also check vector_dim is
            # NOT hard-coded to 384 in the real-model branch.
            return [[0.0] * 768]

        monkeypatch.setattr(embedding_module, "st_embedding", _fake_st)

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            model_name="sentence-transformers/all-mpnet-base-v2",
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )

        p = result.payload
        assert p["mode"] == "real-model"
        assert p["model_name"] == "sentence-transformers/all-mpnet-base-v2"
        assert p["vector_dim"] == 768
        assert p["vector_count"] == 5
        # st_embedding must have been called with the rule's model_name,
        # not the executor's mock literal.
        assert captured["model_name"] == (
            "sentence-transformers/all-mpnet-base-v2"
        )

    async def test_real_model_failure_propagates(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The worker maps any executor exception to FAILED with the
        # exception class name as the error_code.  Make sure load /
        # encode failures bubble out instead of being swallowed into
        # a SUCCEEDED payload (which would silently lose data).
        def _boom(_texts: Any, *, model_name: str) -> list[list[float]]:
            raise RuntimeError(f"unknown model: {model_name}")

        monkeypatch.setattr(embedding_module, "st_embedding", _boom)

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            model_name="made-up/non-existent",
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        with pytest.raises(RuntimeError, match="unknown model"):
            await EmbeddingExecutor().execute(
                session, task=task, dataset=ds,
            )


# ---------------------------------------------------------------------------
# beta.2a: lance read/write loop
# ---------------------------------------------------------------------------


class TestLanceLoop:

    async def test_multi_column_rule_raises_not_implemented(
        self, session: AsyncSession,
    ) -> None:
        # beta.2a is single-column only; multi-column concat needs a
        # contract decision (separator, NULL handling) which is
        # deferred to beta.2b.  Surface that loudly so callers do not
        # silently get a vector built from one column when they passed
        # two.
        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            source_columns=["title", "body"],
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        with pytest.raises(NotImplementedError, match="beta.2b"):
            await EmbeddingExecutor().execute(
                session, task=task, dataset=ds,
            )

    async def test_transform_callable_produces_target_column(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # The callable lance gets must accept a pa.RecordBatch with
        # ``source_column`` and return a pa.RecordBatch carrying ONLY
        # the new ``target_column`` as a fixed-size float32 list.  Run
        # the callable directly with a tiny synthetic batch so we do
        # not need a real lance dataset.
        pa = pytest.importorskip("pyarrow")

        ds = await _make_dataset(session)
        rule = await _make_rule(session, dataset_uuid=ds.dataset_uuid)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        # Pull the callable lance would have invoked.
        transform = stub_lance_add_columns["calls"][0]["transforms"]
        # Feed a 3-row batch; the callable must return a 3-row batch
        # with one column of fixed-size lists of float32.
        in_batch = pa.RecordBatch.from_arrays(
            [pa.array(["hello", "world", "lance"])],
            names=["title"],
        )
        out_batch = transform(in_batch)
        assert out_batch.num_rows == 3
        assert out_batch.schema.names == ["embedding"]
        out_type = out_batch.schema.field("embedding").type
        # Fixed-size list of float32, dim 384 (mock model contract).
        assert pa.types.is_fixed_size_list(out_type)
        assert out_type.list_size == 384
        assert out_type.value_type == pa.float32()

    async def test_transform_handles_null_input_as_empty_string(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # Lance may pass NULL through ``read_columns`` projection.  The
        # encoder would crash on None; the transform must coerce to "".
        pa = pytest.importorskip("pyarrow")

        ds = await _make_dataset(session)
        rule = await _make_rule(session, dataset_uuid=ds.dataset_uuid)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        transform = stub_lance_add_columns["calls"][0]["transforms"]
        in_batch = pa.RecordBatch.from_arrays(
            [pa.array(["a", None, "c"])],
            names=["title"],
        )
        # Must not raise; NULL row gets a vector built from "".
        out_batch = transform(in_batch)
        assert out_batch.num_rows == 3


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistry:

    def test_default_registry_includes_embedding(self) -> None:
        registry = build_default_registry()
        assert "VECTORIZE" in registry
        # And the registered instance must be an EmbeddingExecutor.
        assert isinstance(registry["VECTORIZE"], EmbeddingExecutor)
