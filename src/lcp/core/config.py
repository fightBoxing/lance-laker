"""Application-wide configuration, sourced from environment variables.

All settings can be overridden through environment variables prefixed with
``LCP_`` (e.g. ``LCP_OIDC_ISSUER``).  The skeleton purposely avoids reading any
secret material from disk; production deployments should mount secrets via
Kubernetes secrets or Vault.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for both REST and gRPC servers."""

    model_config = SettingsConfigDict(
        env_prefix="LCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- App metadata --------------------------------------------------------
    app_name: str = "lcp"
    app_env: str = Field(default="dev", description="dev / staging / prod")

    # -- REST server ---------------------------------------------------------
    # Default to loopback; production manifests must set LCP_REST_HOST=0.0.0.0
    # explicitly so a developer running ``uvicorn`` does not expose the API on
    # every interface by accident.
    rest_host: str = "127.0.0.1"
    rest_port: int = 8080

    # -- gRPC server ---------------------------------------------------------
    # Same secure-by-default rationale as ``rest_host``.
    grpc_host: str = "127.0.0.1"
    grpc_port: int = 50051

    # -- OIDC / OAuth2 -------------------------------------------------------
    # Generic OIDC discovery URL.  The skeleton fetches JWKs lazily.
    oidc_issuer: str = Field(
        default="https://idp.example.com",
        description="OIDC issuer URL; used to resolve .well-known config",
    )
    oidc_audience: str = Field(default="lcp-api", description="Expected JWT aud")
    oidc_jwks_cache_ttl_seconds: int = 3600
    # Pin allowed signature algorithms to public-key families only; this
    # prevents the classic ``alg=HS256`` confusion attack where an attacker
    # signs a token with the public key as if it were an HMAC secret.
    oidc_allowed_algorithms: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384")
    # Tolerate small clock skew between the IdP and LCP (in seconds).
    oidc_clock_skew_leeway_seconds: int = 30

    # -- mTLS (gRPC) ---------------------------------------------------------
    mtls_server_cert_path: str = "/etc/lcp/tls/server.crt"
    mtls_server_key_path: str = "/etc/lcp/tls/server.key"
    mtls_ca_cert_path: str = "/etc/lcp/tls/ca.crt"
    mtls_require_client_cert: bool = True

    # -- Database ------------------------------------------------------------
    db_dsn: str = Field(
        default="mysql+aiomysql://lcp:lcp@127.0.0.1:3306/lcp",
        description="SQLAlchemy async DSN",
    )
    db_pool_size: int = 10
    db_pool_recycle_seconds: int = 3600

    # -- Multi-tenant --------------------------------------------------------
    enforce_tenant_rls: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""

    return Settings()
