"""Indexes router (stub).

Mirrors ``docs/architecture/api/openapi/lcp-index-api.yaml``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal

router = APIRouter(prefix="/v1/indexes", tags=["indexes"])


@router.get("", summary="List indexes")
async def list_indexes(
    dataset_id: str | None = None,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """List vector indexes, optionally scoped to a dataset."""

    return {
        "tenant": principal.tenant_id,
        "items": [],
        "filter": {"dataset_id": dataset_id},
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Create index")
async def create_index(
    payload: dict[str, object],
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Schedule a full index build (HNSW / IVF-PQ)."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"payload": payload, "tenant": principal.tenant_id},
    )


@router.post("/{index_id}/optimize", summary="Optimise index")
async def optimize_index(
    index_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Trigger incremental index optimisation."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"index_id": index_id, "tenant": principal.tenant_id},
    )


@router.delete(
    "/{index_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop index",
)
async def drop_index(
    index_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> None:
    """Drop an index; storage is reclaimed asynchronously."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"index_id": index_id, "tenant": principal.tenant_id},
    )
