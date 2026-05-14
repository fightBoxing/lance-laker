"""LCP observability primitives -- in-process metric registry.

Why this module exists
----------------------
Splits all metric *definitions* out of the call sites that emit them.
Two goals:

1. **Single source of truth** for metric names, labels, and buckets.
   Renaming a metric should touch one file, not five executors.
2. **Test-friendly**: tests can ``reset_registry()`` to get a clean
   slate without monkey-patching individual call sites.

What lives here
---------------
- One ``CollectorRegistry`` we own (we deliberately do **not** use the
  default global registry; that registry is shared with libraries we
  don't control and would leak between tests).
- ``Counter`` / ``Histogram`` / ``Gauge`` instances for the four
  high-ROI signals identified for v0 of P1-7 Monitor:

    1. ``lcp_watcher_pass_total{result}``        -- watcher tick outcome
    2. ``lcp_watcher_pass_duration_seconds``     -- watcher tick latency
    3. ``lcp_watcher_indexes_scanned_total``     -- per-pass scan counter
    4. ``lcp_watcher_tasks_enqueued_total``      -- per-pass emit counter
    5. ``lcp_task_finished_total{type,result}``  -- executor outcome
    6. ``lcp_task_duration_seconds{type}``       -- executor latency
    7. ``lcp_gravitino_request_duration_seconds{op,result}``
                                                 -- catalog client RTT
    8. ``lcp_build_info{version,component}``     -- always-1 gauge

What is intentionally NOT here
------------------------------
- HTTP request metrics for the REST API.  ``prometheus_client``'s
  ASGI middleware is not yet wired; the four signals above are the
  ones an operator actually pages on.  We will add it when we have
  a real SLO to defend.
- Planner / meta-sync metrics.  Both are short-lived CronJob
  processes; pull-mode scraping is unreliable for them.  Pushgateway
  support is deliberately deferred -- success/failure for those jobs
  is observable via k8s ``job_status`` + structured logs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client.exposition import generate_latest

from lcp import __version__

__all__ = [
    "CONTENT_TYPE_LATEST",
    "GRAVITINO_REQUEST_DURATION",
    "TASK_DURATION",
    "TASK_FINISHED",
    "WATCHER_INDEXES_SCANNED",
    "WATCHER_PASS_DURATION",
    "WATCHER_PASS_TOTAL",
    "WATCHER_TASKS_ENQUEUED",
    "render_latest",
    "reset_registry",
    "set_build_component",
    "start_metrics_server",
    "time_block",
]

_LOGGER = logging.getLogger("lcp.observability.metrics")


# ---------------------------------------------------------------------------
# Registry construction
# ---------------------------------------------------------------------------

# Latency buckets cover three regimes:
#   < 1 s      -- in-process (gravitino single GET, watcher idle pass)
#   1 - 30 s   -- single-task work (small index optimise, small compaction)
#   30 - 600 s -- heavy work (large compaction, full index build)
# We deliberately do not extend past 600 s; anything that slow already
# breached SLO and the histogram does not need finer resolution there.
_LATENCY_BUCKETS_SECONDS = (
    0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0,
    60.0, 120.0, 300.0, 600.0,
)


# Content-Type the Prometheus protocol expects.  We re-export rather
# than import-from-our-callers to keep prometheus_client out of the
# REST main module's import surface.
CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


def _build_registry() -> CollectorRegistry:
    """Construct a fresh registry with all metrics pre-registered.

    Kept as a module-level function so ``reset_registry`` can rebuild
    in-place; tests rely on this to isolate state.
    """

    registry = CollectorRegistry()

    # Build-info gauge: always 1, exists so dashboards and alerts can
    # join on the running version + component without scraping the API.
    build_info = Gauge(
        "lcp_build_info",
        "LCP build info; value is always 1.",
        labelnames=("version", "component"),
        registry=registry,
    )
    # ``component`` is filled in by start_metrics_server; the export here
    # is just so the gauge exists in the registry from t=0.
    build_info.labels(version=__version__, component="unknown").set(1)

    # ---- watcher --------------------------------------------------------

    Counter(
        "lcp_watcher_pass_total",
        "Number of completed watcher passes; result = success | error.",
        labelnames=("result",),
        registry=registry,
    )
    Histogram(
        "lcp_watcher_pass_duration_seconds",
        "Wall-clock time of one watcher pass.",
        buckets=_LATENCY_BUCKETS_SECONDS,
        registry=registry,
    )
    Counter(
        "lcp_watcher_indexes_scanned_total",
        "Total indexes scanned across all watcher passes.",
        registry=registry,
    )
    Counter(
        "lcp_watcher_tasks_enqueued_total",
        "Total optimisation tasks enqueued by the watcher.",
        registry=registry,
    )

    # ---- executor / worker ---------------------------------------------

    Counter(
        "lcp_task_finished_total",
        "Number of executor runs that reached a terminal state.",
        labelnames=("type", "result"),
        registry=registry,
    )
    Histogram(
        "lcp_task_duration_seconds",
        "Wall-clock time of one executor run.",
        labelnames=("type",),
        buckets=_LATENCY_BUCKETS_SECONDS,
        registry=registry,
    )

    # ---- gravitino client ----------------------------------------------

    Histogram(
        "lcp_gravitino_request_duration_seconds",
        "Wall-clock time of one Gravitino HTTP request.",
        labelnames=("op", "result"),
        buckets=_LATENCY_BUCKETS_SECONDS,
        registry=registry,
    )

    return registry


_REGISTRY = _build_registry()


# ---------------------------------------------------------------------------
# Public metric handles
# ---------------------------------------------------------------------------
#
# Why we re-resolve from the registry instead of holding the objects
# returned by Counter(...) above:  ``reset_registry()`` rebuilds every
# collector; if call sites cached the original handles they would
# silently keep emitting into a detached registry that no exporter
# scrapes.  Looking up by name on each access (cheap dict get) means
# ``reset_registry`` actually works.

def _collector(name: str):  # type: ignore[no-untyped-def]
    """Look up a pre-registered collector by metric name."""

    # ``_names_to_collectors`` is a private attribute but it has been
    # stable since prometheus_client 0.7; we accept the coupling
    # because the alternative (re-declaring metrics on every reset) is
    # strictly more fragile.
    return _REGISTRY._names_to_collectors[name]  # noqa: SLF001


def WATCHER_PASS_TOTAL() -> Counter:  # noqa: N802 -- public constant-style
    """Return the watcher_pass_total Counter (current registry)."""

    return _collector("lcp_watcher_pass_total")


def WATCHER_PASS_DURATION() -> Histogram:  # noqa: N802
    """Return the watcher_pass_duration_seconds Histogram."""

    return _collector("lcp_watcher_pass_duration_seconds")


def WATCHER_INDEXES_SCANNED() -> Counter:  # noqa: N802
    """Return the watcher_indexes_scanned_total Counter."""

    return _collector("lcp_watcher_indexes_scanned_total")


def WATCHER_TASKS_ENQUEUED() -> Counter:  # noqa: N802
    """Return the watcher_tasks_enqueued_total Counter."""

    return _collector("lcp_watcher_tasks_enqueued_total")


def TASK_FINISHED() -> Counter:  # noqa: N802
    """Return the task_finished_total Counter."""

    return _collector("lcp_task_finished_total")


def TASK_DURATION() -> Histogram:  # noqa: N802
    """Return the task_duration_seconds Histogram."""

    return _collector("lcp_task_duration_seconds")


def GRAVITINO_REQUEST_DURATION() -> Histogram:  # noqa: N802
    """Return the gravitino_request_duration_seconds Histogram."""

    return _collector("lcp_gravitino_request_duration_seconds")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def time_block() -> Iterator[list[float]]:
    """Context manager yielding a 1-element list whose [0] is elapsed seconds.

    Usage::

        with time_block() as elapsed:
            do_work()
        observe(elapsed[0])

    We yield a list rather than returning at the ``__exit__`` because
    Python's contextmanager protocol does not let us return a value
    from ``__exit__``.  The mutable-list pattern keeps call-sites linear
    and avoids the noise of building a wrapper object.
    """

    started = perf_counter()
    bucket: list[float] = [0.0]
    try:
        yield bucket
    finally:
        bucket[0] = perf_counter() - started


def render_latest() -> bytes:
    """Render the registry in Prometheus text-format 0.0.4.

    Returned as bytes so the FastAPI handler can stream it directly
    into a Response without re-encoding.
    """

    return generate_latest(_REGISTRY)


def reset_registry() -> None:
    """Rebuild the registry from scratch.

    Test-only helper: pytest fixtures call this between tests so a
    Counter incremented in one test does not leak into the next.
    Production code MUST NOT call this -- in-flight scrapes would lose
    their target.
    """

    global _REGISTRY  # noqa: PLW0603 -- module-level singleton
    _REGISTRY = _build_registry()


# ---------------------------------------------------------------------------
# Build-info convenience
# ---------------------------------------------------------------------------


def set_build_component(component: str) -> None:
    """Set the ``component`` label of ``lcp_build_info``.

    Call once during process startup with one of:
    ``"rest_api"``, ``"watcher"``, ``"worker"``, ``"planner"``,
    ``"meta_sync"``.  Subsequent calls are no-ops on the previous label
    set -- we delete the placeholder ``component="unknown"`` time-series
    so the gauge does not double-count.
    """

    gauge = _collector("lcp_build_info")
    # Best-effort cleanup of the placeholder label set.  If it was
    # never set (e.g. after reset_registry in tests) the remove() call
    # raises KeyError, which we swallow.
    try:
        gauge.remove(__version__, "unknown")
    except KeyError:
        pass
    gauge.labels(version=__version__, component=component).set(1)


# ---------------------------------------------------------------------------
# Standalone HTTP exporter for daemons
# ---------------------------------------------------------------------------


# Default scrape port for daemons without their own HTTP surface.
# 9090 is Prometheus's own default scrape port; pick an offset for our
# data plane so that running Prometheus + LCP daemons on the same host
# does not collide.  9464 is the OpenTelemetry-prometheus exporter
# convention; reusing it makes ServiceMonitor / scrape configs easier
# to share with any observability stack the operator already runs.
_DEFAULT_METRICS_PORT = 9464


def start_metrics_server(
    component: str,
    *,
    port: int | None = None,
    host: str = "0.0.0.0",  # noqa: S104 -- containers expect 0.0.0.0
) -> int | None:
    """Start a standalone Prometheus exporter HTTP server.

    Returns the port the server is bound to, or ``None`` when the
    server was not started (port == 0).

    :param component:
        Value for the ``lcp_build_info`` ``component`` label; same
        vocabulary as :func:`set_build_component`.
    :param port:
        TCP port to bind.  ``None`` (default) reads ``LCP_METRICS_PORT``
        from the environment, falling back to ``9464``.  Setting the
        port to ``0`` disables the exporter entirely -- useful for
        integration tests that drive a CLI in-process and would
        otherwise leak a listening socket.
    :param host:
        Bind address.  Defaults to ``0.0.0.0`` because daemons run
        inside a container and the kubelet scrapes the Pod IP, not
        loopback.

    Why a separate HTTP server instead of attaching to the daemon's
    asyncio loop:
        ``prometheus_client.start_http_server`` spawns a background
        thread that owns its own ``http.server.HTTPServer``.  That is
        exactly what we want for daemons whose main loop is dedicated
        to claiming and executing tasks -- the metrics endpoint must
        stay responsive even when an executor pegs the event loop.

    Idempotency:
        prometheus_client raises ``OSError(EADDRINUSE)`` on the second
        call to the same port; we catch that and log so re-running a
        watcher pass under pytest does not crash.  We do NOT swallow
        other OSErrors -- they signal real misconfiguration (bad
        bind address, permission denied, etc.).
    """

    set_build_component(component)

    if port is None:
        env_port = os.environ.get("LCP_METRICS_PORT")
        port = int(env_port) if env_port else _DEFAULT_METRICS_PORT

    if port == 0:
        _LOGGER.info(
            "metrics: exporter disabled (port=0); component=%s", component,
        )
        return None

    try:
        start_http_server(port=port, addr=host, registry=_REGISTRY)
    except OSError as exc:
        # 48 = EADDRINUSE on macOS, 98 on Linux.  Either way: another
        # process in the same pod / dev shell already bound the port.
        # We log and proceed -- skipping metrics is strictly better
        # than crash-looping a daemon over telemetry.
        _LOGGER.warning(
            "metrics: failed to bind %s:%d (%s); continuing without exporter",
            host, port, exc,
        )
        return None

    _LOGGER.info(
        "metrics: exporter listening on %s:%d component=%s",
        host, port, component,
    )
    return port
