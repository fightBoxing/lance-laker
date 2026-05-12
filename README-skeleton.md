# LCP Skeleton (Phase-1 Stub)

This is the **runnable stub** of the LanceDB Control Plane (LCP) services.

> **Scope:** REST + gRPC server skeleton with auth middleware, tenant context,
> and CI lint integration. **No business logic** is implemented yet — all
> handlers return `501 Not Implemented` or empty payloads.

## Layout

```
src/lcp/
├── api/
│   ├── rest/                 # FastAPI app
│   └── grpc/                 # gRPC server
├── core/                     # config / tenant context / security helpers
└── db/                       # SQLAlchemy session + RLS event listener
```

## Quick Start

```bash
# Install in editable mode
pip install -e ".[dev]"

# Run REST server
uvicorn lcp.api.rest.main:app --reload --port 8080

# Run gRPC server
python -m lcp.api.grpc.server --port 50051
```

## Health Check

```bash
curl http://localhost:8080/healthz
# {"status":"ok","version":"0.1.0"}
```

## Authentication Modes

| Plane                    | Protocol | Auth                     | Header / Material               |
| ------------------------ | -------- | ------------------------ | ------------------------------- |
| North-bound (User → LCP) | REST     | OAuth2 Bearer (OIDC JWT) | `Authorization: Bearer <jwt>`   |
| East-west (Worker ↔ LCP) | gRPC     | mTLS                     | x509 client cert (CN=worker_id) |

## Multi-Tenant RLS

The skeleton enforces row-level isolation via two layers:

1. **App-layer (mandatory)** — SQLAlchemy `before_execute` hook auto-injects
   `tenant_id = :current_tenant`.
2. **DB-layer (defence-in-depth)** — `v_*_tenant` views in
   `docs/architecture/ddl/lcp_rls_views.sql`.

See [LCP-Security-Design.md](docs/architecture/security/LCP-Security-Design.md).

## Not Implemented (intentionally)

- Real DB connection (engine is created but tables are not bound)
- IdP signing / token issuance (assumes external OIDC provider)
- Business logic in handlers
- Full unit-test coverage
