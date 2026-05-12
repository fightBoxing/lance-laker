"""Pydantic request / response schemas for the index API.

Field names mirror ``docs/architecture/api/openapi/lcp-index-api.yaml`` so the
generated docs and the implementation cannot drift apart.

OpenAPI uses ``column``, ``type`` and ``name``; we translate to the ORM
columns ``column_name``, ``index_type`` and ``index_name`` via Pydantic
aliases so the wire shape stays unchanged.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Enum values come from the OpenAPI spec and DDL comments.
IndexType = Literal["HNSW", "IVF_PQ", "IVF_FLAT", "BTREE", "BITMAP", "FTS"]
IndexStatus = Literal[
    "BUILDING", "READY", "OPTIMIZING", "MERGING", "FAILED", "DROPPED",
]


class IndexCreateRequest(BaseModel):
    """Body of ``POST /v1/datasets/{dataset_uuid}/indexes``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``name`` -> ORM ``index_name``
    index_name: str = Field(
        validation_alias="name",
        serialization_alias="name",
        min_length=1,
        max_length=128,
    )
    # ``column`` -> ORM ``column_name``
    column_name: str = Field(
        validation_alias="column",
        serialization_alias="column",
        min_length=1,
        max_length=128,
    )
    # ``type`` -> ORM ``index_type``
    index_type: IndexType = Field(
        validation_alias="type",
        serialization_alias="type",
    )
    params: dict[str, Any] | None = None
    replace_if_exists: bool = False


class IndexResponse(BaseModel):
    """Shape returned for any single-index endpoint."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Internal names match ORM columns; serialised as the OpenAPI names.
    index_name: str = Field(serialization_alias="index_name")
    dataset_uuid: str
    column_name: str = Field(serialization_alias="column")
    index_type: IndexType = Field(serialization_alias="type")
    status: IndexStatus
    params: dict[str, Any] | None = None
    coverage: Decimal
    fragment_count: int
    delta_count: int
    last_optimized_at: datetime | None = None
    last_merged_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class IndexListResponse(BaseModel):
    """Shape of ``GET /v1/datasets/{dataset_uuid}/indexes``.

    The OpenAPI returns a bare array; we wrap it with ``items`` + ``total``
    for consistency with the dataset/task list responses (callers can still
    read ``items`` to get the same array).
    """

    total: int
    items: list[IndexResponse]
