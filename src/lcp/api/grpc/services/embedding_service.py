"""Embedding service stub.

Maps to ``docs/architecture/api/protobuf/embedding_service.proto``.
"""

from __future__ import annotations

import logging

import grpc

logger = logging.getLogger(__name__)


class EmbeddingServicer:
    """Servicer stub for the ``Embedding`` gRPC service."""

    async def Embed(  # noqa: N802
        self,
        request: object,
        context: grpc.aio.ServicerContext,
    ) -> object:
        """Compute embeddings for a batch of inputs.  Stub implementation."""

        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "Embedding.Embed is not yet implemented",
        )
        return None  # pragma: no cover - abort raises


def register(server: grpc.aio.Server) -> None:
    """Attach the servicer to ``server`` if generated stubs are present."""

    try:
        from lcp.generated import embedding_service_pb2_grpc  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "embedding_service_pb2_grpc not found; skipping Embedding registration.",
        )
        return

    embedding_service_pb2_grpc.add_EmbeddingServicer_to_server(  # type: ignore[attr-defined]
        EmbeddingServicer(),
        server,
    )
