"""Shared helpers for forking Cognis sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from cognis.core.immutable_prefix import (
    PREFIX_EVENT_TYPES,
    ImmutablePrefixEntry,
    build_context_snapshot_event,
    build_prefix_message_events,
)
from cognis.core.session_cache import CachedEvent
from cognis.core.session_event_types import INTARIS_APPENDABLE_EVENT_TYPES
from cognis.logging import get_logger
from cognis.models.session import SessionEvent

logger = get_logger(__name__)

INTARIS_EVENT_APPEND_BATCH_SIZE = 1000


def _prefix_entry_from_event(raw_event: dict[str, Any]) -> ImmutablePrefixEntry | None:
    event_type = str(raw_event.get("type") or "")
    if event_type not in {"system_message", "developer_message"}:
        return None
    data = raw_event.get("data")
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    role = data.get("role")
    if role not in {"system", "developer"}:
        role = "system" if event_type == "system_message" else "developer"
    source = data.get("source")
    return ImmutablePrefixEntry(
        role=role,
        source=str(source or event_type),
        content=content,
        seq=int(raw_event.get("seq", 0) or 0),
    )


def _dedupe_prefix_entries(entries: list[ImmutablePrefixEntry]) -> list[ImmutablePrefixEntry]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[ImmutablePrefixEntry] = []
    for entry in entries:
        key = (entry.role, entry.source, entry.content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


async def fork_session_events(
    *,
    providers: Any,
    session_cache: Any,
    source_cognis_session_id: str | None,
    source_intaris_session_id: str | None,
    target_session: Any,
    source_label: str,
    snapshot_source: str = "fork",
    snapshot_extras: dict[str, Any] | None = None,
    extra_prefix_entries: list[ImmutablePrefixEntry] | None = None,
    extra_history_events: list[SessionEvent] | None = None,
    copy_prefix: bool = True,
    max_source_seq: int | None = None,
    event_filter: Callable[[CachedEvent], bool] | None = None,
    record_source: str = "cognis:fork",
) -> bool:
    """Copy one session's event history into another session.

    Prefix events are not copied as ordinary history. They are re-emitted as
    the target session's immutable prefix with an explicit context snapshot.
    """

    source_events: list[CachedEvent] = []
    prefix_entries: list[ImmutablePrefixEntry] = []

    if source_cognis_session_id:
        cache_entry = session_cache.get_entry(source_cognis_session_id)
        if cache_entry is not None and cache_entry.initialized and cache_entry.events:
            source_events = [
                event for event in cache_entry.events if event.type not in PREFIX_EVENT_TYPES
            ]
        if copy_prefix:
            cached_prefix_entries = session_cache.get_prefix_entries(source_cognis_session_id)
            prefix_entries.extend(cached_prefix_entries or [])

    if not source_events and source_intaris_session_id:
        try:
            event_read = await providers.guardrails.read_events(
                session_id=source_intaris_session_id,
                after_seq=0,
                allow_missing_stream=True,
            )
            for raw_event in sorted(event_read.events, key=lambda event: int(event.get("seq", 0))):
                event_type = str(raw_event.get("type") or "")
                if event_type in PREFIX_EVENT_TYPES:
                    if not copy_prefix:
                        continue
                    entry = _prefix_entry_from_event(raw_event)
                    if entry is not None:
                        prefix_entries.append(entry)
                    continue
                source_events.append(
                    CachedEvent(
                        seq=int(raw_event.get("seq", 0)),
                        type=event_type,
                        data=dict(raw_event.get("data", {})),
                        source=raw_event.get("source"),
                        ts=raw_event.get("ts"),
                    )
                )
        except Exception:
            logger.warning(
                "session fork: failed to read source events",
                extra={"extra_data": {"source_label": source_label}},
                exc_info=True,
            )

    if max_source_seq is not None:
        source_events = [event for event in source_events if event.seq <= max_source_seq]
    if event_filter is not None:
        source_events = [event for event in source_events if event_filter(event)]

    prefix_entries = _dedupe_prefix_entries(
        [
            ImmutablePrefixEntry(role=entry.role, source=entry.source, content=entry.content)
            for entry in prefix_entries
        ]
        + list(extra_prefix_entries or [])
    )
    if extra_history_events:
        source_events.extend(
            CachedEvent(
                seq=0,
                type=event.type,
                data=event.data,
                source="cognis:continuation",
                ts=None,
            )
            for event in extra_history_events
        )
    appendable_source_events: list[CachedEvent] = []
    skipped_event_types: dict[str, int] = {}
    for event in source_events:
        if event.type not in INTARIS_APPENDABLE_EVENT_TYPES:
            skipped_event_types[event.type] = skipped_event_types.get(event.type, 0) + 1
            continue
        appendable_source_events.append(event)
    if skipped_event_types:
        logger.info(
            "session fork: skipped non-appendable source events",
            extra={
                "extra_data": {
                    "source_label": source_label,
                    "skipped_count": sum(skipped_event_types.values()),
                    "skipped_types": dict(sorted(skipped_event_types.items())),
                }
            },
        )
    source_events = appendable_source_events

    if not source_events and not prefix_entries:
        logger.debug(
            "session fork: no source events or prefix entries to copy",
            extra={"extra_data": {"source_label": source_label}},
        )
        return False

    target_intaris_id = target_session.intaris_session_id or target_session.session_id
    try:
        if source_events:
            last_seq = 0
            for batch_start in range(0, len(source_events), INTARIS_EVENT_APPEND_BATCH_SIZE):
                source_batch = source_events[
                    batch_start : batch_start + INTARIS_EVENT_APPEND_BATCH_SIZE
                ]
                session_events = [
                    SessionEvent(type=event.type, data=event.data) for event in source_batch
                ]
                append_result = await providers.guardrails.record_events(
                    session_id=target_intaris_id,
                    events=session_events,
                    source=record_source,
                )
                remapped_events = [
                    CachedEvent(
                        seq=append_result.first_seq + index,
                        type=event.type,
                        data=event.data,
                        source=record_source,
                        ts=event.ts,
                    )
                    for index, event in enumerate(source_batch)
                ]
                await session_cache.seed_events(
                    target_session,
                    remapped_events,
                    append_result.last_seq,
                )
                last_seq = append_result.last_seq
        else:
            last_seq = 0

        if prefix_entries:
            message_events = build_prefix_message_events(prefix_entries)
            message_result = await providers.guardrails.record_events(
                session_id=target_intaris_id,
                events=message_events,
                source="cognis",
                idempotency_key=f"{target_session.session_id}:immutable_prefix:{snapshot_source}:messages",
            )
            if message_result.ok:
                resolved_entries = [
                    ImmutablePrefixEntry(
                        role=entry.role,
                        source=entry.source,
                        content=entry.content,
                        seq=message_result.first_seq + index,
                    )
                    for index, entry in enumerate(prefix_entries)
                ]
                extras = {"source_label": source_label, **(snapshot_extras or {})}
                snapshot_event = build_context_snapshot_event(
                    resolved_entries,
                    snapshot_source=snapshot_source,
                    extras=extras,
                )
                snapshot_events = [snapshot_event]
                snapshot_result = await providers.guardrails.record_events(
                    session_id=target_intaris_id,
                    events=snapshot_events,
                    source="cognis",
                    idempotency_key=f"{target_session.session_id}:immutable_prefix:{snapshot_source}:snapshot",
                )
                if snapshot_result.ok:
                    await session_cache.append_recorded_events(
                        target_session,
                        message_events,
                        message_result,
                    )
                    await session_cache.append_recorded_events(
                        target_session,
                        snapshot_events,
                        snapshot_result,
                    )
                    await session_cache.store_prefix_snapshot(
                        target_session.session_id,
                        resolved_entries,
                        snapshot_seq=snapshot_result.last_seq,
                        snapshot_source=snapshot_source,
                    )
                    last_seq = snapshot_result.last_seq
                else:
                    logger.warning(
                        "session fork: failed to persist fork snapshot event",
                        extra={"extra_data": {"target_session": target_session.session_id}},
                    )
            else:
                logger.warning(
                    "session fork: failed to persist fork prefix messages",
                    extra={"extra_data": {"target_session": target_session.session_id}},
                )

        logger.info(
            "session fork: copied source session into target session",
            extra={
                "extra_data": {
                    "source_label": source_label,
                    "target_session": target_session.session_id,
                    "event_count": len(source_events),
                    "prefix_count": len(prefix_entries),
                    "last_seq": last_seq,
                }
            },
        )
        return True
    except Exception as exc:
        extra_data: dict[str, Any] = {"source_label": source_label}
        if isinstance(exc, httpx.HTTPStatusError):
            extra_data["response_status_code"] = exc.response.status_code
            extra_data["response_body"] = exc.response.text[:1000]
        logger.warning(
            "session fork: failed to copy source session into target session",
            extra={"extra_data": extra_data},
            exc_info=True,
        )
        return False
