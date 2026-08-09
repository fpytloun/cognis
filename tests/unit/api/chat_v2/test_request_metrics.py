"""Tests for fixed-cardinality, fail-open Chat v2 request metrics."""

from __future__ import annotations

from typing import Any, cast

from cognis.api.chat_v2 import request_metrics


def test_request_metrics_use_only_fixed_outcome_labels() -> None:
    assert request_metrics.CACHE_ONLY_LATENCY._labelnames == ("outcome",)
    assert request_metrics.SYNC_LATENCY._labelnames == ("outcome",)


def test_request_metrics_fail_open_for_invalid_outcomes() -> None:
    metrics = request_metrics.ChatV2RequestMetrics()

    metrics.cache_only(cast(Any, "conversation-id"), 0.1)
    metrics.sync(cast(Any, "cursor-value"), 0.1)


def test_request_metrics_fail_open_when_prometheus_fails(monkeypatch: Any) -> None:
    class BrokenHistogram:
        def labels(self, **kwargs: object) -> BrokenHistogram:
            del kwargs
            raise RuntimeError("metrics unavailable")

    broken = BrokenHistogram()
    monkeypatch.setattr(request_metrics, "CACHE_ONLY_LATENCY", broken)
    monkeypatch.setattr(request_metrics, "SYNC_LATENCY", broken)

    metrics = request_metrics.ChatV2RequestMetrics()
    metrics.cache_only("hit_l1", 0.1)
    metrics.sync("success", 0.1)
