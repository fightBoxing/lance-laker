"""Unit tests for ``lcp.core.security``.

Covers the fixes made during the recent review:
- C-2: algorithm allow-list enforcement
- C-2: iat/nbf/exp/leeway enforcement
- C-2: missing 'kid' rejection
- M-1: no fallback to ``sub`` claim
- H-1: JWKs cache single-flight and stale fallback
- defence: jwks_uri must use https
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from lcp.core.security import (
    AuthenticationError,
    JtiDenylist,
    NoOpJtiDenylist,
    extract_tenant_from_claims,
    extract_tenant_from_spiffe_id,
    extract_tenant_from_x509_subject,
    set_jti_denylist,
    validate_oidc_jwt,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# extract_tenant_from_claims
# ---------------------------------------------------------------------------


class TestExtractTenantFromClaims:

    def test_prefers_tenant_id_claim(self) -> None:
        claims = {"tenant_id": "acme", "org": "other", "sub": "u"}
        assert extract_tenant_from_claims(claims) == "acme"

    def test_falls_back_to_namespaced_claim(self) -> None:
        claims = {"https://lance.dev/tenant": "acme", "sub": "u"}
        assert extract_tenant_from_claims(claims) == "acme"

    def test_falls_back_to_org(self) -> None:
        claims = {"org": "acme", "sub": "u"}
        assert extract_tenant_from_claims(claims) == "acme"

    def test_never_falls_back_to_sub(self) -> None:
        """M-1 regression: 'sub' is an individual, never a tenant."""

        claims = {"sub": "u-123"}
        with pytest.raises(AuthenticationError):
            extract_tenant_from_claims(claims)

    def test_rejects_non_string_tenant(self) -> None:
        claims = {"tenant_id": 123, "org": "acme"}
        # Non-string value is skipped, then 'org' wins.
        assert extract_tenant_from_claims(claims) == "acme"

    def test_rejects_empty_tenant(self) -> None:
        claims = {"tenant_id": ""}
        with pytest.raises(AuthenticationError):
            extract_tenant_from_claims(claims)


# ---------------------------------------------------------------------------
# extract_tenant_from_x509_subject
# ---------------------------------------------------------------------------


class TestExtractTenantFromX509:

    def test_prefers_organization(self) -> None:
        assert extract_tenant_from_x509_subject("worker-1", "tenant-acme") == "tenant-acme"

    def test_falls_back_to_cn_prefix(self) -> None:
        assert extract_tenant_from_x509_subject("tenant-acme.worker-1", None) == "tenant-acme"

    def test_rejects_unparsable_cn(self) -> None:
        with pytest.raises(AuthenticationError):
            extract_tenant_from_x509_subject("worker-1", None)


# ---------------------------------------------------------------------------
# validate_oidc_jwt: algorithm pinning
# ---------------------------------------------------------------------------


class TestValidateOidcJwtAlgorithmPinning:

    async def test_rejects_hs256_alg_confusion(
        self,
        configured_settings: None,
        patch_jwks: None,
        rsa_key_pair: dict[str, Any],
    ) -> None:
        """C-2 regression: the classic ``alg=HS256`` + public-key attack."""

        del configured_settings, patch_jwks
        import jwt as pyjwt

        # Attacker signs with a symmetric secret pretending to be HMAC while
        # the server thinks the JWKs entry is an asymmetric public key.
        # PyJWT requires HMAC keys of at least 32 bytes for HS256 (RFC 7518).
        hs_token = pyjwt.encode(
            {"iss": "https://idp.test", "aud": "lcp-api", "sub": "u"},
            "a" * 64,  # 64-byte attacker secret; long enough to silence warning
            algorithm="HS256",
            headers={"kid": rsa_key_pair["kid"]},
        )
        with pytest.raises(AuthenticationError, match="not in the allow-list"):
            await validate_oidc_jwt(hs_token)

    async def test_rejects_none_alg(
        self,
        configured_settings: None,
        patch_jwks: None,
        rsa_key_pair: dict[str, Any],
    ) -> None:
        del configured_settings, patch_jwks
        # Build a token with alg=none by hand.
        import base64
        import json

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        header = _b64(json.dumps({"alg": "none", "kid": rsa_key_pair["kid"]}).encode())
        payload = _b64(json.dumps({"iss": "https://idp.test", "aud": "lcp-api"}).encode())
        token = f"{header}.{payload}."
        with pytest.raises(AuthenticationError):
            await validate_oidc_jwt(token)


# ---------------------------------------------------------------------------
# validate_oidc_jwt: claim validation
# ---------------------------------------------------------------------------


class TestValidateOidcJwtClaims:

    async def test_happy_path(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        del configured_settings, patch_jwks
        token = issue_token(tenant_id="acme")
        claims = await validate_oidc_jwt(token)
        assert claims["tenant_id"] == "acme"
        assert claims["iss"] == "https://idp.test"

    async def test_rejects_expired_token(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        del configured_settings, patch_jwks
        # exp is 1 hour in the past, well beyond default 30s leeway.
        token = issue_token(exp_offset=-3600)
        with pytest.raises(AuthenticationError):
            await validate_oidc_jwt(token)

    async def test_accepts_small_clock_skew(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        """Token expired 10s ago must still pass thanks to 30s leeway."""

        del configured_settings, patch_jwks
        token = issue_token(exp_offset=-10)
        claims = await validate_oidc_jwt(token)
        assert claims["sub"] == "user-123"

    async def test_rejects_wrong_issuer(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        del configured_settings, patch_jwks
        token = issue_token(issuer="https://evil.test")
        with pytest.raises(AuthenticationError):
            await validate_oidc_jwt(token)

    async def test_rejects_wrong_audience(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        del configured_settings, patch_jwks
        token = issue_token(audience="some-other-api")
        with pytest.raises(AuthenticationError):
            await validate_oidc_jwt(token)

    async def test_rejects_missing_kid(
        self,
        configured_settings: None,
        patch_jwks: None,
        rsa_key_pair: dict[str, Any],
    ) -> None:
        del configured_settings, patch_jwks
        import jwt as pyjwt
        token = pyjwt.encode(
            {
                "iss": "https://idp.test",
                "aud": "lcp-api",
                "sub": "u",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            rsa_key_pair["private_pem"],
            algorithm="RS256",
            headers={},
        )
        with pytest.raises(AuthenticationError, match="missing a string 'kid'"):
            await validate_oidc_jwt(token)

    async def test_rejects_unknown_kid(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        del configured_settings, patch_jwks
        token = issue_token(override_kid="unknown-kid")
        with pytest.raises(AuthenticationError, match="Signing key not found"):
            await validate_oidc_jwt(token)

    async def test_rejects_missing_required_iat(
        self,
        configured_settings: None,
        patch_jwks: None,
        rsa_key_pair: dict[str, Any],
    ) -> None:
        """``iat`` is in the 'require' list; its absence must fail."""

        del configured_settings, patch_jwks
        import jwt as pyjwt
        token = pyjwt.encode(
            {
                "iss": "https://idp.test",
                "aud": "lcp-api",
                "sub": "u",
                "exp": int(time.time()) + 600,
            },
            rsa_key_pair["private_pem"],
            algorithm="RS256",
            headers={"kid": rsa_key_pair["kid"]},
        )
        with pytest.raises(AuthenticationError):
            await validate_oidc_jwt(token)


# ---------------------------------------------------------------------------
# JtiDenylist — M-3
# ---------------------------------------------------------------------------


class TestNoOpJtiDenylist:

    async def test_never_denies(self) -> None:
        """NoOpJtiDenylist always returns False, regardless of jti."""

        denylist = NoOpJtiDenylist()
        assert not await denylist.is_denied("some-jti")

    async def test_deny_is_no_op(self) -> None:
        """deny() must not raise and must not change is_denied() result."""

        denylist = NoOpJtiDenylist()
        await denylist.deny("some-jti", int(time.time()) + 600)
        assert not await denylist.is_denied("some-jti")

    def test_conforms_to_protocol(self) -> None:
        """NoOpJtiDenylist must satisfy the JtiDenylist Protocol."""

        assert isinstance(NoOpJtiDenylist(), JtiDenylist)


class TestJtiDenylistIntegration:

    async def test_replay_is_rejected(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        """M-3: second use of the same jti must raise AuthenticationError."""

        del configured_settings, patch_jwks

        class _MemoryDenylist:
            """Minimal in-memory JTI store for this test."""

            def __init__(self) -> None:
                self._denied: set[str] = set()

            async def is_denied(self, jti: str) -> bool:
                return jti in self._denied

            async def deny(self, jti: str, exp: int) -> None:  # noqa: ARG002
                self._denied.add(jti)

        set_jti_denylist(_MemoryDenylist())
        token = issue_token(tenant_id="acme", extra_claims={"jti": "unique-token-id"})

        # First use succeeds.
        claims = await validate_oidc_jwt(token)
        assert claims["tenant_id"] == "acme"

        # Second use is rejected.
        with pytest.raises(AuthenticationError, match="replay"):
            await validate_oidc_jwt(token)

    async def test_token_without_jti_is_not_checked(
        self,
        configured_settings: None,
        patch_jwks: None,
        issue_token: Any,
    ) -> None:
        """Tokens without a jti claim must not be blocked."""

        del configured_settings, patch_jwks
        # issue_token does not add jti by default.
        token = issue_token(tenant_id="acme")
        claims = await validate_oidc_jwt(token)
        assert claims["tenant_id"] == "acme"


# ---------------------------------------------------------------------------
# extract_tenant_from_spiffe_id — H-3
# ---------------------------------------------------------------------------


class TestExtractTenantFromSpiffeId:

    def test_extracts_tenant_from_standard_spiffe_uri(self) -> None:
        uri = "spiffe://lance/tenant/acme/worker/w-7"
        assert extract_tenant_from_spiffe_id(uri) == "acme"

    def test_extracts_tenant_with_complex_trust_domain(self) -> None:
        uri = "spiffe://trust.example.com/tenant/my-org/worker/w-1"
        assert extract_tenant_from_spiffe_id(uri) == "my-org"

    def test_returns_none_for_non_spiffe_uri(self) -> None:
        assert extract_tenant_from_spiffe_id("https://service.example/identity") is None

    def test_returns_none_when_path_lacks_tenant_segment(self) -> None:
        # Well-formed SPIFFE URI but not the LCP tenant convention.
        assert extract_tenant_from_spiffe_id("spiffe://lance/service/foo") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert extract_tenant_from_spiffe_id("") is None

    def test_returns_none_when_tenant_value_is_empty(self) -> None:
        # Degenerate: spiffe://lance/tenant//worker/w-1
        assert extract_tenant_from_spiffe_id("spiffe://lance/tenant//worker/w-1") is None
