"""Global pytest fixtures shared by unit and API tests.

Design rules:
- Tests must not require a real DB/IdP; everything is in-process.
- ``get_settings`` is patched per test to keep test runs hermetic.
- JWT signing uses an ephemeral RSA key generated once per session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# ---------------------------------------------------------------------------
# Settings + cache reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset all module-level singletons between tests for isolation."""

    from lcp.core.config import reset_settings
    from lcp.core.security import reset_jti_denylist_for_tests, reset_jwks_cache_for_tests
    from lcp.core.tenant import _current_tenant
    from lcp.db.session import reset_engine, reset_session_factory

    reset_settings()
    reset_jwks_cache_for_tests()
    reset_jti_denylist_for_tests()
    _current_tenant.set(None)  # belt-and-braces: drop any leaked principal
    reset_engine()
    reset_session_factory()
    yield
    reset_settings()
    reset_jwks_cache_for_tests()
    reset_jti_denylist_for_tests()
    reset_engine()
    reset_session_factory()


# ---------------------------------------------------------------------------
# RSA key + JWKs fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_key_pair() -> dict[str, Any]:
    """Generate a single RSA key-pair for the whole test session.

    Returns a dict with ``private_pem``, ``public_pem``, and a JWKs document
    in the shape an OIDC IdP would publish.
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()

    def _b64url_uint(value: int) -> str:
        import base64
        length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key-1",
        "n": _b64url_uint(public_numbers.n),
        "e": _b64url_uint(public_numbers.e),
    }
    jwks = {"keys": [jwk]}
    return {"private_pem": private_pem, "jwks": jwks, "kid": "test-key-1"}


@pytest.fixture
def issue_token(rsa_key_pair: dict[str, Any]) -> Any:
    """Factory that signs a JWT with the session's private key."""

    private_pem = rsa_key_pair["private_pem"]
    kid = rsa_key_pair["kid"]

    def _issue(
        *,
        tenant_id: str = "tenant-acme",
        sub: str = "user-123",
        issuer: str = "https://idp.test",
        audience: str = "lcp-api",
        algorithm: str = "RS256",
        exp_offset: int = 600,
        extra_claims: dict[str, Any] | None = None,
        override_kid: str | None = None,
    ) -> str:
        import time
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "sub": sub,
            "tenant_id": tenant_id,
            "iat": now,
            "nbf": now,
            "exp": now + exp_offset,
        }
        if extra_claims:
            claims.update(extra_claims)
        return pyjwt.encode(
            claims,
            private_pem,
            algorithm=algorithm,
            headers={"kid": override_kid or kid},
        )

    return _issue


# ---------------------------------------------------------------------------
# JWKs network mocking
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_jwks(monkeypatch: pytest.MonkeyPatch, rsa_key_pair: dict[str, Any]) -> None:
    """Replace ``_do_fetch_jwks`` with an in-memory implementation."""

    from lcp.core import security

    async def fake_fetch(_settings: Any) -> list[dict[str, Any]]:
        return rsa_key_pair["jwks"]["keys"]

    monkeypatch.setattr(security, "_do_fetch_jwks", fake_fetch)


# ---------------------------------------------------------------------------
# REST app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin settings to deterministic values for tests."""

    monkeypatch.setenv("LCP_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("LCP_OIDC_AUDIENCE", "lcp-api")
    monkeypatch.setenv("LCP_DB_DSN", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("LCP_ENFORCE_TENANT_RLS", "true")


@pytest.fixture
async def rest_app(configured_settings: None, patch_jwks: None) -> AsyncIterator[Any]:
    """Build a fresh FastAPI app per test (lifespan disabled for speed)."""

    from fastapi import FastAPI

    from lcp.api.rest.auth import OIDCAuthMiddleware
    from lcp.api.rest.routers import (
        datasets,
        indexes,
        lifecycle,
        meta,
        tasks,
        vectorization,
    )
    from lcp.db.models import Base
    from lcp.db.rls import install_rls_listener
    from lcp.db.session import get_engine

    # Create the ORM schema in the in-memory sqlite DB and install the RLS hook
    # so the handlers that hit the DB behave like production.  ``rest_app`` is
    # function-scoped, so the singletons were already reset by the
    # ``_reset_singletons`` autouse fixture in this module.
    engine = get_engine()
    install_rls_listener(engine.sync_engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.add_middleware(OIDCAuthMiddleware)
    app.include_router(datasets.router)
    app.include_router(tasks.router)
    app.include_router(indexes.router)
    app.include_router(lifecycle.router)
    app.include_router(vectorization.router)
    app.include_router(meta.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    yield app
    # Drop the in-memory tables and dispose the engine so the next test starts
    # from a pristine state (the LRU cache reset alone is not enough for
    # aiosqlite ``StaticPool`` connections).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def http_client(rest_app: Any) -> AsyncIterator[Any]:
    """Async HTTPX client wired to the in-process FastAPI app."""

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=rest_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Stub session for handlers that do not actually use the DB
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``db_session`` dependency with a no-op generator."""

    from lcp.api.rest import deps

    async def _empty_session() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(deps, "db_session", _empty_session)


def _maybe_decode(value: bytes | str) -> str:
    """Helper for tests that must compare gRPC auth_context strings."""

    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


__all__ = [
    "issue_token",
    "patch_jwks",
    "configured_settings",
    "rest_app",
    "http_client",
    "rsa_key_pair",
    "stub_session",
    "_maybe_decode",
]
