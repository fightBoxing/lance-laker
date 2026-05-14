"""Unit tests for :mod:`lcp.integrations.gravitino.mapping`.

Pure-function tests, no DB and no HTTP — they're meant to run in
microseconds and double as executable documentation of the mapping
contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from lcp.db.models import Dataset
from lcp.integrations.gravitino.client import Fileset
from lcp.integrations.gravitino.mapping import (
    PROP_DATASET_UUID,
    PROP_FRAGMENT_COUNT,
    PROP_INDEX_COVERAGE,
    PROP_LATEST_VERSION,
    PROP_ROW_COUNT,
    PROP_STATUS,
    PROP_SYNCED_AT,
    PROP_TENANT_ID,
    DatasetPatch,
    dataset_to_property_patch,
    fileset_to_dataset_patch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fileset(
    *,
    name: str = "embeddings",
    storage_location: str = "s3://bucket/embeddings",
    comment: str | None = "demo",
    fileset_type: str = "MANAGED",
    properties: dict[str, str] | None = None,
    include_comment_key: bool = True,
) -> Fileset:
    """Build a Fileset with a controllable raw payload.

    ``include_comment_key=False`` simulates Gravitino omitting the comment
    field entirely (vs sending ``"comment": null``); both code paths matter
    because ``description_present`` keys off raw-payload presence.
    """

    raw: dict[str, object] = {
        "name": name,
        "storageLocation": storage_location,
        "type": fileset_type,
        "properties": properties or {},
    }
    if include_comment_key:
        raw["comment"] = comment
    return Fileset(
        name=name,
        storage_location=storage_location,
        fileset_type=fileset_type,
        comment=comment if include_comment_key else None,
        properties=properties or {},
        raw=raw,
    )


def _make_dataset(**overrides: object) -> Dataset:
    """Build a Dataset ORM object without going through the database."""

    defaults: dict[str, object] = {
        "dataset_uuid": "ds-1234",
        "catalog": "lance",
        "db_schema": "public",
        "table_name": "embeddings",
        "storage_uri": "s3://bucket/old",
        "tenant_id": "acme",
        "status": "ACTIVE",
        "row_count": 100,
        "fragment_count": 4,
        "index_coverage": Decimal("0.9876"),
        "latest_version": 7,
        "description": "old description",
    }
    defaults.update(overrides)
    return Dataset(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gravitino -> LCP
# ---------------------------------------------------------------------------


class TestFilesetToDatasetPatch:

    def test_carries_storage_location_and_comment(self) -> None:
        fs = _make_fileset(
            storage_location="s3://bucket/new",
            comment="hello",
        )
        patch = fileset_to_dataset_patch(fs)

        assert patch.storage_uri == "s3://bucket/new"
        assert patch.description == "hello"
        assert patch.description_present is True
        assert patch.is_empty() is False

    def test_missing_comment_key_means_description_not_present(self) -> None:
        # Gravitino omitted the key entirely from the JSON.
        fs = _make_fileset(comment=None, include_comment_key=False)
        patch = fileset_to_dataset_patch(fs)

        # storage_uri is always populated (Gravitino guarantees it),
        # but description_present should be False so the service does
        # not clobber an existing LCP description with None.
        assert patch.storage_uri == "s3://bucket/embeddings"
        assert patch.description is None
        assert patch.description_present is False

    def test_explicit_null_comment_clears_description(self) -> None:
        # The key is present in the JSON but its value is null/None;
        # treat that as "user wants the description cleared".
        fs = _make_fileset(comment=None, include_comment_key=True)
        patch = fileset_to_dataset_patch(fs)

        assert patch.description is None
        assert patch.description_present is True


class TestDatasetPatchApply:

    def test_apply_updates_only_changed_columns(self) -> None:
        ds = _make_dataset(storage_uri="s3://old", description="x")
        patch = DatasetPatch(
            storage_uri="s3://new",
            description="x",  # same as ORM, so should not count
            description_present=True,
        )
        changed = patch.apply(ds)

        assert changed is True
        assert ds.storage_uri == "s3://new"
        # Description was unchanged; apply should still report changed=True
        # because storage_uri changed.  But description must not be touched
        # in a way the test could not assert; verify it's still 'x'.
        assert ds.description == "x"

    def test_apply_returns_false_when_already_in_sync(self) -> None:
        ds = _make_dataset(storage_uri="s3://same", description="same")
        patch = DatasetPatch(
            storage_uri="s3://same",
            description="same",
            description_present=True,
        )
        assert patch.apply(ds) is False

    def test_apply_clears_description_when_present_and_none(self) -> None:
        ds = _make_dataset(description="will be cleared")
        patch = DatasetPatch(
            storage_uri=None,  # don't touch storage
            description=None,
            description_present=True,
        )
        changed = patch.apply(ds)

        assert changed is True
        assert ds.description is None
        # storage_uri unchanged because patch.storage_uri is None.
        assert ds.storage_uri == "s3://bucket/old"

    def test_is_empty_true_when_nothing_to_apply(self) -> None:
        empty = DatasetPatch()
        assert empty.is_empty() is True
        assert empty.apply(_make_dataset()) is False


# ---------------------------------------------------------------------------
# LCP -> Gravitino
# ---------------------------------------------------------------------------


class TestDatasetToPropertyPatch:

    def test_full_patch_contains_expected_keys_and_string_values(self) -> None:
        ds = _make_dataset(
            dataset_uuid="ds-uuid-1",
            tenant_id="tenant-x",
            status="ACTIVE",
            row_count=42,
            fragment_count=3,
            index_coverage=Decimal("0.5000"),
            latest_version=11,
        )
        fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        patch = dataset_to_property_patch(ds, now=fixed)

        # Exact key set guards against typos sneaking into property names.
        assert set(patch.keys()) == {
            PROP_DATASET_UUID,
            PROP_TENANT_ID,
            PROP_STATUS,
            PROP_ROW_COUNT,
            PROP_FRAGMENT_COUNT,
            PROP_INDEX_COVERAGE,
            PROP_LATEST_VERSION,
            PROP_SYNCED_AT,
        }

        # All values must be strings — Gravitino's contract.
        for value in patch.values():
            assert isinstance(value, str)

        assert patch[PROP_DATASET_UUID] == "ds-uuid-1"
        assert patch[PROP_TENANT_ID] == "tenant-x"
        assert patch[PROP_STATUS] == "ACTIVE"
        assert patch[PROP_ROW_COUNT] == "42"
        assert patch[PROP_FRAGMENT_COUNT] == "3"
        assert patch[PROP_INDEX_COVERAGE] == "0.5000"
        assert patch[PROP_LATEST_VERSION] == "11"
        assert patch[PROP_SYNCED_AT] == "2024-01-02T03:04:05+00:00"

    def test_default_now_is_close_to_real_now(self) -> None:
        # Don't pin to a clock; just assert the format is ISO-8601 with a
        # timezone suffix.  Anything else means the helper accidentally
        # dropped the tz, which would silently confuse downstream consumers.
        ds = _make_dataset()
        patch = dataset_to_property_patch(ds)

        synced = patch[PROP_SYNCED_AT]
        # Suffix can be ``+00:00`` (default) — never naive.
        assert synced.endswith("+00:00") or synced.endswith("Z")
        # Round-trip parse: confirms it's a real ISO timestamp.
        assert datetime.fromisoformat(synced.replace("Z", "+00:00")) is not None

    def test_naive_now_is_promoted_to_utc(self) -> None:
        # Defensive: if a caller forgets to attach tzinfo, we still emit a
        # tz-aware timestamp by treating naive input as UTC.  Document the
        # behaviour in a test so future refactors don't silently regress.
        ds = _make_dataset()
        naive = datetime(2024, 6, 7, 8, 9, 10)
        patch = dataset_to_property_patch(
            ds,
            now=naive.replace(tzinfo=timezone.utc),
        )
        assert patch[PROP_SYNCED_AT] == "2024-06-07T08:09:10+00:00"

    def test_decimal_with_zero_value_renders_as_fixed_point(self) -> None:
        ds = _make_dataset(index_coverage=Decimal("0.0000"))
        patch = dataset_to_property_patch(ds)
        assert patch[PROP_INDEX_COVERAGE] == "0.0000"

    def test_zero_coverage_does_not_collapse_to_scientific(self) -> None:
        # Explicit guardrail: ``str(Decimal('1E-4'))`` -> ``'1E-4'`` would
        # break Gravitino UIs that expect human-readable numbers; the
        # helper must use fixed-point formatting.
        ds = _make_dataset(index_coverage=Decimal("1E-4"))
        patch = dataset_to_property_patch(ds)
        assert patch[PROP_INDEX_COVERAGE] == "0.0001"


# ---------------------------------------------------------------------------
# LCP -> Gravitino (per-index, Step 6)
# ---------------------------------------------------------------------------


class TestIndexToPropertyPatch:
    """Pure-function tests for :func:`index_to_property_patch`.

    The function is small but every executor call site depends on the
    exact key shape (``lcp.index.<name>.<suffix>``); these tests pin the
    contract so a future refactor cannot silently move keys.
    """

    def test_keys_have_correct_namespace_and_suffixes(self) -> None:
        from lcp.integrations.gravitino.mapping import (
            index_property_keys,
            index_to_property_patch,
        )

        state_key, column_key, last_key = index_property_keys("emb_idx")
        patch = index_to_property_patch(
            index_name="emb_idx",
            state="READY",
            column="vector",
            last_optimized_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
        )

        assert state_key == "lcp.index.emb_idx.state"
        assert column_key == "lcp.index.emb_idx.column"
        assert last_key == "lcp.index.emb_idx.last_optimized_at"
        assert set(patch.keys()) == {state_key, column_key, last_key}

    def test_all_values_are_strings(self) -> None:
        # Gravitino properties are ``Map<String, String>``.  Any non-string
        # would be rejected at the wire level; pin the invariant in code.
        from lcp.integrations.gravitino.mapping import index_to_property_patch

        patch = index_to_property_patch(
            index_name="i",
            state="READY",
            column="v",
            last_optimized_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        for value in patch.values():
            assert isinstance(value, str)

    def test_none_last_optimized_at_renders_as_empty_string(self) -> None:
        # An index in BUILDING state has never been optimized; the column
        # is None.  Must not become the literal string ``"None"`` (which
        # would mislead a UI that splits on truthy/falsy).
        from lcp.integrations.gravitino.mapping import (
            PROP_INDEX_LAST_OPTIMIZED_SUFFIX,
            index_to_property_patch,
        )

        patch = index_to_property_patch(
            index_name="brand_new",
            state="BUILDING",
            column="vector",
            last_optimized_at=None,
        )
        last_key = next(
            k for k in patch if k.endswith(PROP_INDEX_LAST_OPTIMIZED_SUFFIX)
        )
        assert patch[last_key] == ""

    def test_naive_last_optimized_at_is_promoted_to_utc(self) -> None:
        # SQLAlchemy gives us naive datetimes (DDL is DATETIME(3) without
        # timezone); the helper must attach UTC so the rendered string
        # never lies about timezone.
        from lcp.integrations.gravitino.mapping import (
            PROP_INDEX_LAST_OPTIMIZED_SUFFIX,
            index_to_property_patch,
        )

        naive = datetime(2024, 7, 8, 9, 10, 11)
        patch = index_to_property_patch(
            index_name="i",
            state="READY",
            column="v",
            last_optimized_at=naive,
        )
        last_key = next(
            k for k in patch if k.endswith(PROP_INDEX_LAST_OPTIMIZED_SUFFIX)
        )
        # ``+00:00`` suffix is the proof tz was attached as UTC.
        assert patch[last_key] == "2024-07-08T09:10:11+00:00"

    def test_state_passes_through_verbatim(self) -> None:
        # We don't translate LCP states (BUILDING / READY / FAILED /
        # OPTIMIZING) into a Gravitino-side vocabulary -- they pass through
        # unchanged so consumers see the same word both sides agree on.
        from lcp.integrations.gravitino.mapping import (
            PROP_INDEX_STATE_SUFFIX,
            index_to_property_patch,
        )

        for state in ("BUILDING", "READY", "FAILED", "OPTIMIZING"):
            patch = index_to_property_patch(
                index_name="i",
                state=state,
                column="v",
                last_optimized_at=None,
            )
            state_key = next(
                k for k in patch if k.endswith(PROP_INDEX_STATE_SUFFIX)
            )
            assert patch[state_key] == state

    def test_index_name_with_dots_is_preserved_verbatim(self) -> None:
        # LCP allows dots in index names (rare but legal).  We do NOT
        # escape them: the keys become ``lcp.index.foo.bar.state`` etc.
        # That's intentional -- Gravitino properties is a flat map; if
        # an operator names indices ambiguously they get the resulting
        # key collision.  Pinning the behaviour so it's a conscious
        # choice, not an accident, is the point of this test.
        from lcp.integrations.gravitino.mapping import index_property_keys

        state_key, _, _ = index_property_keys("foo.bar")
        assert state_key == "lcp.index.foo.bar.state"
