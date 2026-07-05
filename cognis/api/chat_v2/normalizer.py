"""Normalize pluggable session-store events for Chat v2 projection.

The normalizer is intentionally conservative. It translates known Cognis
session event shapes into a small stable input model for the pure projector and
filters events that must not become user-visible chat timeline state.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.schemas import SourceRef, StrictModel
from cognis.api.timeline_visibility import (
    is_transient_compaction_start_notice,
    is_visible_persisted_system_message,
)

NormalizedEventKind = Literal[
    "user_message",
    "assistant_message",
    "system_message",
    "thinking",
    "tool_call",
    "tool_result",
    "delegation",
    "managed_conversation",
    "task",
    "question_set",
    "auth_challenge",
    "credential_request",
    "todo_state",
    "artifact",
    "file_diff",
    "evaluation",
    "notice",
    "compaction",
    "error",
    "unknown",
]

DEFAULT_VISIBLE_LANES: frozenset[str | None] = frozenset({None, "", "main", "default"})
HIDDEN_PROMPT_VISIBILITIES: frozenset[str] = frozenset(
    {
        "hidden",
        "internal",
        "model_only",
        "prompt_only",
        "none",
        "private",
        "audit",
        "audit_only",
        "debug",
        "diagnostic",
    }
)
IGNORED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "checkpoint",
        "reasoning",
        "developer_message",
        "context_snapshot",
        "part",
        "transcript",
    }
)
VISIBLE_LIFECYCLE_EVENTS: frozenset[str] = frozenset(
    {
        "system_notice",
        "task_result",
        "task_failed",
        "task_cancelled",
        "workflow_composed",
    }
)


class NormalizedChatEvent(StrictModel):
    """Stable projector input derived from a raw session event."""

    kind: NormalizedEventKind
    source_ref: SourceRef
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None
    lineage_ordinal: int = Field(ge=0)
    local_ordinal: int = Field(ge=0)
    assistant_phase_index: int | None = Field(default=None, ge=0)
    turn_cycle_index: int | None = Field(default=None, ge=0)


class NormalizationResult(StrictModel):
    """Normalizer output plus explicit accounting for skipped raw events."""

    events: list[NormalizedChatEvent] = Field(default_factory=list)
    skipped_count: int = 0
    skipped_event_types: dict[str, int] = Field(default_factory=dict)


def normalize_session_events(
    raw_events: Iterable[RawSessionEvent],
    *,
    lineage_ordinal: int = 0,
    visible_lanes: frozenset[str | None] = DEFAULT_VISIBLE_LANES,
) -> NormalizationResult:
    """Normalize raw events from one session stream.

    Unknown user-visible event shapes are retained as ``kind=\"unknown\"`` so the
    projector can create an explicit diagnostic notice instead of silently
    losing potentially important information.
    """

    normalized: list[NormalizedChatEvent] = []
    skipped_event_types: dict[str, int] = {}
    skipped_count = 0

    for local_ordinal, raw_event in enumerate(raw_events):
        if _should_skip_event(raw_event, visible_lanes=visible_lanes):
            skipped_count += 1
            skipped_event_types[raw_event.type] = skipped_event_types.get(raw_event.type, 0) + 1
            continue

        kind = _event_kind(raw_event)
        data = dict(raw_event.data)
        source_ref = SourceRef(
            store=raw_event.store_id,
            session_id=raw_event.session_id,
            seq=raw_event.seq,
            event_id=raw_event.event_id,
            event_type=raw_event.type,
        )
        normalized.append(
            NormalizedChatEvent(
                kind=kind,
                source_ref=source_ref,
                data=data,
                timestamp=_format_timestamp(raw_event.timestamp),
                lineage_ordinal=_lineage_ordinal(data, default=lineage_ordinal),
                local_ordinal=local_ordinal,
                assistant_phase_index=_assistant_phase_index(data),
                turn_cycle_index=_turn_cycle_index(data),
            )
        )

    return NormalizationResult(
        events=normalized,
        skipped_count=skipped_count,
        skipped_event_types=skipped_event_types,
    )


def _should_skip_event(raw_event: RawSessionEvent, *, visible_lanes: frozenset[str | None]) -> bool:
    if raw_event.type in IGNORED_EVENT_TYPES:
        return True
    if raw_event.type == "system_message" and not is_visible_persisted_system_message(
        raw_event.data
    ):
        return True
    if raw_event.type == "lifecycle":
        lifecycle_event = str(raw_event.data.get("event") or raw_event.data.get("type") or "")
        if lifecycle_event not in VISIBLE_LIFECYCLE_EVENTS:
            return True
        if lifecycle_event == "system_notice" and is_transient_compaction_start_notice(
            raw_event.data
        ):
            return True
    if raw_event.type == "message":
        role = str(raw_event.data.get("role") or "").strip().lower()
        if role == "system":
            return True
    if raw_event.lane not in visible_lanes:
        return True
    prompt_visibility = (raw_event.prompt_visibility or "").strip().lower()
    return prompt_visibility in HIDDEN_PROMPT_VISIBILITIES


def _event_kind(raw_event: RawSessionEvent) -> NormalizedEventKind:
    event_type = raw_event.type
    data = raw_event.data

    if event_type == "message":
        role = str(data.get("role") or "").lower()
        if role == "user":
            return "user_message"
        if role == "assistant":
            return "assistant_message"
        if role == "system":
            return "system_message"
        return "unknown"

    if event_type == "assistant_thinking":
        return "thinking"
    if event_type in {
        "user_message",
        "assistant_message",
        "system_message",
        "tool_call",
        "tool_result",
        "delegation",
        "managed_conversation",
        "question_set",
        "auth_challenge",
        "credential_request",
        "todo_state",
        "artifact",
        "file_diff",
        "error",
        "history_gap",
    }:
        if event_type == "history_gap":
            return "notice"
        return event_type  # type: ignore[return-value]

    if event_type in {"task_result", "task_failed", "task_cancelled"}:
        return "task"

    if event_type == "lifecycle":
        lifecycle_event = str(data.get("event") or data.get("type") or "")
        if lifecycle_event in {"task_result", "task_failed", "task_cancelled", "workflow_composed"}:
            return "task"
        if lifecycle_event == "system_notice":
            return "system_message"
        return "unknown"

    if event_type == "evaluation":
        return "evaluation"

    if event_type == "compaction_summary":
        return "compaction"

    if event_type in {"workflow_composed", "session_recovered"}:
        return "notice"

    return "unknown"


def _lineage_ordinal(data: dict[str, Any], *, default: int) -> int:
    value = data.get("_lineage_index", data.get("lineage_ordinal", default))
    if isinstance(value, int) and value >= 0:
        return value
    return default


def _assistant_phase_index(data: dict[str, Any]) -> int | None:
    value = data.get("assistant_phase_index")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _turn_cycle_index(data: dict[str, Any]) -> int | None:
    value = data.get("turn_cycle_index")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _format_timestamp(timestamp: datetime | None) -> str | None:
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
