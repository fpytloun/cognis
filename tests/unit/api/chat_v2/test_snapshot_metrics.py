from prometheus_client import generate_latest

from cognis.api.chat_v2.snapshot_metrics import SNAPSHOT_CACHE_METRICS


def test_snapshot_metrics_are_exposed_with_fixed_labels_and_process_gauges() -> None:
    SNAPSHOT_CACHE_METRICS.request("l1", "success", 0.01)
    SNAPSHOT_CACHE_METRICS.warm_failure("internal")
    SNAPSHOT_CACHE_METRICS.l1_resident(2, 128)
    SNAPSHOT_CACHE_METRICS.inflight_builds(1)
    SNAPSHOT_CACHE_METRICS.owned_locks(1)
    SNAPSHOT_CACHE_METRICS.warmer(3, 2)
    SNAPSHOT_CACHE_METRICS.append_mapping(4)
    SNAPSHOT_CACHE_METRICS.resolver_active(2)
    SNAPSHOT_CACHE_METRICS.redis_value(1024)
    SNAPSHOT_CACHE_METRICS.client_performance("cached_restore_ms", 25)

    exposition = generate_latest().decode()

    assert (
        'cognis_chat_snapshot_request_latency_seconds_count{outcome="success",tier="l1"}'
        in exposition
    )
    assert (
        'cognis_chat_snapshot_request_latency_seconds_bucket{le="120.0",outcome="success",tier="l1"}'
        in exposition
    )
    assert 'cognis_chat_snapshot_warm_failures_total{reason="internal"}' in exposition
    assert "cognis_chat_snapshot_cache_l1_entries 2.0" in exposition
    assert "cognis_chat_snapshot_cache_l1_bytes 128.0" in exposition
    assert "cognis_chat_snapshot_cache_inflight_builds 1.0" in exposition
    assert "cognis_chat_snapshot_cache_owned_locks 1.0" in exposition
    assert "cognis_chat_snapshot_warmer_pending 3.0" in exposition
    assert "cognis_chat_snapshot_warmer_active 2.0" in exposition
    assert "cognis_chat_snapshot_append_mapping_entries 4.0" in exposition
    assert "cognis_chat_snapshot_resolver_active 2.0" in exposition
    assert "cognis_chat_snapshot_redis_value_bytes_count" in exposition
    assert (
        'cognis_chat_v2_client_performance_milliseconds_count{metric="cached_restore_ms"}'
        in exposition
    )

    SNAPSHOT_CACHE_METRICS.l1_resident(0, 0)
    SNAPSHOT_CACHE_METRICS.inflight_builds(0)
    SNAPSHOT_CACHE_METRICS.owned_locks(0)
    SNAPSHOT_CACHE_METRICS.warmer(0, 0)
    SNAPSHOT_CACHE_METRICS.append_mapping(0)
    SNAPSHOT_CACHE_METRICS.resolver_active(0)
