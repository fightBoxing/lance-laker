"""Lifecycle executor implementations.

One module per task type so the worker / test stack can import a single
executor without dragging the others in.  The package re-exports
:class:`LifecycleExecutor`, :class:`ExecutorResult` and
:func:`build_default_registry` so callers (and the legacy
``lcp.workers.lifecycle_executors`` shim) keep working unchanged.
"""

from __future__ import annotations

from lcp.workers.executors.base import (
    ExecutorResult,
    LifecycleExecutor,
    build_default_registry,
)
from lcp.workers.executors.compaction import CompactionExecutor
from lcp.workers.executors.index_optimize import IndexOptimizeExecutor
from lcp.workers.executors.ttl_delete import TtlDeleteExecutor

__all__ = [
    "CompactionExecutor",
    "ExecutorResult",
    "IndexOptimizeExecutor",
    "LifecycleExecutor",
    "TtlDeleteExecutor",
    "build_default_registry",
]
