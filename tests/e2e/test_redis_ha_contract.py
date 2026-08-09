"""Structural tests for the opt-in live Redis HA qualification contract."""

from __future__ import annotations

import pytest

from tests.e2e.redis_ha_contract import (
    RedisHAPerformanceReport,
    assert_final_exactly_once,
    assert_read_amplification,
    assert_remote_progress_contract,
)


def test_remote_progress_contract_rejects_duplicate_items() -> None:
    events = [
        {
            "type": "chat_v2_frame",
            "runtime": {
                "has_active_turn": True,
                "items": [
                    {"id": "thinking-1", "kind": "thinking"},
                    {"id": "thinking-1", "kind": "thinking"},
                ],
            },
            "ops": [],
        },
    ]
    with pytest.raises(AssertionError, match="duplicate"):
        assert_remote_progress_contract(events)


def test_remote_progress_and_final_contract_accepts_structural_capture() -> None:
    assert_remote_progress_contract(
        [
            {
                "type": "chat_v2_frame",
                "runtime": {
                    "has_active_turn": True,
                    "items": [{"id": "thinking-1", "kind": "thinking"}],
                },
                "ops": [],
            },
            {
                "type": "chat_v2_frame",
                "runtime": {
                    "has_active_turn": True,
                    "items": [{"id": "assistant-1", "kind": "assistant"}],
                },
                "ops": [],
            },
        ]
    )
    assert_final_exactly_once(
        [{"id": "message-1", "kind": "message", "role": "assistant", "status": "completed"}]
    )


def test_read_amplification_requires_real_outage_fallback_counts() -> None:
    assert_read_amplification(
        warm_counts={"session-1?after_seq=20": 0},
        outage_counts={"session-1?after_seq=20": 20},
    )
    with pytest.raises(AssertionError, match="did not fall back"):
        assert_read_amplification(
            warm_counts={"session-1?after_seq=20": 0},
            outage_counts={"session-1?after_seq=20": 0},
        )


def test_performance_report_has_non_brittle_capture_fields() -> None:
    report = RedisHAPerformanceReport(
        owner_local_first_frame_ms=12.5,
        remote_first_frame_delay_ms=4.0,
        observer_enqueue_duration_ms=0.3,
        relay_queue_depth_max=2,
        relay_payload_bytes=[420, 880],
        intaris_requests_by_query={"session-1?after_seq=20": 0},
        ownership_validation_rate=1.0,
    ).as_json()
    assert set(report) == {
        "owner_local_first_frame_ms",
        "remote_first_frame_delay_ms",
        "observer_enqueue_duration_ms",
        "relay_queue_depth_max",
        "relay_payload_bytes",
        "intaris_requests_by_query",
        "ownership_validation_rate",
    }
