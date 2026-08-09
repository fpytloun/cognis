"""Content-free metrics for Chat v2 snapshot orchestration."""

from __future__ import annotations

import logging
from typing import Literal

from prometheus_client import Histogram

logger = logging.getLogger(__name__)

SnapshotStage = Literal[
    "watermarks",
    "window_read",
    "postprocess",
    "pairing",
    "projection",
    "total",
]

_STAGES = frozenset(
    {
        "watermarks",
        "window_read",
        "postprocess",
        "pairing",
        "projection",
        "total",
    }
)
_COUNT_BUCKETS = (0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 800, 1_600, 3_200, 6_400)

SNAPSHOT_STAGE_LATENCY = Histogram(
    "cognis_chat_v2_snapshot_stage_latency_seconds",
    "Chat v2 snapshot orchestration stage latency.",
    ["stage"],
)
SNAPSHOT_LINEAGE_SESSIONS = Histogram(
    "cognis_chat_v2_snapshot_lineage_sessions",
    "Sessions in the authoritative lineage for a Chat v2 snapshot.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_SESSIONS_READ = Histogram(
    "cognis_chat_v2_snapshot_sessions_read",
    "Session event pages read for an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_PAGES_READ = Histogram(
    "cognis_chat_v2_snapshot_pages_read",
    "Event-store pages read for an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_EVENTS_FETCHED = Histogram(
    "cognis_chat_v2_snapshot_events_fetched",
    "Events fetched for an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_EVENTS_SELECTED = Histogram(
    "cognis_chat_v2_snapshot_events_selected",
    "Events selected for an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_EVENTS_DISCARDED = Histogram(
    "cognis_chat_v2_snapshot_events_discarded",
    "Fetched events outside an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)
SNAPSHOT_READ_ROUNDS = Histogram(
    "cognis_chat_v2_snapshot_read_rounds",
    "Adaptive event-store read rounds for an uncached Chat v2 snapshot window.",
    buckets=_COUNT_BUCKETS,
)


class SnapshotSyncMetrics:
    """Fail-open facade with a fixed, content-free metric vocabulary."""

    def observe_stage(self, stage: SnapshotStage, seconds: float) -> None:
        try:
            if stage not in _STAGES:
                raise ValueError(f"unknown snapshot stage: {stage!r}")
            SNAPSHOT_STAGE_LATENCY.labels(stage=stage).observe(seconds)
        except Exception:
            logger.debug("Failed to record Chat v2 snapshot stage metric", exc_info=True)

    def observe_lineage(self, sessions: int) -> None:
        self._observe(SNAPSHOT_LINEAGE_SESSIONS, sessions)

    def observe_window(
        self,
        *,
        sessions_read: int,
        pages_read: int,
        events_fetched: int,
        events_selected: int,
        events_discarded: int,
        rounds: int,
    ) -> None:
        observations = (
            (SNAPSHOT_SESSIONS_READ, sessions_read),
            (SNAPSHOT_PAGES_READ, pages_read),
            (SNAPSHOT_EVENTS_FETCHED, events_fetched),
            (SNAPSHOT_EVENTS_SELECTED, events_selected),
            (SNAPSHOT_EVENTS_DISCARDED, events_discarded),
            (SNAPSHOT_READ_ROUNDS, rounds),
        )
        for metric, value in observations:
            self._observe(metric, value)

    @staticmethod
    def _observe(metric: Histogram, value: int) -> None:
        try:
            metric.observe(value)
        except Exception:
            logger.debug("Failed to record Chat v2 snapshot metric", exc_info=True)


SNAPSHOT_SYNC_METRICS = SnapshotSyncMetrics()

__all__ = [
    "SNAPSHOT_SYNC_METRICS",
    "SnapshotStage",
    "SnapshotSyncMetrics",
]
