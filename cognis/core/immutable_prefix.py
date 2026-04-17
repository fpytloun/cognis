"""Helpers for immutable prefix reconstruction and audit snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cognis.models.session import SessionEvent

PREFIX_SOURCE_ORDER = {
    "identity": 0,
    "project_instructions": 1,
    "memory_instructions": 2,
    "core_memories": 3,
    "compaction_summary": 4,
}

PREFIX_EVENT_TYPES = frozenset({"system_message", "developer_message", "context_snapshot"})


@dataclass(slots=True)
class ImmutablePrefixEntry:
    """A stable immutable-prefix constituent persisted in Intaris."""

    role: str
    source: str
    content: str
    seq: int = 0


def sort_prefix_entries(entries: list[ImmutablePrefixEntry]) -> list[ImmutablePrefixEntry]:
    """Return prefix entries in the canonical Cognis order."""

    return sorted(
        entries,
        key=lambda entry: (PREFIX_SOURCE_ORDER.get(entry.source, 100), entry.seq),
    )


def build_prefix_message_events(entries: list[ImmutablePrefixEntry]) -> list[SessionEvent]:
    """Build the typed Intaris message events for immutable-prefix constituents."""

    session_events: list[SessionEvent] = []
    for entry in sort_prefix_entries(entries):
        event_type = "system_message" if entry.role == "system" else "developer_message"
        session_events.append(
            SessionEvent(
                type=event_type,
                data={
                    "role": entry.role,
                    "content": entry.content,
                    "content_type": "text",
                    "source": entry.source,
                },
            )
        )
    return session_events


def build_context_snapshot_event(
    entries: list[ImmutablePrefixEntry],
    *,
    snapshot_source: str,
    extras: dict[str, object] | None = None,
) -> SessionEvent:
    """Build a context_snapshot event referencing already-persisted entries."""

    return SessionEvent(
        type="context_snapshot",
        data={
            "source": snapshot_source,
            "entries": [
                {
                    "role": entry.role,
                    "source": entry.source,
                    "seq": entry.seq,
                }
                for entry in sort_prefix_entries(entries)
            ],
            "extras": extras or {},
            "captured_at": datetime.now(UTC).isoformat(),
        },
    )


def build_context_snapshot_events(
    entries: list[ImmutablePrefixEntry],
    *,
    snapshot_source: str,
    extras: dict[str, object] | None = None,
) -> list[SessionEvent]:
    """Build constituent audit events plus a trailing context_snapshot event."""

    session_events = build_prefix_message_events(entries)
    session_events.append(
        SessionEvent(
            type="context_snapshot",
            data=build_context_snapshot_event(
                entries,
                snapshot_source=snapshot_source,
                extras=extras,
            ).data,
        )
    )
    return session_events
