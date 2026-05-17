"""Pydantic request / response schemas for the task API.

Field names mirror ``docs/architecture/api/openapi/lcp-task-api.yaml`` so the
generated docs and the implementation cannot drift apart.

OpenAPI exposes the task type as ``type`` (a Python keyword); we keep the
external wire shape identical and translate to the ORM column ``task_type``
through Pydantic ``alias``.  Likewise for ``schema``/``table`` in the dataset
module.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Enum values come straight from the DDL comments and the OpenAPI spec.
TaskType = Literal[
    "VECTORIZE",
    "COMPACTION",
    "TTL_DELETE",
    "INDEX_BUILD",
    "INDEX_OPTIMIZE",
    "INDEX_CLEANUP",
    "LIFECYCLE_RECYCLE",
]
TaskStatus = Literal[
    "PENDING",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]


class TaskSubmitRequest(BaseModel):
    """Body of ``POST /v1/tasks``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # OpenAPI uses ``type``; the ORM column is ``task_type`` to avoid
    # shadowing the Python builtin.  Validation accepts ``type`` from JSON
    # and serialises back to ``type``.
    task_type: TaskType = Field(
        validation_alias="type",
        serialization_alias="type",
    )
    dataset_uuid: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=5, ge=0, le=9)
    params: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    max_attempts: int = Field(default=3, ge=1, le=20)


class TaskResponse(BaseModel):
    """Shape returned for any single-task endpoint."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    task_uuid: str
    # Internal name matches ORM column; serialised as ``type`` to match OpenAPI.
    task_type: TaskType = Field(serialization_alias="type")
    status: TaskStatus
    dataset_uuid: str
    tenant_id: str
    priority: int
    progress: Decimal
    attempt: int
    max_attempts: int
    worker_id: str | None = None
    idempotency_key: str | None = None
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Shape of ``GET /v1/tasks`` (paginated)."""

    total: int
    page: int
    page_size: int
    items: list[TaskResponse]
