from __future__ import annotations

from typing import Any

from cognis.api.chat_v2 import work_overview_metrics


class _Metric:
    def __init__(self) -> None:
        self.labels_seen: list[dict[str, str]] = []

    def labels(self, **labels: str) -> _Metric:
        self.labels_seen.append(labels)
        return self

    def inc(self) -> None:
        return None

    def observe(self, _value: float) -> None:
        return None

    def set(self, _value: int) -> None:
        return None


class _BrokenMetric:
    def labels(self, **_labels: str) -> _BrokenMetric:
        raise RuntimeError("metrics unavailable")

    def set(self, _value: int) -> None:
        raise RuntimeError("metrics unavailable")


def test_work_overview_metrics_use_only_fixed_labels(monkeypatch: Any) -> None:
    metrics_by_name = {
        name: _Metric()
        for name in (
            "_OVERVIEW_REQUESTS",
            "_OVERVIEW_LATENCY",
            "_OVERVIEW_STAGE_LATENCY",
            "_REVISION_RECONCILIATIONS",
            "_REVISION_PUBLICATIONS",
            "_POST_PROJECTION_CALLBACKS",
            "_POST_PROJECTION_COALESCING",
            "_POST_PROJECTION_PENDING",
            "_POST_PROJECTION_ACTIVE",
        )
    }
    for name, metric in metrics_by_name.items():
        monkeypatch.setattr(work_overview_metrics, name, metric)

    metrics = work_overview_metrics.WorkOverviewMetrics()
    metrics.overview_request("success", 0.1)
    metrics.overview_stage("graph", 0.01)
    metrics.revision(True)
    metrics.publication("skipped")
    metrics.callback("failure")
    metrics.coalescing("coalesced_active")
    metrics.coalescer_size(pending=2, active=1)
    metrics.overview_stage("conversation-123", 1)
    metrics.publication("owner@example.com")

    assert metrics_by_name["_OVERVIEW_REQUESTS"].labels_seen == [{"outcome": "success"}]
    assert metrics_by_name["_OVERVIEW_LATENCY"].labels_seen == [{"outcome": "success"}]
    assert metrics_by_name["_OVERVIEW_STAGE_LATENCY"].labels_seen == [{"stage": "graph"}]
    assert metrics_by_name["_REVISION_RECONCILIATIONS"].labels_seen == [{"outcome": "advanced"}]
    assert metrics_by_name["_REVISION_PUBLICATIONS"].labels_seen == [{"outcome": "skipped"}]
    assert metrics_by_name["_POST_PROJECTION_CALLBACKS"].labels_seen == [{"outcome": "failure"}]
    assert metrics_by_name["_POST_PROJECTION_COALESCING"].labels_seen == [
        {"outcome": "coalesced_active"}
    ]


def test_work_overview_metrics_fail_open_when_label_lookup_fails(
    monkeypatch: Any,
) -> None:
    broken = _BrokenMetric()
    for name in (
        "_OVERVIEW_REQUESTS",
        "_OVERVIEW_LATENCY",
        "_OVERVIEW_STAGE_LATENCY",
        "_REVISION_RECONCILIATIONS",
        "_REVISION_PUBLICATIONS",
        "_POST_PROJECTION_CALLBACKS",
        "_POST_PROJECTION_COALESCING",
        "_POST_PROJECTION_PENDING",
        "_POST_PROJECTION_ACTIVE",
    ):
        monkeypatch.setattr(work_overview_metrics, name, broken)

    metrics = work_overview_metrics.WorkOverviewMetrics()
    metrics.overview_request("success", 0.1)
    metrics.overview_stage("graph", 0.01)
    metrics.revision(True)
    metrics.publication("success")
    metrics.callback("failure")
    metrics.coalescing("accepted")
    metrics.coalescer_size(pending=1, active=1)
