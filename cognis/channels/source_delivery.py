"""Intaris-owned source references for continuation delivery.

An intended send is excluded from replay even if its acknowledgement is lost.
Only committed assistant events from explicitly tracked turns are eligible.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cognis.models.session import SessionEvent, next_event_page_after_seq


async def record_state(guardrails: Any, session_id: str, **data: Any) -> None:
    result = await guardrails.record_events(
        session_id=session_id,
        events=[SessionEvent(type="lifecycle", data={"event": "channel_source_delivery", **data})],
        source="cognis:channel",
    )
    if getattr(result, "ok", False) is not True or getattr(result, "count", 0) != 1:
        raise RuntimeError("Channel source reference append was not confirmed")


async def pending_sources(
    guardrails: Any, session_ids: list[str], turn_id: str
) -> tuple[str, list[dict[str, Any]]]:
    messages: dict[tuple[str, str, int, int], tuple[int | None, str]] = {}
    excluded: dict[tuple[str, str, int, int], int] = {}
    fingerprints: dict[tuple[str, str, int, int], tuple[int, str]] = {}
    tracked: set[str] = set()
    parents: dict[str, str] = {}
    for session_id in session_ids:
        after = 0
        while True:
            page = await guardrails.read_events(
                session_id=session_id,
                after_seq=after,
                limit=500,
                types=["assistant_message", "lifecycle", "system_message"],
                allow_missing_stream=False,
            )
            for event in page.events:
                kind = event.get("type") if isinstance(event, dict) else event.type
                data = event.get("data", {}) if isinstance(event, dict) else event.data
                seq = event.get("seq") if isinstance(event, dict) else event.seq
                source_turn = data.get("turn_id")
                if kind == "lifecycle" and data.get("event") == "channel_source_delivery":
                    if data.get("state") == "tracking":
                        tracked.add(source_turn)
                    if data.get("state") in {"intended", "sent", "uncertain"}:
                        for ref in data.get("sources", []):
                            key = (ref["session_id"], ref["turn_id"], ref["phase"], ref["cycle"])
                            excluded[key] = max(excluded.get(key, 0), ref["end"])
                            fingerprints[key] = (ref["end"], ref["prefix_hash"])
                elif (
                    kind == "system_message"
                    and data.get("event") == "turn_initiated"
                    and data.get("origin_kind") == "continuation"
                    and isinstance(data.get("source_id"), str)
                ):
                    parents[source_turn] = data["source_id"]
                elif kind == "assistant_message" and (
                    not data.get("partial")
                    or (
                        data.get("cancelled") is True
                        and data.get("finish_reason") == "user_cancelled"
                    )
                ):
                    content = data.get("content")
                    if isinstance(source_turn, str) and isinstance(content, str) and content:
                        key = (
                            session_id,
                            source_turn,
                            int(data.get("assistant_phase_index", 0)),
                            int(data.get("turn_cycle_index", 0)),
                        )
                        messages[key] = (seq, content)
            next_after = next_event_page_after_seq(page, after)
            if next_after is None:
                break
            after = next_after
    lineage: list[str] = []
    if turn_id not in tracked:
        raise RuntimeError("Current channel turn has no source tracking marker")
    current: str | None = turn_id
    while current and current not in lineage:
        lineage.append(current)
        current = parents.get(current)
    content_parts: list[str] = []
    refs: list[dict[str, Any]] = []
    for source_turn in reversed(lineage):
        if source_turn not in tracked:
            # Legacy immediate sends have no recoverable range information.
            continue
        for (session_id, message_turn, phase, cycle), (seq, content) in messages.items():
            if message_turn != source_turn:
                continue
            start = excluded.get((session_id, message_turn, phase, cycle), 0)
            prior = fingerprints.get((session_id, message_turn, phase, cycle))
            if prior and hashlib.sha256(content[: prior[0]].encode()).hexdigest() != prior[1]:
                raise RuntimeError("Channel source changed after a partial send")
            if start >= len(content):
                continue
            content_parts.append(content[start:])
            refs.append(
                {
                    "session_id": session_id,
                    "turn_id": message_turn,
                    "phase": phase,
                    "cycle": cycle,
                    "seq": seq,
                    "start": start,
                    "end": len(content),
                    "prefix_hash": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
    return "\n\n".join(content_parts), refs
