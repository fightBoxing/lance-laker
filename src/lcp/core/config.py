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

    # -- Index watcher daemon -----------------------------------------------
    # How often the event-driven index watcher polls every watch-enabled
    # index for fresh lance versions / un-indexed rows.
    #
    # Trade-off: lower values shrink the worst-case "user-write -> index
    # covered" SLA but increase MySQL + lance manifest read pressure.
    # 10s leaves >100x headroom over the 1 min lifecycle planner cron
    # while still hitting a single-digit-second SLA in the common case.
    #
    # Override via ``LCP_INDEX_WATCHER_INTERVAL_SECONDS=<float>`` (env
    # var or ``.env``).  The watcher CLI also accepts
    # ``--interval-seconds`` for ad-hoc overrides during incident
    # response; CLI wins over env when both are set.
    index_watcher_interval_seconds: float = Field(
        default=10.0,
        description=(
            "Sleep this long between watch passes; CLI flag overrides."
        ),
    )

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

    # -- Gravitino metadata catalog -----------------------------------------
    # The control plane reconciles MySQL ``dataset`` rows against a remote
    # Gravitino instance.  Empty default means "feature off" so unit tests
    # and skeleton deployments do not require a live Gravitino server.
    #
    # Naming follows ``LCP_GRAVITINO_*`` env-prefix convention; ``url`` keeps
    # parity with the upstream client config field (``GRAVITINO_URL``).
    gravitino_url: str = Field(
        default="",
        description="Gravitino REST endpoint, e.g. http://gravitino:8090",
    )
    gravitino_metalake: str = Field(
        default="lance_laker",
        description="Metalake under which Lance fileset catalogs live",
    )
    gravitino_catalog: str = Field(
        default="lance_oss",
        description="Fileset catalog name; must already exist in Gravitino",
    )
    # Auth: ``none`` = no header (dev / in-cluster).  ``bearer`` reads the
    # token from ``LCP_GRAVITINO_TOKEN``.  Other modes (basic, oauth2 client
    # credentials) are deferred until a real deployment requires them; the
    # client raises ``NotImplementedError`` so production cannot silently
    # drop auth.
    gravitino_auth_type: str = Field(default="none", description="none|bearer")
    gravitino_token: str = Field(default="", description="Bearer token, if any")
    # Per-request HTTP timeout; Gravitino lookups are usually <50ms but we
    # leave headroom for cold starts.
    gravitino_request_timeout_seconds: float = 5.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""

    return Settings()
