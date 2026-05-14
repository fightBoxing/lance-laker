"""Meta sync router.

Mirrors ``docs/architecture/api/openapi/lcp-meta-api.yaml`` — endpoints
used by Gravitino integration and meta reconciliation jobs.

This router only wires HTTP to the service layer; the reconciliation
logic itself lives in :mod:`lcp.services.meta_sync_service` and the
transport client in :mod:`lcp.integrations.gravitino`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.config import get_settings
from lcp.core.tenant import TenantPrincipal
from lcp.db.models import Dataset
from lcp.integrations.gravitino import GravitinoClient
from lcp.schemas.meta import (
    DatasetSyncOutcomeOut,
    MetaSyncRequest,
    MetaSyncResponse,
)
from lcp.services import dataset_service, meta_sync_service

router = APIRouter(prefix="/v1/meta", tags=["meta"])


def _check_gravitino_configured() -> None:
    """503 fast if the operator forgot to set ``LCP_GRAVITINO_URL``.

    We deliberately fail at request time instead of at app startup: tests
    and skeleton deployments boot the REST server without a Gravitino, and
    a startup-time check would mean we can never run the API in those
    environments at all.
    """

    settings = get_settings()
    if not settings.gravitino_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "GRAVITINO_NOT_CONFIGURED",
                "message": "LCP_GRAVITINO_URL is empty; meta-sync is disabled",
            },
        )


@router.post(
    "/sync",
    response_model=MetaSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger meta sync",
)
async def trigger_meta_sync(
    payload: MetaSyncRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> MetaSyncResponse:
    """Reconcile Gravitino metadata with the LCP state store.

    Synchronously runs the reconciliation; the OpenAPI ``sync_id`` field is
    populated with a placeholder so future async migration is non-breaking.
    """

    _check_gravitino_configured()

    if payload.scope == "CATALOG":
        # Reserved in the spec but not yet implemented; refuse loudly so
        # operators don't think it succeeded.  See MetaSyncRequest docstring.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SCOPE_NOT_SUPPORTED",
                "message": "scope=CATALOG is reserved but not implemented yet",
            },
        )

    started_at = datetime.now(tz=timezone.utc)
    error_message: str | None = None

    async with GravitinoClient.from_settings() as client:
        if payload.scope == "DATASET":
            if not payload.dataset_uuid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "DATASET_UUID_REQUIRED",
                        "message": "scope=DATASET requires dataset_uuid",
                    },
                )
            try:
                ds = await dataset_service.get_dataset(session, payload.dataset_uuid)
            except dataset_service.DatasetNotFoundError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "NOT_FOUND",
                        "message": f"dataset {payload.dataset_uuid} not found",
                    },
                ) from exc
            outcome = await meta_sync_service.reconcile_dataset(
                session,
                client,
                ds,
                push_properties=payload.push_properties,
                now=started_at,
            )
            report = meta_sync_service.SyncReport()
            report.record(outcome)
        elif payload.scope == "SCHEMA":
            if not payload.db_schema:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "SCHEMA_REQUIRED",
                        "message": "scope=SCHEMA requires schema",
                    },
                )
            report = await meta_sync_service.reconcile_all(
                session,
                client,
                schema=payload.db_schema,
                push_properties=payload.push_properties,
                now=started_at,
            )
        else:  # ALL (default)
            report = await meta_sync_service.reconcile_all(
                session,
                client,
                schema=None,
                push_properties=payload.push_properties,
                now=started_at,
            )

    finished_at = datetime.now(tz=timezone.utc)
    overall_status = "FAILED" if report.errors > 0 else "SUCCEEDED"
    if report.errors > 0:
        error_message = f"{report.errors} dataset(s) failed; see items[]"

    return MetaSyncResponse(
        status=overall_status,
        scanned=report.total,
        updated=report.changed,
        # OpenAPI's ``skipped`` is the count of "synced but unchanged" rows.
        skipped=report.synced - report.changed,
        pushed=report.pushed,
        missing=report.missing,
        errors=report.errors,
        started_at=started_at,
        finished_at=finished_at,
        items=[
            DatasetSyncOutcomeOut(
                dataset_uuid=item.dataset_uuid,
                status=item.status,
                changed=item.changed,
                pushed_properties=item.pushed_properties,
                error=item.error,
            )
            for item in report.items
        ],
        error_message=error_message,
    )


@router.get("/sync/{run_id}", summary="Get meta sync status")
async def get_meta_sync_status(
    run_id: str,
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> dict[str, object]:
    """Return progress and diff summary for a sync run.

    Currently sync runs are inline (synchronous within the POST request),
    so there is no persistent ``run_id`` to look up.  Returns 501 with a
    machine-readable code so clients can detect this until the async-job
    backing store lands.
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "ASYNC_RUNS_NOT_IMPLEMENTED",
            "message": (
                "meta-sync runs synchronously today; "
                "no persistent run_id is recorded"
            ),
            "run_id": run_id,
        },
    )


@router.get("/datasets/{dataset_id}/snapshot", summary="Dataset snapshot meta")
async def get_dataset_snapshot(
    dataset_id: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> dict[str, object]:
    """Return the latest committed snapshot metadata for a dataset.

    Reads the ``Dataset.extra['gravitino']`` blob populated by the most
    recent reconcile pass, plus the live row.  When meta-sync has never
    run for the dataset, the ``gravitino`` block is empty.
    """

    try:
        ds: Dataset = await dataset_service.get_dataset(session, dataset_id)
    except dataset_service.DatasetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"dataset {dataset_id} not found"},
        ) from exc

    extra = ds.extra or {}
    return {
        "dataset_uuid": ds.dataset_uuid,
        "storage_uri": ds.storage_uri,
        "row_count": ds.row_count,
        "fragment_count": ds.fragment_count,
        "latest_version": ds.latest_version,
        "gravitino": extra.get("gravitino", {}),
    }
