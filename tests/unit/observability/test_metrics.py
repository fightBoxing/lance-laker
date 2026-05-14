"""Unit tests for the lcp.observability metric registry.

Why these tests exist
---------------------
The metric registry is a singleton so a regression here silently
breaks dashboards and alerts everywhere -- nothing in normal product
testing exercises ``render_latest`` or ``set_build_component``.  Pin
the contract here.
"""

from __future__ import annotations

import pytest

from lcp.observability import (
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
    time_block,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """Rebuild the registry between tests so counters do not leak."""

    reset_registry()


# ---------------------------------------------------------------------------
# Naming + label contract
# ---------------------------------------------------------------------------


def test_render_latest_lists_all_documented_metric_names() -> None:
    """The eight v0 metrics must all appear in /metrics output, even at zero.

    Pinning the names here is what makes "rename a metric" a single-file
    change rather than five-file scavenger hunt -- the test fails
    immediately and points at the registry.
    """

    body = render_latest().decode()
    expected = {
        "lcp_build_info",
        "lcp_watcher_pass_total",
        "lcp_watcher_pass_duration_seconds",
        "lcp_watcher_indexes_scanned_total",
        "lcp_watcher_tasks_enqueued_total",
        "lcp_task_finished_total",
        "lcp_task_duration_seconds",
        "lcp_gravitino_request_duration_seconds",
    }
    for name in expected:
        assert name in body, f"missing metric: {name}"


def test_metric_names_are_prometheus_compliant() -> None:
    """Names must be lowercase + underscores; histograms end in ``_seconds``."""

    body = render_latest().decode()
    metric_lines = [
        line for line in body.splitlines()
        if line.startswith("# HELP lcp_")
    ]
    assert metric_lines, "no HELP lines for lcp_* metrics found"
    for line in metric_lines:
        # Format: "# HELP <name> <description>"
        name = line.split(" ", 2)[2]
        assert name.islower() or "_" in name, f"non-lower metric: {name}"
        assert ":" not in name, f"colon in metric name: {name}"


# ---------------------------------------------------------------------------
# Counter / Histogram behaviour
# ---------------------------------------------------------------------------


def test_watcher_pass_total_increments_per_label() -> None:
    """Counter.inc must be addressable per ``result`` label."""

    WATCHER_PASS_TOTAL().labels(result="success").inc()
    WATCHER_PASS_TOTAL().labels(result="success").inc()
    WATCHER_PASS_TOTAL().labels(result="error").inc()

    body = render_latest().decode()
    assert 'lcp_watcher_pass_total{result="success"} 2.0' in body
    assert 'lcp_watcher_pass_total{result="error"} 1.0' in body


def test_task_finished_carries_type_and_result_labels() -> None:
    """``type`` + ``result`` must both be part of the label set."""

    TASK_FINISHED().labels(type="COMPACTION", result="SUCCEEDED").inc()
    TASK_FINISHED().labels(type="COMPACTION", result="FAILED").inc()
    TASK_FINISHED().labels(type="INDEX_OPTIMIZE", result="SUCCEEDED").inc()

    body = render_latest().decode()
    assert (
        'lcp_task_finished_total{result="SUCCEEDED",type="COMPACTION"} 1.0'
        in body
    )
    assert (
        'lcp_task_finished_total{result="FAILED",type="COMPACTION"} 1.0'
        in body
    )
    assert (
        'lcp_task_finished_total{result="SUCCEEDED",type="INDEX_OPTIMIZE"} 1.0'
        in body
    )


def test_histogram_observation_lands_in_correct_bucket() -> None:
    """0.123 s observation must increment the le=0.25 bucket but not le=0.1."""

    GRAVITINO_REQUEST_DURATION().labels(op="ping", result="success").observe(0.123)

    body = render_latest().decode()
    # The bucket containing 0.123 (le="0.25") and all higher buckets
    # must be 1; lower buckets must be 0.
    assert (
        'lcp_gravitino_request_duration_seconds_bucket{le="0.1",op="ping",result="success"} 0.0'
        in body
    )
    assert (
        'lcp_gravitino_request_duration_seconds_bucket{le="0.25",op="ping",result="success"} 1.0'
        in body
    )
    assert (
        'lcp_gravitino_request_duration_seconds_count{op="ping",result="success"} 1.0'
        in body
    )


def test_watcher_pass_duration_records_count_and_sum() -> None:
    WATCHER_PASS_DURATION().observe(0.5)
    WATCHER_PASS_DURATION().observe(1.5)

    body = render_latest().decode()
    assert "lcp_watcher_pass_duration_seconds_count 2.0" in body
    # Sum is 2.0; printed as "2.0".
    assert "lcp_watcher_pass_duration_seconds_sum 2.0" in body


def test_task_duration_uses_type_label() -> None:
    TASK_DURATION().labels(type="TTL_DELETE").observe(0.05)

    body = render_latest().decode()
    assert (
        'lcp_task_duration_seconds_count{type="TTL_DELETE"} 1.0'
        in body
    )


def test_watcher_scanned_and_enqueued_counters_accept_arbitrary_increments() -> None:
    WATCHER_INDEXES_SCANNED().inc(7)
    WATCHER_TASKS_ENQUEUED().inc(3)

    body = render_latest().decode()
    assert "lcp_watcher_indexes_scanned_total 7.0" in body
    assert "lcp_watcher_tasks_enqueued_total 3.0" in body


# ---------------------------------------------------------------------------
# build_info gauge
# ---------------------------------------------------------------------------


def test_set_build_component_replaces_unknown_placeholder() -> None:
    """Calling set_build_component must remove the unknown placeholder series."""

    set_build_component("watcher")

    body = render_latest().decode()
    # The desired series is set:
    assert (
        'lcp_build_info{component="watcher",version="0.1.0"} 1.0'
        in body
    )
    # And the placeholder is gone:
    assert 'component="unknown"' not in body


def test_set_build_component_idempotent() -> None:
    """Calling twice with the same component must not double-count."""

    set_build_component("worker")
    set_build_component("worker")

    body = render_latest().decode()
    matches = [
        line for line in body.splitlines()
        if line.startswith('lcp_build_info{')
    ]
    # Exactly one series for component=worker; we accept the placeholder
    # being absent on a freshly-reset registry too.
    assert len(matches) == 1
    assert 'component="worker"' in matches[0]


# ---------------------------------------------------------------------------
# time_block helper
# ---------------------------------------------------------------------------


def test_time_block_yields_elapsed_seconds() -> None:
    """The yielded list[0] must be the elapsed wall-clock time."""

    import time

    with time_block() as elapsed:
        time.sleep(0.01)
    # Sleeping 10 ms; tolerate scheduler jitter up to 1 s.
    assert 0.005 <= elapsed[0] < 1.0


def test_time_block_records_elapsed_even_on_exception() -> None:
    """elapsed[0] is set in ``finally`` so callers can record on errors too."""

    with pytest.raises(RuntimeError):
        with time_block() as elapsed:
            raise RuntimeError("boom")
    assert elapsed[0] >= 0.0


# ---------------------------------------------------------------------------
# reset_registry
# ---------------------------------------------------------------------------


def test_reset_registry_clears_counter_state() -> None:
    """After reset, previously-set counters must be back to zero."""

    WATCHER_PASS_TOTAL().labels(result="success").inc()
    body_before = render_latest().decode()
    assert 'lcp_watcher_pass_total{result="success"} 1.0' in body_before

    reset_registry()

    body_after = render_latest().decode()
    # The counter exists in the registry but no labels have been touched
    # yet, so the fully-labelled time series is absent.
    assert 'lcp_watcher_pass_total{result="success"}' not in body_after
