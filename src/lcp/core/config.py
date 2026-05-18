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
    # Max overflow connections beyond pool_size.  Under burst write load
    # (e.g. many concurrent compaction triggers) the pool saturates quickly
    # at its base size; allowing overflow lets those requests through without
    # queuing.  Total max open connections = db_pool_size + db_max_overflow.
    db_max_overflow: int = Field(
        default=20,
        description=(
            "SQLAlchemy max_overflow: extra connections allowed beyond "
            "db_pool_size before requests queue.  Only applies to pooled "
            "dialects (MySQL/PostgreSQL); ignored for SQLite."
        ),
    )
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

    # -- Gravitino integration -------------------------------------------------
    # Connection to a Gravitino metalake for schema/catalog discovery.
    # Empty ``gravitino_url`` disables the integration (all meta-sync endpoints
    # return 503).  This is intentional for dev environments that don't run a
    # Gravitino server.
    gravitino_url: str = Field(
        default="",
        description=(
            "Gravitino REST endpoint, e.g. http://gravitino:8090. "
            "Leave empty to disable Gravitino integration."
        ),
    )
    gravitino_metalake: str = Field(
        default="default",
        description="Gravitino metalake name (logical namespace).",
    )
    gravitino_catalog: str = Field(
        default="lance",
        description="Catalog within the metalake that holds LanceDB filesets.",
    )
    gravitino_auth_token: str = Field(
        default="",
        description="Bearer token for Gravitino API auth (empty = no auth).",
    )
    gravitino_timeout_seconds: float = Field(
        default=10.0,
        description="HTTP timeout for Gravitino API calls.",
    )

    # -- Vectorization (Embedding) --------------------------------------------
    # Default embedding service endpoint.  VectorizationRule rows can override
    # this per-rule via ``model_endpoint``; this setting provides the fallback
    # for rules that leave the field empty.
    embedding_default_endpoint: str = Field(
        default="",
        description=(
            "Default embedding model HTTP endpoint, e.g. "
            "http://embedding-svc:8000/v1/embeddings. "
            "Empty = vectorization disabled unless rule specifies endpoint."
        ),
    )
    embedding_request_timeout_seconds: float = Field(
        default=30.0,
        description="HTTP timeout for embedding model calls.",
    )
    # API key for third-party embedding services (OpenAI, Cohere, Zhipu,
    # Dashscope, etc.).  Sent as ``Authorization: Bearer <key>`` header.
    # VectorizationRule rows can override this per-rule via the ``extra``
    # JSON field (key: "api_key"); this setting provides the global fallback.
    embedding_api_key: str = Field(
        default="",
        description=(
            "Default API key for third-party embedding model services. "
            "Sent as Bearer token in the Authorization header. "
            "Leave empty for self-hosted models that need no auth."
        ),
    )
    # Some providers use a custom header name (e.g. "X-Api-Key" for Cohere,
    # "Api-Key" for Azure OpenAI).  This setting controls the header name;
    # the value is always ``embedding_api_key``.
    embedding_api_key_header: str = Field(
        default="Authorization",
        description=(
            "HTTP header name for the API key. "
            "Use 'Authorization' for OpenAI-compatible (sends 'Bearer <key>'). "
            "Use 'X-Api-Key' for Cohere. "
            "Use 'Api-Key' for Azure OpenAI."
        ),
    )

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
