"""Thin wrapper around the ``lance`` python API.

Why this module exists
----------------------
Control-plane code (executors, services, schedulers) needs to issue a
small, well-understood vocabulary against the lance data plane:

- open a dataset
- delete rows by predicate
- compact small files
- create / optimize an ANN index

Funnelling those into a single module makes three things possible:

1. **Optional dependency**: tests do ``monkeypatch.setattr(lance_io, ...)``
   without ever installing the ``pylance`` wheel.  ``import lance`` is
   *deferred* to the moment a real call is made.
2. **One pin to swap**: if the lance API renames ``compact_files`` again,
   we change one file.
3. **Uniform storage_options**: every call uses the same dict produced
   from :class:`lcp.core.config.Settings`; callers do not have to know
   about MinIO endpoint / path-style flags.

Public surface
--------------
- :func:`build_storage_options` -- dict from ``Settings``
- :func:`open_dataset`         -- ``ds = lance_io.open_dataset(uri)``
- :func:`delete_rows`          -- ``lance_io.delete_rows(uri, predicate)``
- :func:`compact_files`        -- ``lance_io.compact_files(uri, **threshold)``
- :func:`optimize_indices`     -- ``lance_io.optimize_indices(uri)``
- :func:`create_index`         -- ``lance_io.create_index(uri, column, index_type, **params)``
- :func:`count_rows`           -- ``lance_io.count_rows(uri)`` (used by tests / e2e)

All functions are *synchronous*: lance itself is sync, and the worker
runs them inside ``asyncio.to_thread`` to keep the event loop free.
That's the executor's job, not ours -- we keep this module
synchronous-and-blunt on purpose so it is trivial to mock.

Error model
-----------
We do **not** re-wrap lance exceptions.  Executors translate them into
``fail_task`` with the original class name; obscuring the original type
would only hurt operability.

Storage options precedence
--------------------------
- Empty endpoint => no S3 options at all (lance treats the URI as
  local-fs / pyarrow-default).  This is what local-fs unit tests want.
- Non-empty endpoint => full set of S3 options is emitted and lance
  routes through its object_store layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lcp.core.config import Settings, get_settings

__all__ = [
    "LanceNotInstalledError",
    "build_storage_options",
    "open_dataset",
    "delete_rows",
    "compact_files",
    "optimize_indices",
    "create_index",
    "count_rows",
    "CompactionStats",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LanceNotInstalledError(RuntimeError):
    """Raised on first real call when the optional ``pylance`` wheel is missing.

    Carries a hint so operators do not have to grep stack traces.
    """


def _import_lance() -> Any:
    """Import ``lance`` lazily; raise a friendly error if missing.

    Tests that *want* to exercise the real lance code path may install
    the ``lance`` extra; tests that monkey-patch this module never reach
    here, so the missing wheel is invisible to them.
    """

    try:
        import lance
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise LanceNotInstalledError(
            "pylance is not installed; install with `pip install lcp[lance]` "
            "to enable real data-plane operations.",
        ) from exc
    return lance


# ---------------------------------------------------------------------------
# Storage options
# ---------------------------------------------------------------------------


def build_storage_options(settings: Settings | None = None) -> dict[str, str]:
    """Translate :class:`Settings` lance fields into a lance storage_options dict.

    Returns an empty dict when no endpoint is configured -- lance then
    treats the URI as a local filesystem path or a pyarrow-default S3.

    Why explicit ``str`` values: lance expects the dict values to be
    strings (booleans must be ``"true"`` / ``"false"``).  We convert
    once, here, so callers cannot accidentally pass a Python ``bool``.
    """

    cfg = settings if settings is not None else get_settings()
    if not cfg.lance_storage_endpoint:
        return {}

    return {
        "endpoint": cfg.lance_storage_endpoint,
        "aws_access_key_id": cfg.lance_storage_access_key,
        "aws_secret_access_key": cfg.lance_storage_secret_key,
        "aws_region": cfg.lance_storage_region,
        "allow_http": "true" if cfg.lance_storage_allow_http else "false",
        "virtual_hosted_style_request": (
            "false" if cfg.lance_storage_path_style else "true"
        ),
    }


# ---------------------------------------------------------------------------
# Dataset access
# ---------------------------------------------------------------------------


def open_dataset(
    uri: str,
    *,
    storage_options: dict[str, str] | None = None,
) -> Any:
    """Open and return a lance dataset.

    Returns ``Any`` (not ``lance.LanceDataset``) so the type stays valid
    when lance is not installed at type-check time.
    """

    lance_mod = _import_lance()
    opts = storage_options if storage_options is not None else build_storage_options()
    if opts:
        return lance_mod.dataset(uri, storage_options=opts)
    return lance_mod.dataset(uri)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def delete_rows(
    uri: str,
    predicate: str,
    *,
    storage_options: dict[str, str] | None = None,
) -> int:
    """Delete rows matching ``predicate``; return the post-delete row count.

    The row count is taken AFTER the delete commits, so callers can
    diff it against a pre-delete count if they want before/after stats.
    """

    ds = open_dataset(uri, storage_options=storage_options)
    ds.delete(predicate)
    # Re-open to see the new manifest version; lance datasets are
    # immutable snapshots, so the in-memory ``ds`` still reflects pre-delete.
    ds_after = open_dataset(uri, storage_options=storage_options)
    return int(ds_after.count_rows())


@dataclass(frozen=True)
class CompactionStats:
    """Subset of lance ``CompactionMetrics`` we care about in payloads."""

    fragments_removed: int
    fragments_added: int
    files_removed: int
    files_added: int

    @classmethod
    def from_lance_metrics(cls, metrics: Any) -> CompactionStats:
        """Coerce a lance metrics object (any version) into a stable shape.

        Different ``pylance`` versions expose slightly different attribute
        names; we read defensively and fall back to 0.  The four numbers
        we surface are the operationally interesting ones.
        """

        return cls(
            fragments_removed=int(getattr(metrics, "fragments_removed", 0) or 0),
            fragments_added=int(getattr(metrics, "fragments_added", 0) or 0),
            files_removed=int(getattr(metrics, "files_removed", 0) or 0),
            files_added=int(getattr(metrics, "files_added", 0) or 0),
        )


def compact_files(
    uri: str,
    *,
    storage_options: dict[str, str] | None = None,
    target_rows_per_fragment: int | None = None,
    materialize_deletions: bool | None = None,
) -> CompactionStats:
    """Run ``ds.optimize.compact_files``; return a normalised stats record.

    Only forwards kwargs that the caller actually set, so we do not pin
    ourselves to a specific lance signature.
    """

    ds = open_dataset(uri, storage_options=storage_options)
    kwargs: dict[str, Any] = {}
    if target_rows_per_fragment is not None:
        kwargs["target_rows_per_fragment"] = target_rows_per_fragment
    if materialize_deletions is not None:
        kwargs["materialize_deletions"] = materialize_deletions
    metrics = ds.optimize.compact_files(**kwargs)
    return CompactionStats.from_lance_metrics(metrics)


def optimize_indices(
    uri: str,
    *,
    storage_options: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Trigger ``ds.optimize.optimize_indices``; return ``(index_count, new_version)``.

    Lance's ``optimize_indices`` rebuilds the delta indices for every
    indexed column, so a single dataset-level call is enough -- we do
    NOT loop per column.

    The second tuple element is the dataset's ``latest_version`` *after*
    the optimize commit, which the watcher uses to dedupe its own
    optimize-induced version drift (without it, every optimize bumps
    the version by one and re-triggers the watcher in a tight loop).
    """

    ds = open_dataset(uri, storage_options=storage_options)
    ds.optimize.optimize_indices()
    # Re-open to pick up the freshly committed version; ``ds`` from
    # before the call still points at the pre-optimize manifest.
    ds_after = open_dataset(uri, storage_options=storage_options)
    new_version = int(ds_after.latest_version)
    # ``list_indices`` is the lance public API; older versions used
    # ``index_statistics``.  Total is informational only.
    return _safe_count_indices(ds_after), new_version


def create_index(
    uri: str,
    *,
    column: str,
    index_type: str,
    storage_options: dict[str, str] | None = None,
    replace: bool = False,
    **index_params: Any,
) -> dict[str, Any]:
    """Create an ANN index on ``column``; return a small descriptor dict.

    ``index_type`` is the lance vocabulary (``IVF_PQ``, ``IVF_HNSW_PQ``,
    ``BTREE``, etc.); we pass it through verbatim.

    Why a dict return: callers want to log the index_type / column they
    just created without re-opening the dataset.  We do NOT return the
    lance dataset object (it would tempt callers to import lance).
    """

    ds = open_dataset(uri, storage_options=storage_options)
    ds.create_index(
        column=column,
        index_type=index_type,
        replace=replace,
        **index_params,
    )
    return {
        "uri": uri,
        "column": column,
        "index_type": index_type,
        "replace": replace,
        "params": dict(index_params),
    }


# ---------------------------------------------------------------------------
# Read-only helpers (used by e2e and tests)
# ---------------------------------------------------------------------------


def count_rows(
    uri: str,
    *,
    storage_options: dict[str, str] | None = None,
) -> int:
    """Return ``ds.count_rows()``."""

    ds = open_dataset(uri, storage_options=storage_options)
    return int(ds.count_rows())


def _safe_count_indices(ds: Any) -> int:
    """Best-effort count of indices; returns 0 if the API is missing or fails.

    Cross-version safety: ``list_indices`` is the modern lance API; older
    versions used ``index_statistics``.  Either path may also raise on
    empty datasets.  We never want a missing stat to crash the worker.
    """

    fn = getattr(ds, "list_indices", None)
    if fn is None:
        return 0
    try:
        return len(list(fn()))
    except Exception:  # noqa: BLE001 -- read-only stat path; never crash worker
        return 0


# ---------------------------------------------------------------------------
# Watcher helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexLiveStats:
    """Snapshot of one index's live state, used by the event-driven watcher.

    All counts are integers; ``None`` is used only for missing fields when
    the underlying lance version does not expose them.

    The watcher consumes this dataclass instead of a raw dict so renames in
    upstream lance keep blast radius to this single module.
    """

    latest_version: int
    num_indexed_rows: int | None
    num_unindexed_rows: int | None
    num_indexed_fragments: int | None
    num_unindexed_fragments: int | None
    updated_at_timestamp_ms: int | None


def read_index_stats(
    uri: str,
    index_name: str,
    *,
    storage_options: dict[str, str] | None = None,
) -> IndexLiveStats | None:
    """Read live stats for one named index; ``None`` if the index is missing.

    The watcher uses this to decide whether to enqueue an INDEX_OPTIMIZE
    task.  Implementation deliberately tolerates missing keys so older
    lance versions degrade gracefully (the watcher then falls back to the
    version-drift signal alone).
    """

    ds = open_dataset(uri, storage_options=storage_options)

    latest_version = int(ds.latest_version)

    # ``index_statistics`` returns a dict keyed by ``num_indexed_rows`` /
    # ``num_unindexed_rows`` etc on lance 6.x; older versions either lack
    # the method (we fall back) or return a different shape (we degrade).
    raw: dict[str, Any] | None = None
    fn = getattr(ds, "index_statistics", None)
    if fn is not None:
        try:
            raw = fn(index_name)
        except Exception:  # noqa: BLE001 -- index missing -> raw stays None
            raw = None
    if raw is None:
        # Fallback: confirm the index name even exists; if it does, return
        # a stats record with None counts so the watcher can still use
        # ``latest_version`` for the version-drift signal.
        names = {idx.get("name") for idx in ds.list_indices()}
        if index_name not in names:
            return None
        return IndexLiveStats(
            latest_version=latest_version,
            num_indexed_rows=None,
            num_unindexed_rows=None,
            num_indexed_fragments=None,
            num_unindexed_fragments=None,
            updated_at_timestamp_ms=None,
        )

    return IndexLiveStats(
        latest_version=latest_version,
        num_indexed_rows=_maybe_int(raw.get("num_indexed_rows")),
        num_unindexed_rows=_maybe_int(raw.get("num_unindexed_rows")),
        num_indexed_fragments=_maybe_int(raw.get("num_indexed_fragments")),
        num_unindexed_fragments=_maybe_int(raw.get("num_unindexed_fragments")),
        updated_at_timestamp_ms=_maybe_int(raw.get("updated_at_timestamp_ms")),
    )


def _maybe_int(value: Any) -> int | None:
    """Coerce numeric values to int; return None for None / unparseable."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
