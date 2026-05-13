"""Unit tests for ``lcp.data_plane.lance_io``.

These tests do NOT require the ``pylance`` wheel.  Every lance call is
intercepted by monkey-patching the lazy ``_import_lance`` helper to
return a hand-rolled fake module.  The fake records what was called so
we can assert on it without booting a real dataset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lcp.core.config import Settings
from lcp.data_plane import lance_io

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake lance module
# ---------------------------------------------------------------------------


@dataclass
class _FakeOptimize:
    """Captures ``ds.optimize.*`` calls."""

    compact_calls: list[dict[str, Any]] = field(default_factory=list)
    optimize_indices_calls: int = 0

    def compact_files(self, **kwargs: Any) -> Any:
        self.compact_calls.append(kwargs)
        return _FakeMetrics(
            fragments_removed=10, fragments_added=2,
            files_removed=8, files_added=1,
        )

    def optimize_indices(self) -> None:
        self.optimize_indices_calls += 1


@dataclass
class _FakeMetrics:
    fragments_removed: int = 0
    fragments_added: int = 0
    files_removed: int = 0
    files_added: int = 0


@dataclass
class _FakeDataset:
    """In-memory stand-in for ``lance.LanceDataset``."""

    uri: str
    rows: int = 5
    latest_version: int = 1
    deleted_predicates: list[str] = field(default_factory=list)
    create_index_calls: list[dict[str, Any]] = field(default_factory=list)
    listed_indices: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.optimize = _FakeOptimize()

    def count_rows(self) -> int:
        return self.rows

    def delete(self, predicate: str) -> None:
        self.deleted_predicates.append(predicate)
        # Simulate that the delete removed exactly one row.
        self.rows = max(0, self.rows - 1)

    def create_index(self, **kwargs: Any) -> None:
        self.create_index_calls.append(kwargs)

    def list_indices(self) -> list[Any]:
        return list(self.listed_indices)


class _FakeLance:
    """Stand-in for the imported ``lance`` module.

    ``dataset(uri, storage_options=...)`` returns a fresh ``_FakeDataset``
    on first call per uri, then re-uses it (so re-opening sees mutations
    -- which is the opposite of real lance, but plenty for unit tests).
    """

    def __init__(self) -> None:
        self.opened: list[tuple[str, dict[str, str] | None]] = []
        self._cache: dict[str, _FakeDataset] = {}

    def dataset(self, uri: str, storage_options: dict[str, str] | None = None) -> _FakeDataset:
        self.opened.append((uri, storage_options))
        ds = self._cache.get(uri)
        if ds is None:
            ds = _FakeDataset(uri=uri)
            self._cache[uri] = ds
        return ds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lance(monkeypatch: pytest.MonkeyPatch) -> _FakeLance:
    """Patch ``_import_lance`` so every public function uses our fake."""

    fake = _FakeLance()
    monkeypatch.setattr(lance_io, "_import_lance", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# build_storage_options
# ---------------------------------------------------------------------------


class TestBuildStorageOptions:

    def test_empty_endpoint_returns_empty_dict(self) -> None:
        # No endpoint => no S3 options at all.  Lance will treat the URI as
        # local fs / pyarrow-default (the unit-test mode).
        cfg = Settings(lance_storage_endpoint="")
        assert lance_io.build_storage_options(cfg) == {}

    def test_full_endpoint_emits_all_keys(self) -> None:
        cfg = Settings(
            lance_storage_endpoint="http://minio:9000",
            lance_storage_access_key="ak",
            lance_storage_secret_key="sk",
            lance_storage_region="us-west-2",
            lance_storage_allow_http=True,
            lance_storage_path_style=True,
        )
        opts = lance_io.build_storage_options(cfg)
        assert opts["endpoint"] == "http://minio:9000"
        assert opts["aws_access_key_id"] == "ak"
        assert opts["aws_secret_access_key"] == "sk"
        assert opts["aws_region"] == "us-west-2"
        assert opts["allow_http"] == "true"
        assert opts["virtual_hosted_style_request"] == "false"

    def test_path_style_off_means_virtual_hosted_on(self) -> None:
        cfg = Settings(
            lance_storage_endpoint="https://s3.aws",
            lance_storage_path_style=False,
        )
        opts = lance_io.build_storage_options(cfg)
        assert opts["virtual_hosted_style_request"] == "true"

    def test_allow_http_off_emits_false_string(self) -> None:
        cfg = Settings(
            lance_storage_endpoint="https://s3.aws",
            lance_storage_allow_http=False,
        )
        opts = lance_io.build_storage_options(cfg)
        assert opts["allow_http"] == "false"


# ---------------------------------------------------------------------------
# open_dataset
# ---------------------------------------------------------------------------


class TestOpenDataset:

    def test_no_storage_options_skips_kwarg(
        self, fake_lance: _FakeLance, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force build_storage_options to return {} so open_dataset skips
        # the kwarg entirely (matches local-fs behaviour).
        monkeypatch.setattr(lance_io, "build_storage_options", lambda: {})
        ds = lance_io.open_dataset("file:///tmp/foo.lance")
        assert isinstance(ds, _FakeDataset)
        assert fake_lance.opened == [("file:///tmp/foo.lance", None)]

    def test_explicit_storage_options_passed_through(
        self, fake_lance: _FakeLance,
    ) -> None:
        opts = {"endpoint": "http://minio:9000"}
        lance_io.open_dataset("s3://b/t.lance", storage_options=opts)
        assert fake_lance.opened == [("s3://b/t.lance", opts)]


# ---------------------------------------------------------------------------
# delete_rows
# ---------------------------------------------------------------------------


class TestDeleteRows:

    def test_calls_delete_and_returns_post_count(
        self, fake_lance: _FakeLance,
    ) -> None:
        # Initial fake count is 5; delete shaves 1 -> 4.
        rows_after = lance_io.delete_rows(
            "s3://b/t.lance",
            "id = 2",
            storage_options={"endpoint": "x"},
        )
        ds = fake_lance._cache["s3://b/t.lance"]
        assert ds.deleted_predicates == ["id = 2"]
        assert rows_after == 4


# ---------------------------------------------------------------------------
# compact_files
# ---------------------------------------------------------------------------


class TestCompactFiles:

    def test_default_kwargs_are_empty(
        self, fake_lance: _FakeLance,
    ) -> None:
        stats = lance_io.compact_files("s3://b/t.lance", storage_options={"e": "x"})
        ds = fake_lance._cache["s3://b/t.lance"]
        assert ds.optimize.compact_calls == [{}]
        assert stats.fragments_removed == 10
        assert stats.fragments_added == 2
        assert stats.files_removed == 8
        assert stats.files_added == 1

    def test_threshold_kwargs_forwarded(
        self, fake_lance: _FakeLance,
    ) -> None:
        lance_io.compact_files(
            "s3://b/t.lance",
            storage_options={"e": "x"},
            target_rows_per_fragment=1_000_000,
            materialize_deletions=True,
        )
        ds = fake_lance._cache["s3://b/t.lance"]
        assert ds.optimize.compact_calls == [
            {"target_rows_per_fragment": 1_000_000, "materialize_deletions": True},
        ]

    def test_unknown_kwargs_not_silently_dropped(
        self, fake_lance: _FakeLance,
    ) -> None:
        # Only the two known kwargs are forwarded; others must be dropped
        # by Python (the function signature does not accept **kwargs).
        with pytest.raises(TypeError):
            lance_io.compact_files(  # type: ignore[call-arg]
                "s3://b/t.lance",
                storage_options={"e": "x"},
                bogus_kwarg=1,
            )


# ---------------------------------------------------------------------------
# optimize_indices
# ---------------------------------------------------------------------------


class TestOptimizeIndices:

    def test_calls_lance_and_returns_index_count_and_version(
        self, fake_lance: _FakeLance,
    ) -> None:
        # Pre-seed two fake indices on the dataset.
        ds_uri = "s3://b/t.lance"
        ds_obj = _FakeDataset(uri=ds_uri, latest_version=42)
        ds_obj.listed_indices = [object(), object()]
        fake_lance._cache[ds_uri] = ds_obj

        count, version = lance_io.optimize_indices(
            ds_uri, storage_options={"e": "x"},
        )
        assert ds_obj.optimize.optimize_indices_calls == 1
        assert count == 2
        # ``version`` is the latest_version of the dataset *after* the
        # optimize commit -- the watcher uses this to dedupe its own
        # optimize-induced version drift.
        assert version == 42

    def test_missing_list_indices_returns_zero(
        self, fake_lance: _FakeLance, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cross-version safety: if list_indices is missing or raises, we
        # still return a number (0) instead of crashing the worker.
        ds_uri = "s3://b/t.lance"
        ds_obj = _FakeDataset(uri=ds_uri, latest_version=7)
        # Drop list_indices so getattr() returns None.
        monkeypatch.setattr(ds_obj, "list_indices", None, raising=False)
        fake_lance._cache[ds_uri] = ds_obj

        count, version = lance_io.optimize_indices(
            ds_uri, storage_options={"e": "x"},
        )
        assert count == 0
        assert version == 7


# ---------------------------------------------------------------------------
# create_index
# ---------------------------------------------------------------------------


class TestCreateIndex:

    def test_forwards_column_index_type_and_params(
        self, fake_lance: _FakeLance,
    ) -> None:
        descriptor = lance_io.create_index(
            "s3://b/t.lance",
            column="embedding",
            index_type="IVF_PQ",
            storage_options={"e": "x"},
            num_partitions=256,
            num_sub_vectors=16,
        )
        ds = fake_lance._cache["s3://b/t.lance"]
        assert ds.create_index_calls == [
            {
                "column": "embedding",
                "index_type": "IVF_PQ",
                "replace": False,
                "num_partitions": 256,
                "num_sub_vectors": 16,
            },
        ]
        assert descriptor == {
            "uri": "s3://b/t.lance",
            "column": "embedding",
            "index_type": "IVF_PQ",
            "replace": False,
            "params": {"num_partitions": 256, "num_sub_vectors": 16},
        }

    def test_replace_flag_passed_through(
        self, fake_lance: _FakeLance,
    ) -> None:
        lance_io.create_index(
            "s3://b/t.lance",
            column="emb",
            index_type="IVF_PQ",
            storage_options={"e": "x"},
            replace=True,
        )
        ds = fake_lance._cache["s3://b/t.lance"]
        assert ds.create_index_calls[0]["replace"] is True


# ---------------------------------------------------------------------------
# count_rows
# ---------------------------------------------------------------------------


class TestCountRows:

    def test_returns_int(self, fake_lance: _FakeLance) -> None:
        assert lance_io.count_rows("s3://b/t.lance", storage_options={"e": "x"}) == 5


# ---------------------------------------------------------------------------
# CompactionStats
# ---------------------------------------------------------------------------


class TestCompactionStats:

    def test_from_lance_metrics_handles_missing_attrs(self) -> None:
        # Object with no fields at all -- everything must default to 0.
        stats = lance_io.CompactionStats.from_lance_metrics(object())
        assert stats == lance_io.CompactionStats(0, 0, 0, 0)

    def test_from_lance_metrics_coerces_none_to_zero(self) -> None:
        @dataclass
        class _Partial:
            fragments_removed: int = 5
            fragments_added: Any = None
            files_removed: Any = None
            files_added: int = 1

        stats = lance_io.CompactionStats.from_lance_metrics(_Partial())
        assert stats.fragments_removed == 5
        assert stats.fragments_added == 0
        assert stats.files_removed == 0
        assert stats.files_added == 1


# ---------------------------------------------------------------------------
# Lazy import error
# ---------------------------------------------------------------------------


class TestLanceNotInstalledError:

    def test_real_import_path_yields_friendly_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When pylance really is missing, ``_import_lance`` must raise
        :class:`LanceNotInstalledError`, not bare ``ImportError``."""

        import builtins

        real_import = builtins.__import__

        def _no_lance(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "lance":
                raise ImportError("no module named 'lance'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_lance)
        with pytest.raises(lance_io.LanceNotInstalledError, match="pylance"):
            lance_io._import_lance()
