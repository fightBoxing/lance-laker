"""Async SQLAlchemy session factory.

The skeleton creates the engine lazily so that import time is cheap and unit
tests can patch :func:`get_settings` before the first connection is opened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the lazily-initialised async engine.

    ``pool_size`` and ``pool_recycle`` are only applied when the underlying
    dialect supports a pooled connection (e.g. MySQL).  SQLite with aiosqlite
    uses ``StaticPool`` and rejects those kwargs, so this helper omits them
    for sqlite URLs to keep the skeleton test-friendly.
    """

    settings = get_settings()
    kwargs: dict[str, object] = {"future": True, "echo": False}
    if not settings.db_dsn.startswith("sqlite"):
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
    elif ":memory:" in settings.db_dsn:
        # ``:memory:`` databases are scoped to a single connection, so the
        # pool MUST keep returning the same one or every checkout will see an
        # empty schema.  StaticPool guarantees that.  ``check_same_thread`` is
        # disabled because aiosqlite drives the connection from greenlets.
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(settings.db_dsn, **kwargs)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a cached session factory bound to :func:`get_engine`."""

    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI/gRPC dependency that yields a tenant-scoped session.

    The RLS event listener (registered in :mod:`lcp.db.rls`) reads the tenant
    principal from the contextvars set by the auth middleware, so handlers do
    not need to thread a tenant id through every call.
    """

    factory = get_session_factory()
    async with factory() as session:
        yield session
