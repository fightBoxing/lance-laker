"""Gravitino REST client + dataset/fileset mapping helpers.

Subpackage layout:

- :mod:`lcp.integrations.gravitino.client` — async HTTP client.
- :mod:`lcp.integrations.gravitino.mapping` — pure-function mapping between
  Gravitino fileset payloads and LCP ``Dataset`` rows.
"""

from lcp.integrations.gravitino.client import (
    Fileset,
    GravitinoAuthError,
    GravitinoClient,
    GravitinoError,
    GravitinoNotFoundError,
)

__all__ = [
    "Fileset",
    "GravitinoAuthError",
    "GravitinoClient",
    "GravitinoError",
    "GravitinoNotFoundError",
]
