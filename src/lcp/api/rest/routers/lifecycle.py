"""Lifecycle policies router.

Mirrors ``docs/architecture/api/openapi/lcp-lifecycle-api.yaml``.  Routes are
nested under ``/v1/datasets/{dataset_uuid}/lifecycle-policies`` so the path
itself expresses the policy-belongs-to-dataset relationship.

Tenancy isolation is delegated to :mod:`lcp.services.lifecycle_service`,
which loads the parent dataset (RLS-filtered) before any policy operation.
A cross-tenant ``dataset_uuid`` is rejected with 404 *before* the policy
table is touched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.lifecycle import (
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyResponse,
    PolicyUpdateRequest,
)
from lcp.services import dataset_service, lifecycle_service

router = APIRouter(
    prefix="/v1/datasets/{dataset_uuid}/lifecycle-policies",
    tags=["lifecycle"],
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


@router.get("", response_model=PolicyListResponse, summary="List policies")
async def list_policies(
    dataset_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyListResponse:
    """List lifecycle policies attached to the dataset."""

    try:
        items, total = await lifecycle_service.list_policies(session, dataset_uuid)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    return PolicyListResponse(
        total=total,
        items=[PolicyResponse.model_validate(it) for it in items],
    )


@router.post(
    "",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create policy",
)
async def create_policy(
    dataset_uuid: str,
    payload: PolicyCreateRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyResponse:
    """Create a new lifecycle policy under the dataset."""

    try:
        obj = await lifecycle_service.create_policy(
            session,
            dataset_uuid=dataset_uuid,
            payload=payload,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyAlreadyExistsError as exc:
        raise _conflict("ALREADY_EXISTS", str(exc)) from exc
    return PolicyResponse.model_validate(obj)


@router.get(
    "/{policy_name}",
    response_model=PolicyResponse,
    summary="Get policy",
)
async def get_policy(
    dataset_uuid: str,
    policy_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyResponse:
    """Return a single policy by name."""

    try:
        obj = await lifecycle_service.get_policy(session, dataset_uuid, policy_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyNotFoundError as exc:
        raise _not_found(
            f"policy {policy_name!r} not found on dataset {dataset_uuid}",
        ) from exc
    return PolicyResponse.model_validate(obj)


@router.patch(
    "/{policy_name}",
    response_model=PolicyResponse,
    summary="Update policy",
)
async def update_policy(
    dataset_uuid: str,
    policy_name: str,
    payload: PolicyUpdateRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyResponse:
    """Partial-update: only supplied fields are mutated."""

    try:
        obj = await lifecycle_service.update_policy(
            session, dataset_uuid, policy_name, payload,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyNotFoundError as exc:
        raise _not_found(
            f"policy {policy_name!r} not found on dataset {dataset_uuid}",
        ) from exc
    return PolicyResponse.model_validate(obj)


@router.delete(
    "/{policy_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete policy",
)
async def delete_policy(
    dataset_uuid: str,
    policy_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> None:
    """Hard-delete the policy row."""

    try:
        await lifecycle_service.delete_policy(session, dataset_uuid, policy_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyNotFoundError as exc:
        raise _not_found(
            f"policy {policy_name!r} not found on dataset {dataset_uuid}",
        ) from exc


@router.post(
    "/{policy_name}/enable",
    response_model=PolicyResponse,
    summary="Enable policy",
)
async def enable_policy(
    dataset_uuid: str,
    policy_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyResponse:
    """Idempotent: enable the policy."""

    try:
        obj = await lifecycle_service.set_enabled(
            session, dataset_uuid, policy_name, enabled=True,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyNotFoundError as exc:
        raise _not_found(
            f"policy {policy_name!r} not found on dataset {dataset_uuid}",
        ) from exc
    return PolicyResponse.model_validate(obj)


@router.post(
    "/{policy_name}/disable",
    response_model=PolicyResponse,
    summary="Disable policy",
)
async def disable_policy(
    dataset_uuid: str,
    policy_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> PolicyResponse:
    """Idempotent: disable the policy."""

    try:
        obj = await lifecycle_service.set_enabled(
            session, dataset_uuid, policy_name, enabled=False,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except lifecycle_service.PolicyNotFoundError as exc:
        raise _not_found(
            f"policy {policy_name!r} not found on dataset {dataset_uuid}",
        ) from exc
    return PolicyResponse.model_validate(obj)
