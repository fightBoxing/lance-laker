"""Datasets router.

Mirrors ``docs/architecture/api/openapi/lcp-control-api.yaml``.  All persistence
work lives in :mod:`lcp.services.dataset_service`; the router only wires HTTP
to the service layer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.dataset import (
    DatasetListResponse,
    DatasetRegisterRequest,
    DatasetResponse,
)
from lcp.services import dataset_service

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])


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


@router.get("", response_model=DatasetListResponse, summary="List datasets")
async def list_datasets(
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
    catalog: Annotated[str | None, Query()] = None,
    schema: Annotated[str | None, Query(alias="schema")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> DatasetListResponse:
    """Return paginated datasets for the caller's tenant."""

    items, total = await dataset_service.list_datasets(
        session,
        catalog=catalog,
        db_schema=schema,
        page=page,
        page_size=page_size,
    )
    return DatasetListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[DatasetResponse.model_validate(it) for it in items],
    )


@router.post(
    "",
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create dataset",
)
async def create_dataset(
    payload: DatasetRegisterRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> DatasetResponse:
    """Register a new dataset (Lance table)."""

    try:
        obj = await dataset_service.create_dataset(session, payload)
    except dataset_service.DatasetAlreadyExistsError as exc:
        raise _conflict("ALREADY_EXISTS", str(exc)) from exc
    return DatasetResponse.model_validate(obj)


@router.get(
    "/{dataset_uuid}",
    response_model=DatasetResponse,
    summary="Get dataset",
)
async def get_dataset(
    dataset_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> DatasetResponse:
    """Return a single dataset by UUID."""

    try:
        obj = await dataset_service.get_dataset(session, dataset_uuid)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    return DatasetResponse.model_validate(obj)


@router.delete(
    "/{dataset_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete dataset",
)
async def delete_dataset(
    dataset_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> None:
    """Soft-delete a dataset; lifecycle worker reclaims storage later."""

    try:
        await dataset_service.delete_dataset(session, dataset_uuid)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
