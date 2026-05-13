"""Pydantic request / response schemas for the lifecycle policy API.

Field names mirror ``docs/architecture/api/openapi/lcp-lifecycle-api.yaml``.

Unlike :mod:`lcp.schemas.dataset` / :mod:`lcp.schemas.task` /
:mod:`lcp.schemas.index`, no field aliasing is needed here: the OpenAPI
names already match the ORM column names one-to-one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PolicyCreateRequest(BaseModel):
    """Body of ``POST /v1/datasets/{dataset_uuid}/lifecycle-policies``."""

    model_config = ConfigDict(extra="forbid")

    policy_name: str = Field(min_length=1, max_length=128)
    tier_rules: dict[str, Any] | None = None
    ttl_days: int | None = Field(default=None, ge=1)
    compaction_threshold: dict[str, Any] | None = None
    index_optimize_cron: str | None = Field(default=None, max_length=64)
    # Watcher (event-driven INDEX_OPTIMIZE) configuration; default-off
    # to keep new policies behaviour-compatible with existing rows.
    index_watch_enabled: bool = False
    index_watch_min_unindexed_rows: int | None = Field(default=1000, ge=1)
    index_watch_min_version_drift: int | None = Field(default=1, ge=1)
    index_watch_stale_minutes: int | None = Field(default=30, ge=1)
    enabled: bool = True


class PolicyUpdateRequest(BaseModel):
    """Body of ``PATCH /v1/.../lifecycle-policies/{policy_name}``.

    All fields are optional; only the supplied fields are mutated.  Sentinel
    ``None`` is used by the service layer to distinguish "field omitted"
    from "explicitly set to null", via :meth:`model_dump(exclude_unset=True)`.
    """

    model_config = ConfigDict(extra="forbid")

    tier_rules: dict[str, Any] | None = None
    ttl_days: int | None = Field(default=None, ge=1)
    compaction_threshold: dict[str, Any] | None = None
    index_optimize_cron: str | None = Field(default=None, max_length=64)
    index_watch_enabled: bool | None = None
    index_watch_min_unindexed_rows: int | None = Field(default=None, ge=1)
    index_watch_min_version_drift: int | None = Field(default=None, ge=1)
    index_watch_stale_minutes: int | None = Field(default=None, ge=1)


class PolicyResponse(BaseModel):
    """Shape returned for any single-policy endpoint."""

    model_config = ConfigDict(from_attributes=True)

    policy_name: str
    dataset_uuid: str
    tier_rules: dict[str, Any] | None = None
    ttl_days: int | None = None
    compaction_threshold: dict[str, Any] | None = None
    index_optimize_cron: str | None = None
    index_watch_enabled: bool = False
    index_watch_min_unindexed_rows: int | None = None
    index_watch_min_version_drift: int | None = None
    index_watch_stale_minutes: int | None = None
    enabled: bool
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PolicyListResponse(BaseModel):
    """Shape of ``GET /v1/.../lifecycle-policies``."""

    total: int
    items: list[PolicyResponse]
