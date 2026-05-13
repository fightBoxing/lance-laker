"""CLI entry-point for the lifecycle planner -- one tick, then exit.

Used by the k8s CronJob (``deploy/k8s/30-planner-cronjob.yaml``).  Kept
intentionally tiny because:

- it owns no business logic; everything substantive lives in
  :func:`lcp.services.lifecycle_planner_service.plan_once`;
- the CronJob model is "spawn a fresh Pod every minute"; this script
  must therefore set up an engine, run exactly one tick under a system
  principal, and tear the engine down before returning so the Pod
  exits with a clean status code.

Run with::

    python -m lcp.workers.lifecycle_planner_cli

Exit codes
----------
- ``0`` -- tick completed (zero or more tasks emitted).
- ``1`` -- unhandled error during the tick (k8s will mark the Pod failed
  and the CronJob's ``failedJobsHistoryLimit`` keeps a small audit
  trail).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings
from lcp.core.tenant import with_system_context
from lcp.db.rls import install_rls_listener
from lcp.services.lifecycle_planner_service import plan_once

_LOGGER = logging.getLogger("lcp.workers.lifecycle_planner_cli")


async def _run() -> int:
    """One planner tick, set-up and tear-down inclusive."""

    settings = get_settings()
    engine = create_async_engine(
        settings.db_dsn,
        future=True,
        echo=False,
        pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        with with_system_context():
            async with factory() as session:
                tick = await plan_once(session)
        _LOGGER.info(
            "planner tick: scanned=%d emitted=%d skipped_duplicates=%d",
            tick.scanned_policies,
            len(tick.emitted_tasks),
            tick.skipped_duplicates,
        )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    """Entry-point usable from CLI and tests."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_run())
    except Exception:
        # Log the full traceback so the failed Pod's logs explain *why*.
        _LOGGER.exception("planner tick failed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
