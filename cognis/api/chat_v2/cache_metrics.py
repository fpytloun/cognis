"""Content-free metrics for canonical Chat v2 event-read caching."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from prometheus_client import Counter, Gauge, Histogram

CacheTier = Literal["local", "redis", "upstream"]
CacheOperation = Literal["page", "watermark"]
PageQueryClass = Literal[
    "forward_delta",
    "initial_forward",
    "backward_tail",
    "historical_backward",
]
DecodeFailureReason = Literal[
    "wire",
    "json",
    "envelope",
    "schema",
    "value",
    "size",
    "decompression",
]
CompressionOutcome = Literal[
    "compressed",
    "disabled",
    "below_threshold",
    "not_smaller",
    "error",
    "raw_oversized",
    "stored_oversized",
]
RefreshTier = Literal["local", "redis"]
InvalidationSource = Literal[
    "append",
    "cluster_signal",
    "generation_change",
    "local_eviction",
]
BypassReason = Literal[
    "cache_disabled",
    "redis_unavailable",
    "oversized",
    "codec_saturated",
    "generation_changed",
    "unverified_empty",
    "redis_value_disabled",
]

_TIERS = frozenset({"local", "redis", "upstream"})
_OPERATIONS = frozenset({"page", "watermark"})
_PAGE_QUERY_CLASSES = frozenset(
    {
        "forward_delta",
        "initial_forward",
        "backward_tail",
        "historical_backward",
    }
)
_DECODE_FAILURE_REASONS = frozenset(
    {"wire", "json", "envelope", "schema", "value", "size", "decompression"}
)
_COMPRESSION_OUTCOMES = frozenset(
    {
        "compressed",
        "disabled",
        "below_threshold",
        "not_smaller",
        "error",
        "raw_oversized",
        "stored_oversized",
    }
)
_REFRESH_TIERS = frozenset({"local", "redis"})
_INVALIDATION_SOURCES = frozenset(
    {"append", "cluster_signal", "generation_change", "local_eviction"}
)
_BYPASS_REASONS = frozenset(
    {
        "cache_disabled",
        "redis_unavailable",
        "oversized",
        "codec_saturated",
        "generation_changed",
        "unverified_empty",
        "redis_value_disabled",
    }
)

CACHE_HITS = Counter(
    "cognis_event_read_cache_hits_total",
    "Canonical event cache hits.",
    ["tier", "operation"],
)
CACHE_MISSES = Counter(
    "cognis_event_read_cache_misses_total",
    "Canonical event cache misses.",
    ["tier", "operation"],
)
CACHE_ERRORS = Counter(
    "cognis_event_read_cache_errors_total",
    "Canonical event cache errors.",
    ["tier", "operation"],
)
SINGLEFLIGHT_JOINS = Counter(
    "cognis_event_read_cache_singleflight_joins_total",
    "Canonical event cache singleflight joins.",
    ["operation"],
)
UPSTREAM_READS = Counter(
    "cognis_event_read_cache_upstream_reads_total",
    "Canonical event-store reads.",
    ["operation"],
)
UPSTREAM_LATENCY = Histogram(
    "cognis_event_read_cache_upstream_latency_seconds",
    "Canonical event-store read latency.",
    ["operation"],
)
INVALIDATIONS = Counter(
    "cognis_event_read_cache_invalidations_total",
    "Canonical event cache invalidations.",
    ["source"],
)
BYPASSED = Counter(
    "cognis_event_read_cache_bypassed_total",
    "Canonical event reads bypassed by reason.",
    ["reason"],
)
CACHE_ENTRIES = Gauge(
    "cognis_event_read_cache_entries",
    "Resident canonical event cache entries.",
)
CACHE_BYTES = Gauge(
    "cognis_event_read_cache_bytes",
    "Estimated resident canonical event cache bytes.",
)
RAW_PAYLOAD_BYTES = Histogram(
    "cognis_event_read_cache_raw_payload_bytes",
    "Raw canonical event cache payload size before optional compression.",
    buckets=(
        1024,
        4 * 1024,
        16 * 1024,
        64 * 1024,
        256 * 1024,
        1024 * 1024,
        2 * 1024 * 1024,
        4 * 1024 * 1024,
        8 * 1024 * 1024,
        16 * 1024 * 1024,
        64 * 1024 * 1024,
    ),
)
STORED_PAYLOAD_BYTES = Histogram(
    "cognis_event_read_cache_stored_payload_bytes",
    "Canonical event cache payload size stored in Redis and L1.",
    buckets=(
        1024,
        4 * 1024,
        16 * 1024,
        64 * 1024,
        256 * 1024,
        1024 * 1024,
        2 * 1024 * 1024,
    ),
)
COMPRESSION_RATIO = Histogram(
    "cognis_event_read_cache_compression_ratio",
    "Stored-to-raw canonical event cache payload ratio for compressed values.",
    buckets=(0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0),
)
COMPRESSION_OUTCOMES = Counter(
    "cognis_event_read_cache_compression_outcomes_total",
    "Canonical event cache compression outcomes.",
    ["outcome"],
)
SLIDING_REFRESHES = Counter(
    "cognis_event_read_cache_sliding_refreshes_total",
    "Canonical event cache sliding expiration refreshes.",
    ["tier"],
)
SLIDING_REFRESH_ERRORS = Counter(
    "cognis_event_read_cache_sliding_refresh_errors_total",
    "Canonical event cache sliding expiration refresh errors.",
    ["tier"],
)
PAGE_QUERIES = Counter(
    "cognis_event_read_cache_page_queries_total",
    "Canonical page reads by fixed query class.",
    ["query_class"],
)
DECODE_FAILURES = Counter(
    "cognis_event_read_cache_decode_failures_total",
    "Canonical event cache decode failures by fixed reason.",
    ["reason"],
)


def _require[Label: str](value: Label, allowed: frozenset[str], label: str) -> Label:
    if value not in allowed:
        raise ValueError(f"unknown {label}: {value!r}")
    return value


def _record(operation: Callable[[], None]) -> None:
    """Keep metric backend failures out of canonical cache behavior."""

    try:
        operation()
    except Exception:
        return


class EventCacheMetrics:
    """Typed metric facade that rejects high-cardinality label additions."""

    def hit(self, tier: CacheTier, operation: CacheOperation) -> None:
        tier = _require(tier, _TIERS, "tier")
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: CACHE_HITS.labels(tier=tier, operation=operation).inc())

    def miss(self, tier: CacheTier, operation: CacheOperation) -> None:
        tier = _require(tier, _TIERS, "tier")
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: CACHE_MISSES.labels(tier=tier, operation=operation).inc())

    def error(self, tier: CacheTier, operation: CacheOperation) -> None:
        tier = _require(tier, _TIERS, "tier")
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: CACHE_ERRORS.labels(tier=tier, operation=operation).inc())

    def singleflight_join(self, operation: CacheOperation) -> None:
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: SINGLEFLIGHT_JOINS.labels(operation=operation).inc())

    def upstream_read(self, operation: CacheOperation) -> None:
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: UPSTREAM_READS.labels(operation=operation).inc())

    def observe_upstream(self, operation: CacheOperation, seconds: float) -> None:
        operation = _require(operation, _OPERATIONS, "operation")
        _record(lambda: UPSTREAM_LATENCY.labels(operation=operation).observe(seconds))

    def invalidation(self, source: InvalidationSource) -> None:
        source = _require(source, _INVALIDATION_SOURCES, "invalidation source")
        _record(lambda: INVALIDATIONS.labels(source=source).inc())

    def bypass(self, reason: BypassReason) -> None:
        reason = _require(reason, _BYPASS_REASONS, "bypass reason")
        _record(lambda: BYPASSED.labels(reason=reason).inc())

    def resident(self, entries: int, estimated_bytes: int) -> None:
        _record(lambda: CACHE_ENTRIES.set(entries))
        _record(lambda: CACHE_BYTES.set(estimated_bytes))

    def payload_sizes(self, raw_bytes: int, stored_bytes: int) -> None:
        _record(lambda: RAW_PAYLOAD_BYTES.observe(raw_bytes))
        _record(lambda: STORED_PAYLOAD_BYTES.observe(stored_bytes))

    def compression(
        self,
        outcome: CompressionOutcome,
        *,
        ratio: float | None = None,
    ) -> None:
        outcome = _require(outcome, _COMPRESSION_OUTCOMES, "compression outcome")
        _record(lambda: COMPRESSION_OUTCOMES.labels(outcome=outcome).inc())
        if ratio is not None:
            _record(lambda: COMPRESSION_RATIO.observe(ratio))

    def sliding_refresh(self, tier: RefreshTier) -> None:
        tier = _require(tier, _REFRESH_TIERS, "refresh tier")
        _record(lambda: SLIDING_REFRESHES.labels(tier=tier).inc())

    def sliding_refresh_error(self, tier: RefreshTier) -> None:
        tier = _require(tier, _REFRESH_TIERS, "refresh tier")
        _record(lambda: SLIDING_REFRESH_ERRORS.labels(tier=tier).inc())

    def page_query(self, query_class: PageQueryClass) -> None:
        query_class = _require(query_class, _PAGE_QUERY_CLASSES, "page query class")
        _record(lambda: PAGE_QUERIES.labels(query_class=query_class).inc())

    def decode_failure(self, reason: DecodeFailureReason) -> None:
        reason = _require(reason, _DECODE_FAILURE_REASONS, "decode failure reason")
        _record(lambda: DECODE_FAILURES.labels(reason=reason).inc())


EVENT_CACHE_METRICS = EventCacheMetrics()


__all__ = [
    "BypassReason",
    "CacheOperation",
    "CacheTier",
    "CompressionOutcome",
    "DecodeFailureReason",
    "EVENT_CACHE_METRICS",
    "EventCacheMetrics",
    "InvalidationSource",
    "PageQueryClass",
    "RefreshTier",
]
