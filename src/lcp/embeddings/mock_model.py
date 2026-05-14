"""Deterministic mock embedding model.

Why this module exists
----------------------
Slice 3 of P0-2 wires the embedding pipeline end-to-end with no real
model dependency.  A deterministic hash-based vector lets us:

* Run the full task -> executor -> lance write-back path under unit
  tests without installing torch / sentence-transformers / a model
  server.
* Verify in real-cluster smoke tests that the same input produces the
  same vector (idempotency contract callers will assume of any future
  real model client).
* Distinguish two different inputs by inspecting the vector (a constant
  vector would mask routing bugs).

This is **not** a useful representation of semantics; the cosine
distance between two unrelated strings here is meaningless.  Once a
real model client lands (BGE / OpenAI / a colocated triton server),
swap the import in the executor; the function signature here matches
what such a client will look like.

Function contract
-----------------
``hash_embedding(texts, dim=384) -> list[list[float]]``

* ``texts``: iterable of strings.  ``None`` -> raise ``TypeError`` (the
  caller must decide how to encode missing source columns).
* ``dim``: vector dimension; must be a positive multiple of 4 because
  we draw 4 bytes per float from the hash digest.  384 (the BGE-base
  default) and 768 (BGE-large) both qualify.
* Returns one vector per input text, each a list of ``dim`` floats in
  ``[-1.0, 1.0]``.  The list-of-lists shape (vs. numpy) keeps the
  module pylance- and torch-free and serialises straight to JSON in
  task payloads.

Determinism
-----------
Same ``(text, dim)`` pair always produces the same vector across
processes / Python versions: SHA-256 is stable, and ``struct.unpack``
is stable for fixed format strings.  The unit tests pin a couple of
known vectors to catch any accidental change to the byte-mixing
recipe.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable

__all__ = ["hash_embedding"]

# 4 bytes per float (we read uint32 then map into [-1, 1]).
_BYTES_PER_FLOAT = 4
# uint32 max; used to scale into the unit interval.
_UINT32_MAX = (1 << 32) - 1


def hash_embedding(
    texts: Iterable[str],
    *,
    dim: int = 384,
) -> list[list[float]]:
    """Return a deterministic ``dim``-vector per text.

    Algorithm: SHA-256(text + counter) repeated until we have enough
    bytes, then unpack into floats in ``[-1.0, 1.0]``.  Cheaper than a
    real model and reproducible across processes.
    """

    if dim <= 0:
        raise ValueError(f"dim must be positive, got {dim}")
    if dim % _BYTES_PER_FLOAT != 0:
        # We draw whole 4-byte words from the hash; partial words would
        # need a different mixing recipe and there is no real use case.
        raise ValueError(
            f"dim must be a multiple of {_BYTES_PER_FLOAT}, got {dim}",
        )

    bytes_needed = dim * _BYTES_PER_FLOAT
    out: list[list[float]] = []
    for text in texts:
        if not isinstance(text, str):
            # Be loud rather than silently coerce -- caller bug otherwise.
            raise TypeError(
                f"hash_embedding texts must be str, got {type(text).__name__}",
            )
        out.append(_one(text, dim, bytes_needed))
    return out


def _one(text: str, dim: int, bytes_needed: int) -> list[float]:
    """Compute a single deterministic vector for ``text``.

    Uses a counter-fed hash chain so we never run out of bytes for any
    realistic ``dim``: digest length is 32 bytes, so ``dim=768`` (3072
    bytes) needs 96 hash rounds.  The counter is encoded into the hash
    input so each round produces independent bytes.
    """

    payload = text.encode("utf-8")
    buf = bytearray()
    counter = 0
    while len(buf) < bytes_needed:
        h = hashlib.sha256()
        h.update(payload)
        # Big-endian counter so round 0 / round 1 produce visibly
        # different inputs even when ``payload`` is empty.
        h.update(counter.to_bytes(4, "big"))
        buf.extend(h.digest())
        counter += 1

    # Unpack ``dim`` uint32 words and map each into [-1, 1] via the
    # midpoint of the uint32 range.  Centred so a constant input does
    # not yield an all-positive vector (which would be misleading).
    raw = struct.unpack(f">{dim}I", bytes(buf[:bytes_needed]))
    half = _UINT32_MAX / 2.0
    return [(value - half) / half for value in raw]
