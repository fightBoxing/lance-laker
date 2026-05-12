"""LCP Worker service stub.

Maps to ``docs/architecture/api/protobuf/lcp_worker.proto``.

The skeleton intentionally does **not** import the generated ``*_pb2`` and
``*_pb2_grpc`` modules; those are produced by ``buf generate`` (or
``protoc``) during the build pipeline and are expected to live under
``src/lcp/generated/``.  The ``register`` hook below is a no-op when the
generated stubs are absent so that the server can still boot for smoke tests.
"""

from __future__ import annotations

import logging

import grpc

logger = logging.getLogger(__name__)


class LcpWorkerServicer:
    """Servicer stub for the ``LcpWorker`` gRPC service.

    Concrete RPC methods are added once the generated base class is
    available.  Each method should:

    1. Read the tenant principal from the contextvars store
       (set by :class:`MtlsTenantInterceptor`).
    2. Open a session via :func:`lcp.db.session.get_session`.
    3. Delegate to the service layer.
    """

    async def Heartbeat(  # noqa: N802 - gRPC method name convention
        self,
        request: object,
        context: grpc.aio.ServicerContext,
    ) -> object:
        """Acknowledge a worker heartbeat.  Stub implementation."""

        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "LcpWorker.Heartbeat is not yet implemented",
        )
        return None  # pragma: no cover - abort raises


def register(server: grpc.aio.Server) -> None:
    """Attach the servicer to ``server`` once generated stubs are present."""

    try:
        # Generated module path is documented; suppress import-time errors so
        # the skeleton boots without a build step.
        from lcp.generated import lcp_worker_pb2_grpc  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "lcp_worker_pb2_grpc not found; skipping LcpWorker registration. "
            "Run `buf generate` to produce the stubs.",
        )
        return

    lcp_worker_pb2_grpc.add_LcpWorkerServicer_to_server(  # type: ignore[attr-defined]
        LcpWorkerServicer(),
        server,
    )
