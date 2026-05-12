"""Indexes router.

Mirrors ``docs/architecture/api/openapi/lcp-index-api.yaml``.  Routes are
nested under ``/v1/datasets/{dataset_uuid}/indexes`` so the path itself
expresses the index-belongs-to-dataset relationship.

Tenancy isolation is delegated to :mod:`lcp.services.index_service`, which
loads the parent dataset (RLS-filtered) before any index operation.  This
means a cross-tenant ``dataset_uuid`` is rejected with 404 *before* the
index table is touched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.index import IndexCreateRequest, IndexListResponse, IndexResponse
from lcp.services import dataset_service, index_service

router = APIRouter(prefix="/v1/datasets/{dataset_uuid}/indexes", tags=["indexes"])


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


@router.get("", response_model=IndexListResponse, summary="List indexes")
async def list_indexes(
    dataset_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> IndexListResponse:
    """List indexes attached to the dataset (caller's tenant scope)."""

    try:
        items, total = await index_service.list_indexes(session, dataset_uuid)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    return IndexListResponse(
        total=total,
        items=[IndexResponse.model_validate(it) for it in items],
    )


@router.post(
    "",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create index",
)
async def create_index(
    dataset_uuid: str,
    payload: IndexCreateRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> IndexResponse:
    """Schedule a full index build (HNSW / IVF-PQ / etc.)."""

    try:
        obj = await index_service.create_index(
            session,
            dataset_uuid=dataset_uuid,
            index_name=payload.index_name,
            column_name=payload.column_name,
            index_type=payload.index_type,
            params=payload.params,
            replace_if_exists=payload.replace_if_exists,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except index_service.IndexAlreadyExistsError as exc:
        raise _conflict("ALREADY_EXISTS", str(exc)) from exc
    return IndexResponse.model_validate(obj)


@router.get(
    "/{index_name}",
    response_model=IndexResponse,
    summary="Get index",
)
async def get_index(
    dataset_uuid: str,
    index_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> IndexResponse:
    """Return params, coverage and status of a single index."""

    try:
        obj = await index_service.get_index(session, dataset_uuid, index_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except index_service.IndexNotFoundError as exc:
        raise _not_found(f"index {index_name!r} not found on dataset {dataset_uuid}") from exc
    return IndexResponse.model_validate(obj)


@router.delete(
    "/{index_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop index",
)
async def drop_index(
    dataset_uuid: str,
    index_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> None:
    """Soft-delete the index; lifecycle worker reclaims storage later."""

    try:
        await index_service.drop_index(session, dataset_uuid, index_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except index_service.IndexNotFoundError as exc:
        raise _not_found(f"index {index_name!r} not found on dataset {dataset_uuid}") from exc


@router.post(
    "/{index_name}/optimize",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Optimize index",
)
async def optimize_index(
    dataset_uuid: str,
    index_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> IndexResponse:
    """Trigger incremental index optimization."""

    try:
        obj = await index_service.optimize_index(session, dataset_uuid, index_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except index_service.IndexNotFoundError as exc:
        raise _not_found(f"index {index_name!r} not found on dataset {dataset_uuid}") from exc
    except index_service.IndexTransitionError as exc:
        raise _conflict("INVALID_TRANSITION", str(exc)) from exc
    return IndexResponse.model_validate(obj)


@router.post(
    "/{index_name}/merge",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Merge index deltas",
)
async def merge_index(
    dataset_uuid: str,
    index_name: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> IndexResponse:
    """Merge unmerged deltas to reduce fragmentation."""

    try:
        obj = await index_service.merge_index(session, dataset_uuid, index_name)
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except index_service.IndexNotFoundError as exc:
        raise _not_found(f"index {index_name!r} not found on dataset {dataset_uuid}") from exc
    except index_service.IndexTransitionError as exc:
        raise _conflict("INVALID_TRANSITION", str(exc)) from exc
    return IndexResponse.model_validate(obj)
