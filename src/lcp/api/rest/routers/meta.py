"""Meta sync router — Gravitino ↔ LCP reconciliation endpoints.

Mirrors ``docs/architecture/api/openapi/lcp-meta-api.yaml``.

When Gravitino integration is disabled (``LCP_GRAVITINO_URL`` empty), all
endpoints return 503 with a clear message so operators know what to
configure.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.config import get_settings
from lcp.core.tenant import TenantPrincipal
from lcp.integrations.gravitino.client import (
    GravitinoDisabledError,
    get_gravitino_client,
)
from lcp.services import meta_sync_service

router = APIRouter(prefix="/v1/meta", tags=["meta"])


def _gravitino_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "GRAVITINO_DISABLED",
            "message": (
                "Gravitino integration is not configured. "
                "Set LCP_GRAVITINO_URL to enable meta sync."
            ),
        },
    )


@router.post(
    "/sync",
    status_code=status.HTTP_200_OK,
    summary="Trigger meta sync",
)
async def trigger_meta_sync(
    principal: Annotated[TenantPrincipal, PrincipalDep],
    session: Annotated[AsyncSession, SessionDep],
    schema: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reconcile Gravitino catalog metadata with the LCP state store.

    - If ``schema`` is provided, only that schema is scanned.
    - If ``dry_run=true``, no DB writes are made; returns what *would* change.
    """

    gc = get_gravitino_client()
    if not gc.enabled:
        raise _gravitino_unavailable()

    try:
        if schema:
            result = await meta_sync_service.reconcile_schema(
                session,
                schema,
                tenant_id=principal.tenant_id,
                client=gc,
                dry_run=dry_run,
            )
        else:
            result = await meta_sync_service.reconcile_all(
                session,
                tenant_id=principal.tenant_id,
                client=gc,
                dry_run=dry_run,
            )
    except GravitinoDisabledError:
        raise _gravitino_unavailable()

    return {
        "schemas_scanned": result.schemas_scanned if not schema else 1,
        "filesets_discovered": result.filesets_discovered,
        "datasets_created": result.datasets_created,
        "datasets_updated": result.datasets_updated,
        "errors": result.errors or [],
        "dry_run": dry_run,
        "tenant_id": principal.tenant_id,
    }


@router.get(
    "/schemas",
    summary="List Gravitino schemas",
)
async def list_schemas(
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> dict[str, Any]:
    """Return the list of schemas in the configured Gravitino catalog."""

    gc = get_gravitino_client()
    if not gc.enabled:
        raise _gravitino_unavailable()

    try:
        schemas = await gc.list_schemas()
    except GravitinoDisabledError:
        raise _gravitino_unavailable()

    return {"schemas": schemas, "catalog": gc._catalog}  # noqa: SLF001


@router.get(
    "/schemas/{schema}/filesets",
    summary="List filesets in a schema",
)
async def list_filesets(
    schema: str,
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> dict[str, Any]:
    """Return fileset names under the given Gravitino schema."""

    gc = get_gravitino_client()
    if not gc.enabled:
        raise _gravitino_unavailable()

    try:
        filesets = await gc.list_filesets(schema)
    except GravitinoDisabledError:
        raise _gravitino_unavailable()

    return {"schema": schema, "filesets": filesets}


@router.post(
    "/datasets/{dataset_uuid}/write-back",
    summary="Push LCP stats back to Gravitino",
)
async def write_back_stats(
    dataset_uuid: str,
    principal: Annotated[TenantPrincipal, PrincipalDep],
    session: Annotated[AsyncSession, SessionDep],
) -> dict[str, Any]:
    """Push operational stats (row_count, coverage, etc.) to Gravitino properties."""

    gc = get_gravitino_client()
    if not gc.enabled:
        raise _gravitino_unavailable()

    success = await meta_sync_service.write_back_stats(
        session,
        dataset_uuid,
        client=gc,
    )
    return {
        "dataset_uuid": dataset_uuid,
        "written_back": success,
        "tenant_id": principal.tenant_id,
    }
