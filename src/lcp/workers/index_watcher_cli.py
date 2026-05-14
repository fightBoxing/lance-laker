"""Index watcher daemon -- long-running event-driven optimisation trigger.

Run with::

    python -m lcp.workers.index_watcher_cli

Process model
-------------
Mirror of :mod:`lcp.workers.lifecycle_worker`:

- One async event loop, one DB engine, no worker registration (the
  watcher is *not* a task consumer; it is a task *producer*, like the
  lifecycle planner).
- Loop body: ``run_watch_pass`` then sleep the configured interval
  before the next pass.  The interval resolves with the precedence
  ``--interval-seconds`` (CLI) > ``LCP_INDEX_WATCHER_INTERVAL_SECONDS``
  (env / .env) > 10 s builtin default -- low enough to catch fresh
  lance writes within an SLA but still leaves >100x headroom over the
  1 min lifecycle planner cron.
- A failing pass logs and continues; we never exit on a single error
  because the engine is happy to keep retrying.
- ``SIGINT`` / ``SIGTERM`` flip an ``asyncio.Event`` so the current
  pass drains before exit.

What is intentionally out of scope
----------------------------------
- Multi-replica coordination.  The watcher is single-replica by k8s
  manifest; idempotency keys make multi-replica safe but adding it now
  hides bugs that would otherwise surface in tests.
- Heartbeat into ``worker_registry``.  The watcher is not a worker; it
  has no lease.  Liveness is observable via the task table (a stuck
  watcher stops emitting ``watcher:*`` keys).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings
from lcp.core.tenant import with_system_context
from lcp.db.rls import install_rls_listener
from lcp.observability import (
    WATCHER_INDEXES_SCANNED,
    WATCHER_PASS_DURATION,
    WATCHER_PASS_TOTAL,
    WATCHER_TASKS_ENQUEUED,
    start_metrics_server,
    time_block,
)
from lcp.services.index_watcher_service import (
    WatchPassReport,
    run_watch_pass,
)

_LOGGER = logging.getLogger("lcp.workers.index_watcher_cli")


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse namespace.  Extracted so tests can call it."""

    parser = argparse.ArgumentParser(
        prog="python -m lcp.workers.index_watcher_cli",
        description="LCP event-driven index watcher daemon",
    )
    # ``default=None`` is a deliberate sentinel: when the operator omits
    # the flag we fall back to ``Settings.index_watcher_interval_seconds``
    # (env-driven), keeping CLI > env > builtin default precedence.  An
    # explicit ``--interval-seconds=5`` still wins, which is what we want
    # for incident-response overrides.
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help=(
            "Sleep this long between watch passes; overrides "
            "LCP_INDEX_WATCHER_INTERVAL_SECONDS when given."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help=(
            "Exit after N passes (0 = forever).  Useful for "
            "smoke-tests / k8s Jobs."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    """Set up the engine, loop until shutdown event fires."""

    settings = get_settings()
    # CLI > env > builtin default.  ``args.interval_seconds`` is None
    # iff the operator did not pass ``--interval-seconds``; in that
    # case we fall back to whatever Settings (env / .env) resolved.
    interval_seconds: float = (
        args.interval_seconds
        if args.interval_seconds is not None
        else settings.index_watcher_interval_seconds
    )
    _LOGGER.info(
        "watcher interval=%.3fs (source=%s)",
        interval_seconds,
        "cli" if args.interval_seconds is not None else "settings",
    )

    engine: AsyncEngine = create_async_engine(
        settings.db_dsn,
        future=True,
        echo=False,
        pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Start the Prometheus exporter once, before the loop.  The HTTP
    # server runs on a background thread (see start_metrics_server
    # docstring); failures bind-side log and we proceed without
    # metrics rather than crash-loop the daemon.
    start_metrics_server("watcher")

    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    iteration = 0
    try:
        with with_system_context():
            while not shutdown.is_set():
                try:
                    with time_block() as elapsed:
                        async with factory() as session:
                            report = await run_watch_pass(
                                session, settings=settings,
                            )
                    _record_pass_metrics(report, elapsed[0], result="success")
                    _log_report(report)
                except Exception:  # noqa: BLE001 -- never exit on tick err
                    # Per the docstring: a failing pass logs and we keep
                    # going.  Engine-level errors (e.g. pool exhausted)
                    # land here too; the next pass usually recovers.
                    WATCHER_PASS_TOTAL().labels(result="error").inc()
                    _LOGGER.exception("watcher pass failed; continuing")

                iteration += 1
                if args.max_iterations and iteration >= args.max_iterations:
                    _LOGGER.info(
                        "max-iterations=%d reached; exiting",
                        args.max_iterations,
                    )
                    break

                # Sleep, but break early when shutdown fires.
                try:
                    await asyncio.wait_for(
                        shutdown.wait(), timeout=interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        await engine.dispose()
    _LOGGER.info("watcher exited cleanly after %d passes", iteration)
    return 0


def _log_report(report: WatchPassReport) -> None:
    """One log line per pass so operators can tail progress."""

    if report.scanned_indexes == 0 and report.enqueued_tasks == 0:
        # Quiet: avoid spamming the log when there are no watch-enabled
        # indexes yet.  DEBUG so ``--log-level=DEBUG`` still surfaces it.
        _LOGGER.debug("watcher pass: nothing to scan")
        return
    _LOGGER.info(
        "watcher pass: scanned=%d enqueued=%d skipped_below=%d "
        "skipped_open_failed=%d skipped_idempotent=%d",
        report.scanned_indexes,
        report.enqueued_tasks,
        report.skipped_below_threshold,
        report.skipped_open_failed,
        report.skipped_idempotent,
    )


def _record_pass_metrics(
    report: WatchPassReport, elapsed_seconds: float, *, result: str,
) -> None:
    """Translate one pass outcome into Prometheus observations.

    Why we increment the scanned / enqueued counters by the pass-level
    deltas instead of incrementing inside the service:
        Keeping observation at the CLI layer means the service stays
        unaware of the metric registry, which lets watcher-service
        unit tests run without registering metrics, and makes the call
        sites discoverable from one place.
    """

    WATCHER_PASS_TOTAL().labels(result=result).inc()
    WATCHER_PASS_DURATION().observe(elapsed_seconds)
    if report.scanned_indexes:
        WATCHER_INDEXES_SCANNED().inc(report.scanned_indexes)
    if report.enqueued_tasks:
        WATCHER_TASKS_ENQUEUED().inc(report.enqueued_tasks)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM to the shutdown event for graceful drain."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # add_signal_handler is unavailable on Windows; fall back
            # to default KeyboardInterrupt behaviour.
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point usable from CLI and from tests."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
