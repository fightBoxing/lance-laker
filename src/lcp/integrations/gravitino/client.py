"""Async HTTP client for the Gravitino REST API.

Supports:
- Health check (``/api/version``)
- List filesets in a schema (``GET /metalakes/{ml}/catalogs/{cat}/schemas/{schema}/filesets``)
- Get fileset detail (``GET .../filesets/{name}``)
- Set fileset properties (``PUT .../filesets/{name}`` with ``setProperties``)

All methods are async (httpx) and respect the configured timeout.  When
``gravitino_url`` is empty the client raises :class:`GravitinoDisabledError`
on any call so callers can surface a clear 503 / skip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from lcp.core.config import Settings, get_settings

__all__ = [
    "GravitinoClient",
    "GravitinoDisabledError",
    "GravitinoAPIError",
    "Fileset",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GravitinoDisabledError(RuntimeError):
    """Raised when a Gravitino call is attempted but integration is disabled."""


class GravitinoAPIError(Exception):
    """Raised when the Gravitino server returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Gravitino API {status_code}: {detail}")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fileset:
    """Minimal representation of a Gravitino fileset (table-equivalent)."""

    name: str
    schema_name: str
    storage_location: str
    comment: str | None = None
    fileset_type: str = "MANAGED"
    properties: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GravitinoClient:
    """Async client for the Gravitino REST catalog API.

    Create via :func:`get_gravitino_client` for production use.  Tests can
    instantiate directly with a custom ``Settings`` instance.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.gravitino_url.rstrip("/")
        self._metalake = self._settings.gravitino_metalake
        self._catalog = self._settings.gravitino_catalog
        self._timeout = self._settings.gravitino_timeout_seconds
        self._token = self._settings.gravitino_auth_token

    @property
    def enabled(self) -> bool:
        """Return True if Gravitino integration is configured."""
        return bool(self._base_url)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise GravitinoDisabledError(
                "Gravitino integration is disabled (LCP_GRAVITINO_URL is empty).",
            )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _schema_base(self, schema: str) -> str:
        return (
            f"{self._base_url}/api/metalakes/{self._metalake}"
            f"/catalogs/{self._catalog}/schemas/{schema}/filesets"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        """Check Gravitino server health via ``/api/version``."""
        self._require_enabled()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base_url}/api/version",
                headers=self._headers(),
            )
            _check_response(resp)
            return resp.json()

    async def list_filesets(self, schema: str) -> list[str]:
        """Return fileset names under *schema*."""
        self._require_enabled()
        url = self._schema_base(schema)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            _check_response(resp)
            data = resp.json()
            # Gravitino returns {"identifiers": [{"name": "..."}]}
            identifiers = data.get("identifiers") or []
            return [ident["name"] for ident in identifiers if "name" in ident]

    async def get_fileset(self, schema: str, name: str) -> Fileset:
        """Fetch full fileset metadata."""
        self._require_enabled()
        url = f"{self._schema_base(schema)}/{name}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            _check_response(resp)
            data = resp.json().get("fileset", resp.json())
            return Fileset(
                name=data.get("name", name),
                schema_name=schema,
                storage_location=data.get("storageLocation", ""),
                comment=data.get("comment"),
                fileset_type=data.get("type", "MANAGED"),
                properties=data.get("properties", {}),
            )

    async def set_properties(
        self,
        schema: str,
        name: str,
        properties: dict[str, str],
    ) -> None:
        """Update fileset properties (write-back from LCP → Gravitino)."""
        self._require_enabled()
        url = f"{self._schema_base(schema)}/{name}"
        body = {
            "updates": [
                {"@type": "setProperty", "property": k, "value": v}
                for k, v in properties.items()
            ],
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(
                url,
                json=body,
                headers=self._headers(),
            )
            _check_response(resp)

    async def list_schemas(self) -> list[str]:
        """Return schema names in the configured catalog."""
        self._require_enabled()
        url = (
            f"{self._base_url}/api/metalakes/{self._metalake}"
            f"/catalogs/{self._catalog}/schemas"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            _check_response(resp)
            data = resp.json()
            identifiers = data.get("identifiers") or []
            return [ident["name"] for ident in identifiers if "name" in ident]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_response(resp: httpx.Response) -> None:
    """Raise :class:`GravitinoAPIError` on non-2xx."""
    if resp.is_success:
        return
    detail = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
    raise GravitinoAPIError(resp.status_code, detail)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_CLIENT: GravitinoClient | None = None


def get_gravitino_client() -> GravitinoClient:
    """Return the shared client singleton."""
    global _CLIENT  # noqa: PLW0603
    if _CLIENT is None:
        _CLIENT = GravitinoClient()
    return _CLIENT


def reset_gravitino_client() -> None:
    """Reset singleton (for tests)."""
    global _CLIENT  # noqa: PLW0603
    _CLIENT = None
