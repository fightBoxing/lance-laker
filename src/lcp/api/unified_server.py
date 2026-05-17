"""Unified LCP server: REST API + gRPC in a single process.

Runs both the north-bound REST surface (Uvicorn/FastAPI on ``rest_port``)
and the east-west gRPC surface (grpc.aio on ``grpc_port``) inside one
asyncio event loop so the two share a single DB engine, RLS listener,
and connection pool.

Entry-point: ``lcp-server`` console script (see ``pyproject.toml``).

The standalone ``lcp-rest`` and ``lcp-grpc`` scripts remain for backward
compatibility and for cases where an operator wants to scale the two
surfaces independently.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import ssl

import grpc
import uvicorn

from lcp import __version__
from lcp.api.grpc.server import (
    MtlsTenantInterceptor,
    _build_server_credentials,
)
from lcp.api.grpc.services import (
    embedding_service,
    vdw_service,
    worker_service,
)
from lcp.api.rest.main import create_app
from lcp.core.config import get_settings
from lcp.db.rls import install_rls_listener
from lcp.db.session import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# gRPC server helpers (extracted from server.py to avoid duplicating _serve)
# ---------------------------------------------------------------------------


def _create_grpc_server() -> grpc.aio.Server:
    """Build and configure the gRPC server (without starting it)."""

    server = grpc.aio.server(interceptors=(MtlsTenantInterceptor(),))
    worker_service.register(server)
    vdw_service.register(server)
    embedding_service.register(server)
    return server


def _bind_grpc(
    server: grpc.aio.Server,
    host: str,
    port: int,
    *,
    insecure: bool,
) -> None:
    """Add the listening port (insecure or mTLS) to *server*."""

    bind_addr = f"{host}:{port}"
    if insecure:
        server.add_insecure_port(bind_addr)
        logger.warning("gRPC: INSECURE mode on %s", bind_addr)
    else:
        server.add_secure_port(bind_addr, _build_server_credentials())
        logger.info("gRPC: mTLS on %s", bind_addr)


# ---------------------------------------------------------------------------
# Unified serve
# ---------------------------------------------------------------------------


async def _serve(*, insecure: bool) -> None:
    """Run REST + gRPC concurrently until interrupted."""

    settings = get_settings()

    # Shared engine + RLS — installed once for both surfaces.
    engine = get_engine()
    install_rls_listener(engine.sync_engine)

    # --- REST (Uvicorn low-level API) ---
    rest_app = create_app(manage_engine=False)
    uvi_config = uvicorn.Config(
        app=rest_app,
        host=settings.rest_host,
        port=settings.rest_port,
        log_level="info",
    )
    uvi_server = uvicorn.Server(uvi_config)

    # --- gRPC ---
    grpc_server = _create_grpc_server()
    _bind_grpc(
        grpc_server,
        settings.grpc_host,
        settings.grpc_port,
        insecure=insecure,
    )
    await grpc_server.start()

    logger.info(
        "LCP unified server v%s ready  "
        "(REST :%d  gRPC :%d  insecure=%s)",
        __version__,
        settings.rest_port,
        settings.grpc_port,
        insecure,
    )

    # Setup signal handlers for graceful shutdown.
    shutdown = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown.set()
        uvi_server.should_exit = True

    loop = asyncio.get_running_loop()
    import signal as _signal

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows fallback

    try:
        # Call uvicorn._serve() directly to bypass capture_signals();
        # we're managing signals ourselves via loop.add_signal_handler().
        await asyncio.gather(
            uvi_server._serve(),
            grpc_server.wait_for_termination(),
        )
    finally:
        # Graceful cleanup: stop gRPC first (short grace), then engine.
        await grpc_server.stop(grace=5)
        await engine.dispose()
        logger.info("LCP unified server stopped")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> None:
    """Entry-point for the ``lcp-server`` console script."""

    parser = argparse.ArgumentParser(description="LCP unified server (REST + gRPC)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable mTLS on the gRPC port (development only)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        asyncio.run(_serve(insecure=args.insecure))
    except KeyboardInterrupt:
        logger.info("Unified server stopped by signal")
    except (FileNotFoundError, ssl.SSLError) as exc:
        logger.error("TLS material is missing or invalid: %s", exc)
        raise


if __name__ == "__main__":
    run()
