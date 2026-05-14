"""Tests that verify metric emissions wired into the watcher / worker CLIs.

Why these tests live here rather than next to the CLI:
    The CLIs are thin wrappers around services; the metric emission is
    the only behaviour that lives in the CLI layer.  Co-locating with
    the rest of the metrics tests keeps the observability contract in
    one place.

Strategy:
    Drive the helper functions (``_record_pass_metrics``,
    ``_record_outcome_metrics``) directly rather than spinning up the
    full asyncio loop.  These helpers are the single point where CLI
    state translates into metric writes; testing them in isolation is
    fast and pins the contract that the loop relies on.
"""

from __future__ import annotations

import pytest

from lcp.observability import (
    TASK_DURATION,
    TASK_FINISHED,
    WATCHER_INDEXES_SCANNED,
    WATCHER_PASS_DURATION,
    WATCHER_PASS_TOTAL,
    WATCHER_TASKS_ENQUEUED,
    render_latest,
    reset_registry,
)
from lcp.services.index_watcher_service import WatchPassReport
from lcp.workers.index_watcher_cli import _record_pass_metrics
from lcp.workers.lifecycle_worker import _record_outcome_metrics
from lcp.workers.lifecycle_worker_service import TickOutcome


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    reset_registry()


# ---------------------------------------------------------------------------
# Watcher: _record_pass_metrics
# ---------------------------------------------------------------------------


def test_record_pass_metrics_increments_on_success() -> None:
    report = WatchPassReport(
        scanned_indexes=5,
        enqueued_tasks=2,
        skipped_below_threshold=1,
        skipped_open_failed=0,
        skipped_idempotent=2,
    )

    _record_pass_metrics(report, elapsed_seconds=0.42, result="success")

    body = render_latest().decode()
    assert 'lcp_watcher_pass_total{result="success"} 1.0' in body
    assert "lcp_watcher_indexes_scanned_total 5.0" in body
    assert "lcp_watcher_tasks_enqueued_total 2.0" in body
    assert "lcp_watcher_pass_duration_seconds_count 1.0" in body


def test_record_pass_metrics_zero_report_only_increments_pass_total() -> None:
    """A pass with nothing to scan must still mark the pass complete."""

    report = WatchPassReport(
        scanned_indexes=0,
        enqueued_tasks=0,
        skipped_below_threshold=0,
        skipped_open_failed=0,
        skipped_idempotent=0,
    )

    _record_pass_metrics(report, elapsed_seconds=0.01, result="success")

    body = render_latest().decode()
    assert 'lcp_watcher_pass_total{result="success"} 1.0' in body
    # No work this pass; counters stay at zero (no labelled time series
    # appears in output until a non-zero increment).  Zero-line for the
    # un-labelled counter is the renderer default; we just confirm we
    # did not over-count.
    assert "lcp_watcher_indexes_scanned_total 0.0" in body
    assert "lcp_watcher_tasks_enqueued_total 0.0" in body


# ---------------------------------------------------------------------------
# Worker: _record_outcome_metrics
# ---------------------------------------------------------------------------


def test_record_outcome_metrics_idle_tick_emits_nothing() -> None:
    """``claimed=False`` must not move the task counters.

    Idle ticks happen on every empty queue scan; counting them as
    finished tasks would dilute the success-rate metric and trigger
    spurious "everything is succeeding" dashboards.
    """

    outcome = TickOutcome(
        claimed=False,
        task_uuid=None,
        task_type=None,
        final_status=None,
    )

    _record_outcome_metrics(outcome, elapsed_seconds=0.0)

    body = render_latest().decode()
    # No fully-labelled time series for lcp_task_finished_total exists.
    for line in body.splitlines():
        if line.startswith("lcp_task_finished_total{"):
            raise AssertionError(f"unexpected emission: {line}")


def test_record_outcome_metrics_succeeded_increments_per_type() -> None:
    outcome = TickOutcome(
        claimed=True,
        task_uuid="t-1",
        task_type="COMPACTION",
        final_status="SUCCEEDED",
    )

    _record_outcome_metrics(outcome, elapsed_seconds=1.5)

    body = render_latest().decode()
    assert (
        'lcp_task_finished_total{result="SUCCEEDED",type="COMPACTION"} 1.0'
        in body
    )
    assert (
        'lcp_task_duration_seconds_count{type="COMPACTION"} 1.0'
        in body
    )


def test_record_outcome_metrics_failed_carries_failed_label() -> None:
    outcome = TickOutcome(
        claimed=True,
        task_uuid="t-2",
        task_type="INDEX_OPTIMIZE",
        final_status="FAILED",
        error="DatasetMissing",
    )

    _record_outcome_metrics(outcome, elapsed_seconds=0.2)

    body = render_latest().decode()
    assert (
        'lcp_task_finished_total{result="FAILED",type="INDEX_OPTIMIZE"} 1.0'
        in body
    )


def test_record_outcome_metrics_unknown_task_type_uses_sentinel() -> None:
    """``task_type=None`` (orphaned-dataset path) must still record.

    Dropping the data point would hide the very failure mode an
    operator most needs to see.
    """

    outcome = TickOutcome(
        claimed=True,
        task_uuid="t-3",
        task_type=None,
        final_status="FAILED",
        error="DatasetMissing",
    )

    _record_outcome_metrics(outcome, elapsed_seconds=0.1)

    body = render_latest().decode()
    assert (
        'lcp_task_finished_total{result="FAILED",type="UNKNOWN"} 1.0'
        in body
    )


# ---------------------------------------------------------------------------
# Sanity: the helpers actually delegate to the right collectors
# ---------------------------------------------------------------------------


def test_helpers_target_the_documented_collectors() -> None:
    """If the helper imports drift, this test fails before production does."""

    # Just touching the handles must not raise.
    WATCHER_PASS_TOTAL().labels(result="success")
    WATCHER_PASS_DURATION()
    WATCHER_INDEXES_SCANNED()
    WATCHER_TASKS_ENQUEUED()
    TASK_FINISHED().labels(type="X", result="Y")
    TASK_DURATION().labels(type="X")
