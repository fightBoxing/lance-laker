"""Embedding model clients used by the EmbeddingExecutor.

Slice 3 ships the mock-only implementation; slice 4 adds the real
sentence-transformers backend.  The executor routes between them by
reading ``rule.model_name`` so adding a third backend (BGE / OpenAI)
later does not change the executor's contract.
"""

from lcp.embeddings.mock_model import hash_embedding
from lcp.embeddings.sentence_transformer_model import (
    SentenceTransformersNotInstalledError,
    st_embedding,
)

__all__ = [
    "SentenceTransformersNotInstalledError",
    "hash_embedding",
    "st_embedding",
]
