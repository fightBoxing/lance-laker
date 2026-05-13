"""Tests that exercise the real (lance-backed) code path of each executor.

These tests do NOT install the ``pylance`` wheel: they monkey-patch
``lcp.data_plane.lance_io`` functions to capture invocations.  The
fallback (stub) path is already covered by
``tests/unit/workers/test_lifecycle_executors.py``; here we focus on
what changes when ``Settings.lance_storage_endpoint`` is set.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.core import config as config_mod
from lcp.data_plane import lance_io
from lcp.db.models import Base, Dataset, Index, Task
from lcp.workers.executors import (
    CompactionExecutor,
    IndexBuildExecutor,
    IndexOptimizeExecutor,
    TtlDeleteExecutor,
)

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
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def lance_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the executor onto the real-lance code path.

    ``get_settings`` is ``lru_cache``d, so we replace the underlying
    callable instead of mutating the cached object.
    """

    from lcp.core.config import Settings

    fake = Settings(
        lance_storage_endpoint="http://minio.test:9000",
        lance_storage_access_key="ak",
        lance_storage_secret_key="sk",
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake)
    # Also patch the symbol that each executor imports by name.
    from lcp.workers.executors import compaction as compaction_mod
    from lcp.workers.executors import index_build as index_build_mod
    from lcp.workers.executors import index_optimize as index_optimize_mod
    from lcp.workers.executors import ttl_delete as ttl_delete_mod
    monkeypatch.setattr(ttl_delete_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(compaction_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(index_build_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(index_optimize_mod, "get_settings", lambda: fake)


@dataclass
class _LanceCalls:
    """Records every call made through the lance_io facade."""

    delete_rows: list[dict[str, Any]]
    compact_files: list[dict[str, Any]]
    optimize_indices: list[dict[str, Any]]
    create_index: list[dict[str, Any]]


@pytest.fixture
def fake_lance_io(monkeypatch: pytest.MonkeyPatch) -> _LanceCalls:
    """Replace lance_io functions so executors never touch the real lance."""

    calls = _LanceCalls(
        delete_rows=[], compact_files=[], optimize_indices=[], create_index=[],
    )

    def _delete_rows(uri: str, predicate: str, *, storage_options: Any) -> int:
        calls.delete_rows.append({
            "uri": uri,
            "predicate": predicate,
            "storage_options": storage_options,
        })
        return 7  # arbitrary post-delete count

    def _compact_files(uri: str, *, storage_options: Any, **kwargs: Any) -> Any:
        calls.compact_files.append({
            "uri": uri,
            "storage_options": storage_options,
            "kwargs": kwargs,
        })
        return lance_io.CompactionStats(
            fragments_removed=10, fragments_added=2,
            files_removed=8, files_added=1,
        )

    def _optimize_indices(uri: str, *, storage_options: Any) -> int:
        calls.optimize_indices.append({
            "uri": uri,
            "storage_options": storage_options,
        })
        return 3  # arbitrary index count

    def _create_index(
        uri: str,
        *,
        column: str,
        index_type: str,
        storage_options: Any,
        replace: bool = False,
        **index_params: Any,
    ) -> dict[str, Any]:
        calls.create_index.append({
            "uri": uri,
            "column": column,
            "index_type": index_type,
            "storage_options": storage_options,
            "replace": replace,
            "index_params": index_params,
        })
        return {
            "uri": uri,
            "column": column,
            "index_type": index_type,
            "replace": replace,
            "params": dict(index_params),
        }

    monkeypatch.setattr(lance_io, "delete_rows", _delete_rows)
    monkeypatch.setattr(lance_io, "compact_files", _compact_files)
    monkeypatch.setattr(lance_io, "optimize_indices", _optimize_indices)
    monkeypatch.setattr(lance_io, "create_index", _create_index)
    return calls


async def _make_dataset(
    session: AsyncSession, *, tenant_id: str = "acme",
) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        catalog="lance",
        db_schema="public",
        table_name=f"t_{uuid.uuid4().hex[:6]}",
        storage_uri="s3://bucket/dataset.lance",
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
# TTL_DELETE - real mode
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("lance_endpoint")
class TestTtlDeleteRealMode:

    async def test_calls_lance_delete_and_includes_rows_after(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
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

        # One real lance call landed.
        assert len(fake_lance_io.delete_rows) == 1
        call = fake_lance_io.delete_rows[0]
        assert call["uri"] == ds.storage_uri
        # The predicate is an ANSI-style timestamp literal so DataFusion
        # can parse it.
        assert call["predicate"].startswith("created_at < timestamp '")
        # Storage options carry the configured endpoint.
        assert call["storage_options"]["endpoint"] == "http://minio.test:9000"

        # Payload reflects real mode + real row count.
        assert result.payload["mode"] == "real"
        assert result.payload["rows_after"] == 7
        assert result.payload["ttl_days"] == 30
        # Predicate preview kept human-friendly for the audit trail.
        assert "30 DAY" in result.payload["predicate_preview"]


# ---------------------------------------------------------------------------
# COMPACTION - real mode
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("lance_endpoint")
class TestCompactionRealMode:

    async def test_forwards_known_threshold_keys_only(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="COMPACTION",
            params={
                "threshold": {
                    "target_rows_per_fragment": 1_000_000,
                    "small_files": 100,  # not a lance kwarg -- must be ignored
                },
            },
        )

        result = await CompactionExecutor().execute(
            session, task=task, dataset=ds,
        )

        assert len(fake_lance_io.compact_files) == 1
        call = fake_lance_io.compact_files[0]
        # Only the lance-known kwarg made it to the data plane.
        assert call["kwargs"] == {"target_rows_per_fragment": 1_000_000}

        # Audit trail: the unknown key is surfaced so operators can see
        # what was dropped, but it was NOT silently forwarded.
        assert result.payload["mode"] == "real"
        assert result.payload["ignored_threshold_keys"] == ["small_files"]
        assert result.payload["stats"] == {
            "fragments_removed": 10, "fragments_added": 2,
            "files_removed": 8, "files_added": 1,
        }

    async def test_empty_threshold_calls_lance_with_no_kwargs(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="COMPACTION",
            params=None,
        )

        await CompactionExecutor().execute(
            session, task=task, dataset=ds,
        )
        assert fake_lance_io.compact_files[0]["kwargs"] == {}


# ---------------------------------------------------------------------------
# INDEX_OPTIMIZE - real mode
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("lance_endpoint")
class TestIndexOptimizeRealMode:

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

    async def test_calls_lance_then_promotes_state(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        ds = await _make_dataset(session)
        idx = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid,
            index_name="idx_x", status="OPTIMIZING",
        )

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )
        result = await IndexOptimizeExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        # Lance was called exactly once with the dataset's storage URI.
        assert len(fake_lance_io.optimize_indices) == 1
        assert fake_lance_io.optimize_indices[0]["uri"] == ds.storage_uri

        # State machine still ran: the OPTIMIZING row is now READY.
        await session.refresh(idx)
        assert idx.status == "READY"
        assert idx.last_optimized_at is not None

        # Payload merges both halves.
        assert result.payload["mode"] == "real"
        assert result.payload["promoted_count"] == 1
        # Real-mode-only key, present iff lance was called.
        assert result.payload["lance_index_count"] == 3

    async def test_lance_failure_does_not_promote(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invariant: OPTIMIZING -> READY only when lance succeeded.

        We swap optimize_indices for a function that raises; the
        executor must propagate, and the index row must STILL be in
        OPTIMIZING (the worker would rollback the txn at this point,
        but inside the executor we simply assert it never wrote READY).
        """

        ds = await _make_dataset(session)
        idx = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid,
            index_name="idx_y", status="OPTIMIZING",
        )

        def _boom(uri: str, *, storage_options: Any) -> int:
            raise RuntimeError("lance exploded")

        monkeypatch.setattr(lance_io, "optimize_indices", _boom)

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )
        with pytest.raises(RuntimeError, match="lance exploded"):
            await IndexOptimizeExecutor().execute(
                session, task=task, dataset=ds,
            )

        # Row state is still OPTIMIZING -- proves we did lance work
        # BEFORE touching the row.
        await session.refresh(idx)
        assert idx.status == "OPTIMIZING"


# ---------------------------------------------------------------------------
# INDEX_BUILD - real mode
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("lance_endpoint")
class TestIndexBuildRealMode:
    """Block B: ``BUILDING -> READY`` with a real lance.create_index call."""

    async def _seed_index(
        self,
        session: AsyncSession,
        *,
        dataset_uuid: str,
        index_name: str,
    ) -> Index:
        """Seed a vector_index row exactly as ``index_service.create_index``
        would: ``BUILDING`` status, no last_optimized_at."""

        idx = Index(
            dataset_uuid=dataset_uuid,
            index_name=index_name,
            index_type="IVF_PQ",
            column_name="embedding",
            status="BUILDING",
        )
        session.add(idx)
        await session.commit()
        await session.refresh(idx)
        return idx

    async def test_calls_lance_create_index_then_promotes_named_row_only(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        """Happy path: lance.create_index runs, BUILDING -> READY, only on
        the row named in task.params (Q2 = A scoping)."""

        ds = await _make_dataset(session)
        target = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid, index_name="idx_target",
        )
        # A second BUILDING row that this task must NOT touch -- proves the
        # executor scopes by index_name, not by dataset.
        sibling = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid, index_name="idx_other",
        )

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "idx_target",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
                "params": {"num_partitions": 64, "num_sub_vectors": 8},
            },
        )

        result = await IndexBuildExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        # Lance was invoked exactly once with the right knobs.
        assert len(fake_lance_io.create_index) == 1
        call = fake_lance_io.create_index[0]
        assert call["uri"] == ds.storage_uri
        assert call["column"] == "embedding"
        assert call["index_type"] == "IVF_PQ"
        assert call["replace"] is True  # idempotent retry safety
        assert call["index_params"] == {
            "num_partitions": 64, "num_sub_vectors": 8,
        }
        assert call["storage_options"]["endpoint"] == "http://minio.test:9000"

        # Target row promoted; sibling row untouched.
        await session.refresh(target)
        await session.refresh(sibling)
        assert target.status == "READY"
        assert target.last_optimized_at is not None
        assert sibling.status == "BUILDING"
        assert sibling.last_optimized_at is None

        # Payload reflects real mode + the lance descriptor.
        assert result.payload["mode"] == "real"
        assert result.payload["index_name"] == "idx_target"
        assert result.payload["lance_descriptor"]["column"] == "embedding"

    async def test_missing_required_param_raises_value_error(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        """Defensive: malformed task fails fast, never touches lance."""

        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={"index_name": "idx_x"},  # missing column_name + index_type
        )

        with pytest.raises(ValueError, match="missing required params"):
            await IndexBuildExecutor().execute(
                session, task=task, dataset=ds,
            )
        # No lance call leaked through.
        assert fake_lance_io.create_index == []

    async def test_missing_index_row_raises_value_error(
        self,
        session: AsyncSession,
        fake_lance_io: _LanceCalls,
    ) -> None:
        """If the named vector_index row is gone (dropped between submit
        and execute), we fail loudly before doing any lance work."""

        ds = await _make_dataset(session)  # no Index seeded
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "ghost",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
            },
        )

        with pytest.raises(ValueError, match="target index not found"):
            await IndexBuildExecutor().execute(
                session, task=task, dataset=ds,
            )
        assert fake_lance_io.create_index == []

    async def test_lance_failure_keeps_status_building(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invariant: BUILDING -> READY only when lance.create_index
        succeeded.  A lance error must propagate AND leave the row in
        BUILDING (the worker rolls back the txn around this executor)."""

        ds = await _make_dataset(session)
        idx = await self._seed_index(
            session, dataset_uuid=ds.dataset_uuid, index_name="idx_z",
        )

        def _boom(uri: str, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("lance create_index exploded")

        monkeypatch.setattr(lance_io, "create_index", _boom)

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "idx_z",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
            },
        )
        with pytest.raises(RuntimeError, match="exploded"):
            await IndexBuildExecutor().execute(
                session, task=task, dataset=ds,
            )

        await session.refresh(idx)
        assert idx.status == "BUILDING"
        assert idx.last_optimized_at is None


# ---------------------------------------------------------------------------
# INDEX_BUILD - stub mode (no lance endpoint configured)
# ---------------------------------------------------------------------------


class TestIndexBuildStubMode:
    """Without a lance endpoint, the executor still flips BUILDING ->
    READY (the LCP state-machine transition is real LCP work) but
    declines to call lance."""

    async def test_stub_promotes_state_without_calling_lance(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lcp.core.config import Settings
        from lcp.workers.executors import index_build as index_build_mod

        # Empty endpoint -> stub path.
        empty = Settings(lance_storage_endpoint="")
        monkeypatch.setattr(index_build_mod, "get_settings", lambda: empty)

        # If lance_io.create_index is reached, fail the test loudly: the
        # stub path must not touch the data plane.
        def _must_not_call(*args: Any, **kwargs: Any) -> None:
            raise AssertionError(
                "stub mode must not invoke lance_io.create_index",
            )
        monkeypatch.setattr(lance_io, "create_index", _must_not_call)

        ds = await _make_dataset(session)
        idx = Index(
            dataset_uuid=ds.dataset_uuid,
            index_name="stub_idx",
            index_type="IVF_PQ",
            column_name="embedding",
            status="BUILDING",
        )
        session.add(idx)
        await session.commit()
        await session.refresh(idx)

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "stub_idx",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
            },
        )
        result = await IndexBuildExecutor().execute(
            session, task=task, dataset=ds,
        )
        await session.commit()

        await session.refresh(idx)
        assert idx.status == "READY"
        assert result.payload["mode"] == "stub"
        assert result.payload["would_call"] == "lance.LanceDataset.create_index"
