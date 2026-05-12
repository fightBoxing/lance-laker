"""Pydantic request / response schemas for the dataset API.

Field names mirror the OpenAPI specification at
``docs/architecture/api/openapi/lcp-control-api.yaml`` so that the generated
docs and the implementation cannot drift apart.

Caveat: OpenAPI uses ``schema`` and ``table`` as field names; we keep the
public wire shape identical and translate to the DDL columns
(``db_schema`` and ``table_name``) inside the service layer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Status values come straight from the DDL comment on ``dataset.status``.
DatasetStatus = str  # 'ACTIVE' | 'PAUSED' | 'ARCHIVED' | 'DELETED'


class DatasetRegisterRequest(BaseModel):
    """Body of ``POST /v1/datasets``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    catalog: str = Field(min_length=1, max_length=128)
    # OpenAPI uses ``schema`` and ``table``; map to ORM-friendly names
    # internally so handlers never have to translate.
    db_schema: str = Field(
        validation_alias="schema",
        serialization_alias="schema",
        min_length=1,
        max_length=128,
    )
    table_name: str = Field(
        validation_alias="table",
        serialization_alias="table",
        min_length=1,
        max_length=128,
    )
    storage_uri: str = Field(min_length=1, max_length=1024)
    owner: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)


class DatasetResponse(BaseModel):
    """Shape returned for any single-dataset endpoint."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    dataset_uuid: str
    catalog: str
    # Internal name matches the ORM column so ``model_validate(orm_obj)`` works
    # zero-copy; the OpenAPI-facing name is set via ``serialization_alias``.
    db_schema: str = Field(serialization_alias="schema")
    table_name: str = Field(serialization_alias="table")
    storage_uri: str
    owner: str | None = None
    description: str | None = None
    tenant_id: str
    status: DatasetStatus
    row_count: int
    fragment_count: int
    index_coverage: Decimal
    created_at: datetime
    updated_at: datetime


class DatasetListResponse(BaseModel):
    """Shape of ``GET /v1/datasets`` (paginated)."""

    total: int
    page: int
    page_size: int
    items: list[DatasetResponse]
