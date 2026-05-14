"""Async HTTP client for the Gravitino REST API (fileset catalog mode).

Scope (intentionally narrow — see Karpathy rule 2):

- List filesets in ``{metalake}/{catalog}/{schema}``.
- Get a single fileset by name.
- Update fileset properties (used by LCP to write index/lifecycle state
  back to Gravitino so it surfaces in the upstream catalog UI).

Out of scope on purpose:

- Creating metalake / catalog / schema.  Those are platform-team concerns
  and creating them implicitly from inside LCP would be a footgun.
- Tables/views.  We map LCP datasets to **filesets**; the Lance file
  layout is naturally a fileset (a directory of immutable files), and
  Gravitino does not yet ship a first-class Lance table provider.
- Authentication modes other than ``none`` and ``bearer`` (basic / OAuth2
  client-credentials).  Defer until a deployment actually needs them; the
  factory raises ``NotImplementedError`` instead of silently dropping
  auth.

Endpoint reference (Gravitino 0.6+):

- GET    ``/api/metalakes/{metalake}/catalogs/{catalog}/schemas/{schema}/filesets``
- GET    ``/api/metalakes/{metalake}/catalogs/{catalog}/schemas/{schema}/filesets/{name}``
- PUT    same path (request body: ``{"updates": [...]}``).

The minimal payload fields we depend on are documented in
:class:`Fileset`; we deliberately ignore unknown fields so an upstream
schema bump does not break us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx

from lcp.core.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Public exception hierarchy
# ---------------------------------------------------------------------------


class GravitinoError(Exception):
    """Base class for all Gravitino client failures.

    Carries the HTTP status (when available) so callers can decide whether
    to retry vs surface the error.  ``status`` is ``None`` for transport-
    level failures (DNS, connection refused, timeout).
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GravitinoNotFoundError(GravitinoError):
    """404 on a Gravitino resource (metalake/catalog/schema/fileset)."""


class GravitinoAuthError(GravitinoError):
    """401/403 — credentials missing, expired, or not authorised."""


# ---------------------------------------------------------------------------
# Domain object — a thin slice of the Gravitino fileset payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fileset:
    """A single Gravitino fileset, normalised to the fields LCP cares about.

    We intentionally keep this dataclass minimal.  The raw upstream JSON is
    available in :attr:`raw` for callers that need fields we have not yet
    surfaced — that escape hatch lets us avoid a sweeping mapping refactor
    every time Gravitino evolves.
    """

    name: str
    """Fileset name, e.g. ``customer_embeddings``.  Maps to LCP ``table_name``."""

    storage_location: str
    """Object-storage URI, e.g. ``s3://bucket/path``.  Maps to LCP ``storage_uri``."""

    fileset_type: str
    """``MANAGED`` or ``EXTERNAL``.  LCP-managed datasets should be MANAGED."""

    comment: str | None
    """Free-form description; maps to LCP ``description``."""

    properties: dict[str, str] = field(default_factory=dict)
    """User-defined key/value bag.  LCP writes ``lcp.*`` keys here for state."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Original Gravitino JSON, for fields we have not normalised yet."""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Fileset:
        """Parse a Gravitino fileset JSON object into a :class:`Fileset`.

        Tolerates missing optional fields; raises ``KeyError`` only on the
        truly required ``name``/``storageLocation``/``type`` triplet because
        those are always present in valid Gravitino responses.
        """

        return cls(
            name=str(payload["name"]),
            storage_location=str(payload["storageLocation"]),
            fileset_type=str(payload["type"]),
            comment=payload.get("comment"),
            # Gravitino guarantees ``properties`` is a flat string-string map.
            properties=dict(payload.get("properties") or {}),
            raw=dict(payload),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GravitinoClient:
    """Thin async wrapper around the Gravitino REST API.

    Designed as an async context manager so callers do not leak the
    underlying ``httpx.AsyncClient``::

        async with GravitinoClient.from_settings() as client:
            fs = await client.get_fileset("public", "embeddings")

    The class is also unit-testable without a real Gravitino: pass a
    ``transport=httpx.MockTransport(...)`` in the constructor to intercept
    outbound HTTP.
    """

    def __init__(
        self,
        *,
        base_url: str,
        metalake: str,
        catalog: str,
        auth_type: str = "none",
        token: str = "",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise GravitinoError("Gravitino base_url is empty; refusing to start")

        if auth_type not in ("none", "bearer"):
            # Fail loud — silently dropping auth in prod would be a P0.
            raise NotImplementedError(
                f"Gravitino auth_type={auth_type!r} not supported; "
                "use 'none' or 'bearer'.",
            )

        if auth_type == "bearer" and not token:
            raise GravitinoError("auth_type=bearer requires a non-empty token")

        self._metalake = metalake
        self._catalog = catalog

        headers: dict[str, str] = {"Accept": "application/json"}
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"

        # ``base_url`` is normalised: trim trailing slash so f-string joins
        # do not produce ``//api/...`` paths.
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    # ------- lifecycle ----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> GravitinoClient:
        """Build a client straight from the ``LCP_*`` env-driven settings."""

        cfg = settings or get_settings()
        return cls(
            base_url=cfg.gravitino_url,
            metalake=cfg.gravitino_metalake,
            catalog=cfg.gravitino_catalog,
            auth_type=cfg.gravitino_auth_type,
            token=cfg.gravitino_token,
            timeout_seconds=cfg.gravitino_request_timeout_seconds,
        )

    async def __aenter__(self) -> GravitinoClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self._http.aclose()

    # ------- properties (handy for service callers) ----------------------

    @property
    def metalake(self) -> str:
        return self._metalake

    @property
    def catalog(self) -> str:
        return self._catalog

    # ------- public API ---------------------------------------------------

    async def list_filesets(self, schema: str) -> list[str]:
        """Return fileset *names* under ``{metalake}/{catalog}/{schema}``.

        Gravitino's list endpoint returns a lightweight name index; pull the
        full payload via :meth:`get_fileset` only when you actually need the
        properties.  This split mirrors how the upstream Java SDK is used.
        """

        path = self._fileset_collection_path(schema)
        payload = await self._get_json(path)
        # Response shape: ``{"identifiers": [{"namespace": [...], "name": "x"}, ...]}``.
        identifiers = payload.get("identifiers") or []
        return [str(item["name"]) for item in identifiers if "name" in item]

    async def get_fileset(self, schema: str, name: str) -> Fileset:
        """Fetch a single fileset; raises :class:`GravitinoNotFoundError` on 404."""

        path = f"{self._fileset_collection_path(schema)}/{name}"
        payload = await self._get_json(path)
        # Gravitino wraps the actual entity under ``"fileset"``.  Tolerate
        # either shape so a future flat response does not break us.
        body = payload.get("fileset") if "fileset" in payload else payload
        return Fileset.from_payload(body)

    async def set_fileset_properties(
        self,
        schema: str,
        name: str,
        properties: dict[str, str],
    ) -> Fileset:
        """Patch ``properties`` on an existing fileset.

        Implementation note: Gravitino's ``alterFileset`` PUT expects an
        ``updates`` list where each entry has ``@type`` discriminator.  We
        emit one ``setProperty`` entry per key — simpler than a single
        ``replaceProperties`` because it keeps user-set properties we do
        not know about untouched.
        """

        if not properties:
            raise GravitinoError("set_fileset_properties called with empty dict")

        path = f"{self._fileset_collection_path(schema)}/{name}"
        body = {
            "updates": [
                {"@type": "setProperty", "property": key, "value": value}
                for key, value in properties.items()
            ],
        }
        payload = await self._put_json(path, body)
        entity = payload.get("fileset") if "fileset" in payload else payload
        return Fileset.from_payload(entity)

    # ------- internals ---------------------------------------------------

    def _fileset_collection_path(self, schema: str) -> str:
        return (
            f"/api/metalakes/{self._metalake}"
            f"/catalogs/{self._catalog}"
            f"/schemas/{schema}"
            f"/filesets"
        )

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise GravitinoError(f"GET {path} failed: {exc}") from exc
        return self._parse(response, path=path, method="GET")

    async def _put_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.put(path, json=body)
        except httpx.HTTPError as exc:
            raise GravitinoError(f"PUT {path} failed: {exc}") from exc
        return self._parse(response, path=path, method="PUT")

    @staticmethod
    def _parse(response: httpx.Response, *, path: str, method: str) -> dict[str, Any]:
        """Translate HTTP status to typed exceptions; return JSON on success."""

        status = response.status_code
        if status == 404:
            raise GravitinoNotFoundError(
                f"{method} {path} -> 404", status=status,
            )
        if status in (401, 403):
            raise GravitinoAuthError(
                f"{method} {path} -> {status}", status=status,
            )
        if status >= 400:
            # Surface the body for ops debuggability, capped to keep logs sane.
            snippet = response.text[:512] if response.text else ""
            raise GravitinoError(
                f"{method} {path} -> {status}: {snippet}", status=status,
            )

        # 2xx with empty body is unusual but legal (e.g. 204).  We never
        # call endpoints that return 204 today, so treat empty as ``{}``.
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise GravitinoError(
                f"{method} {path} -> {status} non-JSON body",
                status=status,
            ) from exc
        if not isinstance(data, dict):
            raise GravitinoError(
                f"{method} {path} -> {status} expected JSON object, got {type(data).__name__}",
                status=status,
            )
        return data


__all__ = [
    "Fileset",
    "GravitinoAuthError",
    "GravitinoClient",
    "GravitinoError",
    "GravitinoNotFoundError",
]
