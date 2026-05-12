"""Reusable FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.core.tenant import TenantPrincipal, require_current_tenant
from lcp.db.session import get_session


async def db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI wrapper around :func:`lcp.db.session.get_session`."""

    async for session in get_session():
        yield session


def current_principal() -> TenantPrincipal:
    """Return the currently authenticated tenant principal."""

    return require_current_tenant()


# Type aliases that handlers can import for cleaner signatures.
SessionDep = Depends(db_session)
PrincipalDep = Depends(current_principal)
