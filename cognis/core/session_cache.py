"""In-memory cache for Intaris-derived session state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from prometheus_client import Counter, Gauge

from cognis.logging import get_logger
from cognis.models.session import EventAppendResult, SessionEvent, SessionModel

logger = get_logger(__name__)

CACHE_HITS = Counter("cognis_session_cache_hits_total", "Session cache hits")
CACHE_MISSES = Counter("cognis_session_cache_misses_total", "Session cache misses")
CACHE_EVICTIONS = Counter("cognis_session_cache_evictions_total", "Session cache evictions")
CACHE_SIZE = Gauge("cognis_session_cache_size", "Session cache entry count")

_NEW_SESSION_STREAM_GRACE = timedelta(seconds=30)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass(slots=True)
class CachedEvent:
    """Cached Intaris event with normalized fields."""

    seq: int
    type: str
    data: dict[str, Any]
    source: str | None = None
    ts: str | None = None


@dataclass(slots=True)
class CachedSessionState:
    """Cache entry for a single Cognis session."""

    session_id: str
    intaris_session_id: str
    events: list[CachedEvent] = field(default_factory=list)
    last_event_seq: int = 0
    last_compaction_seq: int = 0
    last_compaction_summary: str | None = None
    intention: str | None = None
    touched_at: float = field(default_factory=monotonic)
    initialized: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionCache:
    """L1 in-memory cache for Intaris-derived session state."""

    def __init__(self, guardrails: Any, max_entries: int = 200) -> None:
        self.guardrails = guardrails
        self.max_entries = max_entries
        self._entries: dict[str, CachedSessionState] = {}
        self._entries_lock = asyncio.Lock()

    async def refresh(self, session: SessionModel) -> CachedSessionState:
        """Load or incrementally refresh a cache entry from Intaris."""

        entry = await self._ensure_entry(session)
        async with entry.lock:
            if not entry.initialized:
                await self._cold_load(entry, session)
                logger.debug(
                    "cache: cold load complete",
                    extra={
                        "extra_data": {
                            "session_id": entry.session_id,
                            "event_count": len(entry.events),
                            "last_seq": entry.last_event_seq,
                        }
                    },
                )
            else:
                event_read = await self.guardrails.read_events(
                    session_id=entry.intaris_session_id,
                    after_seq=entry.last_event_seq,
                    allow_missing_stream=True,
                )
                self._apply_intaris_events(entry, event_read.events)
                entry.last_event_seq = max(entry.last_event_seq, event_read.last_seq)
                logger.debug(
                    "cache: warm refresh complete",
                    extra={
                        "extra_data": {
                            "session_id": entry.session_id,
                            "new_events": len(event_read.events),
                            "last_seq": entry.last_event_seq,
                        }
                    },
                )
            entry.touched_at = monotonic()
        return entry

    async def append_recorded_events(
        self,
        session: SessionModel,
        events: list[SessionEvent],
        append_result: EventAppendResult,
    ) -> CachedSessionState:
        """Append freshly recorded Intaris events to the cache."""

        entry = await self._ensure_entry(session)
        async with entry.lock:
            next_seq = append_result.first_seq
            for event in events:
                self._apply_cached_event(
                    entry,
                    CachedEvent(seq=next_seq, type=event.type, data=dict(event.data)),
                )
                next_seq += 1
            entry.last_event_seq = max(entry.last_event_seq, append_result.last_seq)
            entry.touched_at = monotonic()
        return entry

    async def apply_compaction(
        self,
        session: SessionModel,
        *,
        summary: str,
        compaction_seq: int,
    ) -> CachedSessionState:
        """Update cache state after a confirmed compaction write."""

        entry = await self._ensure_entry(session)
        async with entry.lock:
            entry.last_compaction_summary = summary
            entry.last_compaction_seq = compaction_seq
            entry.events = [event for event in entry.events if event.seq > compaction_seq]
            entry.last_event_seq = max(entry.last_event_seq, compaction_seq)
            entry.touched_at = monotonic()
        return entry

    async def update_intention(self, session_id: str, intention: str | None) -> None:
        """Update cached intention for an existing session entry."""

        entry = self.get_entry(session_id)
        if entry is None:
            return
        async with entry.lock:
            entry.intention = intention
            entry.touched_at = monotonic()

    async def evict(self, session_id: str) -> bool:
        """Evict a cache entry if present."""

        async with self._entries_lock:
            entry = self._entries.get(session_id)
            if entry is None or entry.lock.locked():
                return False
            self._entries.pop(session_id, None)
            CACHE_EVICTIONS.inc()
            CACHE_SIZE.set(len(self._entries))
            return True

    def get_entry(self, session_id: str) -> CachedSessionState | None:
        """Get a cache entry without mutating it."""

        entry = self._entries.get(session_id)
        if entry is not None:
            entry.touched_at = monotonic()
        return entry

    def get_intention(self, session_id: str) -> str | None:
        """Get the cached intention for a session."""

        entry = self.get_entry(session_id)
        return None if entry is None else entry.intention

    def get_compaction_summary(self, session_id: str) -> str | None:
        """Get the cached compaction summary for a session."""

        entry = self.get_entry(session_id)
        return None if entry is None else entry.last_compaction_summary

    def get_events_since_compaction(
        self, session_id: str, types: list[str] | None = None
    ) -> list[CachedEvent]:
        """Return buffered events after the latest compaction."""

        entry = self.get_entry(session_id)
        if entry is None:
            return []
        events = entry.events
        if types is not None:
            allowed = set(types)
            events = [event for event in events if event.type in allowed]
        return list(events)

    async def _ensure_entry(self, session: SessionModel) -> CachedSessionState:
        async with self._entries_lock:
            entry = self._entries.get(session.session_id)
            if entry is None:
                CACHE_MISSES.inc()
                await self._evict_oldest_unlocked()
                entry = CachedSessionState(
                    session_id=session.session_id,
                    intaris_session_id=session.intaris_session_id or session.session_id,
                )
                self._entries[session.session_id] = entry
                CACHE_SIZE.set(len(self._entries))
                logger.debug(
                    "cache: entry created",
                    extra={
                        "extra_data": {
                            "session_id": session.session_id,
                            "intaris_session_id": entry.intaris_session_id,
                        }
                    },
                )
            else:
                CACHE_HITS.inc()
                entry.intaris_session_id = session.intaris_session_id or session.session_id
            entry.touched_at = monotonic()
            return entry

    async def _cold_load(self, entry: CachedSessionState, session: SessionModel) -> None:
        allow_missing_stream = False
        if session.started_at is not None:
            allow_missing_stream = (
                datetime.now(UTC) - _normalize_utc(session.started_at) <= _NEW_SESSION_STREAM_GRACE
            )

        event_read = await self.guardrails.read_events(
            session_id=entry.intaris_session_id,
            after_seq=0,
            allow_missing_stream=allow_missing_stream,
        )
        self._apply_intaris_events(entry, event_read.events)
        entry.last_event_seq = event_read.last_seq
        entry.initialized = True

    def _apply_intaris_events(
        self, entry: CachedSessionState, raw_events: list[dict[str, Any]]
    ) -> None:
        for raw_event in sorted(raw_events, key=lambda item: int(item.get("seq", 0))):
            cached_event = CachedEvent(
                seq=int(raw_event.get("seq", 0)),
                type=str(raw_event.get("type", "")),
                data=dict(raw_event.get("data", {})),
                source=raw_event.get("source"),
                ts=raw_event.get("ts"),
            )
            self._apply_cached_event(entry, cached_event)
        entry.initialized = True

    def _apply_cached_event(self, entry: CachedSessionState, event: CachedEvent) -> None:
        if event.type == "compaction_summary":
            summary = event.data.get("summary")
            entry.last_compaction_summary = summary if isinstance(summary, str) else None
            entry.last_compaction_seq = event.seq
            entry.events = [existing for existing in entry.events if existing.seq > event.seq]
        elif event.seq > entry.last_compaction_seq:
            entry.events.append(event)
        entry.last_event_seq = max(entry.last_event_seq, event.seq)

    async def _evict_oldest_unlocked(self) -> None:
        if self.max_entries <= 0 or len(self._entries) < self.max_entries:
            return
        eviction_candidates = [entry for entry in self._entries.values() if not entry.lock.locked()]
        if not eviction_candidates:
            return
        oldest_entry = min(eviction_candidates, key=lambda entry: entry.touched_at)
        self._entries.pop(oldest_entry.session_id, None)
        CACHE_EVICTIONS.inc()
        CACHE_SIZE.set(len(self._entries))
