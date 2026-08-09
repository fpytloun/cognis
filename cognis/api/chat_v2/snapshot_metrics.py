"""Low-cardinality metrics for shared Chat v2 snapshot caching and warming."""

from __future__ import annotations

from typing import Literal

from prometheus_client import Counter, Gauge, Histogram

SnapshotRequestTier = Literal["unknown", "l1", "redis", "build", "bypass"]
SnapshotRequestOutcome = Literal["success", "error"]
WarmFailureReason = Literal[
    "context_missing",
    "context_changed",
    "redis_unavailable",
    "lock_lost",
    "oversized",
    "internal",
]

_HITS = Counter(
    "cognis_chat_snapshot_cache_hits_total",
    "Shared canonical ChatSnapshot cache hits.",
    ["tier"],
)
_MISSES = Counter(
    "cognis_chat_snapshot_cache_misses_total",
    "Shared canonical ChatSnapshot cache misses.",
)
_BUILDS = Counter(
    "cognis_chat_snapshot_cache_builds_total",
    "Canonical ChatSnapshot projection builds.",
)
_LOCK_WAITS = Counter(
    "cognis_chat_snapshot_cache_lock_waits_total",
    "Shared snapshot requests waiting for a distributed rebuild owner.",
)
_LOCK_FALLBACKS = Counter(
    "cognis_chat_snapshot_cache_lock_fallbacks_total",
    "Shared snapshot rebuild ownership recovered after lease loss or expiry.",
)
_REDIS_BYPASSES = Counter(
    "cognis_chat_snapshot_cache_redis_bypasses_total",
    "Shared snapshot requests bypassing Redis after unavailability.",
)
_INVALID = Counter(
    "cognis_chat_snapshot_cache_invalid_total",
    "Shared snapshots not admitted because they were oversized or invalid.",
)
_OVERFLOW = Counter(
    "cognis_chat_snapshot_cache_overflow_total",
    "Bounded shared snapshot structures rejecting additional work.",
    ["structure"],
)
_CODEC_SATURATED = Counter(
    "cognis_chat_snapshot_cache_codec_saturated_total",
    "Shared snapshot reads bypassed because bounded codec workers were saturated.",
)
WARM_EVENTS = Counter(
    "cognis_chat_snapshot_warm_total",
    "Application-scoped snapshot warming lifecycle events.",
    ["outcome"],
)
WARM_LAG = Histogram(
    "cognis_chat_snapshot_warm_lag_seconds",
    "Delay from a warm request to successful canonical snapshot refresh.",
)
REQUEST_LATENCY = Histogram(
    "cognis_chat_snapshot_request_latency_seconds",
    "End-to-end Chat v2 snapshot request latency by returned path.",
    ["tier", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
WARM_FAILURES = Counter(
    "cognis_chat_snapshot_warm_failures_total",
    "Failed Chat v2 snapshot warm attempts by fixed reason.",
    ["reason"],
)
L1_ENTRIES = Gauge(
    "cognis_chat_snapshot_cache_l1_entries",
    "Process-local resident Chat v2 snapshot L1 entries.",
)
L1_BYTES = Gauge(
    "cognis_chat_snapshot_cache_l1_bytes",
    "Process-local estimated resident Chat v2 snapshot L1 bytes.",
)
INFLIGHT_BUILDS = Gauge(
    "cognis_chat_snapshot_cache_inflight_builds",
    "Process-local in-flight Chat v2 snapshot builds.",
)
OWNED_LOCKS = Gauge(
    "cognis_chat_snapshot_cache_owned_locks",
    "Process-local distributed Chat v2 snapshot locks currently owned.",
)
WARMER_PENDING = Gauge(
    "cognis_chat_snapshot_warmer_pending",
    "Process-local pending Chat v2 snapshot warm requests.",
)
WARMER_ACTIVE = Gauge(
    "cognis_chat_snapshot_warmer_active",
    "Process-local active Chat v2 snapshot warm requests.",
)
APPEND_MAPPING_ENTRIES = Gauge(
    "cognis_chat_snapshot_append_mapping_entries",
    "Process-local append-to-conversation mapping entries awaiting resolution.",
)
RESOLVER_ACTIVE = Gauge(
    "cognis_chat_snapshot_resolver_active",
    "Process-local active append-to-conversation resolvers.",
)
REDIS_VALUE_BYTES = Histogram(
    "cognis_chat_snapshot_redis_value_bytes",
    "Observed encoded Chat v2 snapshot value size read from or written to Redis.",
    buckets=(1024, 4096, 16384, 65536, 262144, 1048576, 2097152, 4194304),
)
CLIENT_PERFORMANCE_MS = Histogram(
    "cognis_chat_v2_client_performance_milliseconds",
    "Untrusted best-effort authenticated browser Chat v2 UX timing trends.",
    ["metric"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
)


class SnapshotCacheMetrics:
    def hit(self, tier: str) -> None:
        if tier not in {"l1", "redis"}:
            raise ValueError("invalid snapshot cache tier")
        _HITS.labels(tier=tier).inc()

    def miss(self) -> None:
        _MISSES.inc()

    def build(self) -> None:
        _BUILDS.inc()

    def lock_wait(self) -> None:
        _LOCK_WAITS.inc()

    def lock_fallback(self) -> None:
        _LOCK_FALLBACKS.inc()

    def redis_bypass(self) -> None:
        _REDIS_BYPASSES.inc()

    def oversized_or_invalid(self) -> None:
        _INVALID.inc()

    def overflow(self, structure: str) -> None:
        if structure not in {
            "inflight",
            "l1_index",
            "warmer",
            "append_mapping",
            "resolver",
        }:
            raise ValueError("invalid snapshot overflow structure")
        _OVERFLOW.labels(structure=structure).inc()

    def codec_saturated(self) -> None:
        _CODEC_SATURATED.inc()

    def request(
        self,
        tier: SnapshotRequestTier,
        outcome: SnapshotRequestOutcome,
        seconds: float,
    ) -> None:
        if tier not in {"unknown", "l1", "redis", "build", "bypass"}:
            raise ValueError("invalid snapshot request tier")
        if outcome not in {"success", "error"}:
            raise ValueError("invalid snapshot request outcome")
        REQUEST_LATENCY.labels(tier=tier, outcome=outcome).observe(seconds)

    def warm_failure(self, reason: WarmFailureReason) -> None:
        if reason not in {
            "context_missing",
            "context_changed",
            "redis_unavailable",
            "lock_lost",
            "oversized",
            "internal",
        }:
            raise ValueError("invalid snapshot warm failure reason")
        WARM_FAILURES.labels(reason=reason).inc()

    def l1_resident(self, entries: int, estimated_bytes: int) -> None:
        L1_ENTRIES.set(entries)
        L1_BYTES.set(estimated_bytes)

    def inflight_builds(self, count: int) -> None:
        INFLIGHT_BUILDS.set(count)

    def owned_locks(self, count: int) -> None:
        OWNED_LOCKS.set(count)

    def warmer(self, pending: int, active: int) -> None:
        WARMER_PENDING.set(pending)
        WARMER_ACTIVE.set(active)

    def append_mapping(self, count: int) -> None:
        APPEND_MAPPING_ENTRIES.set(count)

    def resolver_active(self, count: int) -> None:
        RESOLVER_ACTIVE.set(count)

    def redis_value(self, size_bytes: int) -> None:
        REDIS_VALUE_BYTES.observe(size_bytes)

    def client_performance(self, metric: str, duration_ms: float) -> None:
        if metric not in {"cached_restore_ms", "timeline_fresh_ms"}:
            raise ValueError("invalid client performance metric")
        CLIENT_PERFORMANCE_MS.labels(metric=metric).observe(duration_ms)


SNAPSHOT_CACHE_METRICS = SnapshotCacheMetrics()
