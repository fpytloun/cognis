"""Tests for content-free, fail-open Chat v2 snapshot metrics."""

from __future__ import annotations

from typing import Any, cast

from cognis.api.chat_v2 import sync_metrics


def test_snapshot_metrics_have_only_one_bounded_stage_label() -> None:
    assert sync_metrics.SNAPSHOT_STAGE_LATENCY._labelnames == ("stage",)
    assert sync_metrics.SNAPSHOT_LINEAGE_SESSIONS._labelnames == ()
    assert sync_metrics.SNAPSHOT_SESSIONS_READ._labelnames == ()
    assert sync_metrics.SNAPSHOT_PAGES_READ._labelnames == ()
    assert sync_metrics.SNAPSHOT_EVENTS_FETCHED._labelnames == ()
    assert sync_metrics.SNAPSHOT_EVENTS_SELECTED._labelnames == ()
    assert sync_metrics.SNAPSHOT_EVENTS_DISCARDED._labelnames == ()
    assert sync_metrics.SNAPSHOT_READ_ROUNDS._labelnames == ()


def test_snapshot_metrics_fail_open_for_invalid_stage() -> None:
    metrics = sync_metrics.SnapshotSyncMetrics()

    metrics.observe_stage(cast(Any, "conversation-id"), 0.1)


def test_snapshot_metrics_fail_open_when_prometheus_observation_fails(
    monkeypatch: Any,
) -> None:
    class BrokenHistogram:
        def labels(self, **kwargs: object) -> BrokenHistogram:
            del kwargs
            raise RuntimeError("metrics unavailable")

        def observe(self, value: object) -> None:
            del value
            raise RuntimeError("metrics unavailable")

    broken = BrokenHistogram()
    monkeypatch.setattr(sync_metrics, "SNAPSHOT_STAGE_LATENCY", broken)
    monkeypatch.setattr(sync_metrics, "SNAPSHOT_LINEAGE_SESSIONS", broken)

    metrics = sync_metrics.SnapshotSyncMetrics()
    metrics.observe_stage("total", 0.1)
    metrics.observe_lineage(18)
