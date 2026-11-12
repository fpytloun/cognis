"""Sequence rebasing for projection decisions carried across session rotation."""

from __future__ import annotations

from typing import Any


def carried_event_sequence_map(events: list[Any], source_session_id: Any) -> dict[int, int]:
    """Resolve legacy carried references in their original session namespace."""
    if not isinstance(source_session_id, str) or not source_session_id:
        return {}
    return {
        event.data["source_seq"]: event.seq
        for event in events
        if event.data.get("compaction_tail") is True
        and event.data.get("source_session_id") == source_session_id
        and isinstance(event.data.get("source_seq"), int)
        and not isinstance(event.data["source_seq"], bool)
        and event.data["source_seq"] > 0
    }


def remap_projection_event_decisions(
    decisions: list[Any], sequence_map: dict[int, int]
) -> list[dict[str, Any]]:
    """Rebase event-scoped decisions, dropping targets outside the copied tail."""
    result: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if decision.get("operation") != "omit_event":
            result.append(dict(decision))
            continue
        parameters = decision.get("parameters", {})
        source_seq = decision.get("event_seq")
        if source_seq is None and isinstance(parameters, dict):
            source_seq = parameters.get("event_seq")
        if not isinstance(source_seq, int) or isinstance(source_seq, bool):
            continue
        target_seq = sequence_map.get(source_seq)
        if target_seq is None:
            continue
        remapped = {
            **decision,
            "event_seq": target_seq,
            "parameters": {"event_seq": target_seq},
        }
        remapped.pop("call_id", None)
        result.append(remapped)
    return result
