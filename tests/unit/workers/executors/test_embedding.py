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
from sqlalchemy import select
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
    """Default fixture: stub the two ``lance_io`` seams the executor uses.

    Every executor test path that reaches the lance call must NOT hit a
    real bucket.  We capture the calls so individual tests can assert
    on the transform / read_columns shape and on the validator's input.
    Returns the capture dict keyed by ``calls`` (add_columns) and
    ``schema_calls`` (read_dataset_schema).

    The default fake schema covers ``title`` and ``body`` as utf8
    strings -- the columns existing rules use.  Tests that need a
    different schema (missing column / non-string type) override the
    stub via their own monkeypatch.

    Tests that need to drive the transform callable themselves
    overwrite the add_columns stub via their own monkeypatch.
    """

    pa = pytest.importorskip("pyarrow")

    capture: dict[str, Any] = {"calls": [], "schema_calls": []}

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

    # Schema covers the columns every existing rule uses; broad
    # enough that the default ``_validate_source_columns`` call
    # passes for ``["title"]`` and ``["title", "body"]`` alike.
    default_schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("title", pa.string()),
        pa.field("body", pa.string()),
    ])

    def _fake_schema(
        uri: str,
        *,
        storage_options: dict[str, str] | None = None,
    ) -> Any:
        capture["schema_calls"].append({
            "uri": uri,
            "storage_options": storage_options,
        })
        return default_schema

    monkeypatch.setattr(lance_io, "add_columns_from_func", _fake_add)
    monkeypatch.setattr(lance_io, "read_dataset_schema", _fake_schema)
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
    extra: dict[str, Any] | None = None,
) -> VectorizationRule:
    rule = VectorizationRule(
        dataset_uuid=dataset_uuid,
        target_column=target_column,
        # Default single-column rule keeps the bulk of the suite
        # unchanged; multi-column tests pass an explicit list.
        source_columns=source_columns if source_columns is not None else ["title"],
        model_name=model_name,
        model_version="v1",
        batch_size=64,
        trigger_type="ON_INSERT",
        enabled=enabled,
        extra=extra,
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

    async def test_single_column_rule_still_works(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # beta.2b unifies the single- and multi-column code paths; the
        # single-column case must still go through unchanged.  This is
        # the regression guard for the old ``len == 1`` branch.
        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            source_columns=["title"],
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        # read_columns matches the rule exactly, no ad-hoc unwrap.
        assert stub_lance_add_columns["calls"][0]["read_columns"] == ["title"]
        # New payload field is present even when only one column is
        # configured -- contract is uniform across single / multi.
        assert result.payload["concat_separator"] == " "

    async def test_multi_column_concat_default_separator(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # beta.2b: two columns concat with the default single-space
        # separator.  Drive the transform directly so we can read the
        # exact strings the encoder saw.
        pa = pytest.importorskip("pyarrow")

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
        # Capture the inputs the mock encoder receives so we can
        # assert on the concatenation, not just the vector shape.
        captured: dict[str, Any] = {}

        def _spy_hash(texts: Any) -> list[list[float]]:
            captured.setdefault("texts", []).extend(list(texts))
            return [[0.0] * 384 for _ in texts]

        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )

        call = stub_lance_add_columns["calls"][0]
        assert call["read_columns"] == ["title", "body"]
        # Exercise the transform with a synthetic 2-row batch.
        in_batch = pa.RecordBatch.from_arrays(
            [
                pa.array(["hello", "foo"]),
                pa.array(["world", "bar"]),
            ],
            names=["title", "body"],
        )
        # Patch hash_embedding for the duration of the transform call
        # so we can read the materialised inputs.  Patch via the
        # module the executor imports from -- name resolution happens
        # at call time inside the closure.
        original = embedding_module.hash_embedding
        embedding_module.hash_embedding = _spy_hash
        try:
            out_batch = call["transforms"](in_batch)
        finally:
            embedding_module.hash_embedding = original
        # Default separator is one ASCII space.
        assert captured["texts"] == ["hello world", "foo bar"]
        assert out_batch.num_rows == 2
        assert out_batch.schema.names == ["embedding"]
        # Payload reports the same default separator.
        assert result.payload["concat_separator"] == " "
        assert result.payload["source_columns"] == ["title", "body"]

    async def test_multi_column_custom_separator_via_extra(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # ``rule.extra["concat_separator"]`` overrides the default;
        # newline is a common pick when the encoder treats it as a
        # sentence boundary.
        pa = pytest.importorskip("pyarrow")

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            source_columns=["title", "body"],
            extra={"concat_separator": "\n"},
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )

        captured: list[str] = []

        def _spy_hash(texts: Any) -> list[list[float]]:
            captured.extend(list(texts))
            return [[0.0] * 384 for _ in texts]

        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        call = stub_lance_add_columns["calls"][0]
        in_batch = pa.RecordBatch.from_arrays(
            [pa.array(["a"]), pa.array(["b"])],
            names=["title", "body"],
        )
        original = embedding_module.hash_embedding
        embedding_module.hash_embedding = _spy_hash
        try:
            call["transforms"](in_batch)
        finally:
            embedding_module.hash_embedding = original
        assert captured == ["a\nb"]
        # Separator surfaced verbatim in the payload.
        assert result.payload["concat_separator"] == "\n"

    async def test_multi_column_skips_null_fields(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        # NULL fields are dropped before joining, so a single-NULL row
        # does not leak a stray separator into the encoded text and
        # an all-NULL row degrades to "" (matches single-column NULL
        # behaviour from beta.2a).
        pa = pytest.importorskip("pyarrow")

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

        captured: list[str] = []

        def _spy_hash(texts: Any) -> list[list[float]]:
            captured.extend(list(texts))
            return [[0.0] * 384 for _ in texts]

        await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        call = stub_lance_add_columns["calls"][0]
        # Three rows: title-NULL, body-NULL, both-NULL.
        in_batch = pa.RecordBatch.from_arrays(
            [
                pa.array([None, "foo", None]),
                pa.array(["world", None, None]),
            ],
            names=["title", "body"],
        )
        original = embedding_module.hash_embedding
        embedding_module.hash_embedding = _spy_hash
        try:
            out_batch = call["transforms"](in_batch)
        finally:
            embedding_module.hash_embedding = original
        # No leading / trailing separators; all-NULL row -> empty str.
        assert captured == ["world", "foo", ""]
        assert out_batch.num_rows == 3

    async def test_missing_source_column_raises_value_error(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Validator runs before the model probe / lance write; a typo
        # in source_columns must surface as a clear ValueError, not a
        # cryptic lance / encoder failure deep in the transform.
        pa = pytest.importorskip("pyarrow")

        # Schema lacks ``body``; the rule asks for it.
        narrow_schema = pa.schema([
            pa.field("id", pa.int64()),
            pa.field("title", pa.string()),
        ])
        monkeypatch.setattr(
            lance_io,
            "read_dataset_schema",
            lambda uri, *, storage_options=None: narrow_schema,
        )

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
        with pytest.raises(ValueError, match="not found in dataset schema"):
            await EmbeddingExecutor().execute(
                session, task=task, dataset=ds,
            )
        # And no lance write happened: short-circuit on validation.
        assert stub_lance_add_columns["calls"] == []

    async def test_non_string_source_column_raises_value_error(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Numeric column would silently get str()'d if we coerced; the
        # contract says no -- embedding user_ids is almost never what
        # the operator wanted, so we fail loud.
        pa = pytest.importorskip("pyarrow")

        wrong_typed_schema = pa.schema([
            pa.field("title", pa.string()),
            pa.field("body", pa.int64()),  # NOT string
        ])
        monkeypatch.setattr(
            lance_io,
            "read_dataset_schema",
            lambda uri, *, storage_options=None: wrong_typed_schema,
        )

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
        with pytest.raises(ValueError, match="must be utf8 string type"):
            await EmbeddingExecutor().execute(
                session, task=task, dataset=ds,
            )
        assert stub_lance_add_columns["calls"] == []

    async def test_empty_source_columns_raises_value_error(
        self, session: AsyncSession,
    ) -> None:
        # Defence in depth: REST should reject this at create time,
        # but an old/malformed row must not silently embed nothing.
        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            source_columns=[],
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        with pytest.raises(ValueError, match="empty source_columns"):
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
# β.3: auto-index-build
# ---------------------------------------------------------------------------


class TestAutoIndexBuild:
    """β.3: EmbeddingExecutor auto-submits INDEX_BUILD when configured."""

    async def test_no_index_config_returns_none(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        """Without index_config in extra, no index is created."""

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session, dataset_uuid=ds.dataset_uuid, extra=None,
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert result.payload["index_build_submitted"] is None

    async def test_auto_build_false_returns_none(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        """auto_build=false means no index is created."""

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            extra={"index_config": {"auto_build": False}},
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert result.payload["index_build_submitted"] is None

    async def test_auto_build_creates_index_and_task(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        """Happy path: auto_build=true inserts Index + Task rows."""

        from lcp.db.models import Index, Task as TaskModel

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            extra={
                "index_config": {
                    "auto_build": True,
                    "index_type": "IVF_PQ",
                    "params": {"num_partitions": 32},
                },
            },
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        assert result.payload["index_build_submitted"] is True

        # Verify Index row was created.
        idx_stmt = select(Index).where(
            Index.dataset_uuid == ds.dataset_uuid,
            Index.index_name == "idx_embedding",
        )
        idx = (await session.execute(idx_stmt)).scalar_one()
        assert idx.status == "BUILDING"
        assert idx.column_name == "embedding"
        assert idx.index_type == "IVF_PQ"

        # Verify INDEX_BUILD task was created.
        task_stmt = select(TaskModel).where(
            TaskModel.task_type == "INDEX_BUILD",
            TaskModel.dataset_uuid == ds.dataset_uuid,
        )
        build_task = (await session.execute(task_stmt)).scalar_one()
        assert build_task.status == "PENDING"
        assert build_task.params["index_name"] == "idx_embedding"
        assert build_task.params["column_name"] == "embedding"
        assert build_task.params["params"] == {"num_partitions": 32}

    async def test_auto_build_custom_index_name(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        """User-specified index_name is honoured."""

        from lcp.db.models import Index

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            extra={
                "index_config": {
                    "auto_build": True,
                    "index_name": "my_custom_idx",
                },
            },
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        assert result.payload["index_build_submitted"] is True
        idx_stmt = select(Index).where(
            Index.dataset_uuid == ds.dataset_uuid,
            Index.index_name == "my_custom_idx",
        )
        idx = (await session.execute(idx_stmt)).scalar_one()
        assert idx.column_name == "embedding"

    async def test_auto_build_skips_existing_index(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
    ) -> None:
        """If the index already exists, skip (idempotent)."""

        from lcp.db.models import Index

        ds = await _make_dataset(session)
        # Pre-seed an existing index.
        existing_idx = Index(
            dataset_uuid=ds.dataset_uuid,
            index_name="idx_embedding",
            column_name="embedding",
            index_type="IVF_PQ",
            status="READY",
        )
        session.add(existing_idx)
        await session.commit()

        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            extra={"index_config": {"auto_build": True}},
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert result.payload["index_build_submitted"] is False

    async def test_auto_build_error_does_not_fail_vectorization(
        self, session: AsyncSession,
        stub_lance_add_columns: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If index creation fails, vectorization still succeeds."""

        from lcp.db.models import Index

        ds = await _make_dataset(session)
        rule = await _make_rule(
            session,
            dataset_uuid=ds.dataset_uuid,
            extra={"index_config": {"auto_build": True}},
        )
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            params={"vectorization_rule_id": rule.id},
        )

        # Make the Index query blow up to simulate a DB error.
        original_execute = session.execute

        async def _boom_on_index_select(stmt, *args, **kwargs):
            # Only blow up on the Index select inside
            # _maybe_submit_index_build.
            stmt_str = str(stmt)
            if "vector_index" in stmt_str and "idx_embedding" not in stmt_str:
                raise RuntimeError("simulated DB failure")
            return await original_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "execute", _boom_on_index_select)

        # Should NOT raise -- vectorization succeeds despite index error.
        result = await EmbeddingExecutor().execute(
            session, task=task, dataset=ds,
        )
        # index_build_submitted is None (error swallowed).
        assert result.payload["index_build_submitted"] is None
        # But vectorization payload is still complete.
        assert result.payload["vector_count"] == 5


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistry:

    def test_default_registry_includes_embedding(self) -> None:
        registry = build_default_registry()
        assert "VECTORIZE" in registry
        # And the registered instance must be an EmbeddingExecutor.
        assert isinstance(registry["VECTORIZE"], EmbeddingExecutor)
