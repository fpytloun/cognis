"""Low-cardinality, fail-open metrics for Work overview revision handling."""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import Counter, Gauge, Histogram

_OVERVIEW_REQUESTS = Counter(
    "cognis_chat_work_overview_requests_total",
    "Activity Overview requests by fixed outcome.",
    ["outcome"],
)
_OVERVIEW_LATENCY = Histogram(
    "cognis_chat_work_overview_latency_seconds",
    "End-to-end Activity Overview request latency.",
    ["outcome"],
)
_OVERVIEW_STAGE_LATENCY = Histogram(
    "cognis_chat_work_overview_stage_latency_seconds",
    "Activity Overview request latency by fixed stage.",
    ["stage"],
)
_REVISION_RECONCILIATIONS = Counter(
    "cognis_chat_work_revision_reconciliations_total",
    "Work revision reconciliation outcomes.",
    ["outcome"],
)
_REVISION_PUBLICATIONS = Counter(
    "cognis_chat_work_revision_publications_total",
    "Work invalidation publication outcomes.",
    ["outcome"],
)
_POST_PROJECTION_CALLBACKS = Counter(
    "cognis_chat_work_post_projection_callbacks_total",
    "Post-projection callback outcomes.",
    ["outcome"],
)
_POST_PROJECTION_COALESCING = Counter(
    "cognis_chat_work_post_projection_coalescing_total",
    "Post-projection callback admission and coalescing outcomes.",
    ["outcome"],
)
_POST_PROJECTION_PENDING = Gauge(
    "cognis_chat_work_post_projection_pending",
    "Pending post-projection conversation callbacks.",
)
_POST_PROJECTION_ACTIVE = Gauge(
    "cognis_chat_work_post_projection_active",
    "Active post-projection conversation callbacks.",
)


class WorkOverviewMetrics:
    """Record only fixed labels and never disrupt the Work request path."""

    _request_outcomes = frozenset({"success", "error", "timeout"})
    _stages = frozenset({"graph", "watermarks", "reconcile", "repository"})
    _revision_outcomes = frozenset({"advanced", "noop"})
    _publication_outcomes = frozenset({"success", "failure", "skipped"})
    _callback_outcomes = frozenset({"success", "failure", "cancelled"})
    _coalescing_outcomes = frozenset(
        {"accepted", "coalesced_pending", "coalesced_active", "overflow", "stopped"}
    )

    @staticmethod
    def _safe(operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception:
            return

    def overview_request(self, outcome: str, seconds: float) -> None:
        if outcome not in self._request_outcomes:
            return
        self._safe(lambda: _OVERVIEW_REQUESTS.labels(outcome=outcome).inc())
        self._safe(lambda: _OVERVIEW_LATENCY.labels(outcome=outcome).observe(max(0.0, seconds)))

    def overview_stage(self, stage: str, seconds: float) -> None:
        if stage not in self._stages:
            return
        self._safe(lambda: _OVERVIEW_STAGE_LATENCY.labels(stage=stage).observe(max(0.0, seconds)))

    def revision(self, advanced: bool) -> None:
        outcome = "advanced" if advanced else "noop"
        self._safe(lambda: _REVISION_RECONCILIATIONS.labels(outcome=outcome).inc())

    def publication(self, outcome: str) -> None:
        if outcome not in self._publication_outcomes:
            return
        self._safe(lambda: _REVISION_PUBLICATIONS.labels(outcome=outcome).inc())

    def callback(self, outcome: str) -> None:
        if outcome not in self._callback_outcomes:
            return
        self._safe(lambda: _POST_PROJECTION_CALLBACKS.labels(outcome=outcome).inc())

    def coalescing(self, outcome: str) -> None:
        if outcome not in self._coalescing_outcomes:
            return
        self._safe(lambda: _POST_PROJECTION_COALESCING.labels(outcome=outcome).inc())

    def coalescer_size(self, *, pending: int, active: int) -> None:
        self._safe(lambda: _POST_PROJECTION_PENDING.set(max(0, pending)))
        self._safe(lambda: _POST_PROJECTION_ACTIVE.set(max(0, active)))


WORK_OVERVIEW_METRICS = WorkOverviewMetrics()

__all__ = ["WORK_OVERVIEW_METRICS", "WorkOverviewMetrics"]
