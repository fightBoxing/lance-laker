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
from lcp.integrations.gravitino.mapping import (
    LCP_PROPERTY_PREFIX,
    DatasetPatch,
    dataset_to_property_patch,
    fileset_to_dataset_patch,
)

__all__ = [
    "LCP_PROPERTY_PREFIX",
    "DatasetPatch",
    "Fileset",
    "GravitinoAuthError",
    "GravitinoClient",
    "GravitinoError",
    "GravitinoNotFoundError",
    "dataset_to_property_patch",
    "fileset_to_dataset_patch",
]
