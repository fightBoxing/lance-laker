"""Pydantic request / response schemas for the vectorization rule API.

Field names mirror ``docs/architecture/api/openapi/lcp-vectorization-api.yaml``.

Cross-field validation
----------------------
``cron_expr`` is required *iff* ``trigger_type == "SCHEDULED"``.  This is
enforced by a Pydantic v2 ``model_validator`` on both create and update:

- create: ``trigger_type`` defaults to ``ON_INSERT`` so the rule is well
  formed even if the client omits it.
- update: only the supplied fields are mutated (``model_dump(exclude_unset=True)``
  in the service layer); the validator looks at the merged final state via
  ``info.context`` when called from the service, but we keep the request
  schema's validator only in ``RuleCreateRequest`` to avoid a confusing
  partial-update validation error -- the service layer re-checks the merged
  state instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TriggerType = Literal["ON_INSERT", "SCHEDULED", "MANUAL"]


def _validate_cron_required_when_scheduled(
    trigger_type: TriggerType,
    cron_expr: str | None,
) -> None:
    """Raise ValueError when SCHEDULED rule has no cron expression."""

    if trigger_type == "SCHEDULED" and not cron_expr:
        raise ValueError(
            "cron_expr is required when trigger_type is 'SCHEDULED'",
        )


class RuleCreateRequest(BaseModel):
    """Body of ``POST /v1/datasets/{dataset_uuid}/vectorization-rules``."""

    model_config = ConfigDict(extra="forbid")

    target_column: str = Field(min_length=1, max_length=128)
    source_columns: list[str] = Field(min_length=1)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    model_endpoint: str | None = Field(default=None, max_length=512)
    batch_size: int = Field(default=64, ge=1)
    trigger_type: TriggerType = "ON_INSERT"
    cron_expr: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_cron(self) -> RuleCreateRequest:
        _validate_cron_required_when_scheduled(self.trigger_type, self.cron_expr)
        # Trim empty source column entries early so storage stays clean.
        if any(not s.strip() for s in self.source_columns):
            raise ValueError("source_columns entries must be non-empty strings")
        return self


class RuleUpdateRequest(BaseModel):
    """Body of ``PATCH /v1/.../vectorization-rules/{target_column}``.

    All fields are optional; the cross-field check happens in the service
    layer once the merged state is known (a partial PATCH cannot validate
    the trigger_type/cron_expr pair on its own).
    """

    model_config = ConfigDict(extra="forbid")

    source_columns: list[str] | None = Field(default=None, min_length=1)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)
    model_endpoint: str | None = Field(default=None, max_length=512)
    batch_size: int | None = Field(default=None, ge=1)
    trigger_type: TriggerType | None = None
    cron_expr: str | None = Field(default=None, max_length=64)
    extra: dict[str, Any] | None = None


class RuleResponse(BaseModel):
    """Shape returned for any single-rule endpoint."""

    model_config = ConfigDict(from_attributes=True)

    target_column: str
    dataset_uuid: str
    source_columns: list[str]
    model_name: str
    model_version: str
    model_endpoint: str | None = None
    batch_size: int
    trigger_type: TriggerType
    cron_expr: str | None = None
    enabled: bool
    extra: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class RuleListResponse(BaseModel):
    """Shape of ``GET /v1/.../vectorization-rules``."""

    total: int
    items: list[RuleResponse]


# Re-export for the service layer's merged-state validator.
validate_cron_required_when_scheduled = _validate_cron_required_when_scheduled
