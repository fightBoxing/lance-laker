"""Vector search router (β.4).

Provides ``POST /v1/datasets/{dataset_uuid}/search`` for ANN (or flat-scan)
nearest-neighbour retrieval against a lance vector column.

Tenancy isolation is delegated to :mod:`lcp.services.search_service`, which
loads the parent dataset (RLS-filtered) before any lance operation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.search import VectorSearchRequest, VectorSearchResponse
from lcp.services import dataset_service, search_service

router = APIRouter(prefix="/v1/datasets/{dataset_uuid}", tags=["search"])


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOT_FOUND", "message": message},
    )


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )


@router.post(
    "/search",
    response_model=VectorSearchResponse,
    summary="Vector search",
)
async def vector_search(
    dataset_uuid: str,
    payload: VectorSearchRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> VectorSearchResponse:
    """Run ANN (or flat-scan) nearest-neighbour search on a vector column.

    Lance automatically uses the best available index on the requested
    column; if no index exists it falls back to a brute-force scan.
    """

    try:
        results = await search_service.search(
            session,
            dataset_uuid=dataset_uuid,
            vector=payload.vector,
            column=payload.column,
            k=payload.k,
            filter_expr=payload.filter,
            select_columns=payload.select,
            nprobes=payload.nprobes,
            refine_factor=payload.refine_factor,
        )
    except dataset_service.DatasetNotFoundError as exc:
        raise _not_found(f"dataset {dataset_uuid} not found") from exc
    except search_service.ColumnNotFoundError as exc:
        raise _bad_request("COLUMN_NOT_FOUND", str(exc)) from exc
    except search_service.DimensionMismatchError as exc:
        raise _bad_request("DIMENSION_MISMATCH", str(exc)) from exc

    return VectorSearchResponse(
        results=results,
        count=len(results),
        column=payload.column,
        k=payload.k,
    )
