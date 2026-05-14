"""Application-wide configuration, sourced from environment variables.

All settings can be overridden through environment variables prefixed with
``LCP_`` (e.g. ``LCP_OIDC_ISSUER``).  The skeleton purposely avoids reading any
secret material from disk; production deployments should mount secrets via
Kubernetes secrets or Vault.
"""

from __future__ import annotations

import os
import warnings

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

    # -- Lance data-plane ----------------------------------------------------
    # Object-storage credentials and endpoint for the lance dataset layer.
    # Empty defaults are intentional: tests and pure-control-plane unit
    # environments must not require a real bucket.  When the worker is
    # deployed to talk to MinIO/S3, set ``LCP_LANCE_*`` env vars.
    #
    # Why a flat set of strings instead of a single ``storage_options`` dict:
    # pydantic-settings parses dicts from JSON env vars, which is awkward
    # for ops; ``LCP_LANCE_ACCESS_KEY=...`` is closer to the AWS CLI muscle
    # memory operators already have.
    lance_storage_endpoint: str = Field(
        default="",
        description="S3-compatible endpoint URL, e.g. http://minio:9000",
    )
    lance_storage_access_key: str = Field(default="", description="AWS_ACCESS_KEY_ID")
    lance_storage_secret_key: str = Field(default="", description="AWS_SECRET_ACCESS_KEY")
    lance_storage_region: str = Field(default="us-east-1", description="AWS region")
    # MinIO in dev runs plain HTTP; production should terminate TLS upstream
    # and flip this to False.
    lance_storage_allow_http: bool = True
    # Path-style requests: MinIO does not implement virtual-hosted-style
    # bucket subdomains.  Real AWS S3 prefers virtual-hosted, so flip this
    # off when pointing at AWS.
    lance_storage_path_style: bool = True

    def model_post_init(self, __context: object) -> None:  # noqa: ANN001
        """Warn on unrecognised ``LCP_``-prefixed environment variables.

        ``extra="ignore"`` prevents start-up failures when the process
        environment contains unrelated ``LCP_`` variables (e.g. from another
        service sharing the same host).  However, a silent ignore also means
        typos like ``LCP_OIDC_AUDIANCE`` pass unnoticed and the intended
        override never takes effect.

        This hook bridges the gap: it scans ``os.environ`` for ``LCP_``
        keys that do not map to a known field and emits a ``UserWarning``.
        The warning is surfaced at startup so operators spot typos
        immediately without requiring a full ``extra="forbid"`` policy.
        """
        known = {
            f"LCP_{name.upper()}"
            for name in self.__class__.model_fields
        }
        unknown = [
            key
            for key in os.environ
            if key.startswith("LCP_") and key not in known
        ]
        if unknown:
            warnings.warn(
                f"Unrecognised LCP_ environment variable(s): {', '.join(sorted(unknown))}. "
                "Check for typos — these settings will be ignored.",
                UserWarning,
                stacklevel=2,
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the shared ``Settings`` singleton, creating it on first call.

    Use :func:`reset_settings` in tests to force a fresh instance on the
    next call.  The explicit singleton avoids the ``lru_cache.cache_clear()``
    ritual that test authors are otherwise required to remember.
    """

    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset the singleton so the next :func:`get_settings` call creates a
    fresh instance.  For test use only.
    """

    global _settings  # noqa: PLW0603
    _settings = None
