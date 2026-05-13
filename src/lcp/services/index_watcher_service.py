"""Event-driven index watcher: poll lance, emit ``INDEX_OPTIMIZE`` tasks.

Why this exists
---------------
The :mod:`lcp.services.lifecycle_planner_service` already plans index
optimisation on a coarse hourly cron bucket.  That is fine for steady
ingest but lossy for the common case in vector workloads:

- A user calls ``lance.write_dataset(..., mode="append")`` directly
  (i.e. bypasses the LCP REST API), producing a fresh dataset version
  with rows that no existing index covers.
- Without a watcher, those rows stay un-indexed until either the next
  cron tick fires or the user manually invokes ``POST /indexes/.../optimize``.
  Search recall on those rows is poor in the meantime (lance falls back
  to a brute-force scan of the un-indexed delta).

This service is the LCP analog of the LanceDB Enterprise "Indexer Fleet"
loop: a *high-frequency* polling worker that reads live index stats from
lance and decides "this index has drifted enough; enqueue an optimise".

Process model
-------------
:func:`run_watch_pass` performs one scan-and-emit pass.  The CLI
(:mod:`lcp.workers.index_watcher_cli`) wraps it in a ``while not
shutdown`` loop with a configurable interval (default 10 s) so that this
module stays unit-testable without daemon plumbing.

What this module does NOT do
----------------------------
- It does NOT actually run ``lance.optimize_indices``; it only enqueues
  the task.  The existing :class:`lcp.workers.executors.index_optimize.
  IndexOptimizeExecutor` consumes the task and does the real work.
- It does NOT do bulk reads / scans against the dataset; only the cheap
  ``index_statistics`` and ``latest_version`` calls.
- It does NOT replace the lifecycle planner.  The two run side-by-side:
  the planner is the safety net (cron-bucketed, idempotent), the
  watcher is the fast path (reactive to writes).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import Settings, get_settings
from lcp.core.tenant import get_current_tenant
from lcp.data_plane import lance_io
from lcp.db.models import Dataset, Index, LifecyclePolicy, Task

__all__ = [
    "TASK_INDEX_OPTIMIZE",
    "TriggerDecision",
    "WatcherNotSystemError",
    "WatchPassReport",
    "decide_trigger",
    "run_watch_pass",
]

_LOGGER = logging.getLogger(__name__)

# Match the constant defined in the lifecycle planner so an ops engineer
# can grep both producers under one symbol.
TASK_INDEX_OPTIMIZE = "INDEX_OPTIMIZE"

# Trigger reason tags propagated into the task params + idempotency key
# suffix.  Lower-cased so they double as URL-safe tokens for log scraping.
TRIGGER_UNINDEXED_ROWS = "unindexed_rows"
TRIGGER_VERSION_DRIFT = "version_drift"
TRIGGER_STALE = "stale"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WatcherNotSystemError(Exception):
    """Raised when the watcher runs without a system principal.

    Mirrors :class:`lcp.services.lifecycle_planner_service.PlannerNotSystemError`
    so operators see a uniform "must run as system" failure mode.
    """


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerDecision:
    """Outcome of :func:`decide_trigger` for a single index.

    ``reason`` is ``None`` when no signal crossed its threshold; in that
    case the watcher skips this index entirely.  Otherwise ``reason`` is
    one of :data:`TRIGGER_UNINDEXED_ROWS`, :data:`TRIGGER_VERSION_DRIFT`,
    :data:`TRIGGER_STALE` -- whichever fired first under the OR-rule.
    """

    reason: str | None
    unindexed_rows: int | None
    version_drift: int
    stale_minutes: float


@dataclass
class WatchPassReport:
    """Counters returned by :func:`run_watch_pass`.

    Mutable on purpose: the watcher accumulates counts as it iterates so
    a later assertion can read them without reconstruction.  Per-index
    detail is kept in :attr:`emitted_keys` for tests / debug logging.
    """

    scanned_indexes: int = 0
    enqueued_tasks: int = 0
    skipped_no_dataset: int = 0
    skipped_below_threshold: int = 0
    skipped_open_failed: int = 0
    skipped_idempotent: int = 0
    emitted_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure-logic helper (separately testable)
# ---------------------------------------------------------------------------


def decide_trigger(
    *,
    unindexed_rows: int | None,
    latest_version: int,
    last_seen_version: int | None,
    last_optimized_at: datetime | None,
    now: datetime,
    policy: LifecyclePolicy,
) -> TriggerDecision:
    """Return the first signal that crosses its threshold, or ``None``.

    Evaluated in a fixed order so behaviour is deterministic in tests:

    1. ``unindexed_rows >= index_watch_min_unindexed_rows``
    2. ``latest_version - last_seen_version >= index_watch_min_version_drift``
    3. ``stale_minutes >= index_watch_stale_minutes``

    A signal is considered "off" when its threshold field on the policy
    is ``None``, so operators can disable individual signals without
    rewriting the watcher.
    """

    # Drift uses 0 (not None) when never observed, so a brand-new READY
    # index whose ``last_seen_version`` is NULL gets credit for ALL the
    # versions ever produced.
    seen = last_seen_version if last_seen_version is not None else 0
    version_drift = max(0, int(latest_version) - int(seen))

    # Compute stale duration only when we have a baseline; an index that
    # has never been optimised (e.g. just BUILD-ed) skips the stale path
    # so the watcher does not double up on the build's own rebuild.
    if last_optimized_at is None:
        stale_minutes = 0.0
    else:
        delta = now - last_optimized_at
        stale_minutes = delta.total_seconds() / 60.0

    # Signal 1: unindexed rows.  Only fires when lance reported a count
    # AND the policy enabled this signal AND the count crossed.
    threshold_rows = policy.index_watch_min_unindexed_rows
    if (
        unindexed_rows is not None
        and threshold_rows is not None
        and unindexed_rows >= threshold_rows
    ):
        return TriggerDecision(
            reason=TRIGGER_UNINDEXED_ROWS,
            unindexed_rows=unindexed_rows,
            version_drift=version_drift,
            stale_minutes=stale_minutes,
        )

    # Signal 2: lance version drift.
    threshold_drift = policy.index_watch_min_version_drift
    if threshold_drift is not None and version_drift >= int(threshold_drift):
        return TriggerDecision(
            reason=TRIGGER_VERSION_DRIFT,
            unindexed_rows=unindexed_rows,
            version_drift=version_drift,
            stale_minutes=stale_minutes,
        )

    # Signal 3: stale fallback.
    threshold_stale = policy.index_watch_stale_minutes
    if (
        threshold_stale is not None
        and last_optimized_at is not None
        and stale_minutes >= float(threshold_stale)
    ):
        return TriggerDecision(
            reason=TRIGGER_STALE,
            unindexed_rows=unindexed_rows,
            version_drift=version_drift,
            stale_minutes=stale_minutes,
        )

    return TriggerDecision(
        reason=None,
        unindexed_rows=unindexed_rows,
        version_drift=version_drift,
        stale_minutes=stale_minutes,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_watch_pass(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> WatchPassReport:
    """Scan all watch-enabled READY indexes; emit ``INDEX_OPTIMIZE`` tasks.

    The caller MUST be in a system principal context
    (:func:`lcp.core.tenant.with_system_context`).  Same constraint as
    the lifecycle planner -- the watcher reads across all tenants.

    :param session: Async session.
    :param settings: Override settings (tests inject a fake to avoid
        reading env).  Defaults to :func:`lcp.core.config.get_settings`.
    :param now: Override "current time"; tests use this to make stale
        thresholds deterministic.  Defaults to UTC ``now()``.
    """

    _require_system()
    if settings is None:
        settings = get_settings()
    if now is None:
        now = _utcnow()

    storage_options = lance_io.build_storage_options(settings)

    rows = await _fetch_candidates(session)
    report = WatchPassReport()

    for index, dataset, policy in rows:
        report.scanned_indexes += 1

        if dataset is None:
            # The LEFT JOIN winner has no dataset; should not happen
            # because of the FK CASCADE, but stay defensive.
            report.skipped_no_dataset += 1
            continue

        try:
            stats = await _read_stats_offthread(
                dataset.storage_uri,
                index.index_name,
                storage_options=storage_options,
            )
        except Exception as exc:  # noqa: BLE001 -- never crash the pass
            _LOGGER.warning(
                "watcher: failed to read stats for index %s/%s: %s",
                dataset.dataset_uuid, index.index_name, exc,
            )
            report.skipped_open_failed += 1
            continue

        if stats is None:
            # Index disappeared on disk (e.g. external drop); the LCP
            # row will be reconciled by a future sync; for now skip.
            report.skipped_no_dataset += 1
            continue

        decision = decide_trigger(
            unindexed_rows=stats.num_unindexed_rows,
            latest_version=stats.latest_version,
            last_seen_version=index.last_seen_version,
            last_optimized_at=index.last_optimized_at,
            now=now,
            policy=policy,
        )

        # NOTE on ``last_seen_version`` ownership:
        # The field is owned by :class:`IndexOptimizeExecutor` -- it pins
        # the version that lance wrote *after* the optimize commit.  The
        # watcher deliberately does NOT touch it, because lance itself
        # bumps ``latest_version`` on every successful ``optimize_indices``
        # call; if the watcher updated the field on every pass, we would
        # observe a self-induced drift right after our own optimize and
        # re-fire forever on idle datasets.
        #
        # Repeat-trigger protection within a single pre-optimize version
        # is handled by the ``watcher:<id>:INDEX_OPTIMIZE:v<n>:<reason>``
        # idempotency key, backed by the UNIQUE constraint on
        # ``task.idempotency_key``.

        if decision.reason is None:
            report.skipped_below_threshold += 1
            continue

        emitted, key = await _try_emit_optimize_task(
            session,
            index=index,
            dataset=dataset,
            decision=decision,
            latest_version=stats.latest_version,
            now=now,
        )
        if emitted:
            report.enqueued_tasks += 1
            report.emitted_keys.append(key)
        else:
            report.skipped_idempotent += 1

    if report.scanned_indexes:
        # Even pure "skip" passes are committed so that any future
        # writes added to this loop body land atomically; today the
        # watcher does not mutate index rows itself (last_seen_version
        # is owned by the executor) so the commit is effectively a
        # no-op, but staying in lockstep with the planner pattern
        # avoids surprises if we ever add a watcher-side update.
        await session.commit()

    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Naive UTC datetime to match the DATETIME(3) columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_system() -> None:
    """Refuse to run unless the caller bound a system principal."""

    principal = get_current_tenant()
    if principal is None or not getattr(principal, "is_system", False):
        raise WatcherNotSystemError(
            "index watcher must run under with_system_context()",
        )


async def _read_stats_offthread(
    uri: str,
    index_name: str,
    *,
    storage_options: dict[str, str],
) -> lance_io.IndexLiveStats | None:
    """Run :func:`lance_io.read_index_stats` on a worker thread.

    PyLance is a synchronous C-extension; calling it directly from the
    asyncio event loop would block other coroutines.  Hopping to a
    thread keeps the pass non-blocking even when listing indices is
    slow (cold cache, large object store).
    """

    import asyncio

    return await asyncio.to_thread(
        lance_io.read_index_stats,
        uri,
        index_name,
        storage_options=storage_options or None,
    )


async def _fetch_candidates(
    session: AsyncSession,
) -> Sequence[tuple[Index, Dataset, LifecyclePolicy]]:
    """Return ``(index, dataset, policy)`` triples worth scanning.

    Selection criteria (combined with AND):

    1. Index is in ``READY`` state -- BUILDING / OPTIMIZING / MERGING
       are all transient states that already have a worker on them, so
       the watcher must not double up.  FAILED / DROPPED are terminal.
    2. The dataset has at least one enabled :class:`LifecyclePolicy`
       with ``index_watch_enabled=True``.  We pick the first such
       policy; multi-policy resolution is not in scope.
    """

    # Single round-trip: join Index -> Dataset -> first watch-enabled
    # policy.  Order by (dataset, index) for deterministic test output.
    stmt: Select[Any] = (
        select(Index, Dataset, LifecyclePolicy)
        .join(Dataset, Dataset.dataset_uuid == Index.dataset_uuid)
        .join(LifecyclePolicy, LifecyclePolicy.dataset_uuid == Index.dataset_uuid)
        .where(Index.status == "READY")
        .where(LifecyclePolicy.enabled.is_(True))
        .where(LifecyclePolicy.index_watch_enabled.is_(True))
        .order_by(Index.dataset_uuid.asc(), Index.index_name.asc())
    )
    result = await session.execute(stmt)
    return list(result.all())


async def _try_emit_optimize_task(
    session: AsyncSession,
    *,
    index: Index,
    dataset: Dataset,
    decision: TriggerDecision,
    latest_version: int,
    now: datetime,
) -> tuple[bool, str]:
    """Insert one ``INDEX_OPTIMIZE`` task; return ``(emitted, key)``.

    Side effect (intentional): when emitting, also flips the index from
    ``READY`` to ``OPTIMIZING`` and refreshes ``last_optimized_at`` so
    the executor's ``status == OPTIMIZING`` filter matches this row.
    Mirrors :func:`lcp.services.index_service.optimize_index` -- same
    state-machine transition, just driven by the watcher instead of an
    HTTP caller.

    Idempotency key embeds ``latest_version`` so two passes within the
    same lance version dedupe via the UNIQUE constraint without any
    application-level locking.
    """

    key = (
        f"watcher:{index.id}:{TASK_INDEX_OPTIMIZE}:"
        f"v{latest_version}:{decision.reason}"
    )

    # Cheap pre-check first: avoids the rollback/refetch dance in the
    # common case where the task already exists from an earlier pass.
    existing_stmt: Select[Any] = select(Task.task_uuid).where(
        Task.idempotency_key == key,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return False, key

    # Flip state BEFORE inserting the task so the executor never sees a
    # READY row that points at an already-emitted task.  Same ordering
    # as :func:`index_service.optimize_index`.
    index.status = "OPTIMIZING"
    index.last_optimized_at = now

    task = Task(
        task_uuid=str(uuid.uuid4()),
        task_type=TASK_INDEX_OPTIMIZE,
        dataset_uuid=dataset.dataset_uuid,
        tenant_id=dataset.tenant_id,
        status="PENDING",
        priority=5,
        idempotency_key=key,
        params={
            "index_name": index.index_name,
            "trigger_reason": decision.reason,
            "lance_version": latest_version,
            "unindexed_rows": decision.unindexed_rows,
            "version_drift": decision.version_drift,
            "stale_minutes": round(decision.stale_minutes, 3),
            "source": "index_watcher",
        },
        max_attempts=3,
    )
    session.add(task)
    try:
        await session.flush()
    except IntegrityError:
        # Race: a concurrent pass won this idempotency key.  Roll back
        # so we don't leave the index half-flipped, then mark as dup.
        await session.rollback()
        return False, key

    return True, key
