"""Vector Data Writer (VDW) service stub.

Maps to ``docs/architecture/api/protobuf/vdw_writer.proto``.
"""

from __future__ import annotations

import logging

import grpc

logger = logging.getLogger(__name__)


class VdwWriterServicer:
    """Servicer stub for the ``VdwWriter`` gRPC service."""

    async def WriteBatch(  # noqa: N802
        self,
        request: object,
        context: grpc.aio.ServicerContext,
    ) -> object:
        """Persist a batch of vector rows.  Stub implementation."""

        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "VdwWriter.WriteBatch is not yet implemented",
        )
        return None  # pragma: no cover - abort raises


def register(server: grpc.aio.Server) -> None:
    """Attach the servicer to ``server`` if generated stubs are present."""

    try:
        from lcp.generated import vdw_writer_pb2_grpc  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "vdw_writer_pb2_grpc not found; skipping VdwWriter registration.",
        )
        return

    vdw_writer_pb2_grpc.add_VdwWriterServicer_to_server(  # type: ignore[attr-defined]
        VdwWriterServicer(),
        server,
    )
