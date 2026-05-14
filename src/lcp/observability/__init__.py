"""LCP observability primitives (metrics, tracing, logging adapters).

For now this package only exposes the metric registry; tracing and
structured-log fan-out can land later without churning the import
surface.
"""

from lcp.observability.metrics import (
    CONTENT_TYPE_LATEST,
    GRAVITINO_REQUEST_DURATION,
    TASK_DURATION,
    TASK_FINISHED,
    WATCHER_INDEXES_SCANNED,
    WATCHER_PASS_DURATION,
    WATCHER_PASS_TOTAL,
    WATCHER_TASKS_ENQUEUED,
    render_latest,
    reset_registry,
    set_build_component,
    start_metrics_server,
    time_block,
)

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
