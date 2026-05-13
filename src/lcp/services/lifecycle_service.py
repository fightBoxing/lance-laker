"""Business logic for the ``lifecycle_policy`` table.

Tenant isolation strategy
-------------------------
Same pattern as :mod:`lcp.services.index_service`: ``lifecycle_policy`` has no
``tenant_id`` column, so every operation first calls
:func:`lcp.services.dataset_service.get_dataset` (which IS RLS-filtered) and
relies on its 404 to reject cross-tenant access.

What this module does NOT do
----------------------------
- It does NOT execute the policy.  The lifecycle worker (out of scope for
  this iteration) will read the rows and schedule ``Task`` rows accordingly.
- It does NOT validate the contents of ``tier_rules`` or
  ``compaction_threshold``.  Those JSON shapes are intentionally free-form
  so the policy schema can evolve independently of the API surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import LifecyclePolicy
from lcp.schemas.lifecycle import PolicyCreateRequest, PolicyUpdateRequest
from lcp.services import dataset_service

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PolicyNotFoundError(Exception):
    """Raised when a (dataset_uuid, policy_name) pair cannot be resolved."""


class PolicyAlreadyExistsError(Exception):
    """Raised when ``(dataset_uuid, policy_name)`` already exists."""


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


async def create_policy(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    payload: PolicyCreateRequest,
) -> LifecyclePolicy:
    """Insert a new lifecycle policy under the given dataset."""

    # Tenant guard: cross-tenant dataset_uuid -> DatasetNotFoundError.
    await dataset_service.get_dataset(session, dataset_uuid)

    obj = LifecyclePolicy(
        dataset_uuid=dataset_uuid,
        policy_name=payload.policy_name,
        tier_rules=payload.tier_rules,
        ttl_days=payload.ttl_days,
        compaction_threshold=payload.compaction_threshold,
        index_optimize_cron=payload.index_optimize_cron,
        index_watch_enabled=payload.index_watch_enabled,
        index_watch_min_unindexed_rows=payload.index_watch_min_unindexed_rows,
        index_watch_min_version_drift=payload.index_watch_min_version_drift,
        index_watch_stale_minutes=payload.index_watch_stale_minutes,
        enabled=payload.enabled,
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise PolicyAlreadyExistsError(
            f"policy {payload.policy_name!r} already exists on dataset {dataset_uuid}",
        ) from exc
    await session.commit()
    await session.refresh(obj)
    return obj


async def get_policy(
    session: AsyncSession,
    dataset_uuid: str,
    policy_name: str,
) -> LifecyclePolicy:
    """Return one policy; cross-tenant access yields 404 via dataset guard."""

    await dataset_service.get_dataset(session, dataset_uuid)
    obj = await _maybe_get_policy(session, dataset_uuid, policy_name)
    if obj is None:
        raise PolicyNotFoundError(f"{dataset_uuid}/{policy_name}")
    return obj


async def list_policies(
    session: AsyncSession,
    dataset_uuid: str,
) -> tuple[Sequence[LifecyclePolicy], int]:
    """Return ``(items, total)`` for the dataset's policies."""

    await dataset_service.get_dataset(session, dataset_uuid)

    base: Select[Any] = select(LifecyclePolicy).where(
        LifecyclePolicy.dataset_uuid == dataset_uuid,
    )
    items = (
        await session.execute(base.order_by(LifecyclePolicy.created_at.desc()))
    ).scalars().all()

    count_stmt: Select[Any] = select(func.count(LifecyclePolicy.id)).where(
        LifecyclePolicy.dataset_uuid == dataset_uuid,
    )
    total = (await session.execute(count_stmt)).scalar_one()
    return items, int(total)


async def update_policy(
    session: AsyncSession,
    dataset_uuid: str,
    policy_name: str,
    payload: PolicyUpdateRequest,
) -> LifecyclePolicy:
    """Partial-update: only fields explicitly supplied in the body are mutated.

    Uses ``model_dump(exclude_unset=True)`` so omitting a field keeps the
    existing value; supplying ``null`` (e.g. ``ttl_days: null``) clears it.
    """

    obj = await get_policy(session, dataset_uuid, policy_name)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(obj, field, value)
    await session.commit()
    await session.refresh(obj)
    return obj


async def delete_policy(
    session: AsyncSession,
    dataset_uuid: str,
    policy_name: str,
) -> None:
    """Hard-delete a policy row.

    Unlike :func:`dataset_service.delete_dataset` (soft-delete), policies are
    cheap rows with no derived state, so a real ``DELETE`` keeps the table
    clean.  ``ON DELETE CASCADE`` from the parent dataset handles teardown
    when the dataset itself is removed.
    """

    obj = await get_policy(session, dataset_uuid, policy_name)
    await session.delete(obj)
    await session.commit()


async def set_enabled(
    session: AsyncSession,
    dataset_uuid: str,
    policy_name: str,
    *,
    enabled: bool,
) -> LifecyclePolicy:
    """Toggle the ``enabled`` flag (idempotent)."""

    obj = await get_policy(session, dataset_uuid, policy_name)
    if obj.enabled != enabled:
        obj.enabled = enabled
        await session.commit()
        await session.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _maybe_get_policy(
    session: AsyncSession,
    dataset_uuid: str,
    policy_name: str,
) -> LifecyclePolicy | None:
    """Return the policy row or None; assumes the dataset guard already ran."""

    stmt: Select[Any] = select(LifecyclePolicy).where(
        LifecyclePolicy.dataset_uuid == dataset_uuid,
        LifecyclePolicy.policy_name == policy_name,
    )
    return (await session.execute(stmt)).scalar_one_or_none()
