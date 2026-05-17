"""FastAPI application entry-point for LCP REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware import Middleware

from lcp import __version__
from lcp.api.rest.auth import OIDCAuthMiddleware
from lcp.api.rest.routers import (
    datasets,
    indexes,
    lifecycle,
    meta,
    search,
    tasks,
    vectorization,
)
from lcp.core.config import get_settings
from lcp.db.rls import install_rls_listener
from lcp.db.session import get_engine, get_session_factory

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _managed_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Install RLS hook on startup; dispose engine on shutdown.

    Used when the REST API runs **standalone** (``lcp-rest`` entry point).
    When the unified server (``lcp-server``) hosts the app, it passes
    ``manage_engine=False`` to :func:`create_app` and handles engine
    lifecycle externally so the hook and pool are shared with the gRPC
    server without double-install or double-dispose.
    """

    engine = get_engine()
    try:
        install_rls_listener(engine.sync_engine)
        yield
    finally:
        await engine.dispose()


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """No-op lifespan for when the caller manages engine lifecycle."""

    yield


def create_app(*, manage_engine: bool = True) -> FastAPI:
    """Application factory used by ``uvicorn``, unified server, and tests.

    :param manage_engine: When ``True`` (default, standalone mode), the
        app's lifespan installs the RLS listener and disposes the engine
        on shutdown.  Set to ``False`` when the caller (e.g.
        ``unified_server``) already owns engine lifecycle.
    """

    settings = get_settings()
    app = FastAPI(
        title="LanceDB Control Plane API",
        version=__version__,
        summary="Control-plane REST surface for the Lance Lakehouse platform",
        lifespan=_managed_lifespan if manage_engine else _noop_lifespan,
        # P-1: wire the pure-ASGI middleware via the ``middleware_stack``
        # constructor rather than ``add_middleware()``.  FastAPI/Starlette
        # builds the ASGI chain once during ``__init__`` when
        # ``middleware=`` is used, which means:
        #   • The middleware instance wraps the app at construction time —
        #     no lazy wrapping that could race with the first request.
        #   • ``contextvars`` set inside ``OIDCAuthMiddleware.__call__``
        #     propagate correctly into the downstream route handler because
        #     both run in the *same* asyncio task (pure-ASGI guarantee).
        #   • ``app.add_middleware()`` rebuilds the middleware stack on
        #     every call and has historically triggered subtle ordering bugs
        #     when called after routes are already registered.
        middleware=[
            Middleware(OIDCAuthMiddleware),
        ],
    )

    app.include_router(datasets.router)
    app.include_router(tasks.router)
    app.include_router(indexes.router)
    app.include_router(search.router)
    app.include_router(lifecycle.router)
    app.include_router(vectorization.router)
    app.include_router(meta.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """Liveness probe — does not touch downstream dependencies."""

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ok",
                "version": __version__,
                "env": settings.app_env,
            },
        )

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness probe — verifies DB connectivity before accepting traffic.

        Executes a lightweight ``SELECT 1`` against the database.  If the
        check fails the probe returns 503 so the kubelet stops routing
        traffic to this Pod until the DB recovers.
        """

        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — probe must never crash the app
            _logger.warning("readyz probe failed: DB unreachable", exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not ready", "reason": "database unreachable"},
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready"},
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus scrape endpoint.

        Returns the LCP metric registry in Prometheus text-format 0.0.4.
        Anonymous (whitelisted in :class:`OIDCAuthMiddleware`); access
        control is the Service / NetworkPolicy layer's job, not the
        application's.
        """

        return Response(
            content=render_latest(),
            media_type=CONTENT_TYPE_LATEST,
            status_code=status.HTTP_200_OK,
        )

    return app


app = create_app()


def run() -> None:
    """Entry-point for the ``lcp-rest`` console script."""

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "lcp.api.rest.main:app",
        host=settings.rest_host,
        port=settings.rest_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
