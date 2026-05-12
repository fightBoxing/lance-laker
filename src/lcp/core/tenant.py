"""Tenant context propagation across async tasks.

The current request's tenant identifier is stored in a :mod:`contextvars`
``ContextVar`` so it is naturally scoped to the asyncio task that handles the
request.  Both REST middleware and gRPC interceptors push the tenant id into
this variable; downstream code (SQLAlchemy event listener, service layer)
reads it through :func:`get_current_tenant`.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantPrincipal:
    """Identity and tenant info extracted from the inbound credential."""

    tenant_id: str
    subject: str
    auth_method: str  # "oidc" | "mtls"


_current_tenant: ContextVar[TenantPrincipal | None] = ContextVar(
    "lcp_current_tenant",
    default=None,
)


def set_current_tenant(principal: TenantPrincipal) -> Token[TenantPrincipal | None]:
    """Bind ``principal`` to the current asyncio task.

    The returned token can be passed back to :func:`reset_current_tenant`
    so callers can restore the previous value at the end of a scope.
    """

    return _current_tenant.set(principal)


def reset_current_tenant(token: Token[TenantPrincipal | None]) -> None:
    """Restore the tenant principal to the value captured by ``token``."""

    _current_tenant.reset(token)


def get_current_tenant() -> TenantPrincipal | None:
    """Return the currently bound tenant principal, or ``None``."""

    return _current_tenant.get()


def require_current_tenant() -> TenantPrincipal:
    """Return the currently bound tenant principal or raise.

    Used by code paths that must never run outside an authenticated request,
    e.g. the SQLAlchemy RLS event listener.
    """

    principal = _current_tenant.get()
    if principal is None:
        raise RuntimeError(
            "No tenant principal bound to the current task; "
            "this code path requires an authenticated request context.",
        )
    return principal
