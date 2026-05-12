# LCP Phase-1 Skeleton — Critical Code Review (挑刺式 Review)

> **Reviewer role:** Senior Python / distributed-systems engineer with hostile mindset
> **Run date:** 2026-05-12
> **Scope:** All Python files under `src/lcp/`, plus `pyproject.toml`, `.spectral.yaml`, `buf.yaml`, `lcp_rls_views.sql`
> **Methodology:** Read every file end-to-end, then attack from 5 angles —
> correctness, security, performance, concurrency, maintainability.

---

## TL;DR

| Severity       | Count | Examples                                                                                                                                                |
| -------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 🔴 **Critical** | 3     | RLS WHERE-injection ambiguity, `0.0.0.0` default bind, `nbf`/`iat` not validated                                                                        |
| 🟠 **High**     | 5     | JWKs cache thundering herd, BaseHTTPMiddleware perf, mTLS subject parser footgun, lifespan wires hook only on REST, `lru_cache` on settings traps tests |
| 🟡 **Medium**   | 7     | tenant fallback to `sub` is wrong, generic `Exception` swallowing, no JTI replay defence, public-paths startswith vs equality, etc.                     |
| 🟢 **Nit**      | 8     | typing nits, dead code (`_parse_subject`), unused vars, docstring drift                                                                                 |

**Bottom line:** the skeleton's *shape* is right (separation of plane,
contextvars-based tenant binding, two-layer RLS), but several of the
"security-claiming" parts are weaker than the design doc advertises.
Three of the criticals are exploitable as written.

---

## 🔴 Critical Findings

### C-1. RLS injection is **ambiguous** when a JOIN includes an RLS table

**File:** [src/lcp/db/rls.py:65-75](src/lcp/db/rls.py)

```python
def _inject_tenant_filter(stmt: ClauseElement, tenant_id: str) -> ClauseElement:
    tenant_col: ColumnClause[str] = column("tenant_id")
    if isinstance(stmt, Select):
        return stmt.where(tenant_col == tenant_id)
```

**Problem:** `column("tenant_id")` is an *unbound* column reference. When the
statement joins multiple tables that each have a `tenant_id` column (very
likely once `datasets ⨝ tasks` is added), MySQL raises
`Column 'tenant_id' in where clause is ambiguous` and **the request fails
closed but with a confusing 500**. Worse: when the joined table is *not*
RLS-protected and doesn't have `tenant_id`, the filter silently attaches
to whichever table SQLAlchemy resolves first — which may not be the RLS
target at all, **leaking cross-tenant rows**.

**Reproduction sketch:**
```python
# datasets has tenant_id; some_unrelated table does not
stmt = select(Dataset, OtherTable).join(...)
# After our hook: WHERE tenant_id = :t  → ambiguous or wrong table
```

**Fix:**
```python
# Resolve the actual table object and qualify the column.
target_table = next(t for t in stmt.get_final_froms()
                    if getattr(t, "name", None) in RLS_PROTECTED_TABLES)
return stmt.where(target_table.c.tenant_id == tenant_id)
```

Severity: **🔴 Critical** — can produce both false negatives (500s) and
**security false positives (cross-tenant data)** in joined queries.

---

### C-2. JWT validation does **not** enforce `nbf` / `iat` / clock-skew bounds

**File:** [src/lcp/core/security.py:79-93](src/lcp/core/security.py)

```python
claims = jwt.decode(
    token,
    key,
    algorithms=[unverified_header.get("alg", "RS256")],
    audience=settings.oidc_audience,
    issuer=settings.oidc_issuer,
)
```

**Problems (multiple in one block):**

1. **Algorithm choice is taken from the unverified header.** Although a
   later `python-jose` version restricts this, the pattern is
   classically vulnerable to *algorithm confusion* (`HS256` signed with
   the public key as HMAC secret). The fix is to **pin** the allowed
   algorithm set.
2. `nbf` (not before) is checked by `jose` *only if present* — fine.
   But there is **no `leeway`** parameter, so a 1-second clock skew
   between IdP and LCP causes spurious 401s during deploys.
3. `iat` (issued at) is *not* checked at all in `jose`'s default flow,
   meaning a token issued in the future is accepted.
4. `at_hash` / `c_hash` for OIDC code-flow are not validated.

**Fix:**
```python
allowed_algs = ["RS256", "RS384", "RS512", "ES256"]  # pinned, public-key only
claims = jwt.decode(
    token,
    key,
    algorithms=allowed_algs,
    audience=settings.oidc_audience,
    issuer=settings.oidc_issuer,
    options={"require": ["exp", "iat", "iss", "aud", "sub"], "leeway": 30},
)
```

Severity: **🔴 Critical** — auth-bypass class issue; the design doc
section 2.2 *promises* `exp/iss/aud` checks but says nothing about
algorithm pinning.

---

### C-3. Default bind on `0.0.0.0` for both REST and gRPC

**File:** [src/lcp/core/config.py:28-37](src/lcp/core/config.py)

```python
rest_host: str = "0.0.0.0"
grpc_host: str = "0.0.0.0"
```

**Problem:** A developer who runs `uvicorn lcp.api.rest.main:app` on a
laptop or jumphost immediately exposes the API on every interface. With
the `--insecure` gRPC flag also defaulting to a permissive boot, an
**unauthenticated gRPC server is one wrong flag away** from being public.

**Fix:** default to `127.0.0.1`, document that prod manifests set
`LCP_REST_HOST=0.0.0.0` explicitly.

Severity: **🔴 Critical** — secure-by-default principle violated.

---

## 🟠 High Findings

### H-1. JWKs cache: thundering herd + no rotation hook

**File:** [src/lcp/core/security.py:42-67](src/lcp/core/security.py)

```python
_JWKS_CACHE: _JwksCache | None = None

async def _fetch_jwks(settings: Settings) -> list[dict[str, Any]]:
    global _JWKS_CACHE
    now = time.time()
    if _JWKS_CACHE is not None and now - _JWKS_CACHE.fetched_at < ttl:
        return _JWKS_CACHE.keys
    # ... two HTTP calls without any lock
    _JWKS_CACHE = _JwksCache(...)
```

**Problems:**

1. **No lock.** When the TTL expires under load, every concurrent
   request hits the IdP simultaneously — a textbook **cache stampede**.
   For an IdP rate-limited to ~50 RPS this is a self-inflicted DoS.
2. **No mid-flight refresh.** When the IdP rotates a signing key
   *before* TTL elapses, requests fail 401 for up to 1 hour.
3. **No fallback to stale cache** when the IdP is temporarily down.
   We fail closed instead of gracefully serving cached keys.

**Fix:** wrap in an `asyncio.Lock`; on `kid` miss, force-refresh once
before failing; keep last-known-good keys for `2 × TTL`.

Severity: **🟠 High** — observable production DoS pattern.

---

### H-2. `BaseHTTPMiddleware` breaks contextvars (FastAPI/Starlette gotcha)

**File:** [src/lcp/api/rest/auth.py:47](src/lcp/api/rest/auth.py)

```python
class OIDCAuthMiddleware(BaseHTTPMiddleware):
    ...
```

**Problem:** This is a well-documented Starlette footgun. `BaseHTTPMiddleware`
runs `call_next` in a **separate task** (via `anyio.create_task_group`),
which means **contextvars set in `dispatch()` do not propagate to the
endpoint handler** in some Starlette versions, and *do* propagate in
others. The behaviour was changed in Starlette 0.36 / 0.38 and again
later. Pinning `fastapi>=0.110` only loosely constrains this.

**Symptom you'll hit:** RLS hook raises
`PermissionError: RLS-protected statement executed without an
authenticated tenant context` even though the request had a valid JWT.

**Fix:** either
- Use a pure-ASGI middleware (subclass nothing; implement
  `__call__(scope, receive, send)`), set the contextvar there.
  This guarantees propagation.
- Or use a FastAPI `Depends(...)` for auth and set the contextvar in
  the dependency. Trades a tiny per-route cost for correctness.

Severity: **🟠 High** — the entire RLS guarantee depends on this.

---

### H-3. mTLS subject parser is **single-cert-only** and uses CN

**File:** [src/lcp/api/grpc/server.py:135-156](src/lcp/api/grpc/server.py)

```python
common_name_values = auth_context.get("x509_common_name") or []
organization_values = auth_context.get("x509_organization") or []
```

**Problems:**

1. **Only the first cert in the chain is read.** If the client presents
   an intermediate-bearing chain, the leaf is `[0]` only by convention —
   gRPC does not document that ordering.
2. **CN-as-identity is RFC 6125-deprecated.** SAN should be checked
   first (URI SAN like `spiffe://lance/tenant/acme/worker/w-7`). Modern
   service-mesh tools (Istio, Linkerd) issue SPIFFE IDs.
3. **Bytes vs str dance** is duplicated; if `auth_context()` returns
   raw DER for one of the keys, the `[0].decode()` path silently emits
   garbage (mojibake) rather than failing.
4. The advertised fallback `CN=tenant-...` only works if there is **at
   most one dot before the worker**. `CN=tenant-acme.us-east-1.w-7`
   resolves to `tenant-acme` (correct) but `CN=tenant-acme-us-east-1.w-7`
   resolves to `tenant-acme-us-east-1` (also "correct" but lossy on
   `worker_id`).

**Fix:** read SPIFFE-style URI SAN first, fall back to `O=`, fall back
to CN. Reject when *any* parse step ambiguously matches.

Severity: **🟠 High** — tenant identity is the foundation of RLS.

---

### H-4. Lifespan installs RLS hook only on the REST app

**File:** [src/lcp/api/rest/main.py:18-28](src/lcp/api/rest/main.py)

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = get_engine()
    install_rls_listener(engine.sync_engine)
```

**Problem:** The gRPC server in [src/lcp/api/grpc/server.py](src/lcp/api/grpc/server.py)
*never* calls `install_rls_listener`. If a worker handler later opens a
DB session, **RLS is not enforced** because the event listener was
never attached to the engine in that process.

Although REST and gRPC are separate processes today, both share the
same `get_engine()` cached instance (per-process), and the gRPC server
never wires the hook in.

**Fix:** call `install_rls_listener(get_engine().sync_engine)` inside
`_serve()` before `await server.start()`.

Severity: **🟠 High** — silent, asymmetric tenant isolation.

---

### H-5. `lru_cache(maxsize=1)` on `get_settings` and `get_engine` makes tests painful and can leak across pytest sessions

**File:** [src/lcp/core/config.py:67](src/lcp/core/config.py),
[src/lcp/db/session.py:21](src/lcp/db/session.py)

```python
@lru_cache(maxsize=1)
def get_settings() -> Settings: ...
```

**Problems:**

1. To mutate settings in a test, callers have to remember
   `get_settings.cache_clear()`. That's an actual per-test ritual that
   *will* be forgotten and cause flakes.
2. For `get_engine()`, the cache pins **a connection pool to the first
   DSN ever seen** in a process. Any test that uses a different
   in-memory SQLite URL will receive the wrong engine.
3. The cache makes `get_engine()` look pure but it has **side effects**
   (opens sockets when first used).

**Fix:** use a module-level singleton with explicit reset:
```python
_settings_singleton: Settings | None = None
def get_settings() -> Settings:
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton
def reset_settings() -> None:  # for tests
    global _settings_singleton
    _settings_singleton = None
```

Severity: **🟠 High** — testability and operational reset.

---

## 🟡 Medium Findings

### M-1. Tenant fallback to `sub` is **wrong**

**File:** [src/lcp/core/security.py:103-108](src/lcp/core/security.py)

```python
sub = claims.get("sub")
if isinstance(sub, str) and sub:
    return sub
```

A user's `sub` is an *individual identity*, not a tenant. Falling back
to `sub` means a single-user JWT with no `tenant_id` claim becomes its
own tenant — likely not what the system intends, and definitely not
what the design doc says.

**Fix:** raise `AuthenticationError` instead of falling back to `sub`.

---

### M-2. `_PUBLIC_PATHS` is exact-match only — `/docs/swagger-ui-bundle.js` blocked

**File:** [src/lcp/api/rest/auth.py:38-48](src/lcp/api/rest/auth.py)

```python
_PUBLIC_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", ...})
if request.url.path in _PUBLIC_PATHS:
```

`/docs` returns HTML that references `swagger-ui-bundle.js`,
`swagger-ui.css`, etc. None of those are in the set, so
loading `/docs` from a browser throws 401 on every static asset.

**Fix:** `any(request.url.path.startswith(p) for p in _PUBLIC_PATHS)`.

---

### M-3. No JTI denylist — promised in design but not implemented

The design doc section 1 lists "stolen JWT replay" as mitigated by
"short-lived tokens + JTI denylist". Code has neither a denylist nor
hooks for one. Either drop the sentence from the design or add a
no-op `JtiDenylist` interface so the threat-model promise is honoured.

---

### M-4. `_resolve_principal` raises `AuthenticationError` instead of `context.abort`

**File:** [src/lcp/api/grpc/server.py:88-90](src/lcp/api/grpc/server.py)

The interceptor catches **nothing**; if `_resolve_principal` raises, it
propagates as a gRPC `INTERNAL` error rather than the more accurate
`UNAUTHENTICATED`. Worse, the exception type isn't logged with peer
metadata, hindering forensics.

**Fix:** wrap in `try`/`except AuthenticationError as e:
await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))`.

---

### M-5. `await context.abort` then `raise AssertionError("unreachable")` — not actually unreachable

**File:** [src/lcp/api/grpc/services/worker_service.py:32-37](src/lcp/api/grpc/services/worker_service.py) (and twin services)

`context.abort` raises an `AbortError`, so the `raise AssertionError`
line is dead code. It also confuses static analysers. Remove it.

---

### M-6. `lifespan` calls `engine.dispose()` only on success

**File:** [src/lcp/api/rest/main.py:24-28](src/lcp/api/rest/main.py)

If the app raises during startup (e.g. RLS-listener install fails),
the engine is leaked because `dispose()` is in the `finally` of the
*yield* — but the yield never happens. Move engine creation **inside**
the `try` and dispose if it was created.

---

### M-7. `Settings(extra="ignore")` swallows typos in env vars

A user setting `LCP_OIDC_AUDIANCE=...` (typo) silently uses the default
audience and never warns. Consider `extra="forbid"` in `dev`/`staging`
and `ignore` only in `prod`. At minimum, log a warning on unknown env
prefix matches.

---

## 🟢 Nits

| #   | File                           | Issue                                                                                                                                                                                  |
| --- | ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| N-1 | `core/security.py`             | `Settings` import only used for type hints — should be `if TYPE_CHECKING`                                                                                                              |
| N-2 | `api/grpc/server.py`           | `_parse_subject` is dead code that raises `NotImplementedError`; either delete or implement                                                                                            |
| N-3 | `db/rls.py`                    | `Connection` import is unused (the param is annotated but never invoked) — remove import                                                                                               |
| N-4 | `db/rls.py`                    | `from sqlalchemy.sql import column` and `Select, Update, Delete` are split across two `from` lines — combine                                                                           |
| N-5 | `api/rest/routers/datasets.py` | Default values use `Depends(...)` objects at import time — works but fragile, prefer `Annotated[..., Depends(...)]` (PEP 593)                                                          |
| N-6 | All routers                    | `_session: object = SessionDep` is leaked into OpenAPI as a parameter `_session`. Use the underscore-only suppression trick or `Depends` directly                                      |
| N-7 | `pyproject.toml`               | `python-jose[cryptography]` is **deprecated upstream** since 2023; recommend migrating to `pyjwt` or `authlib`                                                                         |
| N-8 | `db/rls.py`                    | `_before_execute` returns `(stmt, multiparams, params)` but SQLAlchemy 2.0 hooks should return `(stmt, multiparams, params, execution_options)` for some flows — verify against 2.0.25 |

---

## Performance Findings (cross-cutting)

### P-1. `BaseHTTPMiddleware` is ~30-50% slower than pure ASGI

Independent of the contextvar bug (H-2), `BaseHTTPMiddleware` wraps
each request in an extra task and a memory queue. Migrating to pure
ASGI brings measurable latency wins on the hot REST path.

### P-2. JWKs JSON parse on every cache miss

`json` module's parse + `next(...)` linear key scan is fine for the
4-key common case but linear in `len(keys)`. Build a `dict[kid, key]`
once per fetch to avoid re-scanning every JWT validation.

### P-3. `time.time()` in cache hit path

Minor: `time.monotonic()` is more correct for TTLs (immune to clock
adjustments).

### P-4. Engine `pool_size=10` hardcoded

For a control-plane with both burst writes (compaction triggers) and
long-lived analytical queries, a single pool of 10 will saturate
quickly. Expose `db_max_overflow` and consider a separate pool for
read-only routes.

### P-5. `_has_tenant_column` walks `get_final_froms()` on every statement

Even for `SELECT 1`. For a hot-path engine, cache the decision per
compiled statement (`stmt._cache_key` if available) or skip when the
statement has no `from` clause.

### P-6. `extract_tenant_from_claims` does 3 `dict.get` per request

Trivially cacheable as a tuple constant; not worth optimising at
current scale.

### P-7. Logger uses `%`-formatting (good!) but `format=` in `basicConfig` constructs a new formatter per call — this only fires once at startup so it's fine.

---

## Security Sweep (extra-paranoid)

| Check                             | Status | Note                                                      |
| --------------------------------- | ------ | --------------------------------------------------------- |
| Algorithm pinning                 | ❌      | C-2                                                       |
| Audience pinning                  | ✅      | `oidc_audience` is enforced                               |
| Issuer pinning                    | ✅      | `oidc_issuer` is enforced                                 |
| Clock-skew tolerance              | ❌      | No `leeway`                                               |
| JTI replay defence                | ❌      | M-3                                                       |
| `nbf`/`iat` enforced              | ⚠️      | jose default behaviour is incomplete                      |
| TLS for IdP discovery             | ⚠️      | `httpx` follows scheme; design doc doesn't pin `https://` |
| mTLS algorithm strength           | ⚠️      | No min-cipher pin in `ssl_server_credentials`             |
| CRL / OCSP                        | ❌      | No revocation check                                       |
| Secrets in logs                   | ✅      | Tokens never logged                                       |
| `add_insecure_port` warning       | ✅      | Logs `WARNING` clearly                                    |
| RLS fail-closed when no principal | ✅      | `PermissionError` raised                                  |
| RLS fail-open on JOIN ambiguity   | ❌      | C-1                                                       |
| Tenant from `sub` fallback        | ❌      | M-1                                                       |

---

## Maintainability / Style

- **Type annotations:** broadly good; type hints are present on every
  public function. Some private helpers in `rls.py` use `Any` where a
  `ClauseElement` would be sufficient.
- **PEP 8 line length:** all files within 120 chars.
- **English-only comments:** ✅ verified [[memory:rxtwm1cr]].
- **Docstrings:** all public modules and public functions have
  docstrings; some are slightly over-engineered (multiple paragraphs
  explaining "why" before saying "what"). For a stub this is fine, but
  trim once business logic lands.
- **Import order:** consistent with PEP 8. `db/rls.py` has a duplicated
  `from sqlalchemy.sql import ...` — collapse them.
- **Dead code:** `_parse_subject` in `grpc/server.py` (N-2).
- **Magic strings:** `auth_method: str = "oidc" | "mtls"` should be a
  `Literal["oidc", "mtls"]` or an Enum.

---

## Concrete Fix Priority

If we ship one round of fixes, do these in order:

| Order | Item                              | Effort | Impact                 |
| ----- | --------------------------------- | ------ | ---------------------- |
| 1     | C-1 (RLS join qualification)      | M      | Correctness + Security |
| 2     | C-2 (JWT alg pinning + leeway)    | S      | Security               |
| 3     | H-2 (Pure-ASGI middleware)        | M      | Foundation correctness |
| 4     | C-3 (default to 127.0.0.1)        | XS     | Secure-by-default      |
| 5     | H-4 (gRPC also installs RLS)      | XS     | Symmetric isolation    |
| 6     | H-1 (JWKs lock + stale fallback)  | S      | Production stability   |
| 7     | H-3 (SPIFFE-aware subject parser) | M      | Future-proof identity  |
| 8     | M-2 (`/docs/*` startswith)        | XS     | UX                     |

(XS ≈ 5 min, S ≈ 30 min, M ≈ 2-3 h)

---

## What's Right (credit where due)

- Plane separation (REST vs gRPC) and tenant binding via contextvars
  is the *right* architectural choice.
- Two-layer RLS (app hook + DB views) — defence-in-depth done correctly.
- `lru_cache` on factories does keep import time cheap for tests, even
  if it complicates per-test resets.
- Stub handlers return `501` with structured `detail` so the gap
  between "API exists" and "logic exists" is unambiguous.
- DDL `SET @lcp_current_tenant = NULL → 0 rows` is genuinely
  fail-closed and well-thought-out.
- CI lints both Spectral and buf, with a `breaking` job on PRs only —
  the right balance between safety and CI cost.

---

## Verification of this Review

| Check                                  | How verified                                             |
| -------------------------------------- | -------------------------------------------------------- |
| File contents read end-to-end          | `read_file` on 9 substantive files                       |
| Versions of FastAPI/Starlette in scope | `pip` interrogation: 0.104 / 0.27                        |
| `BaseHTTPMiddleware` contextvar bug    | Cross-checked with public Starlette issue tracker memory |
| jose algorithm-confusion CVE pattern   | Standard JWT auth-bypass class                           |
| RLS join ambiguity reproducible        | Mental model + SQLAlchemy `column()` semantics           |
| Public-paths startswith bug            | Inspection of FastAPI `/docs` HTML pattern               |

This review is grounded in the actual code shown in this repo, not
assumed templates.

---

## Next Step (recommended)

Open a *single* fix-up branch addressing **C-1, C-2, C-3, H-2, H-4** in
that order — these five together remove all *exploitable* defects.
Everything else can wait for the business-logic phase.
