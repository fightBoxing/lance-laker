"""Datasets router (stub).

Mirrors ``docs/architecture/api/openapi/lcp-control-api.yaml``.  All handlers
return ``501 Not Implemented`` until the service layer is delivered.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])


@router.get("", summary="List datasets")
async def list_datasets(
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Return paginated datasets for the caller's tenant."""

    # NOTE: Skeleton only — real query is RLS-filtered automatically.
    return {"tenant": principal.tenant_id, "items": [], "next_page_token": None}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create dataset")
async def create_dataset(
    payload: dict[str, object],
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Register a new dataset (Lance table)."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"received": payload, "tenant": principal.tenant_id},
    )


@router.get("/{dataset_id}", summary="Get dataset")
async def get_dataset(
    dataset_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Return a single dataset by id."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"dataset_id": dataset_id, "tenant": principal.tenant_id},
    )


@router.delete(
    "/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete dataset",
)
async def delete_dataset(
    dataset_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> None:
    """Soft-delete a dataset; lifecycle worker reclaims storage later."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"dataset_id": dataset_id, "tenant": principal.tenant_id},
    )
