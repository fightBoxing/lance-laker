"""Pydantic request / response schemas for the vector search API.

Field names mirror ``docs/architecture/api/openapi/lcp-search-api.yaml``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VectorSearchRequest(BaseModel):
    """Body of ``POST /v1/datasets/{dataset_uuid}/search``."""

    model_config = ConfigDict(extra="forbid")

    vector: list[float] = Field(min_length=1)
    column: str = Field(min_length=1, max_length=128)
    k: int = Field(default=10, ge=1, le=1000)
    filter: str | None = Field(default=None, max_length=4096)
    select: list[str] | None = None
    nprobes: int | None = Field(default=None, ge=1)
    refine_factor: int | None = Field(default=None, ge=1)


class VectorSearchResponse(BaseModel):
    """Shape returned by ``POST /v1/datasets/{dataset_uuid}/search``."""

    model_config = ConfigDict(from_attributes=True)

    results: list[dict[str, Any]]
    count: int
    column: str
    k: int
