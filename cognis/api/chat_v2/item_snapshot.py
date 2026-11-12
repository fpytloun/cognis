"""Strict, bounded canonical assistant-item evidence at one immutable event."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionEventStore,
    require_session_history,
)
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import MessageTimelineItem, SourceRef, TimelineItem

PAGE_EVENTS = 400
MAX_EVENTS = 5000
MAX_PAGES = 16
MAX_REPLAY_BYTES = 8 * 1024 * 1024


class ItemSnapshotUnavailable(ValueError):
    """Completeness, identity or a bounded replay budget could not be proven."""


class _ReplayBudget:
    def __init__(self) -> None:
        self.bytes_left = MAX_REPLAY_BYTES

    def charge(self, value: Any) -> None:
        # Walk existing JSON without building an encoded copy or expanding containers.
        stack: list[Iterator[Any]] = [iter((value,))]
        while stack:
            try:
                node = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            self.bytes_left -= 2
            if self.bytes_left < 0:
                raise ItemSnapshotUnavailable("Replay byte budget exhausted")
            if isinstance(node, str):
                if len(node) > self.bytes_left:
                    raise ItemSnapshotUnavailable("Replay byte budget exhausted")
                for start in range(0, len(node), 4096):
                    chunk = node[start : start + 4096]
                    self.bytes_left -= len(chunk.encode("utf-8")) + sum(
                        1 if char in '"\\\b\f\n\r\t' else 5 if ord(char) < 32 else 0
                        for char in chunk
                    )
                    if self.bytes_left < 0:
                        raise ItemSnapshotUnavailable("Replay byte budget exhausted")
            elif isinstance(node, dict):
                stack.append(iter(node.items()))
            elif isinstance(node, (list, tuple)):
                stack.append(iter(node))
            elif isinstance(node, bool) or node is None:
                self.bytes_left -= 5
            elif isinstance(node, int):
                self.bytes_left -= 1 + node.bit_length() // 3
            elif isinstance(node, float):
                self.bytes_left -= 32
            else:
                raise ItemSnapshotUnavailable("Non-JSON replay data")
            if self.bytes_left < 0:
                raise ItemSnapshotUnavailable("Replay byte budget exhausted")

    def event(self, event: RawSessionEvent) -> None:
        self.charge(event.data)
        self.charge(
            (
                event.store_id,
                event.session_id,
                event.type,
                event.event_id,
                event.lane,
                event.prompt_visibility,
            )
        )


def _message(items: list[TimelineItem], item_id: str) -> MessageTimelineItem:
    matches = [item for item in items if item.id == item_id]
    if (
        len(matches) != 1
        or not isinstance(matches[0], MessageTimelineItem)
        or matches[0].role != "assistant"
    ):
        raise ItemSnapshotUnavailable("Canonical assistant item is unavailable")
    return matches[0]


def _page_identity(page: SessionEventPage, source: SourceRef, limit: int) -> None:
    if (
        page.store_id != source.store
        or page.session_id != source.session_id
        or len(page.events) > limit
    ):
        raise ItemSnapshotUnavailable("Ambiguous canonical page identity")


def _event_identity(event: RawSessionEvent, source: SourceRef) -> None:
    if event.store_id != source.store or event.session_id != source.session_id:
        raise ItemSnapshotUnavailable("Ambiguous canonical event identity")


def _contributes(item: MessageTimelineItem, source: SourceRef) -> bool:
    return any(
        ref.store == source.store
        and ref.session_id == source.session_id
        and ref.seq == source.seq
        and ref.event_type == source.event_type
        and (source.event_id is None or ref.event_id == source.event_id)
        for ref in item.source_refs
    )


async def read_assistant_item_as_of(
    reader: SessionEventStore, *, source: SourceRef, item_id: str
) -> MessageTimelineItem:
    """Prove a same-session item snapshot without a live snapshot or another coalescer.

    The caller owns authorization and binds the reader before entry. All state is
    request-local; cancellation propagates. No database session, durable cache,
    transcript copy or source authority is created here.
    """
    if source.seq < 1:
        raise ItemSnapshotUnavailable("Invalid snapshot upper bound")
    page = await reader.read_session_events(
        session_id=source.session_id, before_seq=source.seq + 1, limit=1, direction="backward"
    )
    _page_identity(page, source, 1)
    require_session_history(page.availability, from_seq=source.seq, to_seq=source.seq)
    if len(page.events) != 1 or page.events[0].seq != source.seq:
        raise ItemSnapshotUnavailable("Selected event is unavailable")
    selected = page.events[0]
    _event_identity(selected, source)
    budget = _ReplayBudget()
    budget.event(selected)
    normalized = normalize_session_events([selected]).events
    item = _message(project_timeline(normalized).timeline.items, item_id)
    if not _contributes(item, source):
        raise ItemSnapshotUnavailable("Selected event is not a canonical contributor")
    if not any(
        event.kind == "assistant_message" and event.data.get("content_is_delta") is True
        for event in normalized
    ):
        return item

    retained = [selected]
    examined = 1
    before = source.seq
    for _ in range(MAX_PAGES - 1):
        limit = min(PAGE_EVENTS, MAX_EVENTS - examined)
        if limit <= 0 or before <= 1:
            break
        page = await reader.read_session_events(
            session_id=source.session_id, before_seq=before, limit=limit, direction="backward"
        )
        _page_identity(page, source, limit)
        examined += len(page.events)
        if not page.events:
            raise ItemSnapshotUnavailable("Assistant base is unavailable")
        # Page length is checked before this bounded allocation.
        for event in sorted(page.events, key=lambda event: event.seq, reverse=True):
            _event_identity(event, source)
            if event.seq != before - 1:
                raise ItemSnapshotUnavailable("Incomplete or stalled canonical history")
            require_session_history(page.availability, from_seq=event.seq, to_seq=source.seq)
            budget.event(event)  # Charge before retention, normalization or projection.
            retained.append(event)
            before = event.seq
            # Only the canonical projector decides message identity and delta/replacement.
            normalized = normalize_session_events([event]).events
            items = project_timeline(normalized).timeline.items
            matches = [candidate for candidate in items if candidate.id == item_id]
            if matches and (
                len(matches) != 1
                or not isinstance(matches[0], MessageTimelineItem)
                or matches[0].role != "assistant"
            ):
                raise ItemSnapshotUnavailable("Ambiguous assistant item identity")
            if matches and not any(
                event.kind == "assistant_message" and event.data.get("content_is_delta") is True
                for event in normalized
            ):
                result = _message(
                    project_timeline(
                        normalize_session_events(list(reversed(retained))).events
                    ).timeline.items,
                    item_id,
                )
                if not _contributes(result, source):
                    raise ItemSnapshotUnavailable("Snapshot lost the selected contributor")
                return result
        if not page.has_more_before:
            break
    raise ItemSnapshotUnavailable("Assistant base is unproven within replay limits")
