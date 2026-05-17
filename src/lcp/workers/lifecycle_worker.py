"""Lifecycle worker daemon -- the long-running process that consumes tasks.

Run with::

    python -m lcp.workers.lifecycle_worker

Or with a specific task type filter so multiple worker pods can specialise::

    python -m lcp.workers.lifecycle_worker --task-type INDEX_OPTIMIZE

Process model
-------------
- One async event loop, one DB engine, one ``register_worker`` call at
  startup.
- A parallel heartbeat task refreshes ``last_heartbeat_at`` every
  ``--heartbeat-seconds`` (default 10s) so the reaper does not mistake
  a healthy worker for a dead one.
- A parallel planner task runs ``plan_once()`` every
  ``--planner-seconds`` (default 60s), replacing the standalone
  ``lcp-planner`` CronJob.  Disable with ``--disable-planner``.
- Loop body: ``run_iteration``; sleep ``--idle-seconds`` between empty
  ticks (no task claimed) and ``--busy-seconds`` between successful
  ticks so the worker yields the DB connection promptly.
- ``SIGINT`` / ``SIGTERM`` flip an ``asyncio.Event`` and let the
  current iteration drain to completion before exit -- no half-done
  tasks left in RUNNING.

What is intentionally out of scope
----------------------------------
- Metrics.  No prometheus / log fan-out beyond stdlib ``logging``.
- Multi-worker capacity per process.  ``capacity=1`` is hard-coded; lift
  via ``register_worker(capacity=...)`` once executors stop being stubs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import uuid

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings
from lcp.core.tenant import with_system_context
from lcp.db.rls import install_rls_listener
from lcp.observability import (
    TASK_DURATION,
    TASK_FINISHED,
    start_metrics_server,
    time_block,
)
from lcp.services import scheduler_service
from lcp.services.lifecycle_planner_service import plan_once
from lcp.workers.lifecycle_executors import build_default_registry
from lcp.workers.lifecycle_worker_service import (
    TickOutcome,
    WorkerConfig,
    run_iteration,
)

_LOGGER = logging.getLogger("lcp.workers.lifecycle_worker")


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse namespace.  Extracted so tests can call it."""

    parser = argparse.ArgumentParser(
        prog="python -m lcp.workers.lifecycle_worker",
        description="LCP lifecycle worker daemon",
    )
    parser.add_argument(
        "--task-type",
        default=None,
        help=(
            "Only claim tasks of this type "
            "(TTL_DELETE | COMPACTION | INDEX_OPTIMIZE).  "
            "Default: claim any type."
        ),
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=2.0,
        help="Sleep this long after an empty claim.",
    )
    parser.add_argument(
        "--busy-seconds",
        type=float,
        default=0.1,
        help="Sleep this long after a successful claim before next pull.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=10.0,
        help="Interval between heartbeat refreshes (default 10s).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help=(
            "Exit after N iterations (0 = forever).  Useful for "
            "smoke-tests / k8s Jobs."
        ),
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help=(
            "Override the auto-generated worker id "
            "(default: '<hostname>-<8 hex>')."
        ),
    )
    parser.add_argument(
        "--planner-seconds",
        type=float,
        default=60.0,
        help=(
            "Interval between planner ticks that scan lifecycle policies "
            "and emit tasks (default 60s).  Set 0 to disable."
        ),
    )
    parser.add_argument(
        "--disable-planner",
        action="store_true",
        help="Disable the embedded planner loop (run as pure executor only).",
    )
    return parser.parse_args(argv)


def _default_worker_id() -> str:
    """Stable-ish worker id: '<hostname>-<random suffix>'."""

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _heartbeat_loop(
    factory: async_sessionmaker,
    worker_id: str,
    lease_id: str,
    interval: float,
    shutdown: asyncio.Event,
) -> None:
    """Background task that refreshes the worker's heartbeat periodically.

    Runs until *shutdown* is set.  Errors are logged and swallowed — a
    single heartbeat failure must not crash the worker; the reaper's
    timeout provides a grace window of ~3× the heartbeat interval.
    """

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break  # shutdown fired
        except asyncio.TimeoutError:
            pass  # interval elapsed — time to heartbeat

        try:
            async with factory() as session:
                await scheduler_service.heartbeat(
                    session,
                    worker_id=worker_id,
                    lease_id=lease_id,
                )
            _LOGGER.debug("heartbeat sent for %s", worker_id)
        except Exception:  # noqa: BLE001 — heartbeat failures are non-fatal
            _LOGGER.warning(
                "heartbeat failed for %s; will retry next interval",
                worker_id,
                exc_info=True,
            )


async def _planner_loop(
    factory: async_sessionmaker,
    interval: float,
    shutdown: asyncio.Event,
) -> None:
    """Background task that scans lifecycle policies and emits tasks.

    Replaces the standalone ``lcp-planner`` CronJob.  Runs every *interval*
    seconds (default 60s) inside the worker's event loop.  Errors are
    logged and swallowed — a single planner tick failure must not crash the
    worker; the next tick will retry cleanly thanks to idempotency keys.
    """

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break  # shutdown fired
        except asyncio.TimeoutError:
            pass  # interval elapsed — time to plan

        try:
            async with factory() as session:
                tick = await plan_once(session)
            _LOGGER.info(
                "planner: scanned=%d emitted=%d skipped=%d",
                tick.scanned_policies,
                len(tick.emitted_tasks),
                tick.skipped_duplicates,
            )
        except Exception:  # noqa: BLE001 — planner failures are non-fatal
            _LOGGER.warning(
                "planner tick failed; will retry next interval",
                exc_info=True,
            )


async def _run(args: argparse.Namespace) -> int:
    """Set up the engine, register, loop until shutdown event fires."""

    settings = get_settings()
    engine: AsyncEngine = create_async_engine(
        settings.db_dsn,
        future=True,
        echo=False,
        pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(
        engine, expire_on_commit=False,
    )

    worker_id = args.worker_id or _default_worker_id()
    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    # Start the Prometheus exporter once, before the loop.  Same
    # rationale as the watcher CLI: long-running daemon, pull-mode
    # scrape, EADDRINUSE is logged and tolerated.
    start_metrics_server("worker")

    iteration = 0
    try:
        with with_system_context():
            # 1. Register once at startup.
            async with factory() as session:
                worker = await scheduler_service.register_worker(
                    session,
                    worker_id=worker_id,
                    worker_type="lifecycle",
                )
            _LOGGER.info(
                "worker %s registered (lease=%s, task_type=%s)",
                worker.worker_id, worker.lease_id, args.task_type or "ANY",
            )

            config = WorkerConfig(
                worker_id=worker.worker_id,
                lease_id=worker.lease_id,
                task_type=args.task_type,
            )
            registry = build_default_registry()

            # 2. Start background heartbeat so the reaper never
            #    mistakes a healthy worker for a dead one.
            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(
                    factory,
                    worker_id=worker.worker_id,
                    lease_id=worker.lease_id,
                    interval=args.heartbeat_seconds,
                    shutdown=shutdown,
                ),
            )

            # 3. Start embedded planner (replaces the standalone CronJob).
            planner_task: asyncio.Task[None] | None = None
            if not args.disable_planner and args.planner_seconds > 0:
                planner_task = asyncio.create_task(
                    _planner_loop(
                        factory,
                        interval=args.planner_seconds,
                        shutdown=shutdown,
                    ),
                )
                _LOGGER.info(
                    "planner loop started (interval=%.0fs)",
                    args.planner_seconds,
                )
            else:
                _LOGGER.info("planner loop disabled")

            # 4. Drain loop.
            try:
                while not shutdown.is_set():
                    async with factory() as session:
                        outcome = await run_iteration(
                            session,
                            config=config,
                            registry=registry,
                        )
                    _log_outcome(outcome)

                    iteration += 1
                    if args.max_iterations and iteration >= args.max_iterations:
                        _LOGGER.info(
                            "max-iterations=%d reached; exiting",
                            args.max_iterations,
                        )
                        break

                    # Sleep, but break early when shutdown fires.
                    sleep_seconds = (
                        args.busy_seconds if outcome.claimed
                        else args.idle_seconds
                    )
                    try:
                        await asyncio.wait_for(
                            shutdown.wait(), timeout=sleep_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
            finally:
                # Cancel background tasks so they don't outlive the loop.
                shutdown.set()
                for bg_task in (heartbeat_task, planner_task):
                    if bg_task is not None:
                        bg_task.cancel()
                        try:
                            await bg_task
                        except asyncio.CancelledError:
                            pass
    finally:
        await engine.dispose()
    _LOGGER.info("worker exited cleanly after %d iterations", iteration)
    return 0


def _log_outcome(outcome: TickOutcome) -> None:
    """One log line per iteration so operators can tail progress."""

    if not outcome.claimed:
        _LOGGER.debug("idle tick -- no task claimed")
        return
    if outcome.final_status == "SUCCEEDED":
        _LOGGER.info(
            "task %s (%s) -> SUCCEEDED",
            outcome.task_uuid, outcome.task_type,
        )
    else:
        _LOGGER.warning(
            "task %s (%s) -> FAILED (%s)",
            outcome.task_uuid, outcome.task_type, outcome.error,
        )


def _record_outcome_metrics(
    outcome: TickOutcome, elapsed_seconds: float,
) -> None:
    """Translate one tick outcome into Prometheus observations.

    Idle ticks (``claimed=False``) are not counted as task runs --
    they would dilute lcp_task_finished_total with empty pulls and
    make the success-rate alert meaningless.  Idle frequency is
    inferable from ``lcp_task_duration_seconds_count``-on-rate when
    we need it later.
    """

    if not outcome.claimed:
        return
    # outcome.task_type is None only for the dataset-missing path; we
    # still want to count those, so coerce to a sentinel rather than
    # drop the data point.
    task_type = outcome.task_type or "UNKNOWN"
    result = outcome.final_status or "UNKNOWN"
    TASK_FINISHED().labels(type=task_type, result=result).inc()
    TASK_DURATION().labels(type=task_type).observe(elapsed_seconds)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM to the shutdown event for graceful drain."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # add_signal_handler is unavailable on Windows; fall back to
            # default KeyboardInterrupt behaviour.
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
