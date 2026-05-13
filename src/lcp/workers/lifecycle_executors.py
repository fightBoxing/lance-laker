"""Compatibility shim re-exporting executors from the new sub-package.

Why this file still exists
--------------------------
The original implementation lived here.  After splitting one-module-per
-executor under :mod:`lcp.workers.executors`, every concrete executor
moved out, but several callers (worker, tests) still import from this
module.  Keeping a thin re-export shim avoids touching those callers.

New code SHOULD import from :mod:`lcp.workers.executors` directly.
"""

from __future__ import annotations

from lcp.workers.executors import (
    CompactionExecutor,
    ExecutorResult,
    IndexOptimizeExecutor,
    LifecycleExecutor,
    TtlDeleteExecutor,
    build_default_registry,
)

__all__ = [
    "CompactionExecutor",
    "ExecutorResult",
    "IndexOptimizeExecutor",
    "LifecycleExecutor",
    "TtlDeleteExecutor",
    "build_default_registry",
]
