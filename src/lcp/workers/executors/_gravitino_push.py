"""Best-effort index-property push from executors to Gravitino.

Why this is a separate module
-----------------------------
``IndexBuildExecutor`` and ``IndexOptimizeExecutor`` both want to surface
their per-index state into the upstream Gravitino fileset properties so
the catalog UI shows fresh ``BUILDING/READY`` / ``last_optimized_at``
without LCP shipping its own catalog API.  The push is identical in both
executors, hence one helper, not two near-duplicates.

It lives under ``workers/executors/`` (not ``services/``) because:

* It is *executor-local glue*, not a public service primitive.  Other
  callers should reconcile via :mod:`lcp.services.meta_sync_service`,
  not punch through to property writes.
* The leading underscore in the file name signals "internal to executors".

Failure semantics (deliberate)
------------------------------
The push is **best-effort**:

* If ``settings.gravitino_url`` is empty (Gravitino integration disabled),
  the helper is a no-op.  The dataset state machine still progresses.
* If Gravitino is up but the call fails (5xx, timeout, 404 because the
  fileset got renamed underneath us), we log at WARNING and swallow.

Rationale: the executor's contract is "build/optimize the index"; the
Gravitino mirror is a *reflection* of that work, not part of it.
Failing the task because the catalog UI cannot be updated would be
strictly worse than letting the next meta-sync cron pass do it.

Note: this helper does NOT participate in the executor's session/
transaction.  It uses its own httpx client; both succeed and failure
leave the SQLAlchemy session untouched.
"""

from __future__ import annotations

import logging
from datetime import datetime

from lcp.core.config import Settings
from lcp.integrations.gravitino.client import GravitinoClient, GravitinoError
from lcp.integrations.gravitino.mapping import index_to_property_patch

__all__ = ["push_index_properties"]

_LOGGER = logging.getLogger(__name__)


async def push_index_properties(
    *,
    settings: Settings,
    schema: str,
    table: str,
    index_name: str,
    state: str,
    column: str,
    last_optimized_at: datetime | None,
) -> bool:
    """Push a single index's state to Gravitino as fileset properties.

    Returns ``True`` when the push happened (HTTP 200), ``False`` when
    it was skipped or failed.  Callers can record this in their result
    payload for observability but MUST NOT raise on ``False`` — the
    contract is best-effort.

    Why we build a fresh client per call:
        Executors run inside a synchronous-ish worker loop; we don't have
        a long-lived async client at executor scope.  ``from_settings``
        is cheap (just opens a connection pool) and ``async with`` makes
        the cleanup automatic, so per-call construction is fine until we
        see the connection pool overhead in profiles -- not before.
    """

    if not settings.gravitino_url:
        # Gravitino integration disabled: silent no-op.  Same behaviour
        # the meta-sync REST trigger gives via 503; here we just skip
        # because executors must not fail the task.
        _LOGGER.debug(
            "gravitino disabled; skipping index property push for %s/%s.%s",
            schema,
            table,
            index_name,
        )
        return False

    properties = index_to_property_patch(
        index_name=index_name,
        state=state,
        column=column,
        last_optimized_at=last_optimized_at,
    )

    try:
        async with GravitinoClient.from_settings(settings) as client:
            await client.set_fileset_properties(schema, table, properties)
    except GravitinoError as exc:
        # Log at WARNING (not ERROR) because this is non-fatal: the next
        # meta-sync sweep will reconcile the gap.  Including index_name +
        # schema/table in the message lets ops grep for "lost" indices.
        _LOGGER.warning(
            "gravitino property push failed for %s/%s.%s: %s",
            schema,
            table,
            index_name,
            exc,
        )
        return False

    return True
