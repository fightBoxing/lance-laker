"""FastAPI application entry-point for LCP REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.responses import JSONResponse

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
from lcp.db.session import get_engine
from lcp.observability import (
    CONTENT_TYPE_LATEST,
    render_latest,
    set_build_component,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire up engine-level hooks on startup; dispose on shutdown.

    The engine reference is captured **before** the RLS hook is installed so
    that a startup failure (e.g. a bad DSN producing an unusable engine) is
    still followed by a clean ``dispose()`` and never leaks a connection
    pool.
    """

    del app  # unused
    # Stamp the build-info gauge with our component label.  Idempotent
    # across restarts; safe even when create_app() runs multiple times
    # in tests because set_build_component() removes the placeholder
    # series first.
    set_build_component("rest_api")
    engine = get_engine()
    try:
        install_rls_listener(engine.sync_engine)
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Application factory used by both ``uvicorn`` and tests."""

    settings = get_settings()
    app = FastAPI(
        title="LanceDB Control Plane API",
        version=__version__,
        summary="Control-plane REST surface for the Lance Lakehouse platform",
        lifespan=lifespan,
    )

    app.add_middleware(OIDCAuthMiddleware)

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
        """Readiness probe — placeholder until DB ping is wired in."""

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
