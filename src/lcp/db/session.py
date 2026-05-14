"""Async SQLAlchemy session factory.

The skeleton creates the engine lazily so that import time is cheap and unit
tests can patch :func:`get_settings` before the first connection is opened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the shared async engine, creating it on first call.

    ``pool_size`` and ``pool_recycle`` are only applied when the underlying
    dialect supports a pooled connection (e.g. MySQL).  SQLite with aiosqlite
    uses ``StaticPool`` and rejects those kwargs, so this helper omits them
    for sqlite URLs to keep the skeleton test-friendly.

    Use :func:`reset_engine` in tests to force a fresh engine on the next call.
    """

    global _engine  # noqa: PLW0603
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"future": True, "echo": False}
        if not settings.db_dsn.startswith("sqlite"):
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
            kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
        elif ":memory:" in settings.db_dsn:
            # ``:memory:`` databases are scoped to a single connection, so the
            # pool MUST keep returning the same one or every checkout will see an
            # empty schema.  StaticPool guarantees that.  ``check_same_thread`` is
            # disabled because aiosqlite drives the connection from greenlets.
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(settings.db_dsn, **kwargs)
    return _engine


def reset_engine() -> None:
    """Discard the cached engine so the next :func:`get_engine` call creates a
    fresh one bound to the current settings.  For test use only.
    """

    global _engine  # noqa: PLW0603
    _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a shared session factory bound to :func:`get_engine`.

    Use :func:`reset_session_factory` in tests to force a fresh factory on
    the next call (typically after resetting the engine).
    """

    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


def reset_session_factory() -> None:
    """Discard the cached factory.  For test use only."""

    global _session_factory  # noqa: PLW0603
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI/gRPC dependency that yields a tenant-scoped session.

    The RLS event listener (registered in :mod:`lcp.db.rls`) reads the tenant
    principal from the contextvars set by the auth middleware, so handlers do
    not need to thread a tenant id through every call.
    """

    factory = get_session_factory()
    async with factory() as session:
        yield session
