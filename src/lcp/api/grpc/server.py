"""gRPC server entry-point with mTLS and tenant-binding interceptor.

The server requires a client certificate; the certificate's *Common Name*
identifies the worker, and an ``O=`` attribute (or a ``CN=tenant-...`` prefix)
identifies the tenant.  The :class:`MtlsTenantInterceptor` translates that
material into a :class:`TenantPrincipal` and binds it to ``contextvars`` for
the duration of the RPC.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import grpc

from lcp import __version__
from lcp.api.grpc.services import (
    embedding_service,
    vdw_service,
    worker_service,
)
from lcp.core.config import get_settings
from lcp.core.security import (
    AuthenticationError,
    extract_tenant_from_x509_subject,
)
from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)
from lcp.db.rls import install_rls_listener
from lcp.db.session import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interceptor
# ---------------------------------------------------------------------------


class MtlsTenantInterceptor(grpc.aio.ServerInterceptor):
    """Interceptor that derives a tenant principal from the peer certificate."""

    async def intercept_service(  # type: ignore[override]
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails],
            Awaitable[grpc.RpcMethodHandler],
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        original_handler = await continuation(handler_call_details)
        if original_handler is None:
            return original_handler  # type: ignore[return-value]

        return _wrap_handler(original_handler)


def _wrap_handler(handler: grpc.RpcMethodHandler) -> grpc.RpcMethodHandler:
    """Wrap an RPC handler so each invocation binds a tenant principal."""

    async def _bind_then_call(
        request_or_iterator: Any,
        context: grpc.aio.ServicerContext,
    ) -> Any:
        try:
            principal = _resolve_principal(context)
        except AuthenticationError as exc:
            # Surface as the proper gRPC status rather than INTERNAL.
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
            raise  # pragma: no cover - abort raises, this is just for typing

        token = set_current_tenant(principal)
        try:
            if handler.unary_unary:
                return await handler.unary_unary(request_or_iterator, context)
            if handler.unary_stream:
                return handler.unary_stream(request_or_iterator, context)
            if handler.stream_unary:
                return await handler.stream_unary(request_or_iterator, context)
            if handler.stream_stream:
                return handler.stream_stream(request_or_iterator, context)
            await context.abort(
                grpc.StatusCode.INTERNAL,
                "Unsupported RPC handler kind",
            )
        finally:
            reset_current_tenant(token)

    if handler.unary_unary:
        return grpc.unary_unary_rpc_method_handler(
            _bind_then_call,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.unary_stream:
        return grpc.unary_stream_rpc_method_handler(
            _bind_then_call,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.stream_unary:
        return grpc.stream_unary_rpc_method_handler(
            _bind_then_call,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    return grpc.stream_stream_rpc_method_handler(
        _bind_then_call,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )


def _resolve_principal(context: grpc.aio.ServicerContext) -> TenantPrincipal:
    """Build a :class:`TenantPrincipal` from the gRPC peer auth context."""

    auth_context = context.auth_context()
    common_name_values = auth_context.get("x509_common_name") or []
    organization_values = auth_context.get("x509_organization") or []

    if not common_name_values:
        raise AuthenticationError(
            "Peer certificate is missing CN; mTLS authentication failed.",
        )

    cn = _decode_first(common_name_values)
    organization = _decode_first(organization_values) if organization_values else None

    tenant_id = extract_tenant_from_x509_subject(cn, organization)
    return TenantPrincipal(
        tenant_id=tenant_id,
        subject=cn,
        auth_method="mtls",
    )


def _decode_first(values: list[Any]) -> str:
    """Decode the first element of a gRPC auth-context value list."""

    head = values[0]
    if isinstance(head, bytes):
        return head.decode("utf-8")
    return str(head)


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------


def _build_server_credentials() -> grpc.ServerCredentials:
    """Load TLS material from disk and build mTLS server credentials."""

    settings = get_settings()
    with open(settings.mtls_server_cert_path, "rb") as fh:
        server_cert = fh.read()
    with open(settings.mtls_server_key_path, "rb") as fh:
        server_key = fh.read()
    with open(settings.mtls_ca_cert_path, "rb") as fh:
        ca_cert = fh.read()

    return grpc.ssl_server_credentials(
        [(server_key, server_cert)],
        root_certificates=ca_cert,
        require_client_auth=settings.mtls_require_client_cert,
    )


async def _serve(host: str, port: int, *, insecure: bool) -> None:
    # Install the RLS event listener on the engine that gRPC handlers will
    # share.  Without this call the gRPC process would silently bypass
    # tenant filtering — see review finding H-4.
    engine = get_engine()
    install_rls_listener(engine.sync_engine)

    server = grpc.aio.server(interceptors=(MtlsTenantInterceptor(),))
    worker_service.register(server)
    vdw_service.register(server)
    embedding_service.register(server)

    bind_addr = f"{host}:{port}"
    if insecure:
        server.add_insecure_port(bind_addr)
        logger.warning("Starting gRPC server in INSECURE mode on %s", bind_addr)
    else:
        server.add_secure_port(bind_addr, _build_server_credentials())
        logger.info("Starting gRPC server with mTLS on %s", bind_addr)

    await server.start()
    logger.info("LCP gRPC server v%s ready", __version__)
    try:
        await server.wait_for_termination()
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    """Entry-point for the ``lcp-grpc`` console script."""

    parser = argparse.ArgumentParser(description="LCP gRPC server")
    parser.add_argument("--host", default=None, help="Bind host")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable mTLS (development only)",
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

    settings = get_settings()
    host = args.host or settings.grpc_host
    port = args.port or settings.grpc_port

    try:
        asyncio.run(_serve(host, port, insecure=args.insecure))
    except KeyboardInterrupt:
        logger.info("gRPC server stopped by signal")
    except (FileNotFoundError, ssl.SSLError) as exc:
        logger.error("TLS material is missing or invalid: %s", exc)
        raise


if __name__ == "__main__":
    run()
