"""Service layer for vector search (β.4).

Thin orchestration: resolve dataset (RLS-filtered) → delegate to
:func:`lance_io.vector_search`.  The service exists so the REST router
stays free of storage-options plumbing and so unit tests can exercise
the business logic without standing up FastAPI.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.config import get_settings
from lcp.data_plane import lance_io
from lcp.services import dataset_service


class ColumnNotFoundError(Exception):
    """Raised when the requested vector column does not exist."""


class DimensionMismatchError(Exception):
    """Raised when the query vector dimension does not match the column."""


async def search(
    session: AsyncSession,
    *,
    dataset_uuid: str,
    vector: list[float],
    column: str,
    k: int = 10,
    filter_expr: str | None = None,
    select_columns: list[str] | None = None,
    nprobes: int | None = None,
    refine_factor: int | None = None,
) -> list[dict[str, Any]]:
    """Execute a vector search against a dataset's lance storage.

    Resolves the dataset via :func:`dataset_service.get_dataset` (which
    enforces RLS tenant isolation), then dispatches the lance search to
    a worker thread so the asyncio loop stays responsive.

    Raises
    ------
    dataset_service.DatasetNotFoundError
        If the dataset does not exist or is not visible to the current
        tenant.
    ColumnNotFoundError
        If ``column`` is not present in the dataset schema.
    DimensionMismatchError
        If the query vector length does not match the column's
        dimensionality.
    """

    ds = await dataset_service.get_dataset(session, dataset_uuid)

    settings = get_settings()
    storage_options = lance_io.build_storage_options(settings)

    try:
        results = await asyncio.to_thread(
            lance_io.vector_search,
            ds.storage_uri,
            vector=vector,
            column=column,
            k=k,
            filter_expr=filter_expr,
            select_columns=select_columns,
            nprobes=nprobes,
            refine_factor=refine_factor,
            storage_options=storage_options,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found in dataset schema" in msg:
            raise ColumnNotFoundError(msg) from exc
        if "dimension" in msg.lower():
            raise DimensionMismatchError(msg) from exc
        raise

    return results
