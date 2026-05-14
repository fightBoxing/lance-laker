"""Real embedding client backed by ``sentence-transformers``.

Why this lives next to ``mock_model.py``
----------------------------------------
:func:`lcp.embeddings.mock_model.hash_embedding` and
:func:`st_embedding` here share the same call shape::

    vectors: list[list[float]] = func(texts, ...)

so :class:`lcp.workers.executors.embedding.EmbeddingExecutor` can route
between them by reading ``rule.model_name`` without owning any model
loading logic itself.

Slice 4 / B.5 scoping
---------------------
* Single backend (sentence-transformers); BGE / OpenAI clients can land
  next to this module later with the same signature -- the executor
  contract does NOT need to change to add a third backend.
* Module-level model cache keyed by ``model_name``.  The
  ``SentenceTransformer`` constructor downloads weights from the
  Hugging Face hub the first time, then caches under
  ``~/.cache/huggingface``; we cache the loaded object so subsequent
  tasks in the same worker process skip the load entirely.
* No GPU detection, no device argument: CPU-only is the slice-4 default.
  Add a ``device`` kwarg later when a real GPU appears.

Optional dependency
-------------------
``sentence_transformers`` is installed only via the ``[embedding]``
extras.  We import it lazily inside the call so:

* unit tests that monkeypatch ``_load_model`` never need the package;
* the base image stays slim for installs that do not need real models.

Failure mode mirrors :mod:`lance_io`: if the package is missing AND a
real model is requested, raise a friendly error pointing at the
install command, instead of letting Python's bare ``ImportError`` leak.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["SentenceTransformersNotInstalledError", "st_embedding"]

# Process-wide cache: model_name -> loaded SentenceTransformer instance.
# Two tasks in the same worker process for the same rule reuse the load.
_MODEL_CACHE: dict[str, Any] = {}


class SentenceTransformersNotInstalledError(RuntimeError):
    """Raised when ``st_embedding`` is called without the [embedding] extras.

    Carries an install hint so operators do not have to grep stack traces.
    """


def _load_model(model_name: str) -> Any:
    """Return a (possibly cached) ``SentenceTransformer`` instance.

    Lazy import so the base image (no [embedding] extras) still loads
    this module without crashing -- callers only fail when they
    actually try to embed.

    Cache scope is module-level on purpose: each long-running worker
    pod keeps the loaded weights warm for the lifetime of the process,
    which is the common case (one rule fires N tasks against the same
    model_name).
    """

    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SentenceTransformersNotInstalledError(
            "sentence-transformers is not installed; install with "
            "`pip install lcp[embedding]` to enable real embedding models.",
        ) from exc

    model = SentenceTransformer(model_name)
    _MODEL_CACHE[model_name] = model
    return model


def st_embedding(
    texts: Iterable[str],
    *,
    model_name: str,
) -> list[list[float]]:
    """Encode ``texts`` into vectors using ``model_name``.

    ``model_name`` is forwarded verbatim to ``SentenceTransformer`` so
    the caller can pin any Hugging Face hub model id; we do not impose
    a whitelist.  The dimension of each returned vector is decided by
    the model (``all-MiniLM-L6-v2`` => 384, ``all-mpnet-base-v2`` =>
    768, ``bge-large-en-v1.5`` => 1024, ...).

    Returns one vector per input text.  Each vector is a plain
    ``list[float]`` so the call site can serialise it straight to JSON
    (the task ``params`` / ``result`` columns are JSON in the DDL) and
    so callers do not have to know whether numpy is installed.

    Raises:
        SentenceTransformersNotInstalledError: package missing.
        TypeError: ``texts`` contains a non-str element.
    """

    materialised: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            raise TypeError(
                f"st_embedding texts must be str, got {type(text).__name__}",
            )
        materialised.append(text)

    if not materialised:
        # Mirror hash_embedding: zero rows -> zero vectors, no work.
        return []

    model = _load_model(model_name)
    # ``encode`` returns numpy.ndarray; convert to plain Python lists
    # so callers do not transitively need numpy.  ``tolist()`` is
    # available on both numpy.ndarray and torch.Tensor for safety
    # across sentence-transformers versions.
    raw = model.encode(materialised)
    return [list(row.tolist()) if hasattr(row, "tolist") else list(row) for row in raw]
