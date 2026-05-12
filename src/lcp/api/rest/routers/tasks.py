"""Tasks router (stub).

Mirrors ``docs/architecture/api/openapi/lcp-task-api.yaml``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.get("", summary="List tasks")
async def list_tasks(
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
    status_filter: str | None = None,
) -> dict[str, object]:
    """List background tasks (compaction, indexing, lifecycle)."""

    return {
        "tenant": principal.tenant_id,
        "items": [],
        "filter": {"status": status_filter},
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Submit task")
async def submit_task(
    payload: dict[str, object],
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Submit a task; returns ``202 Accepted`` with a task id."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"payload": payload, "tenant": principal.tenant_id},
    )


@router.get("/{task_id}", summary="Get task status")
async def get_task(
    task_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Return progress and metadata for a single task."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"task_id": task_id, "tenant": principal.tenant_id},
    )


@router.post("/{task_id}/cancel", summary="Cancel task")
async def cancel_task(
    task_id: str,
    principal: TenantPrincipal = PrincipalDep,
    _session: object = SessionDep,
) -> dict[str, object]:
    """Best-effort cancellation; idempotent."""

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"task_id": task_id, "tenant": principal.tenant_id},
    )
