"""OAuth2 / OIDC authentication for the REST surface.

Implemented as a **pure ASGI middleware** rather than a
:class:`starlette.middleware.base.BaseHTTPMiddleware` subclass.  The latter
runs ``call_next`` in a separate task and has historically broken
``contextvars`` propagation depending on the Starlette version, which would
silently disable the downstream RLS guard.  A pure ASGI middleware sets the
contextvar in the same task that runs the route handler, which guarantees
propagation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from lcp.core.security import (
    AuthenticationError,
    extract_tenant_from_claims,
    validate_oidc_jwt,
)
from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)

# OAuth2PasswordBearer is used purely to surface the lock icon in /docs;
# real flows are Authorization Code or Client Credentials handled by the IdP.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/oauth2/token",
    auto_error=False,
)


# Path prefixes that bypass authentication.  ``startswith`` matching is used
# so that the Swagger UI assets under ``/docs/swagger-ui-bundle.js`` etc. are
# also reachable without a token.
#
# ``/metrics`` is on this list because Prometheus scrape targets
# present no Bearer token; the metric output itself contains no
# tenant data (only counter / histogram aggregates), so anonymous
# scrape is the standard pattern.  Restricting access is a Service /
# NetworkPolicy concern, not an application concern.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/livez",
    "/readyz",
    "/metrics",
    "/openapi.json",
    "/docs",
    "/redoc",
)


ASGIScope = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


def _is_public(path: str) -> bool:
    """Return True if ``path`` should bypass authentication."""

    return any(path == p or path.startswith(p + "/") or path == p for p in _PUBLIC_PREFIXES)


async def _send_401(send: ASGISend, detail: str) -> None:
    """Emit a minimal 401 response over ASGI."""

    body = (
        b'{"detail":"' + detail.replace('"', "'").encode("utf-8") + b'"}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_401_UNAUTHORIZED,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        },
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class OIDCAuthMiddleware:
    """Pure-ASGI middleware that validates OIDC Bearer tokens."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if _is_public(path):
            await self.app(scope, receive, send)
            return

        token = _extract_bearer_token(scope)
        if token is None:
            await _send_401(send, "Missing Bearer token")
            return

        try:
            claims = await validate_oidc_jwt(token)
            tenant_id = extract_tenant_from_claims(claims)
        except AuthenticationError as exc:
            await _send_401(send, str(exc))
            return
        except HTTPException as exc:  # defensive: reused helpers may raise
            await _send_401(send, str(exc.detail))
            return

        principal = TenantPrincipal(
            tenant_id=tenant_id,
            subject=str(claims.get("sub", "")),
            auth_method="oidc",
        )
        token_handle = set_current_tenant(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(token_handle)


def _extract_bearer_token(scope: ASGIScope) -> str | None:
    """Pull the Bearer token out of the ASGI ``headers`` list."""

    raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in raw_headers:
        if name == b"authorization":
            decoded = value.decode("latin-1")
            if decoded.lower().startswith("bearer "):
                return decoded.split(" ", 1)[1].strip()
            return None
    return None
