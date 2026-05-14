"""Pydantic schemas for the meta-sync REST endpoints.

Field names mirror ``docs/architecture/api/openapi/lcp-meta-api.yaml``.
Where the OpenAPI uses ``schema`` (a Python keyword in some contexts),
the request/response objects expose it via ``populate_by_name`` so callers
can pass either the JSON name or the Python-friendly alias.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Closed vocabulary mirroring the OpenAPI ``MetaSyncRequest.scope`` enum.
SyncScope = str  # 'ALL' | 'SCHEMA' | 'DATASET'  (CATALOG deferred, see below)


class MetaSyncRequest(BaseModel):
    """Body of ``POST /v1/meta/sync``.

    ``scope`` follows the OpenAPI enum but the implementation only honours
    a subset:

        * ``ALL``     (default) — reconcile every dataset visible to the
                      caller's tenant.
        * ``SCHEMA``  — reconcile every dataset under ``schema``.
        * ``DATASET`` — reconcile a single ``dataset_uuid``.

    ``CATALOG`` scope is reserved in the OpenAPI but not yet implemented;
    the handler returns a 400 if it is requested so we don't silently
    fall back to ``ALL`` and surprise operators.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: str = Field(default="ALL", description="ALL | SCHEMA | DATASET")
    db_schema: str | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
        max_length=128,
    )
    dataset_uuid: str | None = Field(default=None, max_length=64)
    # If False, the handler only pulls Gravitino -> LCP and skips the
    # property write-back.  Useful for read-only diff dashboards.
    push_properties: bool = True


class DatasetSyncOutcomeOut(BaseModel):
    """Per-dataset outcome surfaced in :class:`MetaSyncResponse.items`."""

    dataset_uuid: str
    status: str
    changed: bool = False
    pushed_properties: bool = False
    error: str | None = None


class MetaSyncResponse(BaseModel):
    """Body of ``POST /v1/meta/sync`` response.

    OpenAPI defines a job-style response (``sync_id`` + ``status``) but
    the current implementation reconciles synchronously, so:

        * ``sync_id`` is a deterministic placeholder ``"sync-inline"`` so
          downstream tooling that just logs it doesn't error out.
        * ``status`` is ``SUCCEEDED`` if no per-dataset error, else
          ``FAILED`` (the response still contains useful per-item info).

    When this becomes a real async job, swap the placeholder for a real
    UUID + persisted record and the schema does not need to change.
    """

    sync_id: str = Field(default="sync-inline")
    status: str
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    pushed: int = 0
    missing: int = 0
    errors: int = 0
    started_at: datetime
    finished_at: datetime
    items: list[DatasetSyncOutcomeOut] = Field(default_factory=list)
    error_message: str | None = None
