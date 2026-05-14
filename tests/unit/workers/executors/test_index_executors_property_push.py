"""Spec tests: index executors call ``push_index_properties`` correctly.

These tests are deliberately *narrow*: they don't re-test the LCP
state-machine (the existing ``test_lifecycle_executors.py`` covers that
in detail) and they don't re-test the Gravitino I/O semantics (covered
by ``test_gravitino_push.py``).  They just pin the wire between the
two: did the executor call the helper, with the right arguments?

Why a separate file: keeping these spec tests away from the state-
machine tests means a change to either side fails one file at a time,
which speeds up debugging.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.db.models import Base, Dataset, Index, Task
from lcp.workers.executors.index_build import IndexBuildExecutor
from lcp.workers.executors.index_optimize import IndexOptimizeExecutor


# ---------------------------------------------------------------------------
# Fixtures (kept local rather than imported from sibling test file: copying
# 30 lines of fixture is cheaper than introducing a shared conftest that
# couples two otherwise-independent test files).
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


async def _make_dataset(session: AsyncSession) -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id="acme",
        catalog="lance",
        db_schema="public",
        table_name="embeddings",
        storage_uri="s3://bucket/embeddings",
        status="ACTIVE",
        row_count=10,
        fragment_count=1,
        index_coverage=Decimal("0.5"),
        latest_version=1,
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
    params: dict[str, object],
) -> Task:
    # Tasks are not committed here; executors only read attributes.
    return Task(
        task_uuid=str(uuid.uuid4()),
        dataset_uuid=dataset_uuid,
        tenant_id=tenant_id,
        task_type=task_type,
        params=params,
        status="RUNNING",
        priority=0,
        attempt=1,
    )


# ---------------------------------------------------------------------------
# IndexBuildExecutor: stub mode pushes properties with column + READY
# ---------------------------------------------------------------------------


class TestIndexBuildPushesProperties:

    async def test_stub_path_calls_helper_with_correct_args(
        self, session: AsyncSession,
    ) -> None:
        """Stub mode (lance not configured) still pushes properties.

        We call the executor with default settings (no
        ``lance_storage_endpoint``) so it takes the stub branch, and
        assert the helper was called once with the right kwargs.

        Why patch at the executor module's import site, not the source:
        the executor does ``from ... import push_index_properties``,
        which binds the name in its own module namespace; patching
        ``_gravitino_push.push_index_properties`` would not intercept.
        """

        ds = await _make_dataset(session)
        idx = Index(
            dataset_uuid=ds.dataset_uuid,
            index_name="emb_idx",
            index_type="IVF_PQ",
            column_name="embedding",
            status="BUILDING",
        )
        session.add(idx)
        await session.commit()

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "emb_idx",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
            },
        )

        mock_push = AsyncMock(return_value=True)
        with patch(
            "lcp.workers.executors.index_build.push_index_properties",
            mock_push,
        ):
            result = await IndexBuildExecutor().execute(
                session, task=task, dataset=ds,
            )
        # Executors run inside the worker's transaction; the worker
        # commits after a successful execute().  We mimic that here so
        # the refresh below sees the row's new state.
        await session.commit()

        # Helper called exactly once.
        mock_push.assert_called_once()
        kwargs = mock_push.call_args.kwargs
        assert kwargs["schema"] == "public"
        assert kwargs["table"] == "embeddings"
        assert kwargs["index_name"] == "emb_idx"
        assert kwargs["state"] == "READY"
        assert kwargs["column"] == "embedding"
        # last_optimized_at must equal what the executor wrote on the
        # row -- proves the wire and the LCP write agree.  Use a
        # ``is`` style identity check via the row to avoid a flaky
        # wall-clock comparison.
        await session.refresh(idx)
        assert kwargs["last_optimized_at"] == idx.last_optimized_at

        # Payload should also surface the push outcome so operators can
        # diagnose silent UI lag.
        assert result.payload["gravitino_property_pushed"] is True

    async def test_helper_failure_returns_false_does_not_raise(
        self, session: AsyncSession,
    ) -> None:
        """If helper returns False, executor still SUCCEEDs (best-effort).

        This is the contract Step 6 promised; pin it so a future
        refactor cannot accidentally make the property push mandatory.
        """

        ds = await _make_dataset(session)
        idx = Index(
            dataset_uuid=ds.dataset_uuid,
            index_name="emb_idx",
            index_type="IVF_PQ",
            column_name="embedding",
            status="BUILDING",
        )
        session.add(idx)
        await session.commit()

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_BUILD",
            params={
                "index_name": "emb_idx",
                "column_name": "embedding",
                "index_type": "IVF_PQ",
            },
        )

        with patch(
            "lcp.workers.executors.index_build.push_index_properties",
            AsyncMock(return_value=False),
        ):
            result = await IndexBuildExecutor().execute(
                session, task=task, dataset=ds,
            )
        await session.commit()

        # State-machine still progresses.
        await session.refresh(idx)
        assert idx.status == "READY"
        # Payload reflects the unsuccessful push without failing the task.
        assert result.payload["gravitino_property_pushed"] is False


# ---------------------------------------------------------------------------
# IndexOptimizeExecutor: pushes once per promoted index
# ---------------------------------------------------------------------------


class TestIndexOptimizePushesProperties:

    async def test_pushes_once_per_promoted_index(
        self, session: AsyncSession,
    ) -> None:
        """Two OPTIMIZING rows -> two helper calls; one READY row -> 0 calls."""

        ds = await _make_dataset(session)
        for name, status in (
            ("idx_a", "OPTIMIZING"),
            ("idx_b", "OPTIMIZING"),
            ("idx_c", "READY"),  # must NOT be pushed
        ):
            session.add(Index(
                dataset_uuid=ds.dataset_uuid,
                index_name=name,
                index_type="IVF_PQ",
                column_name="embedding",
                status=status,
            ))
        await session.commit()

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )

        mock_push = AsyncMock(return_value=True)
        with patch(
            "lcp.workers.executors.index_optimize.push_index_properties",
            mock_push,
        ):
            result = await IndexOptimizeExecutor().execute(
                session, task=task, dataset=ds,
            )

        # Exactly two pushes -- one per promoted (OPTIMIZING) row.  The
        # READY row was already done last cycle and must not get re-pushed
        # (would burn Gravitino quota for no information change).
        assert mock_push.call_count == 2
        promoted_names = {
            call.kwargs["index_name"] for call in mock_push.call_args_list
        }
        assert promoted_names == {"idx_a", "idx_b"}
        # All promoted pushes carry state=READY, not OPTIMIZING.  The LCP
        # row has already flipped to READY by the time we push, so the
        # property reflects post-optimize truth.
        for call in mock_push.call_args_list:
            assert call.kwargs["state"] == "READY"

        # Payload counter aligns with successful push count.
        assert result.payload["gravitino_property_pushed_count"] == 2

    async def test_partial_failure_does_not_block_other_pushes(
        self, session: AsyncSession,
    ) -> None:
        """If one push returns False, the others still happen.

        Sequential pushes are intentional (see executor docstring); pin
        that one bad apple cannot starve the rest.
        """

        ds = await _make_dataset(session)
        for name in ("idx_a", "idx_b", "idx_c"):
            session.add(Index(
                dataset_uuid=ds.dataset_uuid,
                index_name=name,
                index_type="IVF_PQ",
                column_name="embedding",
                status="OPTIMIZING",
            ))
        await session.commit()

        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )

        # First call fails (False), second + third succeed (True).
        mock_push = AsyncMock(side_effect=[False, True, True])
        with patch(
            "lcp.workers.executors.index_optimize.push_index_properties",
            mock_push,
        ):
            result = await IndexOptimizeExecutor().execute(
                session, task=task, dataset=ds,
            )

        assert mock_push.call_count == 3
        # 2/3 pushed.  The counter is *successful pushes*, not attempts.
        assert result.payload["gravitino_property_pushed_count"] == 2

    async def test_no_promoted_indexes_no_pushes(
        self, session: AsyncSession,
    ) -> None:
        """Dataset with zero OPTIMIZING rows -> helper never called."""

        ds = await _make_dataset(session)
        task = _make_task(
            dataset_uuid=ds.dataset_uuid,
            tenant_id=ds.tenant_id,
            task_type="INDEX_OPTIMIZE",
            params={},
        )

        mock_push = AsyncMock(return_value=True)
        with patch(
            "lcp.workers.executors.index_optimize.push_index_properties",
            mock_push,
        ):
            result = await IndexOptimizeExecutor().execute(
                session, task=task, dataset=ds,
            )

        mock_push.assert_not_called()
        assert result.payload["gravitino_property_pushed_count"] == 0


# Tell pytest-asyncio to treat all coroutines in this module as auto async.
pytestmark = pytest.mark.asyncio
