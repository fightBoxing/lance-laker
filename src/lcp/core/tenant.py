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
    """Identity and tenant info extracted from the inbound credential.

    The optional ``is_system`` flag is set by
    :func:`with_system_context` so the RLS hook in :mod:`lcp.db.rls` can
    detect a scheduler / worker thread and skip the per-tenant filter.
    Regular request handlers always see ``is_system=False``.
    """

    tenant_id: str
    subject: str
    auth_method: str  # "oidc" | "mtls" | "system"
    is_system: bool = False


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


# ---------------------------------------------------------------------------
# System principal helpers
# ---------------------------------------------------------------------------


SYSTEM_TENANT_ID = "__system__"


def _system_principal(subject: str = "scheduler") -> TenantPrincipal:
    """Build the canonical system principal used by background processes."""

    return TenantPrincipal(
        tenant_id=SYSTEM_TENANT_ID,
        subject=subject,
        auth_method="system",
        is_system=True,
    )


class _SystemContext:
    """Context manager that binds a system principal for the current task.

    Use this around scheduler / reaper code paths that legitimately need to
    read or write rows belonging to many tenants in one transaction.  The
    RLS hook detects ``is_system`` and skips its tenant filter, but the
    code is still expected to preserve each row's ``tenant_id`` on writes.
    """

    def __init__(self, subject: str = "scheduler") -> None:
        self._subject = subject
        self._token: Token[TenantPrincipal | None] | None = None

    def __enter__(self) -> TenantPrincipal:
        principal = _system_principal(self._subject)
        self._token = _current_tenant.set(principal)
        return principal

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _current_tenant.reset(self._token)


def with_system_context(subject: str = "scheduler") -> _SystemContext:
    """Convenience factory: ``with with_system_context(): ...``."""

    return _SystemContext(subject)


# ---------------------------------------------------------------------------
# System principal guard
# ---------------------------------------------------------------------------


class NotSystemPrincipalError(PermissionError):
    """Raised when system-only code runs without ``is_system=True``."""


def require_system_context(caller: str = "system operation") -> None:
    """Refuse to proceed unless the current principal is a system principal.

    Scheduler and planner primitives use this to enforce the invariant
    that cross-tenant operations are only legal under
    :func:`with_system_context`.
    """

    principal = _current_tenant.get()
    if principal is None or not getattr(principal, "is_system", False):
        raise NotSystemPrincipalError(
            f"{caller} must run under with_system_context()",
        )
