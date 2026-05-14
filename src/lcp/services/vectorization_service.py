"""Business logic for the ``vectorization_rule`` table.

Tenant isolation strategy
-------------------------
Same pattern as :mod:`lcp.services.lifecycle_service` and
:mod:`lcp.services.index_service`: the table has no ``tenant_id`` column,
so every operation first calls
:func:`lcp.services.dataset_service.get_dataset` (which IS RLS-filtered)
and lets its 404 reject cross-tenant access before any rule row is touched.

PATCH semantics
---------------
The update path uses ``model_dump(exclude_unset=True)`` so that:

- omitted fields keep their prior value;
- explicitly ``null`` fields (e.g. ``model_endpoint: null``) clear the value.

After the merge, the trigger_type / cron_expr cross-field invariant is
re-checked because a PATCH that only ships ``trigger_type=SCHEDULED``
without a ``cron_expr`` would otherwise produce a non-runnable rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Task, VectorizationRule
from lcp.schemas.vectorization import (
    RuleCreateRequest,
    RuleUpdateRequest,
    validate_cron_required_when_scheduled,
)
from lcp.services import dataset_service, task_service

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RuleNotFoundError(Exception):
    """Raised when a (dataset_uuid, target_column) pair cannot be resolved."""


class RuleAlreadyExistsError(Exception):
    """Raised when ``(dataset_uuid, target_column)`` already exists."""


class RuleValidationError(Exception):
    """Raised when a merged-state cross-field invariant is violated."""


class RuleDisabledError(Exception):
    """Raised when ``vectorize-now`` targets a disabled rule."""


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


async def create_rule(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    payload: RuleCreateRequest,
) -> VectorizationRule:
    """Insert a new vectorization rule under the given dataset."""

    # Tenant guard: cross-tenant dataset_uuid -> DatasetNotFoundError.
    await dataset_service.get_dataset(session, dataset_uuid)

    obj = VectorizationRule(
        dataset_uuid=dataset_uuid,
        target_column=payload.target_column,
        source_columns=payload.source_columns,
        model_name=payload.model_name,
        model_version=payload.model_version,
        model_endpoint=payload.model_endpoint,
        batch_size=payload.batch_size,
        trigger_type=payload.trigger_type,
        cron_expr=payload.cron_expr,
        enabled=payload.enabled,
        extra=payload.extra,
    )
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise RuleAlreadyExistsError(
            f"rule on column {payload.target_column!r} already exists "
            f"on dataset {dataset_uuid}",
        ) from exc
    await session.commit()
    await session.refresh(obj)
    return obj


async def get_rule(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
) -> VectorizationRule:
    """Return one rule; cross-tenant access yields 404 via dataset guard."""

    await dataset_service.get_dataset(session, dataset_uuid)
    obj = await _maybe_get_rule(session, dataset_uuid, target_column)
    if obj is None:
        raise RuleNotFoundError(f"{dataset_uuid}/{target_column}")
    return obj


async def list_rules(
    session: AsyncSession,
    dataset_uuid: str,
) -> tuple[Sequence[VectorizationRule], int]:
    """Return ``(items, total)`` for the dataset's rules."""

    await dataset_service.get_dataset(session, dataset_uuid)

    base: Select[Any] = select(VectorizationRule).where(
        VectorizationRule.dataset_uuid == dataset_uuid,
    )
    items = (
        await session.execute(base.order_by(VectorizationRule.created_at.desc()))
    ).scalars().all()

    count_stmt: Select[Any] = select(func.count(VectorizationRule.id)).where(
        VectorizationRule.dataset_uuid == dataset_uuid,
    )
    total = (await session.execute(count_stmt)).scalar_one()
    return items, int(total)


async def update_rule(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
    payload: RuleUpdateRequest,
) -> VectorizationRule:
    """Partial-update; re-checks the trigger_type/cron_expr invariant."""

    obj = await get_rule(session, dataset_uuid, target_column)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(obj, field, value)

    # Re-validate the merged state: a PATCH that flips trigger_type to
    # SCHEDULED without supplying cron_expr would otherwise leave the rule
    # non-runnable.
    try:
        validate_cron_required_when_scheduled(obj.trigger_type, obj.cron_expr)
    except ValueError as exc:
        await session.rollback()
        raise RuleValidationError(str(exc)) from exc

    await session.commit()
    await session.refresh(obj)
    return obj


async def delete_rule(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
) -> None:
    """Hard-delete the rule row; ``ON DELETE CASCADE`` cleans up via the dataset."""

    obj = await get_rule(session, dataset_uuid, target_column)
    await session.delete(obj)
    await session.commit()


async def set_enabled(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
    *,
    enabled: bool,
) -> VectorizationRule:
    """Toggle the ``enabled`` flag (idempotent)."""

    obj = await get_rule(session, dataset_uuid, target_column)
    if obj.enabled != enabled:
        obj.enabled = enabled
        await session.commit()
        await session.refresh(obj)
    return obj


async def submit_vectorize_task(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
) -> tuple[Task, bool]:
    """Enqueue a ``VECTORIZE`` task for the given rule.

    Used by ``POST /v1/datasets/{uuid}/vectorization-rules/{col}/vectorize-now``
    so callers can trigger a one-off run without waiting for a planner /
    cron tick.  Mirrors ``index_service.create_index`` -> ``task_service.
    submit_task`` wiring (Karpathy rule 3: same path, same idempotency-key
    shape).

    Returns ``(task, created)`` so the router can map the boolean to
    202 Accepted (fresh) vs 200 OK (idempotent replay) the same way the
    /v1/tasks endpoint already does.

    Raises:
        DatasetNotFoundError: cross-tenant or unknown dataset.
        RuleNotFoundError:    rule row missing.
        RuleDisabledError:    rule exists but ``enabled=False``; running a
            disabled rule would surprise operators who turned it off.
    """

    rule = await get_rule(session, dataset_uuid, target_column)
    if not rule.enabled:
        raise RuleDisabledError(
            f"vectorization rule on {target_column!r} is disabled; "
            f"enable it before requesting vectorize-now",
        )

    # Idempotency key: per-rule, per-row-id so two rapid HTTP retries from
    # the same client coalesce, but a re-create of the rule (different id)
    # gets its own task.  Matches the ``build:<dataset>/<name>:<id>`` shape
    # used by index_service.create_index.
    idempotency_key = f"vectorize:{dataset_uuid}/{target_column}:{rule.id}"
    return await task_service.submit_task(
        session,
        task_type="VECTORIZE",
        dataset_uuid=dataset_uuid,
        params={"vectorization_rule_id": rule.id},
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _maybe_get_rule(
    session: AsyncSession,
    dataset_uuid: str,
    target_column: str,
) -> VectorizationRule | None:
    """Return the rule row or None; assumes the dataset guard already ran."""

    stmt: Select[Any] = select(VectorizationRule).where(
        VectorizationRule.dataset_uuid == dataset_uuid,
        VectorizationRule.target_column == target_column,
    )
    return (await session.execute(stmt)).scalar_one_or_none()
