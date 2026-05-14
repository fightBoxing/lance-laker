"""Meta-sync service: reconcile MySQL ``dataset`` rows against Gravitino.

Source-of-Truth contract (decided at design time, see
``lcp.integrations.gravitino.mapping`` docstring):

    * Gravitino owns *catalog* facts (storage_location, comment, type).
    * LCP  owns *operational* facts (row_count, status, index_coverage, ...)
      and writes them back to ``fileset.properties`` under ``lcp.*``.

Reconcile flow per dataset:

    1. Resolve the LCP dataset row (RLS filtered by tenant).
    2. Fetch the corresponding Gravitino fileset (``schema=db_schema``,
       ``name=table_name``).
    3. Project the fileset into a :class:`DatasetPatch` and apply non-empty
       changes to the ORM row.
    4. Push the LCP operational state into ``fileset.properties`` so the
       Gravitino catalog UI surfaces it.
    5. Stamp ``Dataset.extra['gravitino_synced_at']`` with the wall clock
       so observability dashboards can detect drifting reconcilers.

Tenancy:

    The service expects a system principal (set via :func:`with_system_context`)
    because cron jobs and the meta-sync REST endpoint do not carry per-user
    JWTs.  The router layer is responsible for setting that context before
    calling in.

Why a service module (not a router that does it inline):

    * Cron jobs and REST handlers both invoke this module.  Sharing the
      logic keeps the SLO-relevant code in one place.
    * Tests can drive the service with a ``MagicMock(spec=GravitinoClient)``
      and an in-memory sqlite session — no live Gravitino, no MySQL.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.db.models import Dataset
from lcp.integrations.gravitino.client import (
    Fileset,
    GravitinoError,
    GravitinoNotFoundError,
)
from lcp.integrations.gravitino.mapping import (
    DatasetPatch,
    dataset_to_property_patch,
    fileset_to_dataset_patch,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


# DDL contract: ``Dataset.extra`` is JSON; we tuck Gravitino sync metadata
# under this key so it does not collide with future ``extra`` consumers.
EXTRA_KEY = "gravitino"


class _ClientProtocol(Protocol):
    """Subset of :class:`GravitinoClient` we depend on.

    Declared here (rather than importing the concrete class everywhere)
    so unit tests can pass a plain ``MagicMock`` without inheriting from
    the real client; satisfies the duck-typed interface.
    """

    async def get_fileset(self, schema: str, name: str) -> Fileset: ...

    async def list_filesets(self, schema: str) -> list[str]: ...

    async def set_fileset_properties(
        self, schema: str, name: str, properties: dict[str, str],
    ) -> Fileset: ...


@dataclass
class DatasetSyncOutcome:
    """Per-dataset reconcile outcome.

    ``status`` is a small closed vocabulary so the REST layer can render
    it without further translation:

        * ``synced``       - reconciled successfully (whether or not a
                             change was needed); see ``changed`` flag.
        * ``not_in_gravitino`` - LCP knows the dataset, Gravitino does
                             not (404).  Operator action required.
        * ``error``        - any other failure; ``error`` field carries
                             the human-readable message.
    """

    dataset_uuid: str
    status: str
    changed: bool = False
    pushed_properties: bool = False
    error: str | None = None


@dataclass
class SyncReport:
    """Aggregate report for a reconcile run.

    ``items`` keeps per-dataset detail; the counters are derived but
    cached on the dataclass so a JSON-serialised report (sent via the
    REST layer) does not require the consumer to recompute them.
    """

    total: int = 0
    synced: int = 0
    changed: int = 0
    pushed: int = 0
    missing: int = 0
    errors: int = 0
    items: list[DatasetSyncOutcome] = field(default_factory=list)

    def record(self, outcome: DatasetSyncOutcome) -> None:
        """Append ``outcome`` and update aggregate counters."""

        self.items.append(outcome)
        self.total += 1
        if outcome.status == "synced":
            self.synced += 1
            if outcome.changed:
                self.changed += 1
            if outcome.pushed_properties:
                self.pushed += 1
        elif outcome.status == "not_in_gravitino":
            self.missing += 1
        else:
            self.errors += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reconcile_dataset(
    session: AsyncSession,
    client: _ClientProtocol,
    dataset: Dataset,
    *,
    push_properties: bool = True,
    now: datetime | None = None,
) -> DatasetSyncOutcome:
    """Reconcile a single ORM ``dataset`` against Gravitino.

    The caller is expected to have already resolved the ORM row (so that
    RLS + dataset_service.get_dataset semantics live in the router/cron
    layer, not here).  Keeps this function easy to test in isolation.

    ``push_properties=False`` lets a cron job pull-only on a tight loop
    while a slower job handles the write-back, but the default does both
    in one shot for simplicity.
    """

    schema = dataset.db_schema
    table = dataset.table_name
    log_ctx = {"dataset_uuid": dataset.dataset_uuid, "schema": schema, "table": table}

    try:
        fileset = await client.get_fileset(schema, table)
    except GravitinoNotFoundError:
        # Operator-visible signal: LCP claims a dataset that Gravitino
        # has never seen.  Don't auto-create on the Gravitino side —
        # creating catalog entries from inside LCP would be a footgun.
        logger.warning("meta-sync: fileset not found in Gravitino", extra=log_ctx)
        return DatasetSyncOutcome(
            dataset_uuid=dataset.dataset_uuid,
            status="not_in_gravitino",
        )
    except GravitinoError as exc:
        logger.exception("meta-sync: Gravitino get_fileset failed", extra=log_ctx)
        return DatasetSyncOutcome(
            dataset_uuid=dataset.dataset_uuid,
            status="error",
            error=str(exc),
        )

    # ----- Direction A: Gravitino -> LCP ----------------------------------
    patch: DatasetPatch = fileset_to_dataset_patch(fileset)
    changed = patch.apply(dataset)

    # ----- Direction B: LCP -> Gravitino properties -----------------------
    # Skip the round-trip when caller opts out (pull-only mode).  Keeps
    # the REST trigger fast for read-only diff reports.
    pushed = False
    if push_properties:
        try:
            properties = dataset_to_property_patch(dataset, now=now)
            await client.set_fileset_properties(schema, table, properties)
            pushed = True
        except GravitinoError as exc:
            logger.exception(
                "meta-sync: set_fileset_properties failed", extra=log_ctx,
            )
            # We *did* update LCP from the fileset; flag the property
            # push as failed but keep the LCP change.  Operators can
            # retry; meanwhile LCP is at least consistent with Gravitino
            # in the catalog direction.  Commit the LCP-side patch now
            # before bailing out so a subsequent ``session.refresh(ds)``
            # in the test (or a parallel reader in production) sees the
            # new ``storage_uri``/``description`` we just applied.
            if changed:
                await session.commit()
            return DatasetSyncOutcome(
                dataset_uuid=dataset.dataset_uuid,
                status="error",
                changed=changed,
                pushed_properties=False,
                error=f"set_fileset_properties: {exc}",
            )

    # ----- Stamp last-sync metadata into Dataset.extra --------------------
    # Mutating ``dataset.extra`` in-place is intentional: SQLAlchemy will
    # detect the JSON column change because we reassign the dict (some
    # mutation-tracking impls don't pick up nested edits otherwise).
    extra: dict[str, Any] = dict(dataset.extra or {})
    sync_meta: dict[str, Any] = dict(extra.get(EXTRA_KEY) or {})
    sync_meta["last_synced_at"] = (now or datetime.now(tz=timezone.utc)).astimezone(
        timezone.utc,
    ).isoformat()
    sync_meta["last_storage_location"] = fileset.storage_location
    sync_meta["last_pushed_properties"] = pushed
    extra[EXTRA_KEY] = sync_meta
    dataset.extra = extra

    # Persist whatever we changed in this run (extra is always touched;
    # the patch may have touched storage_uri/description too).
    await session.commit()

    return DatasetSyncOutcome(
        dataset_uuid=dataset.dataset_uuid,
        status="synced",
        changed=changed,
        pushed_properties=pushed,
    )


async def reconcile_all(
    session: AsyncSession,
    client: _ClientProtocol,
    *,
    schema: str | None = None,
    push_properties: bool = True,
    now: datetime | None = None,
) -> SyncReport:
    """Reconcile every dataset (optionally filtered by schema).

    The query is RLS-filtered by the current principal — production
    callers (cron jobs) must enter a system context first if they want
    to scan across tenants; the REST trigger uses the caller's tenant.

    We deliberately reconcile sequentially.  Concurrent fan-out would
    speed things up, but Gravitino servers can be modest and mass
    parallel reads from a cron job risks overwhelming them.  When this
    becomes a real bottleneck, switch to a bounded asyncio.Semaphore.
    """

    stmt = select(Dataset)
    if schema is not None:
        stmt = stmt.where(Dataset.db_schema == schema)
    stmt = stmt.order_by(Dataset.id.asc())

    datasets: Sequence[Dataset] = (await session.execute(stmt)).scalars().all()

    report = SyncReport()
    for ds in datasets:
        outcome = await reconcile_dataset(
            session,
            client,
            ds,
            push_properties=push_properties,
            now=now,
        )
        report.record(outcome)
    return report


async def discover_unknown_filesets(
    client: _ClientProtocol,
    session: AsyncSession,
    *,
    schema: str,
) -> list[str]:
    """Return Gravitino fileset names not present in LCP (read-only diff).

    We *do not* auto-register them.  Auto-creation would require choosing
    a tenant_id and owner from thin air, which is a policy decision that
    belongs to a human operator.  Listing them is enough to drive a
    one-click registration UI later.
    """

    fileset_names = await client.list_filesets(schema)
    if not fileset_names:
        return []

    stmt = (
        select(Dataset.table_name)
        .where(Dataset.db_schema == schema)
        .where(Dataset.table_name.in_(fileset_names))
    )
    known = {row for row in (await session.execute(stmt)).scalars().all()}
    return [name for name in fileset_names if name not in known]


__all__ = [
    "DatasetSyncOutcome",
    "EXTRA_KEY",
    "SyncReport",
    "discover_unknown_filesets",
    "reconcile_all",
    "reconcile_dataset",
]
