"""Security helpers shared by REST and gRPC stacks.

Two authentication modes are supported in the skeleton:

1. **OIDC JWT validation** for the north-bound REST API.  Tokens are
   signed by an external Identity Provider (Keycloak / Okta / Auth0 / etc.)
   and validated locally using JWKs fetched from the issuer's
   ``.well-known/openid-configuration`` endpoint.

2. **mTLS** for the east-west gRPC traffic.  The gRPC server requires a
   client certificate; the certificate's *Common Name* is treated as the
   ``worker_id`` and an x509 *organization* attribute (or a custom OID)
   carries the ``tenant_id``.

This module deliberately exposes only stateless helpers; concrete
middleware/interceptor wiring lives in :mod:`lcp.api.rest.auth` and
:mod:`lcp.api.grpc.server`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import PyJWTError

from lcp.core.config import get_settings

if TYPE_CHECKING:
    from lcp.core.config import Settings


class AuthenticationError(Exception):
    """Raised when an inbound credential cannot be validated."""


# ---------------------------------------------------------------------------
# JTI replay-defence interface (M-3)
# ---------------------------------------------------------------------------


@runtime_checkable
class JtiDenylist(Protocol):
    """Protocol for JWT ID (jti) replay-attack denial.

    Callers call :meth:`is_denied` before trusting a token.  If the jti has
    been seen before the implementation must return ``True`` and the token
    should be rejected.  After accepting a token the caller must call
    :meth:`deny` so subsequent presentations are blocked.

    The no-op stub :class:`NoOpJtiDenylist` allows the server to start without
    a persistent store.  Replace it with a Redis- or DB-backed implementation
    in production to satisfy the replay-defence requirement in the security
    design document.
    """

    async def is_denied(self, jti: str) -> bool:
        """Return ``True`` if *jti* has already been used."""
        ...

    async def deny(self, jti: str, exp: int) -> None:
        """Record *jti* as consumed until its expiry timestamp *exp*."""
        ...


class NoOpJtiDenylist:
    """No-op :class:`JtiDenylist` — every token is accepted.

    Suitable for development and test environments.  In production wire in
    an implementation backed by a shared store (Redis, DB) to prevent token
    replay across multiple API instances.
    """

    async def is_denied(self, jti: str) -> bool:  # noqa: ARG002
        return False

    async def deny(self, jti: str, exp: int) -> None:  # noqa: ARG002
        pass


_JTI_DENYLIST: JtiDenylist = NoOpJtiDenylist()


def set_jti_denylist(denylist: JtiDenylist) -> None:
    """Replace the module-level JTI denylist (call once at startup)."""

    global _JTI_DENYLIST  # noqa: PLW0603
    _JTI_DENYLIST = denylist


# ---------------------------------------------------------------------------
# JWKs cache
# ---------------------------------------------------------------------------


@dataclass
class _JwksCache:
    """Cached JWKs indexed by ``kid`` for O(1) signing-key lookup."""

    keys_by_kid: dict[str, dict[str, Any]]
    fetched_at: float
    # Keep the last successful payload for graceful degradation when the IdP
    # becomes temporarily unavailable.
    raw_keys: list[dict[str, Any]] = field(default_factory=list)


_JWKS_CACHE: _JwksCache | None = None
# Single-flight lock prevents the cache stampede when many requests arrive
# simultaneously after the TTL expires.
_JWKS_REFRESH_LOCK: asyncio.Lock | None = None


def _get_refresh_lock() -> asyncio.Lock:
    """Return the module-level lock, creating it on first use.

    The lock cannot be created at import time because that would bind it to
    whatever event loop happens to be current then, which differs from the
    one Uvicorn / gRPC eventually run.
    """

    global _JWKS_REFRESH_LOCK  # noqa: PLW0603 - module-level singleton
    if _JWKS_REFRESH_LOCK is None:
        _JWKS_REFRESH_LOCK = asyncio.Lock()
    return _JWKS_REFRESH_LOCK


async def _do_fetch_jwks(settings: Settings) -> list[dict[str, Any]]:
    """Network round-trip: discovery -> jwks_uri -> JWK list."""

    issuer = settings.oidc_issuer.rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=5.0) as client:
        discovery_resp = await client.get(discovery_url)
        discovery_resp.raise_for_status()
        jwks_uri = discovery_resp.json()["jwks_uri"]
        # Defensive: the IdP must serve JWKs over TLS.
        if not jwks_uri.startswith("https://"):
            raise AuthenticationError(
                f"OIDC jwks_uri must use https:// (got {jwks_uri!r})",
            )
        jwks_resp = await client.get(jwks_uri)
        jwks_resp.raise_for_status()
        keys = jwks_resp.json().get("keys", [])
    return keys


def _build_cache(keys: list[dict[str, Any]]) -> _JwksCache:
    """Index a JWK list by ``kid`` for fast lookup."""

    keys_by_kid: dict[str, dict[str, Any]] = {}
    for key in keys:
        kid = key.get("kid")
        if isinstance(kid, str):
            keys_by_kid[kid] = key
    return _JwksCache(
        keys_by_kid=keys_by_kid,
        fetched_at=time.monotonic(),
        raw_keys=keys,
    )


async def _get_jwks(settings: Settings, *, force_refresh: bool = False) -> _JwksCache:
    """Return JWKs, refreshing under a lock when the TTL has elapsed.

    On IdP failure we keep serving the last-known-good cache for up to
    ``2 * ttl`` seconds to absorb transient outages instead of failing
    authentication system-wide.
    """

    global _JWKS_CACHE  # noqa: PLW0603 - module-level cache by design
    now = time.monotonic()
    ttl = settings.oidc_jwks_cache_ttl_seconds

    # Fast-path: fresh cache, no refresh requested.
    if (
        not force_refresh
        and _JWKS_CACHE is not None
        and now - _JWKS_CACHE.fetched_at < ttl
    ):
        return _JWKS_CACHE

    lock = _get_refresh_lock()
    async with lock:
        # Double-check inside the lock; another coroutine may have refreshed.
        cache = _JWKS_CACHE
        if (
            not force_refresh
            and cache is not None
            and time.monotonic() - cache.fetched_at < ttl
        ):
            return cache

        try:
            keys = await _do_fetch_jwks(settings)
            _JWKS_CACHE = _build_cache(keys)
            return _JWKS_CACHE
        except (httpx.HTTPError, AuthenticationError) as exc:
            # Graceful degradation: serve stale cache up to 2*ttl old.
            if cache is not None and time.monotonic() - cache.fetched_at < 2 * ttl:
                return cache
            raise AuthenticationError(f"Unable to fetch JWKs: {exc}") from exc


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------


async def validate_oidc_jwt(token: str) -> dict[str, Any]:
    """Validate an OIDC JWT and return its claims.

    Performs signature, issuer, audience, expiry, ``iat``, and ``nbf`` checks
    with a configurable clock-skew leeway, and pins the algorithm allow-list
    to public-key families to defeat the ``alg=HS256`` confusion attack.

    If the token carries a ``jti`` claim it is checked against the configured
    :class:`JtiDenylist` to prevent replay attacks.  By default the no-op
    denylist is active; wire in a persistent implementation via
    :func:`set_jti_denylist` at startup.
    """

    settings = get_settings()

    try:
        unverified_header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        raise AuthenticationError(f"Malformed JWT header: {exc}") from exc

    alg = unverified_header.get("alg")
    if alg not in settings.oidc_allowed_algorithms:
        raise AuthenticationError(
            f"JWT algorithm {alg!r} is not in the allow-list "
            f"{tuple(settings.oidc_allowed_algorithms)}",
        )

    kid = unverified_header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AuthenticationError("JWT header is missing a string 'kid'")

    cache = await _get_jwks(settings)
    jwk_dict = cache.keys_by_kid.get(kid)
    if jwk_dict is None:
        # Force one refresh in case the IdP rotated keys before TTL expired.
        cache = await _get_jwks(settings, force_refresh=True)
        jwk_dict = cache.keys_by_kid.get(kid)
    if jwk_dict is None:
        raise AuthenticationError("Signing key not found in JWKs")

    try:
        signing_key = PyJWK.from_dict(jwk_dict).key
    except (KeyError, ValueError, PyJWTError) as exc:
        raise AuthenticationError(f"Cannot load signing key from JWK: {exc}") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=list(settings.oidc_allowed_algorithms),
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_clock_skew_leeway_seconds,
            options={
                # pyjwt uses a ``require`` list rather than per-claim flags.
                # Enumerate every claim that must be present so that a
                # malformed token fails fast.
                "require": ["exp", "iat", "sub", "iss", "aud"],
            },
        )
    except PyJWTError as exc:
        raise AuthenticationError(f"Invalid JWT: {exc}") from exc

    # JTI replay check — no-op when no persistent denylist is configured.
    jti = claims.get("jti")
    if isinstance(jti, str) and jti:
        if await _JTI_DENYLIST.is_denied(jti):
            raise AuthenticationError("JWT ID (jti) has already been used (replay detected)")
        await _JTI_DENYLIST.deny(jti, int(claims.get("exp", 0)))

    return claims


def extract_tenant_from_claims(claims: dict[str, Any]) -> str:
    """Resolve a tenant id from JWT claims.

    Checks (in order): ``tenant_id``, ``https://lance.dev/tenant``, ``org``.
    There is **no** fallback to ``sub``: ``sub`` identifies an individual
    principal, not a tenant, and using it as such would create an unbounded
    set of one-user tenants.
    """

    for key in ("tenant_id", "https://lance.dev/tenant", "org"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    raise AuthenticationError(
        "Unable to resolve tenant_id from JWT claims; "
        "set a 'tenant_id' (or 'org') claim in the IdP",
    )


def extract_tenant_from_spiffe_id(spiffe_id: str) -> str | None:
    """Extract a tenant id from a SPIFFE URI SAN (H-3).

    LCP convention: ``spiffe://<trust-domain>/tenant/<tenant_id>[/…]``.
    Returns ``None`` if *spiffe_id* is not a SPIFFE URI or does not follow
    the expected path structure — callers should fall back to x509 subject
    parsing in that case.

    Examples::

        >>> extract_tenant_from_spiffe_id("spiffe://lance/tenant/acme/worker/w-7")
        'acme'
        >>> extract_tenant_from_spiffe_id("spiffe://other/service/foo")
        None
    """

    if not spiffe_id.startswith("spiffe://"):
        return None
    # Strip the scheme and split on "/"
    # spiffe://trust-domain/tenant/<tenant_id>/...
    path = spiffe_id[len("spiffe://"):]
    parts = path.split("/")
    # parts[0] = trust-domain, parts[1] = "tenant", parts[2] = tenant_id
    if len(parts) >= 3 and parts[1] == "tenant" and parts[2]:
        return parts[2]
    return None


def extract_tenant_from_x509_subject(subject_cn: str, organization: str | None) -> str:
    """Resolve a tenant id from an mTLS client certificate.

    Convention: ``CN=<worker_id>``, ``O=<tenant_id>``.  If ``O`` is missing,
    the function falls back to a parsable ``tenant-<value>`` prefix in CN.
    """

    if organization:
        return organization
    if subject_cn.startswith("tenant-"):
        return subject_cn.split(".", 1)[0]
    raise AuthenticationError(
        "Client certificate does not carry a tenant attribute; "
        "expected O=<tenant_id> or CN=tenant-<id>.<worker>",
    )


def reset_jwks_cache_for_tests() -> None:
    """Clear the module-level JWKs cache.  For tests only."""

    global _JWKS_CACHE, _JWKS_REFRESH_LOCK  # noqa: PLW0603
    _JWKS_CACHE = None
    _JWKS_REFRESH_LOCK = None


def reset_jti_denylist_for_tests() -> None:
    """Reset the JTI denylist to the no-op implementation.  For tests only."""

    global _JTI_DENYLIST  # noqa: PLW0603
    _JTI_DENYLIST = NoOpJtiDenylist()
