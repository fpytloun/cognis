"""Assertions and reports shared by opt-in Redis HA E2E scenarios."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RedisHAPerformanceReport:
    owner_local_first_frame_ms: float | None = None
    remote_first_frame_delay_ms: float | None = None
    observer_enqueue_duration_ms: float | None = None
    relay_queue_depth_max: int | None = None
    relay_payload_bytes: list[int] = field(default_factory=list)
    intaris_requests_by_query: dict[str, int] = field(default_factory=dict)
    ownership_validation_rate: float | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def visible_item_ids(events: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for event in events:
        if event.get("type") != "chat_v2_frame":
            continue
        for operation in event.get("ops", []):
            item = operation.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
        runtime = event.get("runtime")
        if isinstance(runtime, dict):
            for item in runtime.get("volatile_items", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.append(item["id"])
    return ids


def assert_remote_progress_contract(events: list[dict[str, Any]]) -> None:
    frames = [event for event in events if event.get("type") == "chat_v2_frame"]
    assert frames, "remote controller received no Chat v2 frames"
    payload = str(frames)
    assert '"has_active_turn": True' in payload or "'has_active_turn': True" in payload
    assert any(
        token in payload for token in ("thinking", "tool_call", "tool_progress", "assistant")
    ), "remote controller received no runtime assistant/thinking/tool progress"
    for frame in frames:
        ids = visible_item_ids([frame])
        assert len(ids) == len(set(ids)), "one remote frame contained duplicate visible item IDs"


def assert_remote_terminal_contract(events: list[dict[str, Any]]) -> None:
    terminal_frames = [
        event
        for event in events
        if event.get("type") == "chat_v2_frame"
        and isinstance(event.get("runtime"), dict)
        and event["runtime"].get("has_active_turn") is False
    ]
    assert terminal_frames, "remote controller received no terminal Chat v2 frame"
    assert any(
        item.get("kind") == "message" and item.get("role") == "assistant"
        for frame in terminal_frames
        for item in frame["runtime"].get("volatile_items", [])
        if isinstance(item, dict)
    ), "terminal Chat v2 frame contained no final assistant item"


def assert_final_exactly_once(items: list[dict[str, Any]]) -> None:
    final_assistants = [
        item
        for item in items
        if item.get("kind") == "message"
        and item.get("role") == "assistant"
        and item.get("status") in {None, "completed", "final"}
    ]
    assert len(final_assistants) == 1, (
        f"expected one canonical assistant completion, got {len(final_assistants)}"
    )


def assert_read_amplification(
    *,
    warm_counts: dict[str, int],
    outage_counts: dict[str, int],
) -> None:
    assert warm_counts, "Intaris request counter returned no production-path queries"
    assert set(warm_counts) <= set(outage_counts)
    for query, warm_count in warm_counts.items():
        assert warm_count == 0, f"warm Redis unexpectedly read Intaris for {query}: {warm_count}"
        assert outage_counts[query] > warm_count, (
            f"Redis outage did not fall back to Intaris for {query}"
        )
