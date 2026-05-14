"""Pure-function mapping between Gravitino fileset payloads and LCP datasets.

The two directions are deliberately asymmetric, matching the SoT contract
agreed at the meta-sync design step:

    Gravitino is the Source of Truth for *catalog* facts:
        - storage_location  (where the lance directory lives)
        - comment           (free-form description)
        - fileset_type      (MANAGED vs EXTERNAL)

    LCP is the Source of Truth for *operational* state and writes it back
    into ``fileset.properties`` under the ``lcp.*`` namespace so the upstream
    Gravitino UI can surface it without LCP having to expose another API:
        - lcp.dataset_uuid       (LCP-side primary key, useful for support)
        - lcp.tenant_id          (which tenant owns this in LCP)
        - lcp.status             (ACTIVE / PAUSED / ARCHIVED / DELETED)
        - lcp.row_count
        - lcp.fragment_count
        - lcp.index_coverage     (e.g. ``0.9876``)
        - lcp.latest_version
        - lcp.synced_at          (ISO-8601 UTC, for staleness debugging)

Why these are pure functions (no DB, no HTTP):
    They become trivially unit-testable, idempotent, and easy to reason about
    when the service layer composes them.  Side effects live in the service.

Edge cases the helpers must handle:
    * A Gravitino fileset may have no ``comment`` — that's a legitimate
      "clear the description" signal; mapping returns ``None`` so callers
      can write ``NULL`` into MySQL without tripping over an empty string.
    * Fileset properties always serialise to *strings* (Gravitino's contract);
      numeric LCP columns must therefore be stringified before sending.
    * ``Decimal`` cannot be JSON-serialised by the stdlib; we render it via
      ``format(value, 'f')`` which avoids scientific notation for small values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from lcp.db.models import Dataset
from lcp.integrations.gravitino.client import Fileset

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All keys we write into ``fileset.properties`` share this prefix so a future
# audit (or a property-cleanup pass) can identify LCP-owned entries without
# stepping on user-defined properties.
LCP_PROPERTY_PREFIX = "lcp."

# Individual property keys; centralised so both mapping directions agree
# (and tests can assert against exact spellings).
PROP_DATASET_UUID = LCP_PROPERTY_PREFIX + "dataset_uuid"
PROP_TENANT_ID = LCP_PROPERTY_PREFIX + "tenant_id"
PROP_STATUS = LCP_PROPERTY_PREFIX + "status"
PROP_ROW_COUNT = LCP_PROPERTY_PREFIX + "row_count"
PROP_FRAGMENT_COUNT = LCP_PROPERTY_PREFIX + "fragment_count"
PROP_INDEX_COVERAGE = LCP_PROPERTY_PREFIX + "index_coverage"
PROP_LATEST_VERSION = LCP_PROPERTY_PREFIX + "latest_version"
PROP_SYNCED_AT = LCP_PROPERTY_PREFIX + "synced_at"


# ---------------------------------------------------------------------------
# Result types (lightweight; no pydantic to keep mapping pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetPatch:
    """Subset of :class:`~lcp.db.models.Dataset` columns to update.

    ``None`` here means "Gravitino did not carry a value for this field"; the
    service layer treats that as "do not touch the LCP column".  Callers
    that want to *clear* a column should pass an explicit empty string when
    that's a legal value — but for ``description`` Gravitino itself uses
    ``None`` to mean "no description", so we propagate that through.

    Why not just return a ``dict``: the dataclass makes the field set
    explicit and gives mypy something to lock onto, which prevents typos
    like ``patch["storeg_uri"]`` from sneaking through tests.
    """

    storage_uri: str | None = None
    description: str | None = None
    # Whether Gravitino carries a description column at all (vs simply
    # absent in the payload).  Lets the service distinguish "clear the
    # description" from "Gravitino did not say anything".
    description_present: bool = False

    def is_empty(self) -> bool:
        """``True`` when the patch carries no actionable change."""

        return self.storage_uri is None and not self.description_present

    def apply(self, dataset: Dataset) -> bool:
        """Mutate ``dataset`` in place; return ``True`` iff something changed.

        The boolean lets the service skip a needless ``UPDATE`` round-trip
        when Gravitino and LCP already agree.
        """

        changed = False
        if self.storage_uri is not None and dataset.storage_uri != self.storage_uri:
            dataset.storage_uri = self.storage_uri
            changed = True
        if self.description_present and dataset.description != self.description:
            dataset.description = self.description
            changed = True
        return changed


# ---------------------------------------------------------------------------
# Gravitino -> LCP
# ---------------------------------------------------------------------------


def fileset_to_dataset_patch(fileset: Fileset) -> DatasetPatch:
    """Project a :class:`Fileset` into the diff we'd like to apply to LCP.

    We deliberately return a *patch* (not a full Dataset) so the service
    can decide which columns to write and skip the rest — keeping the
    UPDATE narrow and the audit trail readable.

    ``storage_location`` is mandatory in Gravitino, so we always emit it;
    ``comment`` is optional, hence the ``description_present`` flag.
    """

    # Gravitino guarantees ``storage_location`` exists in valid payloads;
    # ``Fileset.from_payload`` already enforces this with a KeyError.
    return DatasetPatch(
        storage_uri=fileset.storage_location,
        description=fileset.comment,
        # ``raw`` is the original JSON; ``"comment"`` being absent there
        # means Gravitino never carried it (vs explicit ``null``).  Both
        # cases collapse to ``description=None`` in our model, which is
        # the right outcome — Lance description is also nullable.
        description_present=("comment" in fileset.raw),
    )


# ---------------------------------------------------------------------------
# LCP -> Gravitino
# ---------------------------------------------------------------------------


def dataset_to_property_patch(
    dataset: Dataset,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Render LCP operational state as a Gravitino ``properties`` patch.

    All values are stringified — Gravitino's ``properties`` map is
    ``Map<String, String>`` on the wire and any non-string would be
    rejected by the server.

    ``now`` is parameterised (rather than calling :func:`datetime.utcnow`
    inside) so tests can pin a deterministic timestamp; the default is
    "now in UTC" which is what production wants.
    """

    if now is None:
        now = datetime.now(tz=timezone.utc)

    return {
        PROP_DATASET_UUID: dataset.dataset_uuid,
        PROP_TENANT_ID: dataset.tenant_id,
        PROP_STATUS: dataset.status,
        PROP_ROW_COUNT: str(dataset.row_count),
        PROP_FRAGMENT_COUNT: str(dataset.fragment_count),
        PROP_INDEX_COVERAGE: _format_decimal(dataset.index_coverage),
        PROP_LATEST_VERSION: str(dataset.latest_version),
        # ``isoformat`` keeps the timezone suffix (``+00:00``) which is
        # essential for downstream consumers to know it's UTC, not local.
        PROP_SYNCED_AT: now.astimezone(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_decimal(value: Decimal | float | int | None) -> str:
    """Format ``value`` as a fixed-point string Gravitino can display.

    ``str(Decimal('0.0000'))`` already gives ``'0.0000'``, but we also
    accept ``float``/``int``/``None`` for resilience: the column is
    declared as ``Numeric`` in SQLAlchemy but in practice some test
    fixtures use ``float`` for convenience.
    """

    if value is None:
        return "0"
    if isinstance(value, Decimal):
        # ``format(d, 'f')`` avoids scientific notation for very small or
        # very large values (e.g. ``1E+2`` -> ``'100'``).
        return format(value, "f")
    return str(value)


# Property key constants are part of the public contract because INDEX_BUILD/
# INDEX_OPTIMIZE executors (Step 6) will write a *different* sub-set of keys
# (e.g. ``lcp.index.<name>.state``) and need the prefix to namespace cleanly.
__all__: list[str] = [
    "DatasetPatch",
    "LCP_PROPERTY_PREFIX",
    "PROP_DATASET_UUID",
    "PROP_FRAGMENT_COUNT",
    "PROP_INDEX_COVERAGE",
    "PROP_LATEST_VERSION",
    "PROP_ROW_COUNT",
    "PROP_STATUS",
    "PROP_SYNCED_AT",
    "PROP_TENANT_ID",
    "dataset_to_property_patch",
    "fileset_to_dataset_patch",
]

# We intentionally do NOT mark the helper ``_format_decimal`` as importable;
# it's a private detail of the property formatter and shouldn't be reused
# elsewhere lest it become an accidental public API.

# NOTE: Any future field added to ``Dataset`` that is "operational state"
# (i.e. LCP-managed, not Gravitino-managed) should be surfaced via a new
# ``PROP_*`` constant + an entry in :func:`dataset_to_property_patch`,
# so the round-trip stays additive and we never silently drop info.
