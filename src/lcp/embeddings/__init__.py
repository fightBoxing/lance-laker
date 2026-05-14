"""Embedding model clients used by the EmbeddingExecutor.

Slice 3 ships a mock-only implementation; the real-model client (BGE /
OpenAI / a colocated server) will land in a follow-up slice without
changing the public function signature.
"""

from lcp.embeddings.mock_model import hash_embedding

__all__ = ["hash_embedding"]
