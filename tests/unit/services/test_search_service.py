"""Unit tests for ``lcp.services.search_service``.

Tests the service-layer orchestration: dataset resolution (RLS) +
delegation to lance_io.vector_search.  The lance layer is fully
monkey-patched; no real dataset or S3 access.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lcp.services import search_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeDataset:
    """Minimal dataset stand-in."""

    def __init__(self, dataset_uuid: str = "ds-123") -> None:
        self.dataset_uuid = dataset_uuid
        self.storage_uri = "s3://bucket/path.lance"
        self.tenant_id = "t-test"


@pytest.fixture
def mock_get_dataset(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub dataset_service.get_dataset to return a fake dataset."""

    from lcp.services import dataset_service

    mock = AsyncMock(return_value=_FakeDataset())
    monkeypatch.setattr(dataset_service, "get_dataset", mock)
    return mock


@pytest.fixture
def mock_lance_search(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub lance_io.vector_search to return canned results."""

    from lcp.data_plane import lance_io

    results = [
        {"_distance": 0.1, "id": 1, "title": "hello"},
        {"_distance": 0.5, "id": 2, "title": "world"},
    ]
    mock = lambda *args, **kwargs: results  # noqa: E731
    monkeypatch.setattr(lance_io, "vector_search", mock)
    return results


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub get_settings to return minimal config."""

    monkeypatch.setenv("LCP_LANCE_STORAGE_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("LCP_LANCE_STORAGE_ACCESS_KEY", "ak")
    monkeypatch.setenv("LCP_LANCE_STORAGE_SECRET_KEY", "sk")
    monkeypatch.setenv("LCP_LANCE_STORAGE_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchService:

    async def test_happy_path_returns_results(
        self,
        mock_get_dataset: AsyncMock,
        mock_lance_search: Any,
        mock_settings: None,
    ) -> None:
        session = AsyncMock()
        results = await search_service.search(
            session,
            dataset_uuid="ds-123",
            vector=[0.1, 0.2, 0.3],
            column="v",
            k=10,
        )
        assert len(results) == 2
        assert results[0]["_distance"] == 0.1
        mock_get_dataset.assert_awaited_once_with(session, "ds-123")

    async def test_dataset_not_found_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_settings: None,
    ) -> None:
        from lcp.services import dataset_service

        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise dataset_service.DatasetNotFoundError("not found")

        monkeypatch.setattr(dataset_service, "get_dataset", _raise)

        session = AsyncMock()
        with pytest.raises(dataset_service.DatasetNotFoundError):
            await search_service.search(
                session,
                dataset_uuid="ds-missing",
                vector=[0.1],
                column="v",
            )

    async def test_column_not_found_translated(
        self,
        mock_get_dataset: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        mock_settings: None,
    ) -> None:
        from lcp.data_plane import lance_io

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise ValueError(
                "Vector column 'bad' not found in dataset schema; "
                "available=['id', 'title']",
            )

        monkeypatch.setattr(lance_io, "vector_search", _raise)

        session = AsyncMock()
        with pytest.raises(search_service.ColumnNotFoundError):
            await search_service.search(
                session,
                dataset_uuid="ds-123",
                vector=[0.1],
                column="bad",
            )

    async def test_dimension_mismatch_translated(
        self,
        mock_get_dataset: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        mock_settings: None,
    ) -> None:
        from lcp.data_plane import lance_io

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise ValueError("dimension mismatch: expected 384, got 3")

        monkeypatch.setattr(lance_io, "vector_search", _raise)

        session = AsyncMock()
        with pytest.raises(search_service.DimensionMismatchError):
            await search_service.search(
                session,
                dataset_uuid="ds-123",
                vector=[0.1, 0.2, 0.3],
                column="v",
            )

    async def test_other_value_error_propagates(
        self,
        mock_get_dataset: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        mock_settings: None,
    ) -> None:
        from lcp.data_plane import lance_io

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise ValueError("some other lance error")

        monkeypatch.setattr(lance_io, "vector_search", _raise)

        session = AsyncMock()
        with pytest.raises(ValueError, match="some other lance error"):
            await search_service.search(
                session,
                dataset_uuid="ds-123",
                vector=[0.1],
                column="v",
            )
