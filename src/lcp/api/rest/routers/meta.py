"""Meta sync router (stub).

Mirrors ``docs/architecture/api/openapi/lcp-meta-api.yaml`` — endpoints used
by Gravitino integration and meta reconciliation jobs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal

router = APIRouter(prefix="/v1/meta", tags=["meta"])


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED, summary="Trigger meta sync")
async def trigger_meta_sync(
    payload: dict[str, object],
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Reconcile Gravitino metadata with the LCP state store."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"payload": payload, "tenant": principal.tenant_id},
    )


@router.get("/sync/{run_id}", summary="Get meta sync status")
async def get_meta_sync_status(
    run_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Return progress and diff summary for a sync run."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"run_id": run_id, "tenant": principal.tenant_id},
    )


@router.get("/datasets/{dataset_id}/snapshot", summary="Dataset snapshot meta")
async def get_dataset_snapshot(
    dataset_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Return the latest committed snapshot metadata for a dataset."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"dataset_id": dataset_id, "tenant": principal.tenant_id},
    )
