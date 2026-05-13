"""Unit tests for the event-driven index watcher.

Strategy: we never need a real lance dataset to exercise the watcher's
business logic; we monkey-patch :func:`lcp.data_plane.lance_io.read_index_stats`
to return a synthetic :class:`IndexLiveStats`.  This isolates the unit
under test from PyLance / object-store flakiness so the test suite stays
fast and hermetic.

What we cover here:

1. Pure trigger logic (:func:`decide_trigger`) -- the matrix of
   unindexed-rows / version-drift / stale signals.
2. ``run_watch_pass`` end-to-end with a stubbed lance, including:
   - state-machine transition READY -> OPTIMIZING + task insertion;
   - idempotency on repeated passes within the same lance version;
   - watch-disabled policies are ignored;
   - ``last_seen_version`` is bumped even when no task is emitted.
3. System-principal guard: refuses to run without
   :func:`with_system_context`.

The MySQL integration test (under ``tests/integration``) is the place
for cross-process / concurrent-pass scenarios.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.data_plane import lance_io
from lcp.db.models import Base, Dataset, Index, LifecyclePolicy, Task
from lcp.services import index_watcher_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session sharing one connection via StaticPool."""

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
def system_principal() -> Iterator[None]:
    """Bind a system principal for one test."""

    with with_system_context():
        yield


@pytest.fixture
def stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass real settings: watcher only needs ``build_storage_options``."""

    # Pretend lance is configured -- the watcher never touches the
    # endpoint directly; it only forwards storage_options to the
    # patched ``read_index_stats``.
    monkeypatch.setattr(
        lance_io,
        "build_storage_options",
        lambda _settings=None: {},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_dataset(session: AsyncSession, *, tenant_id: str = "acme") -> Dataset:
    ds = Dataset(
        dataset_uuid=str(uuid.uuid4()),
        tenant_id=tenant_id,
        catalog="lance",
        db_schema="public",
        table_name="t",
        storage_uri="s3://bucket/x",
        status="READY",
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


async def _seed_policy(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    watch_enabled: bool = True,
    min_unindexed_rows: int | None = 1000,
    min_version_drift: int | None = 1,
    stale_minutes: int | None = 30,
    enabled: bool = True,
) -> LifecyclePolicy:
    policy = LifecyclePolicy(
        dataset_uuid=dataset_uuid,
        policy_name="watch",
        index_watch_enabled=watch_enabled,
        index_watch_min_unindexed_rows=min_unindexed_rows,
        index_watch_min_version_drift=min_version_drift,
        index_watch_stale_minutes=stale_minutes,
        enabled=enabled,
    )
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return policy


async def _seed_index(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    name: str = "vec_idx",
    status: str = "READY",
    last_seen_version: int | None = None,
    last_optimized_at: datetime | None = None,
) -> Index:
    idx = Index(
        dataset_uuid=dataset_uuid,
        index_name=name,
        column_name="vec",
        index_type="IVF_PQ",
        status=status,
        coverage=Decimal("1.0000"),
        last_seen_version=last_seen_version,
        last_optimized_at=last_optimized_at,
    )
    session.add(idx)
    await session.commit()
    await session.refresh(idx)
    return idx


def _stub_stats(
    monkeypatch: pytest.MonkeyPatch,
    *,
    latest_version: int,
    num_unindexed_rows: int | None,
) -> None:
    """Patch ``read_index_stats`` to return one canned record."""

    def fake_read(
        _uri: str,
        _index_name: str,
        *,
        storage_options: dict[str, str] | None = None,
    ) -> lance_io.IndexLiveStats:
        return lance_io.IndexLiveStats(
            latest_version=latest_version,
            num_indexed_rows=None,
            num_unindexed_rows=num_unindexed_rows,
            num_indexed_fragments=None,
            num_unindexed_fragments=None,
            updated_at_timestamp_ms=None,
        )

    monkeypatch.setattr(lance_io, "read_index_stats", fake_read)


async def _all_tasks(session: AsyncSession) -> list[Task]:
    return list(
        (await session.execute(select(Task).order_by(Task.id.asc()))).scalars().all(),
    )


# ---------------------------------------------------------------------------
# Pure-logic decide_trigger
# ---------------------------------------------------------------------------


class TestDecideTrigger:

    def _policy(self, **overrides: object) -> LifecyclePolicy:
        # Construct an unsaved policy: only the threshold fields matter
        # for ``decide_trigger`` so we avoid the DB round-trip here.
        defaults: dict[str, object] = {
            "dataset_uuid": "x",
            "policy_name": "p",
            "index_watch_enabled": True,
            "index_watch_min_unindexed_rows": 100,
            "index_watch_min_version_drift": 1,
            "index_watch_stale_minutes": 30,
            "enabled": True,
        }
        defaults.update(overrides)
        return LifecyclePolicy(**defaults)

    def test_unindexed_rows_takes_priority(self) -> None:
        """When all three signals fire, ``unindexed_rows`` wins."""

        policy = self._policy()
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=500,
            latest_version=10,
            last_seen_version=0,
            last_optimized_at=datetime(2026, 1, 1, 0, 0),
            now=datetime(2026, 1, 1, 5, 0),  # +5h => stale
            policy=policy,
        )
        assert decision.reason == index_watcher_service.TRIGGER_UNINDEXED_ROWS
        assert decision.unindexed_rows == 500
        assert decision.version_drift == 10
        assert decision.stale_minutes == pytest.approx(300.0)

    def test_version_drift_when_unindexed_disabled(self) -> None:
        """With ``min_unindexed_rows=None`` the next signal must fire."""

        policy = self._policy(index_watch_min_unindexed_rows=None)
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=99999,  # would have fired but signal disabled
            latest_version=5,
            last_seen_version=4,
            last_optimized_at=None,
            now=datetime(2026, 1, 1),
            policy=policy,
        )
        assert decision.reason == index_watcher_service.TRIGGER_VERSION_DRIFT

    def test_stale_only_fires_with_baseline(self) -> None:
        """Without a ``last_optimized_at`` the stale signal must NOT fire."""

        policy = self._policy(
            index_watch_min_unindexed_rows=None,
            index_watch_min_version_drift=None,
        )
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=10,
            latest_version=1,
            last_seen_version=1,
            last_optimized_at=None,
            now=datetime(2026, 1, 1),
            policy=policy,
        )
        assert decision.reason is None

    def test_stale_fires_when_old_enough(self) -> None:
        policy = self._policy(
            index_watch_min_unindexed_rows=None,
            index_watch_min_version_drift=None,
            index_watch_stale_minutes=15,
        )
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=0,
            latest_version=1,
            last_seen_version=1,
            last_optimized_at=datetime(2026, 1, 1, 12, 0),
            now=datetime(2026, 1, 1, 12, 30),  # +30 min
            policy=policy,
        )
        assert decision.reason == index_watcher_service.TRIGGER_STALE

    def test_no_signal_returns_none(self) -> None:
        policy = self._policy()
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=0,
            latest_version=1,
            last_seen_version=1,
            last_optimized_at=datetime(2026, 1, 1, 12, 0),
            now=datetime(2026, 1, 1, 12, 1),
            policy=policy,
        )
        assert decision.reason is None


# ---------------------------------------------------------------------------
# System guard
# ---------------------------------------------------------------------------


class TestWatcherSystemGuard:

    async def test_run_without_system_raises(
        self, session: AsyncSession,
    ) -> None:
        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            with pytest.raises(index_watcher_service.WatcherNotSystemError):
                await index_watcher_service.run_watch_pass(session)
        finally:
            reset_current_tenant(token)


# ---------------------------------------------------------------------------
# End-to-end run_watch_pass
# ---------------------------------------------------------------------------


class TestRunWatchPass:

    async def test_emits_optimize_task_and_flips_status(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid)
        idx = await _seed_index(session, dataset_uuid=ds.dataset_uuid)

        _stub_stats(
            monkeypatch, latest_version=7, num_unindexed_rows=2000,
        )

        report = await index_watcher_service.run_watch_pass(session)

        # Counters
        assert report.scanned_indexes == 1
        assert report.enqueued_tasks == 1
        assert report.skipped_below_threshold == 0

        # State machine: READY -> OPTIMIZING.  ``last_seen_version`` is
        # owned by the executor (see IndexOptimizeExecutor) and the watcher
        # deliberately does NOT touch it -- otherwise lance's own version
        # bump after ``optimize_indices`` would re-fire the watcher in a
        # tight loop on idle datasets.  So it stays at the seed value.
        await session.refresh(idx)
        assert idx.status == "OPTIMIZING"
        assert idx.last_seen_version is None

        # Task carries the trigger context the executor can echo back.
        tasks = await _all_tasks(session)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.task_type == "INDEX_OPTIMIZE"
        assert task.tenant_id == ds.tenant_id
        assert task.params is not None
        assert task.params["trigger_reason"] == (
            index_watcher_service.TRIGGER_UNINDEXED_ROWS
        )
        assert task.params["lance_version"] == 7
        assert task.params["unindexed_rows"] == 2000
        assert task.idempotency_key is not None
        assert task.idempotency_key.startswith(f"watcher:{idx.id}:INDEX_OPTIMIZE:v7:")

    async def test_idempotent_within_same_version(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two passes at the same lance version must NOT duplicate tasks."""

        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid)
        await _seed_index(session, dataset_uuid=ds.dataset_uuid)

        _stub_stats(monkeypatch, latest_version=3, num_unindexed_rows=5000)

        first = await index_watcher_service.run_watch_pass(session)

        # Second pass: index is now OPTIMIZING -> not re-scanned, but
        # also no duplicate task even after we flip status back to READY
        # (simulating the executor finishing without bumping version).
        idx = (
            await session.execute(
                select(Index).where(Index.dataset_uuid == ds.dataset_uuid),
            )
        ).scalar_one()
        idx.status = "READY"
        await session.commit()

        second = await index_watcher_service.run_watch_pass(session)

        assert first.enqueued_tasks == 1
        assert second.enqueued_tasks == 0
        assert second.skipped_idempotent == 1

        tasks = await _all_tasks(session)
        assert len(tasks) == 1

    async def test_new_lance_version_emits_new_task(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the dataset version advances, a fresh task is emitted."""

        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid)
        await _seed_index(session, dataset_uuid=ds.dataset_uuid)

        _stub_stats(monkeypatch, latest_version=2, num_unindexed_rows=2000)
        first = await index_watcher_service.run_watch_pass(session)

        # Simulate the executor finishing: status back to READY and the
        # ``last_seen_version`` already pinned to 2 (executor would do
        # this; the watcher already did it on the first pass too).
        idx = (
            await session.execute(
                select(Index).where(Index.dataset_uuid == ds.dataset_uuid),
            )
        ).scalar_one()
        idx.status = "READY"
        await session.commit()

        # Lance produces a new version 3 with more unindexed rows.
        _stub_stats(monkeypatch, latest_version=3, num_unindexed_rows=2000)
        second = await index_watcher_service.run_watch_pass(session)

        assert first.enqueued_tasks == 1
        assert second.enqueued_tasks == 1

        tasks = await _all_tasks(session)
        assert len(tasks) == 2
        # Distinct idempotency keys -- one per version.
        keys = {t.idempotency_key for t in tasks}
        assert len(keys) == 2

    async def test_watch_disabled_policy_is_ignored(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ds = await _seed_dataset(session)
        await _seed_policy(
            session, dataset_uuid=ds.dataset_uuid, watch_enabled=False,
        )
        await _seed_index(session, dataset_uuid=ds.dataset_uuid)

        _stub_stats(monkeypatch, latest_version=5, num_unindexed_rows=99999)
        report = await index_watcher_service.run_watch_pass(session)

        assert report.scanned_indexes == 0
        assert report.enqueued_tasks == 0
        assert await _all_tasks(session) == []

    async def test_below_threshold_does_not_touch_seen_version(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No emission and ``last_seen_version`` MUST stay at its seed value.

        The field is owned exclusively by :class:`IndexOptimizeExecutor`,
        which writes the post-optimize lance version.  If the watcher
        bumped it on every pass, lance's own version-after-optimize
        commit would never look like "new data" to us, but neither
        would real user writes that arrive after our optimize finished
        and before the next pass -- the field would already say
        "already seen V+1".  The unique idempotency key on
        ``watcher:{id}:INDEX_OPTIMIZE:v{n}:{reason}`` is what prevents
        duplicate emission within one version, NOT this field.
        """

        ds = await _seed_dataset(session)
        # Disable all triggers so no task fires.
        # NOTE: SQLAlchemy's ``mapped_column(default=N)`` applies the
        # default whenever the attribute is unset OR ``None`` at flush
        # time, so we must explicitly NULL the columns AFTER the row is
        # in the DB to defeat the Python-side default.
        policy = await _seed_policy(
            session,
            dataset_uuid=ds.dataset_uuid,
            min_unindexed_rows=None,
            min_version_drift=None,
            stale_minutes=None,
        )
        policy.index_watch_min_unindexed_rows = None
        policy.index_watch_min_version_drift = None
        policy.index_watch_stale_minutes = None
        await session.commit()
        idx = await _seed_index(
            session, dataset_uuid=ds.dataset_uuid, last_seen_version=5,
        )

        _stub_stats(monkeypatch, latest_version=42, num_unindexed_rows=0)
        report = await index_watcher_service.run_watch_pass(session)

        await session.refresh(idx)
        assert report.enqueued_tasks == 0
        assert report.skipped_below_threshold == 1
        # Watcher does NOT update ``last_seen_version`` -- it stays at
        # whatever the executor (or seed) last wrote.
        assert idx.last_seen_version == 5
        assert idx.status == "READY"  # untouched

    async def test_stats_failure_skips_index(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lance read error must be logged and skipped, not crash."""

        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid)
        await _seed_index(session, dataset_uuid=ds.dataset_uuid)

        def explode(
            _uri: str, _index_name: str, *, storage_options: object = None,
        ) -> lance_io.IndexLiveStats:
            raise RuntimeError("object store unreachable")

        monkeypatch.setattr(lance_io, "read_index_stats", explode)

        report = await index_watcher_service.run_watch_pass(session)

        assert report.scanned_indexes == 1
        assert report.enqueued_tasks == 0
        assert report.skipped_open_failed == 1
        assert await _all_tasks(session) == []

    async def test_only_ready_indexes_are_scanned(
        self,
        session: AsyncSession,
        system_principal: None,
        stub_settings: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OPTIMIZING / BUILDING / FAILED indexes must NOT be scanned."""

        ds = await _seed_dataset(session)
        await _seed_policy(session, dataset_uuid=ds.dataset_uuid)
        await _seed_index(
            session,
            dataset_uuid=ds.dataset_uuid,
            name="busy",
            status="OPTIMIZING",
        )
        await _seed_index(
            session,
            dataset_uuid=ds.dataset_uuid,
            name="ready",
            status="READY",
        )

        _stub_stats(monkeypatch, latest_version=1, num_unindexed_rows=99999)
        report = await index_watcher_service.run_watch_pass(session)

        # Only the READY one was scanned.
        assert report.scanned_indexes == 1
        assert report.enqueued_tasks == 1


# ---------------------------------------------------------------------------
# decide_trigger: never-optimised brand-new index
# ---------------------------------------------------------------------------


class TestVirginIndexEdgeCase:
    """Edge case: index just BUILD-ed, ``last_seen_version`` is NULL."""

    def test_drift_counts_from_zero(self) -> None:
        policy = LifecyclePolicy(
            dataset_uuid="x", policy_name="p",
            index_watch_enabled=True,
            index_watch_min_unindexed_rows=None,
            index_watch_min_version_drift=2,
            index_watch_stale_minutes=None,
            enabled=True,
        )
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=None,
            latest_version=3,
            last_seen_version=None,
            last_optimized_at=None,
            now=datetime(2026, 1, 1),
            policy=policy,
        )
        # latest 3 - seen 0 = 3 >= threshold 2 -> drift fires.
        assert decision.reason == index_watcher_service.TRIGGER_VERSION_DRIFT
        assert decision.version_drift == 3


# ---------------------------------------------------------------------------
# decide_trigger: signal disable
# ---------------------------------------------------------------------------


class TestAllSignalsDisabled:

    def test_no_signals_means_no_decision(self) -> None:
        policy = LifecyclePolicy(
            dataset_uuid="x", policy_name="p",
            index_watch_enabled=True,
            index_watch_min_unindexed_rows=None,
            index_watch_min_version_drift=None,
            index_watch_stale_minutes=None,
            enabled=True,
        )
        decision = index_watcher_service.decide_trigger(
            unindexed_rows=10**9,
            latest_version=10**9,
            last_seen_version=0,
            last_optimized_at=datetime(2026, 1, 1),
            now=datetime(2026, 1, 1) + timedelta(days=365),
            policy=policy,
        )
        assert decision.reason is None
