"""Vectorization-rule router.

Mirrors ``docs/architecture/api/openapi/lcp-vectorization-api.yaml``.  Routes
are nested under ``/v1/datasets/{dataset_uuid}/vectorization-rules`` so the
path itself expresses the rule-belongs-to-dataset relationship.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.task import TaskResponse
from lcp.schemas.vectorization import (
    RuleCreateRequest,
    RuleListResponse,
    RuleResponse,
    RuleUpdateRequest,
)
from lcp.services import dataset_service, vectorization_service

router = APIRouter(
    prefix="/v1/datasets/{dataset_uuid}/vectorization-rules",
    tags=["vectorization"],
)


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOT_FOUND", "message": message},
    )


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "INVALID_ARGUMENT", "message": message},
    )


@router.get("", response_model=RuleListResponse, summary="List rules")
async def list_rules(
    dataset_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleListResponse:
    """List vectorization rules for the dataset."""

    try:
        items, total = await vectorization_service.list_rules(session, dataset_uuid)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    return RuleListResponse(
        total=total,
        items=[RuleResponse.model_validate(it) for it in items],
    )


@router.post(
    "",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create rule",
)
async def create_rule(
    dataset_uuid: str,
    payload: RuleCreateRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleResponse:
    """Create a vectorization rule under the dataset."""

    try:
        obj = await vectorization_service.create_rule(
            session,
            dataset_uuid=dataset_uuid,
            payload=payload,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleAlreadyExistsError as exc:
        raise _conflict("ALREADY_EXISTS", str(exc)) from exc
    return RuleResponse.model_validate(obj)


@router.get(
    "/{target_column}",
    response_model=RuleResponse,
    summary="Get rule",
)
async def get_rule(
    dataset_uuid: str,
    target_column: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleResponse:
    """Return a single rule by target column."""

    try:
        obj = await vectorization_service.get_rule(
            session, dataset_uuid, target_column,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc
    return RuleResponse.model_validate(obj)


@router.patch(
    "/{target_column}",
    response_model=RuleResponse,
    summary="Update rule",
)
async def update_rule(
    dataset_uuid: str,
    target_column: str,
    payload: RuleUpdateRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleResponse:
    """Partial-update: only supplied fields are mutated."""

    try:
        obj = await vectorization_service.update_rule(
            session, dataset_uuid, target_column, payload,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc
    except vectorization_service.RuleValidationError as exc:
        raise _bad_request(str(exc)) from exc
    return RuleResponse.model_validate(obj)


@router.delete(
    "/{target_column}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete rule",
)
async def delete_rule(
    dataset_uuid: str,
    target_column: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> None:
    """Hard-delete the rule row."""

    try:
        await vectorization_service.delete_rule(
            session, dataset_uuid, target_column,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc


@router.post(
    "/{target_column}/enable",
    response_model=RuleResponse,
    summary="Enable rule",
)
async def enable_rule(
    dataset_uuid: str,
    target_column: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleResponse:
    """Idempotent: enable the rule."""

    try:
        obj = await vectorization_service.set_enabled(
            session, dataset_uuid, target_column, enabled=True,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc
    return RuleResponse.model_validate(obj)


@router.post(
    "/{target_column}/disable",
    response_model=RuleResponse,
    summary="Disable rule",
)
async def disable_rule(
    dataset_uuid: str,
    target_column: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> RuleResponse:
    """Idempotent: disable the rule."""

    try:
        obj = await vectorization_service.set_enabled(
            session, dataset_uuid, target_column, enabled=False,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc
    return RuleResponse.model_validate(obj)


@router.post(
    "/{target_column}/vectorize-now",
    response_model=TaskResponse,
    summary="Trigger one-off vectorize task",
)
async def vectorize_now(
    dataset_uuid: str,
    target_column: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> TaskResponse:
    """Enqueue a one-off ``VECTORIZE`` task for the rule.

    Idempotent per ``(dataset_uuid, target_column, rule_id)``: a second
    request returns the existing task instead of creating a duplicate,
    so retries from a flaky client are safe.

    Returns the task row; the worker will eventually transition it to
    ``SUCCEEDED`` / ``FAILED`` and the caller can poll
    ``GET /v1/tasks/{task_uuid}`` for the outcome.
    """

    try:
        task, _created = await vectorization_service.submit_vectorize_task(
            session, dataset_uuid, target_column,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except vectorization_service.RuleNotFoundError as exc:
        raise _not_found(
            f"rule on column {target_column!r} not found "
            f"on dataset {dataset_uuid}",
        ) from exc
    except vectorization_service.RuleDisabledError as exc:
        raise _conflict("RULE_DISABLED", str(exc)) from exc
    return TaskResponse.model_validate(task)
