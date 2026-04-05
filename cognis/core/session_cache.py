"""In-memory cache for Intaris-derived session state.

Supports an optional Redis L2 layer for cross-restart persistence and
future multi-replica shared state.  When ``redis_url`` is provided,
async mutators write-through to Redis after updating L1.  Sync getters
always read from L1 only (no network I/O on the hot path).

Redis failures are logged and degraded — never fatal.  On L1 miss the
cache attempts to hydrate from Redis before falling back to a cold
Intaris load.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
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
REDIS_HITS = Counter("cognis_session_cache_redis_hits_total", "Redis L2 cache hits")
REDIS_MISSES = Counter("cognis_session_cache_redis_misses_total", "Redis L2 cache misses")
REDIS_ERRORS = Counter("cognis_session_cache_redis_errors_total", "Redis L2 errors")


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
    intention_updated_at: str | None = None
    touched_at: float = field(default_factory=monotonic)
    initialized: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Cached memory content from first Mnemory recall (immutable prefix)
    memory_instructions: str | None = None
    core_memories: str | None = None
    memory_instructions_cached_at: float | None = None
    # Context usage from last context assembly
    last_prompt_tokens: int = 0
    max_context_tokens: int = 0
    context_model: str = ""
    # Per-session overrides (ephemeral, set via /model and /thinking commands)
    model_override: str | None = None
    reasoning_effort_override: str | None = None


# ---------------------------------------------------------------------------
# Redis L2 serialization helpers
# ---------------------------------------------------------------------------

_REDIS_KEY_PREFIX = "cognis:session-cache:v1:"
_REDIS_DEFAULT_TTL = 3600  # 1 hour


def _serialize_entry(entry: CachedSessionState) -> str:
    """Serialize the Redis-storable subset of a cache entry to JSON."""
    return json.dumps(
        {
            "session_id": entry.session_id,
            "intaris_session_id": entry.intaris_session_id,
            "events": [
                {
                    "seq": e.seq,
                    "type": e.type,
                    "data": e.data,
                    "source": e.source,
                    "ts": e.ts,
                }
                for e in entry.events
            ],
            "last_event_seq": entry.last_event_seq,
            "last_compaction_seq": entry.last_compaction_seq,
            "last_compaction_summary": entry.last_compaction_summary,
            "intention": entry.intention,
            "intention_updated_at": entry.intention_updated_at,
            "initialized": entry.initialized,
            "memory_instructions": entry.memory_instructions,
            "core_memories": entry.core_memories,
            "last_prompt_tokens": entry.last_prompt_tokens,
            "max_context_tokens": entry.max_context_tokens,
            "context_model": entry.context_model,
            "model_override": entry.model_override,
            "reasoning_effort_override": entry.reasoning_effort_override,
        },
        separators=(",", ":"),
    )


def _deserialize_entry(raw: str) -> CachedSessionState:
    """Deserialize a JSON string into a CachedSessionState (L1 entry)."""
    data = json.loads(raw)
    entry = CachedSessionState(
        session_id=data["session_id"],
        intaris_session_id=data["intaris_session_id"],
        last_event_seq=data.get("last_event_seq", 0),
        last_compaction_seq=data.get("last_compaction_seq", 0),
        last_compaction_summary=data.get("last_compaction_summary"),
        intention=data.get("intention"),
        intention_updated_at=data.get("intention_updated_at"),
        initialized=data.get("initialized", False),
        memory_instructions=data.get("memory_instructions"),
        core_memories=data.get("core_memories"),
        last_prompt_tokens=data.get("last_prompt_tokens", 0),
        max_context_tokens=data.get("max_context_tokens", 0),
        context_model=data.get("context_model", ""),
        model_override=data.get("model_override"),
        reasoning_effort_override=data.get("reasoning_effort_override"),
    )
    for raw_event in data.get("events", []):
        entry.events.append(
            CachedEvent(
                seq=raw_event["seq"],
                type=raw_event["type"],
                data=raw_event.get("data", {}),
                source=raw_event.get("source"),
                ts=raw_event.get("ts"),
            )
        )
    return entry


class SessionCache:
    """L1 in-memory cache for Intaris-derived session state.

    Optionally backed by Redis L2 for cross-restart persistence.
    """

    def __init__(
        self,
        guardrails: Any,
        max_entries: int = 200,
        redis_url: str = "",
        redis_ttl_seconds: int = _REDIS_DEFAULT_TTL,
    ) -> None:
        self.guardrails = guardrails
        self.max_entries = max_entries
        self._entries: dict[str, CachedSessionState] = {}
        self._entries_lock = asyncio.Lock()
        self._redis: Any | None = None
        self._redis_ttl = redis_ttl_seconds
        if redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                logger.info(
                    "session_cache: Redis L2 enabled",
                    extra={"extra_data": {"redis_url": redis_url}},
                )
            except Exception:
                logger.warning("session_cache: failed to connect to Redis L2", exc_info=True)
                self._redis = None

    async def aclose(self) -> None:
        """Close Redis connection if active."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    # ------------------------------------------------------------------
    # Redis L2 helpers (best-effort, never raise)
    # ------------------------------------------------------------------

    async def _redis_get(self, session_id: str) -> CachedSessionState | None:
        """Try to load a session from Redis L2."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"{_REDIS_KEY_PREFIX}{session_id}")
            if raw is None:
                REDIS_MISSES.inc()
                return None
            REDIS_HITS.inc()
            return _deserialize_entry(raw)
        except Exception:
            REDIS_ERRORS.inc()
            logger.warning(
                "session_cache: Redis L2 read failed",
                extra={"extra_data": {"session_id": session_id}},
                exc_info=True,
            )
            return None

    async def _redis_set(self, entry: CachedSessionState) -> None:
        """Write-through to Redis L2."""
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                f"{_REDIS_KEY_PREFIX}{entry.session_id}",
                self._redis_ttl,
                _serialize_entry(entry),
            )
        except Exception:
            REDIS_ERRORS.inc()
            logger.warning(
                "session_cache: Redis L2 write failed",
                extra={"extra_data": {"session_id": entry.session_id}},
                exc_info=True,
            )

    async def _redis_delete(self, session_id: str) -> None:
        """Delete from Redis L2."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"{_REDIS_KEY_PREFIX}{session_id}")
        except Exception:
            REDIS_ERRORS.inc()

    # ------------------------------------------------------------------
    # Public API (unchanged surface)
    # ------------------------------------------------------------------

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
        await self._redis_set(entry)
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
            entry.initialized = True
            entry.touched_at = monotonic()
        await self._redis_set(entry)
        return entry

    async def seed_events(
        self,
        session: SessionModel,
        events: list[CachedEvent],
        last_seq: int,
    ) -> CachedSessionState:
        """Pre-populate a session's cache with events from another session.

        Used by the workflow engine to implement ``type="full"`` fork
        behaviour: events from the source step's session are written to
        the new Intaris session and then seeded into the cache so the
        context assembler sees them as natural history without a cold load.
        """
        entry = await self._ensure_entry(session)
        async with entry.lock:
            for event in events:
                self._apply_cached_event(entry, event)
            entry.last_event_seq = max(entry.last_event_seq, last_seq)
            entry.initialized = True
            entry.touched_at = monotonic()
        await self._redis_set(entry)
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
        await self._redis_set(entry)
        return entry

    async def update_intention(
        self,
        session_id: str,
        intention: str | None,
        *,
        updated_at: str | None = None,
        force: bool = False,
    ) -> bool:
        """Update cached intention for an existing session entry."""

        entry = self.get_entry(session_id)
        if entry is None:
            return False
        async with entry.lock:
            if not force and updated_at is not None and entry.intention_updated_at is not None:
                if updated_at < entry.intention_updated_at:
                    return False
            entry.intention = intention
            if updated_at is not None:
                entry.intention_updated_at = updated_at
            entry.touched_at = monotonic()
        await self._redis_set(entry)
        return True

    async def evict(self, session_id: str) -> bool:
        """Evict a cache entry if present."""

        async with self._entries_lock:
            entry = self._entries.get(session_id)
            if entry is None or entry.lock.locked():
                return False
            self._entries.pop(session_id, None)
            CACHE_EVICTIONS.inc()
            CACHE_SIZE.set(len(self._entries))
        await self._redis_delete(session_id)
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

    def update_context_usage(
        self,
        session: SessionModel,
        *,
        prompt_tokens: int,
        max_context_tokens: int,
        model: str,
    ) -> None:
        """Store latest context usage from context assembly."""

        entry = self._entries.get(session.session_id)
        if entry is not None:
            entry.last_prompt_tokens = prompt_tokens
            entry.max_context_tokens = max_context_tokens
            entry.context_model = model

    def get_context_usage(self, session_id: str) -> dict[str, Any] | None:
        """Get the cached context usage for a session.

        Returns a dict with ``prompt_tokens``, ``max_context_tokens``,
        ``percentage``, and ``model``, or ``None`` if no data is cached.
        """

        entry = self.get_entry(session_id)
        if entry is None or entry.max_context_tokens <= 0:
            return None
        return {
            "prompt_tokens": entry.last_prompt_tokens,
            "max_context_tokens": entry.max_context_tokens,
            "percentage": round(entry.last_prompt_tokens / entry.max_context_tokens * 100, 1),
            "model": entry.context_model,
            "reasoning_effort": entry.reasoning_effort_override,
        }

    def set_model_override(self, session_id: str, model: str | None) -> None:
        """Set per-session model override (from /model command)."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.model_override = model

    def get_model_override(self, session_id: str) -> str | None:
        """Get per-session model override, or ``None`` for default."""
        entry = self._entries.get(session_id)
        return entry.model_override if entry is not None else None

    def set_reasoning_effort_override(self, session_id: str, effort: str | None) -> None:
        """Set per-session reasoning effort override (from /thinking command)."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.reasoning_effort_override = effort

    def get_reasoning_effort_override(self, session_id: str) -> str | None:
        """Get per-session reasoning effort override, or ``None`` for default."""
        entry = self._entries.get(session_id)
        return entry.reasoning_effort_override if entry is not None else None

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

    def get_cached_memory(
        self, session_id: str, ttl_seconds: float = 1800.0
    ) -> tuple[str | None, str | None, bool]:
        """Return cached (instructions, core_memories, is_valid).

        Returns ``is_valid=False`` when the cache is stale (older than
        *ttl_seconds*, default 30 minutes) or when no cached values exist.
        """

        entry = self.get_entry(session_id)
        if entry is None or entry.memory_instructions is None:
            return None, None, False
        if entry.memory_instructions_cached_at is None:
            return entry.memory_instructions, entry.core_memories, False
        age = monotonic() - entry.memory_instructions_cached_at
        is_valid = age < ttl_seconds
        return entry.memory_instructions, entry.core_memories, is_valid

    async def cache_memory(
        self, session_id: str, instructions: str | None, core_memories: str | None
    ) -> None:
        """Cache memory instructions and core memories from first recall."""

        entry = self.get_entry(session_id)
        if entry is None:
            return
        async with entry.lock:
            entry.memory_instructions = instructions
            entry.core_memories = core_memories
            entry.memory_instructions_cached_at = monotonic()
            entry.touched_at = monotonic()
        await self._redis_set(entry)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_entry(self, session: SessionModel) -> CachedSessionState:
        async with self._entries_lock:
            entry = self._entries.get(session.session_id)
            if entry is None:
                CACHE_MISSES.inc()
                # Evict before inserting any new entry (Redis-hit or fresh)
                await self._evict_oldest_unlocked()
                # Try Redis L2 before creating a blank entry
                redis_entry = await self._redis_get(session.session_id)
                if redis_entry is not None:
                    entry = redis_entry
                    entry.intaris_session_id = session.intaris_session_id or session.session_id
                    logger.debug(
                        "cache: hydrated from Redis L2",
                        extra={
                            "extra_data": {
                                "session_id": session.session_id,
                                "event_count": len(entry.events),
                            }
                        },
                    )
                else:
                    entry = CachedSessionState(
                        session_id=session.session_id,
                        intaris_session_id=session.intaris_session_id or session.session_id,
                    )
                    logger.debug(
                        "cache: entry created",
                        extra={
                            "extra_data": {
                                "session_id": session.session_id,
                                "intaris_session_id": entry.intaris_session_id,
                            }
                        },
                    )
                self._entries[session.session_id] = entry
                CACHE_SIZE.set(len(self._entries))
            else:
                CACHE_HITS.inc()
                entry.intaris_session_id = session.intaris_session_id or session.session_id
            entry.touched_at = monotonic()
            return entry

    async def _cold_load(self, entry: CachedSessionState, session: SessionModel) -> None:
        # Always allow missing streams on cold load.  A 404 from Intaris
        # simply means the session has no recorded events (e.g. pre-Intaris
        # sessions, failed create_session calls, or very new sessions whose
        # stream hasn't been created yet).  The cache initialises as empty
        # and callers degrade gracefully (compaction returns noop, context
        # assembly works with no history).
        event_read = await self.guardrails.read_events(
            session_id=entry.intaris_session_id,
            after_seq=0,
            allow_missing_stream=True,
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
