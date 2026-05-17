"""Gravitino fileset → LCP dataset mapping and meta-sync service.

Responsibilities:
- Discover Gravitino filesets that are not yet registered in LCP
- Reconcile metadata (storage_location, comment) from Gravitino into LCP
- Write back operational stats (row_count, index_coverage) to Gravitino properties

Source-of-truth contract:
- Gravitino owns: storage_location, comment, fileset_type
- LCP owns: row_count, fragment_count, index_coverage, status, latest_version
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.time import utcnow_naive
from lcp.db.models import Dataset
from lcp.integrations.gravitino.client import (
    Fileset,
    GravitinoAPIError,
    GravitinoClient,
    GravitinoDisabledError,
    get_gravitino_client,
)

__all__ = [
    "MetaSyncResult",
    "reconcile_schema",
    "reconcile_all",
    "write_back_stats",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MetaSyncResult:
    """Outcome of a reconciliation pass."""

    schemas_scanned: int = 0
    filesets_discovered: int = 0
    datasets_created: int = 0
    datasets_updated: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reconcile_schema(
    session: AsyncSession,
    schema: str,
    *,
    tenant_id: str,
    client: GravitinoClient | None = None,
    dry_run: bool = False,
) -> MetaSyncResult:
    """Reconcile all filesets in one Gravitino schema with LCP datasets.

    For each fileset found:
    - If no matching LCP dataset exists → create one (ACTIVE status).
    - If a matching dataset exists → update storage_uri/description if
      they diverged from Gravitino's source of truth.

    Matching is on ``(catalog, db_schema, table_name)`` = ``(gravitino_catalog,
    schema, fileset_name)``.
    """

    gc = client or get_gravitino_client()
    result = MetaSyncResult()

    try:
        fileset_names = await gc.list_filesets(schema)
    except (GravitinoDisabledError, GravitinoAPIError) as exc:
        result.errors.append(f"list_filesets({schema}): {exc}")  # type: ignore[union-attr]
        return result

    result.filesets_discovered = len(fileset_names)
    catalog = gc._catalog  # noqa: SLF001

    for name in fileset_names:
        try:
            fileset = await gc.get_fileset(schema, name)
        except GravitinoAPIError as exc:
            result.errors.append(f"get_fileset({schema}/{name}): {exc}")  # type: ignore[union-attr]
            continue

        # Check if dataset already exists in LCP.
        stmt = select(Dataset).where(
            Dataset.catalog == catalog,
            Dataset.db_schema == schema,
            Dataset.table_name == name,
            Dataset.tenant_id == tenant_id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is None:
            # Create new dataset from Gravitino fileset.
            if not dry_run:
                obj = Dataset(
                    dataset_uuid=str(uuid.uuid4()),
                    catalog=catalog,
                    db_schema=schema,
                    table_name=name,
                    storage_uri=fileset.storage_location,
                    tenant_id=tenant_id,
                    description=fileset.comment,
                    status="ACTIVE",
                )
                session.add(obj)
            result.datasets_created += 1
        else:
            # Update if Gravitino metadata diverged.
            changed = False
            if (
                fileset.storage_location
                and existing.storage_uri != fileset.storage_location
            ):
                if not dry_run:
                    existing.storage_uri = fileset.storage_location
                changed = True
            if fileset.comment and existing.description != fileset.comment:
                if not dry_run:
                    existing.description = fileset.comment
                changed = True
            if changed:
                result.datasets_updated += 1

    if not dry_run:
        await session.commit()

    return result


async def reconcile_all(
    session: AsyncSession,
    *,
    tenant_id: str,
    client: GravitinoClient | None = None,
    dry_run: bool = False,
) -> MetaSyncResult:
    """Reconcile all schemas in the configured Gravitino catalog."""

    gc = client or get_gravitino_client()
    total = MetaSyncResult()

    try:
        schemas = await gc.list_schemas()
    except (GravitinoDisabledError, GravitinoAPIError) as exc:
        total.errors.append(f"list_schemas: {exc}")  # type: ignore[union-attr]
        return total

    total.schemas_scanned = len(schemas)

    for schema in schemas:
        partial = await reconcile_schema(
            session,
            schema,
            tenant_id=tenant_id,
            client=gc,
            dry_run=dry_run,
        )
        total.filesets_discovered += partial.filesets_discovered
        total.datasets_created += partial.datasets_created
        total.datasets_updated += partial.datasets_updated
        if partial.errors:
            total.errors.extend(partial.errors)  # type: ignore[union-attr]

    return total


async def write_back_stats(
    session: AsyncSession,
    dataset_uuid: str,
    *,
    client: GravitinoClient | None = None,
) -> bool:
    """Push LCP operational stats back to Gravitino fileset properties.

    Returns True if write-back succeeded, False otherwise (non-fatal).
    """

    gc = client or get_gravitino_client()
    if not gc.enabled:
        return False

    stmt = select(Dataset).where(Dataset.dataset_uuid == dataset_uuid)
    ds = (await session.execute(stmt)).scalar_one_or_none()
    if ds is None:
        return False

    props = {
        "lcp.row_count": str(ds.row_count),
        "lcp.fragment_count": str(ds.fragment_count),
        "lcp.index_coverage": str(ds.index_coverage),
        "lcp.status": ds.status,
        "lcp.latest_version": str(ds.latest_version),
        "lcp.synced_at": utcnow_naive().isoformat(),
    }

    try:
        await gc.set_properties(ds.db_schema, ds.table_name, props)
        return True
    except (GravitinoAPIError, GravitinoDisabledError) as exc:
        logger.warning(
            "write_back_stats failed for %s: %s", dataset_uuid, exc,
        )
        return False
