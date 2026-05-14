#!/usr/bin/env python3
"""Probe a live Gravitino REST endpoint with the LCP client.

Use this when you want to confirm an in-cluster Gravitino is reachable
from where LCP runs, before wiring the client into the meta-sync service.

Usage::

    # Connectivity-only (works on a freshly-created catalog with no schemas):
    LCP_GRAVITINO_URL=http://gravitino.gravitino-system.svc.cluster.local:8090 \\
    LCP_GRAVITINO_METALAKE=lance_laker \\
    LCP_GRAVITINO_CATALOG=lance_oss \\
    python scripts/probe_gravitino.py

    # Deeper probe, when the catalog has at least one schema:
    python scripts/probe_gravitino.py public --fileset embeddings

Args:
    schema: schema name to list filesets under (positional, optional).
        When omitted, the probe only verifies connectivity + that the
        configured metalake/catalog exist (via ``client.ping()``); useful
        for catalogs that have not yet had any schemas created.

Output:
    JSON-ish lines with a one-line summary per fileset; non-zero exit on
    any client error so this can also serve as a readiness gate in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from lcp.core.config import get_settings
from lcp.integrations.gravitino import (
    GravitinoClient,
    GravitinoError,
)


async def _run(schema: str | None, fileset: str | None) -> int:
    settings = get_settings()
    if not settings.gravitino_url:
        print("LCP_GRAVITINO_URL is empty; refusing to probe.", file=sys.stderr)
        return 2

    print(
        f"[probe] url={settings.gravitino_url} "
        f"metalake={settings.gravitino_metalake} "
        f"catalog={settings.gravitino_catalog} "
        f"auth={settings.gravitino_auth_type}",
    )

    try:
        async with GravitinoClient.from_settings(settings) as client:
            # Always do the cheap connectivity check first.  This isolates
            # "URL/metalake/catalog/auth wrong" from "schema wrong" so an
            # operator reading the logs knows which knob to turn.
            catalog_name = await client.ping()
            print(f"[probe] ping ok: catalog={catalog_name!r}")

            if schema is None:
                print("[probe] no schema supplied; connectivity-only probe done.")
                return 0

            names = await client.list_filesets(schema)
            print(f"[probe] found {len(names)} filesets under schema={schema!r}")
            for name in names:
                print(f"  - {name}")

            if fileset is not None:
                fs = await client.get_fileset(schema, fileset)
                print(
                    f"[probe] fileset {fileset!r}: "
                    f"type={fs.fileset_type} location={fs.storage_location} "
                    f"properties={fs.properties}",
                )
    except GravitinoError as exc:
        print(f"[probe] FAILED: status={exc.status} message={exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "schema",
        nargs="?",
        default=None,
        help=(
            "Gravitino schema name (e.g. 'public').  Optional: when omitted, "
            "the probe only verifies connectivity to the metalake/catalog."
        ),
    )
    parser.add_argument(
        "--fileset",
        default=None,
        help="If given, also fetch this fileset and print its properties.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.schema, args.fileset))


if __name__ == "__main__":
    raise SystemExit(main())
