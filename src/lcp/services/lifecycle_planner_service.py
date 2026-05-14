"""Lifecycle planner: turn enabled ``lifecycle_policy`` rows into ``task`` rows.

This is the *first* end-to-end business loop in LCP.  Until now the project
had:

- metadata CRUD (datasets / tasks / indexes / lifecycle_policies / ...)
- scheduler primitives (workers can claim/complete tasks)

What was missing was the bridge: something that *reads user-authored rules*
and *creates the tasks* the scheduler will then dispatch.  This module is
that bridge.

Design notes
------------

1. **System principal only.**  Like ``scheduler_service``, every public
   function here MUST run under :func:`lcp.core.tenant.with_system_context`.
   The planner reads policies and datasets across all tenants; the RLS
   bypass (``is_system=True``) makes that legal while keeping the rest of
   the system fail-closed.

2. **Idempotency by construction.**  Each emitted task gets an
   ``idempotency_key`` that encodes:

   - the policy row id,
   - the task type, and
   - a coarse time bucket (per-day for TTL/COMPACTION, per-hour for
     INDEX_OPTIMIZE).

   Because ``task.idempotency_key`` is ``UNIQUE`` in the DDL, repeated
   ``plan_once()`` calls within the same bucket are guaranteed to be
   no-ops -- no need for a separate ``lifecycle_run`` history table.

3. **Tenant preservation.**  The planner sets ``task.tenant_id`` to the
   parent dataset's ``tenant_id``.  This is the contract every worker
   relies on to scope its work to one tenant at a time.

4. **Cron is intentionally simplified.**  ``index_optimize_cron`` is
   parsed as a presence-flag for now ("if set, the user wants periodic
   optimization") and the planner emits at most one INDEX_OPTIMIZE task
   per dataset per hour.  Replacing this with ``croniter`` is a follow-up
   task; doing it now would require timezone plumbing and is not on the
   critical path of the end-to-end loop.

What this module does NOT do
----------------------------

- It does NOT execute the task (workers do).
- It does NOT run a daemon; ``plan_once()`` is meant to be invoked by an
  external cron / k8s CronJob / supervisor.
- It does NOT delete data; even ``TTL_DELETE`` is just a metadata task,
  the actual data deletion happens in the worker that consumes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.tenant import NotSystemPrincipalError, require_system_context
from lcp.core.time import utcnow_naive
from lcp.db.models import Dataset, LifecyclePolicy, Task

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


TASK_TTL_DELETE = "TTL_DELETE"
TASK_COMPACTION = "COMPACTION"
TASK_INDEX_OPTIMIZE = "INDEX_OPTIMIZE"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlannerNotSystemError(NotSystemPrincipalError):
    """Raised when planner primitives run without a system principal.

    Subclasses :class:`lcp.core.tenant.NotSystemPrincipalError` so callers
    can catch either the specific or generic form.
    """


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerTick:
    """Outcome of a single :func:`plan_once` invocation.

    Returned to the caller (CLI / cron / test) so they can surface metrics
    or assert in tests without re-querying the database.
    """

    scanned_policies: int
    emitted_tasks: list[str]   # task_uuids that were freshly inserted
    skipped_duplicates: int    # idempotency_key collisions (already planned)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# ``utcnow_naive`` is imported from ``lcp.core.time``.


def _require_system() -> None:
    """Wrap :func:`require_system_context` to raise the planner-specific type."""

    try:
        require_system_context("lifecycle planner")
    except NotSystemPrincipalError:
        raise PlannerNotSystemError(
            "lifecycle planner must run under with_system_context()",
        ) from None


def _day_bucket(now: datetime) -> str:
    """Return a YYYYMMDD string for daily-granularity idempotency keys."""

    return now.strftime("%Y%m%d")


def _hour_bucket(now: datetime) -> str:
    """Return a YYYYMMDDHH string for hourly-granularity idempotency keys."""

    return now.strftime("%Y%m%d%H")


def _idempotency_key(
    policy_id: int,
    task_type: str,
    bucket: str,
) -> str:
    """Compose a stable idempotency key for a planner-emitted task.

    The format is ``lifecycle:<policy_id>:<task_type>:<bucket>`` and never
    exceeds 128 characters (DDL limit).
    """

    return f"lifecycle:{policy_id}:{task_type}:{bucket}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def plan_once(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> PlannerTick:
    """Scan all enabled policies and emit tasks that are due.

    The caller controls the cadence: invoke this from a cron / k8s
    CronJob / unit-test loop.  Each call is idempotent thanks to the
    per-policy idempotency keys -- safe to run every minute.

    :param session: AsyncSession bound under :func:`with_system_context`.
    :param now: Override "current time"; tests use this to deterministically
        cross day/hour boundaries.  Defaults to UTC ``now()``.
    """

    _require_system()
    if now is None:
        now = utcnow_naive()

    policies = await _fetch_enabled_policies(session)
    emitted: list[str] = []
    skipped = 0

    for policy in policies:
        # Tenancy: load the parent dataset so we can stamp tenant_id on the
        # emitted task.  A missing dataset means the policy was orphaned by
        # a CASCADE that fired in between -- silently skip; the policy row
        # will be cleaned up by the dataset CASCADE on its own.
        dataset = await _fetch_dataset(session, policy.dataset_uuid)
        if dataset is None:
            continue

        new_uuids, dup = await _plan_for_policy(
            session, policy=policy, dataset=dataset, now=now,
        )
        emitted.extend(new_uuids)
        skipped += dup

    if emitted or skipped:
        # Even pure "skip" ticks should not have to roll back; commit so
        # last_run_at updates land.
        await session.commit()

    return PlannerTick(
        scanned_policies=len(policies),
        emitted_tasks=emitted,
        skipped_duplicates=skipped,
    )


# ---------------------------------------------------------------------------
# Per-policy emission
# ---------------------------------------------------------------------------


async def _plan_for_policy(
    session: AsyncSession,
    *,
    policy: LifecyclePolicy,
    dataset: Dataset,
    now: datetime,
) -> tuple[list[str], int]:
    """Emit zero-or-more tasks for one policy; return (uuids, dup_count)."""

    emitted: list[str] = []
    duplicates = 0

    # 1. TTL_DELETE -- only if the user opted in by setting ttl_days.
    if policy.ttl_days is not None:
        new_uuid, was_dup = await _try_emit_task(
            session,
            policy=policy,
            dataset=dataset,
            task_type=TASK_TTL_DELETE,
            bucket=_day_bucket(now),
            params={
                "ttl_days": policy.ttl_days,
                "policy_name": policy.policy_name,
            },
        )
        if was_dup:
            duplicates += 1
        elif new_uuid is not None:
            emitted.append(new_uuid)

    # 2. COMPACTION -- only if compaction_threshold is configured.
    if policy.compaction_threshold:
        new_uuid, was_dup = await _try_emit_task(
            session,
            policy=policy,
            dataset=dataset,
            task_type=TASK_COMPACTION,
            bucket=_day_bucket(now),
            params={
                "threshold": policy.compaction_threshold,
                "policy_name": policy.policy_name,
            },
        )
        if was_dup:
            duplicates += 1
        elif new_uuid is not None:
            emitted.append(new_uuid)

    # 3. INDEX_OPTIMIZE -- presence-flag interpretation of cron.
    # TODO: replace presence check with croniter-based next_run computation
    #       once timezone handling is decided.
    if policy.index_optimize_cron:
        new_uuid, was_dup = await _try_emit_task(
            session,
            policy=policy,
            dataset=dataset,
            task_type=TASK_INDEX_OPTIMIZE,
            bucket=_hour_bucket(now),
            params={
                "cron": policy.index_optimize_cron,
                "policy_name": policy.policy_name,
            },
        )
        if was_dup:
            duplicates += 1
        elif new_uuid is not None:
            emitted.append(new_uuid)

    # Record the planner's pass against this policy regardless of outcome
    # so operators can see "planner saw this policy at T".
    policy.last_run_at = now

    return emitted, duplicates


async def _try_emit_task(
    session: AsyncSession,
    *,
    policy: LifecyclePolicy,
    dataset: Dataset,
    task_type: str,
    bucket: str,
    params: dict[str, Any],
) -> tuple[str | None, bool]:
    """Insert one task; on UNIQUE collision, treat as a duplicate skip.

    Returns ``(task_uuid_or_None, was_duplicate)``.
    """

    key = _idempotency_key(policy.id, task_type, bucket)

    # Cheap pre-check: if the key is already there, save a round-trip.
    existing_stmt: Select[Any] = select(Task.task_uuid).where(
        Task.idempotency_key == key,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return None, True

    task_uuid = str(uuid.uuid4())
    task = Task(
        task_uuid=task_uuid,
        task_type=task_type,
        dataset_uuid=dataset.dataset_uuid,
        tenant_id=dataset.tenant_id,
        status="PENDING",
        priority=5,
        idempotency_key=key,
        params=params,
        max_attempts=3,
    )
    session.add(task)
    try:
        await session.flush()
    except IntegrityError:
        # Race: another planner instance won this bucket between our
        # SELECT and INSERT.  That is exactly what idempotency keys are
        # for; treat as a duplicate.
        await session.rollback()
        return None, True
    return task_uuid, False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_enabled_policies(
    session: AsyncSession,
) -> Sequence[LifecyclePolicy]:
    """Return every enabled policy across all tenants.

    Order by ``id`` for deterministic emission order in tests.
    """

    stmt: Select[Any] = (
        select(LifecyclePolicy)
        .where(LifecyclePolicy.enabled.is_(True))
        .order_by(LifecyclePolicy.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def _fetch_dataset(
    session: AsyncSession,
    dataset_uuid: str,
) -> Dataset | None:
    """Return the parent dataset row, or None if it has been deleted."""

    stmt: Select[Any] = select(Dataset).where(Dataset.dataset_uuid == dataset_uuid)
    return (await session.execute(stmt)).scalar_one_or_none()
