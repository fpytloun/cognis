"""Fail-open, fixed-cardinality metrics for Chat v2 refresh requests."""

from __future__ import annotations

import logging
from typing import Literal

from prometheus_client import Histogram

logger = logging.getLogger(__name__)

CacheOnlyOutcome = Literal[
    "hit_l1",
    "hit_redis",
    "miss",
    "unavailable",
    "ineligible",
    "error",
]
SyncOutcome = Literal["success", "reset", "error"]

_CACHE_ONLY_OUTCOMES = frozenset(
    {"hit_l1", "hit_redis", "miss", "unavailable", "ineligible", "error"}
)
_SYNC_OUTCOMES = frozenset({"success", "reset", "error"})

CACHE_ONLY_LATENCY = Histogram(
    "cognis_chat_v2_snapshot_cache_only_latency_seconds",
    "Chat v2 cache-only snapshot request latency.",
    ["outcome"],
)
SYNC_LATENCY = Histogram(
    "cognis_chat_v2_sync_request_latency_seconds",
    "Chat v2 incremental sync request latency.",
    ["outcome"],
)


class ChatV2RequestMetrics:
    def cache_only(self, outcome: CacheOnlyOutcome, seconds: float) -> None:
        self._observe(CACHE_ONLY_LATENCY, outcome, _CACHE_ONLY_OUTCOMES, seconds)

    def sync(self, outcome: SyncOutcome, seconds: float) -> None:
        self._observe(SYNC_LATENCY, outcome, _SYNC_OUTCOMES, seconds)

    @staticmethod
    def _observe(
        metric: Histogram,
        outcome: str,
        allowed: frozenset[str],
        seconds: float,
    ) -> None:
        try:
            if outcome not in allowed:
                raise ValueError(f"invalid Chat v2 request outcome: {outcome!r}")
            metric.labels(outcome=outcome).observe(seconds)
        except Exception:
            logger.debug("Failed to record Chat v2 request metric", exc_info=True)


CHAT_V2_REQUEST_METRICS = ChatV2RequestMetrics()
