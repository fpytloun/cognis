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
import collections
import contextlib
import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from prometheus_client import Counter, Gauge

from cognis.core.immutable_prefix import (
    PREFIX_EVENT_TYPES,
    ImmutablePrefixEntry,
    sort_prefix_entries,
)
from cognis.core.project_context import (
    PROJECT_CONTEXT_STATUS_LOADED,
    ProjectContextEntry,
    normalize_project_path,
    project_context_from_event_data,
)
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
    prefix_entries: list[ImmutablePrefixEntry] = field(default_factory=list)
    context_snapshot_seq: int = 0
    context_snapshot_source: str | None = None
    prefix_repair_needed: bool = False
    last_repair_attempt_at: float | None = None
    # Context usage from last context assembly
    last_prompt_tokens: int = 0
    max_context_tokens: int = 0
    context_model: str = ""
    context_provider_id: str | None = None
    reserve_output_tokens: int = 0
    effective_reserve_output_tokens: int = 0
    last_llm_usage: dict[str, int] = field(default_factory=dict)
    context_reserve_clamp_warned: bool = False
    # Per-session overrides (ephemeral, set via /model and /thinking commands)
    model_override: str | None = None
    reasoning_effort_override: str | None = None
    last_tool_runtime_info: dict[str, Any] = field(default_factory=dict)
    activated_skill_tool_ids: set[str] = field(default_factory=set)
    skill_tool_classifications: dict[str, list[str]] = field(default_factory=dict)
    project_contexts: dict[str, ProjectContextEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Redis L2 serialization helpers
# ---------------------------------------------------------------------------

_REDIS_KEY_PREFIX = "cognis:session-cache:v2:"
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
            "prefix_entries": [
                {
                    "role": item.role,
                    "source": item.source,
                    "content": item.content,
                    "seq": item.seq,
                }
                for item in entry.prefix_entries
            ],
            "context_snapshot_seq": entry.context_snapshot_seq,
            "context_snapshot_source": entry.context_snapshot_source,
            "prefix_repair_needed": entry.prefix_repair_needed,
            "last_repair_attempt_at": entry.last_repair_attempt_at,
            "last_prompt_tokens": entry.last_prompt_tokens,
            "max_context_tokens": entry.max_context_tokens,
            "context_model": entry.context_model,
            "context_provider_id": entry.context_provider_id,
            "reserve_output_tokens": entry.reserve_output_tokens,
            "effective_reserve_output_tokens": entry.effective_reserve_output_tokens,
            "last_llm_usage": entry.last_llm_usage,
            "context_reserve_clamp_warned": entry.context_reserve_clamp_warned,
            "model_override": entry.model_override,
            "reasoning_effort_override": entry.reasoning_effort_override,
            "project_contexts": [
                {
                    "project_root": item.project_root,
                    "status": item.status,
                    "source_path": item.source_path,
                    "content": item.content,
                    "content_hash": item.content_hash,
                    "working_directory": item.working_directory,
                    "seq": item.seq,
                }
                for item in sorted(
                    entry.project_contexts.values(), key=lambda item: (item.seq, item.project_root)
                )
            ],
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
        prefix_entries=[
            ImmutablePrefixEntry(
                role=str(item.get("role") or "developer"),
                source=str(item.get("source") or ""),
                content=str(item.get("content") or ""),
                seq=int(item.get("seq") or 0),
            )
            for item in data.get("prefix_entries", [])
            if isinstance(item, dict)
        ],
        context_snapshot_seq=data.get("context_snapshot_seq", 0),
        context_snapshot_source=data.get("context_snapshot_source"),
        prefix_repair_needed=bool(data.get("prefix_repair_needed", False)),
        last_repair_attempt_at=data.get("last_repair_attempt_at"),
        last_prompt_tokens=data.get("last_prompt_tokens", 0),
        max_context_tokens=data.get("max_context_tokens", 0),
        context_model=data.get("context_model", ""),
        context_provider_id=data.get("context_provider_id"),
        reserve_output_tokens=data.get("reserve_output_tokens", 0),
        effective_reserve_output_tokens=data.get("effective_reserve_output_tokens", 0),
        last_llm_usage={
            str(key): int(value)
            for key, value in data.get("last_llm_usage", {}).items()
            if isinstance(key, str) and isinstance(value, int | float)
        },
        context_reserve_clamp_warned=bool(data.get("context_reserve_clamp_warned", False)),
        model_override=data.get("model_override"),
        reasoning_effort_override=data.get("reasoning_effort_override"),
        project_contexts={
            entry.project_root: entry
            for entry in (
                ProjectContextEntry(
                    project_root=str(item.get("project_root") or ""),
                    status=str(item.get("status") or PROJECT_CONTEXT_STATUS_LOADED),
                    source_path=normalize_project_path(item.get("source_path")),
                    content=(
                        str(item.get("content")) if isinstance(item.get("content"), str) else None
                    ),
                    content_hash=(
                        str(item.get("content_hash"))
                        if isinstance(item.get("content_hash"), str)
                        else None
                    ),
                    working_directory=normalize_project_path(item.get("working_directory")),
                    seq=int(item.get("seq") or 0),
                )
                for item in data.get("project_contexts", [])
                if isinstance(item, dict) and isinstance(item.get("project_root"), str)
            )
            if entry.project_root
        },
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
            with contextlib.suppress(Exception):
                await self._redis.aclose()
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
                            "prefix_entry_count": len(entry.prefix_entries),
                            "context_snapshot_seq": entry.context_snapshot_seq,
                            "context_snapshot_source": entry.context_snapshot_source,
                        }
                    },
                )
            else:
                event_read = await self.guardrails.read_events(
                    session_id=entry.intaris_session_id,
                    after_seq=entry.last_event_seq,
                    allow_missing_stream=True,
                )
                if getattr(event_read, "missing_stream_fallback_used", False):
                    logger.warning(
                        "cache: warm refresh fell back to missing Intaris stream",
                        extra={
                            "extra_data": {
                                "session_id": entry.session_id,
                                "intaris_session_id": entry.intaris_session_id,
                                "after_seq": entry.last_event_seq,
                            }
                        },
                    )
                self._apply_intaris_events(entry, event_read.events)
                force_prefix_rebuild = not entry.prefix_entries
                if force_prefix_rebuild:
                    logger.info(
                        "cache: warm refresh forcing full Intaris read to rebuild immutable prefix",
                        extra={
                            "extra_data": {
                                "session_id": entry.session_id,
                                "intaris_session_id": entry.intaris_session_id,
                                "after_seq": entry.last_event_seq,
                                "reason": "missing_prefix_entries",
                            }
                        },
                    )
                if force_prefix_rebuild or any(
                    str(raw_event.get("type") or "") == "context_snapshot"
                    for raw_event in event_read.events
                ):
                    full_read = await self.guardrails.read_events(
                        session_id=entry.intaris_session_id,
                        after_seq=0,
                        allow_missing_stream=True,
                    )
                    if getattr(full_read, "missing_stream_fallback_used", False):
                        logger.warning(
                            "cache: full prefix rebuild fell back to missing Intaris stream",
                            extra={
                                "extra_data": {
                                    "session_id": entry.session_id,
                                    "intaris_session_id": entry.intaris_session_id,
                                    "rebuild_reason": (
                                        "missing_prefix_entries"
                                        if force_prefix_rebuild
                                        else "incremental_context_snapshot"
                                    ),
                                }
                            },
                        )
                    self._replace_from_intaris_events(entry, full_read.events)
                    entry.last_event_seq = max(entry.last_event_seq, full_read.last_seq)
                entry.last_event_seq = max(entry.last_event_seq, event_read.last_seq)
                logger.debug(
                    "cache: warm refresh complete",
                    extra={
                        "extra_data": {
                            "session_id": entry.session_id,
                            "new_events": len(event_read.events),
                            "last_seq": entry.last_event_seq,
                            "prefix_entry_count": len(entry.prefix_entries),
                            "context_snapshot_seq": entry.context_snapshot_seq,
                            "context_snapshot_source": entry.context_snapshot_source,
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
            was_initialized = entry.initialized
            next_seq = append_result.first_seq
            recorded_events: list[CachedEvent] = []
            for event in events:
                cached_event = CachedEvent(seq=next_seq, type=event.type, data=dict(event.data))
                recorded_events.append(cached_event)
                self._apply_cached_event(entry, cached_event)
                next_seq += 1
            if any(item.type in PREFIX_EVENT_TYPES for item in recorded_events):
                self._rebuild_prefix_from_cached_events(entry, recorded_events)
            entry.last_event_seq = max(entry.last_event_seq, append_result.last_seq)
            # If the first event seen by a fresh in-memory cache has a sequence greater than
            # one, the controller restarted and recorded a new event before hydrating old
            # history. Keep the entry cold so the next refresh performs a full Intaris load.
            entry.initialized = was_initialized or append_result.first_seq <= 1
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
            if (
                not force
                and updated_at is not None
                and entry.intention_updated_at is not None
                and updated_at < entry.intention_updated_at
            ):
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
        provider_id: str | None = None,
        reserve_output_tokens: int | None = None,
        effective_reserve_output_tokens: int | None = None,
    ) -> None:
        """Store the latest prompt-usage snapshot for a session."""

        entry = self._entries.get(session.session_id)
        if entry is not None:
            entry.last_prompt_tokens = prompt_tokens
            entry.max_context_tokens = max_context_tokens
            entry.context_model = model
            entry.context_provider_id = provider_id
            if reserve_output_tokens is not None:
                entry.reserve_output_tokens = reserve_output_tokens
            if effective_reserve_output_tokens is not None:
                entry.effective_reserve_output_tokens = effective_reserve_output_tokens

    def get_context_usage(self, session_id: str) -> dict[str, Any] | None:
        """Get the cached context usage for a session.

        Returns a dict with prompt usage, effective reserve/budget values,
        and model identity, or ``None`` if no data is cached.
        """

        entry = self.get_entry(session_id)
        if entry is None or entry.max_context_tokens <= 0:
            return None
        effective_prompt_budget = max(
            0, entry.max_context_tokens - entry.effective_reserve_output_tokens
        )
        return {
            "prompt_tokens": entry.last_prompt_tokens,
            "max_context_tokens": entry.max_context_tokens,
            "percentage": round(entry.last_prompt_tokens / entry.max_context_tokens * 100, 1),
            "model": entry.context_model,
            "provider_id": entry.context_provider_id,
            "reasoning_effort": entry.reasoning_effort_override,
            "reserve_output_tokens": entry.reserve_output_tokens,
            "effective_reserve_output_tokens": entry.effective_reserve_output_tokens,
            "reserve_output_tokens_clamped": (
                entry.reserve_output_tokens != entry.effective_reserve_output_tokens
            ),
            "effective_prompt_budget": effective_prompt_budget,
            "loop_pressure_threshold": int(effective_prompt_budget * 0.95),
            "last_llm_usage": dict(entry.last_llm_usage),
        }

    def update_last_llm_usage(self, session_id: str, usage: dict[str, int] | None) -> None:
        """Store provider-reported token usage for the latest LLM call."""

        entry = self._entries.get(session_id)
        if entry is None:
            return
        entry.last_llm_usage = dict(usage or {})

    def update_tool_runtime_info(self, session_id: str, info: dict[str, Any] | None) -> None:
        """Store the latest tool-exposure runtime metadata for a session."""

        entry = self._entries.get(session_id)
        if entry is None:
            return
        entry.last_tool_runtime_info = dict(info or {})

    def get_tool_runtime_info(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest tool-exposure runtime metadata for a session."""

        entry = self.get_entry(session_id)
        if entry is None or not entry.last_tool_runtime_info:
            return None
        return dict(entry.last_tool_runtime_info)

    def get_activated_skill_tool_ids(self, session_id: str) -> set[str]:
        entry = self.get_entry(session_id)
        if entry is None:
            return set()
        return set(entry.activated_skill_tool_ids)

    def activate_skill_tools(self, session_id: str, skill_id: str, tool_ids: set[str]) -> None:
        entry = self.get_entry(session_id)
        if entry is None:
            return
        entry.activated_skill_tool_ids.update(tool_ids)

    def get_skill_tool_classification(self, session_id: str, cache_key: str) -> list[str] | None:
        entry = self.get_entry(session_id)
        if entry is None:
            return None
        cached = entry.skill_tool_classifications.get(cache_key)
        return list(cached) if cached is not None else None

    def set_skill_tool_classification(
        self, session_id: str, cache_key: str, tool_ids: list[str]
    ) -> None:
        entry = self.get_entry(session_id)
        if entry is None:
            return
        entry.skill_tool_classifications[cache_key] = list(tool_ids)

    def note_context_reserve_clamp(self, session_id: str) -> bool:
        """Return ``True`` the first time a session clamps output reserve."""

        entry = self.get_entry(session_id)
        if entry is None or entry.context_reserve_clamp_warned:
            return False
        entry.context_reserve_clamp_warned = True
        return True

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

    def get_prefix_entries(self, session_id: str) -> list[ImmutablePrefixEntry]:
        """Return the current immutable prefix constituents for a session."""

        entry = self.get_entry(session_id)
        if entry is None:
            return []
        return list(sort_prefix_entries(entry.prefix_entries))

    def get_project_contexts(self, session_id: str) -> list[ProjectContextEntry]:
        """Return frozen project contexts for the session in stable load order."""

        entry = self.get_entry(session_id)
        if entry is None:
            return []
        return sorted(
            entry.project_contexts.values(), key=lambda item: (item.seq, item.project_root)
        )

    def get_project_context(
        self, session_id: str, project_root: str | None
    ) -> ProjectContextEntry | None:
        """Return one frozen project context by canonical project root."""

        normalized = normalize_project_path(project_root)
        if normalized is None:
            return None
        entry = self.get_entry(session_id)
        if entry is None:
            return None
        return entry.project_contexts.get(normalized)

    async def store_project_context(
        self,
        session_id: str,
        project_context: ProjectContextEntry,
    ) -> ProjectContextEntry:
        """Persist one frozen project context in cache state."""

        normalized_root = normalize_project_path(project_context.project_root)
        if normalized_root is None:
            raise ValueError("project_root is required")
        entry = self.get_entry(session_id)
        if entry is None:
            raise KeyError(f"Unknown session cache entry: {session_id}")
        normalized_context = ProjectContextEntry(
            project_root=normalized_root,
            status=project_context.status,
            source_path=normalize_project_path(project_context.source_path),
            content=project_context.content,
            content_hash=project_context.content_hash,
            working_directory=normalize_project_path(project_context.working_directory),
            seq=int(project_context.seq or 0),
        )
        async with entry.lock:
            existing = entry.project_contexts.get(normalized_root)
            if existing is None:
                entry.project_contexts[normalized_root] = normalized_context
            elif existing.status != PROJECT_CONTEXT_STATUS_LOADED:
                normalized_context.seq = existing.seq or normalized_context.seq
                entry.project_contexts[normalized_root] = normalized_context
            entry.touched_at = monotonic()
        await self._redis_set(entry)
        return entry.project_contexts[normalized_root]

    def needs_prefix_repair(self, session_id: str) -> bool:
        """Return whether the cache knows the session is missing a prefix snapshot."""

        entry = self.get_entry(session_id)
        return bool(entry and entry.prefix_repair_needed)

    def get_last_repair_attempt_at(self, session_id: str) -> float | None:
        """Return the monotonic timestamp of the last repair attempt, if any."""

        entry = self.get_entry(session_id)
        return None if entry is None else entry.last_repair_attempt_at

    async def note_repair_attempt(self, session_id: str) -> None:
        """Record that a repair attempt was made for this session."""

        entry = self.get_entry(session_id)
        if entry is None:
            return
        async with entry.lock:
            entry.last_repair_attempt_at = monotonic()
            entry.touched_at = monotonic()
        await self._redis_set(entry)

    async def store_prefix_snapshot(
        self,
        session_id: str,
        entries: list[ImmutablePrefixEntry],
        *,
        snapshot_seq: int,
        snapshot_source: str,
    ) -> None:
        """Persist the active immutable-prefix snapshot in the cache."""

        entry = self.get_entry(session_id)
        if entry is None:
            return
        async with entry.lock:
            entry.prefix_entries = sort_prefix_entries(list(entries))
            entry.context_snapshot_seq = snapshot_seq
            entry.context_snapshot_source = snapshot_source
            entry.prefix_repair_needed = False
            compaction_summary = entry.last_compaction_summary
            for prefix_entry in entry.prefix_entries:
                if prefix_entry.source == "compaction_summary":
                    compaction_summary = prefix_entry.content
                    break
            entry.last_compaction_summary = compaction_summary
            entry.touched_at = monotonic()
        await self._redis_set(entry)

    async def mark_prefix_repair_needed(self, session_id: str) -> None:
        """Mark a session as requiring prefix repair on the next turn."""

        entry = self.get_entry(session_id)
        if entry is None:
            return
        async with entry.lock:
            entry.prefix_repair_needed = True
            entry.touched_at = monotonic()
        await self._redis_set(entry)

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
                                "initialized": entry.initialized,
                                "prefix_entry_count": len(entry.prefix_entries),
                                "context_snapshot_seq": entry.context_snapshot_seq,
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
        if getattr(event_read, "missing_stream_fallback_used", False):
            logger.warning(
                "cache: cold load fell back to missing Intaris stream",
                extra={
                    "extra_data": {
                        "session_id": session.session_id,
                        "intaris_session_id": entry.intaris_session_id,
                    }
                },
            )
        self._replace_from_intaris_events(entry, event_read.events)
        entry.last_event_seq = event_read.last_seq
        entry.initialized = True

    def _replace_from_intaris_events(
        self, entry: CachedSessionState, raw_events: list[dict[str, Any]]
    ) -> None:
        """Replace event-derived cache state from a full Intaris stream read."""

        entry.events = []
        entry.last_event_seq = 0
        entry.last_compaction_seq = 0
        entry.last_compaction_summary = None
        entry.prefix_entries = []
        entry.context_snapshot_seq = 0
        entry.context_snapshot_source = None
        entry.prefix_repair_needed = False
        entry.project_contexts = {}
        self._apply_intaris_events(entry, raw_events)
        self._rebuild_prefix_from_raw_events(entry, raw_events)

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

    def _rebuild_prefix_from_cached_events(
        self,
        entry: CachedSessionState,
        cached_events: list[CachedEvent],
    ) -> None:
        latest_snapshot = max(
            (item for item in cached_events if item.type == "context_snapshot"),
            key=lambda item: item.seq,
            default=None,
        )
        if latest_snapshot is None:
            return

        existing_by_seq = {item.seq: item for item in entry.prefix_entries}
        for event in cached_events:
            if event.type in {"system_message", "developer_message"}:
                data = event.data
                content = data.get("content")
                source = data.get("source")
                role = data.get("role")
                if isinstance(content, str) and isinstance(source, str) and isinstance(role, str):
                    existing_by_seq[event.seq] = ImmutablePrefixEntry(
                        role=role,
                        source=source,
                        content=content,
                        seq=event.seq,
                    )

        snapshot_data = latest_snapshot.data
        rebuilt_entries: list[ImmutablePrefixEntry] = []
        for item in snapshot_data.get("entries", []):
            if not isinstance(item, dict):
                continue
            seq = int(item.get("seq", 0))
            prefix_entry = existing_by_seq.get(seq)
            if prefix_entry is not None:
                rebuilt_entries.append(prefix_entry)

        entry.prefix_entries = sort_prefix_entries(rebuilt_entries)
        entry.context_snapshot_seq = latest_snapshot.seq
        snapshot_source = snapshot_data.get("source")
        entry.context_snapshot_source = (
            snapshot_source if isinstance(snapshot_source, str) else None
        )
        entry.prefix_repair_needed = False
        compaction_summary = entry.last_compaction_summary
        for prefix_entry in entry.prefix_entries:
            if prefix_entry.source == "compaction_summary":
                compaction_summary = prefix_entry.content
                break
        entry.last_compaction_summary = compaction_summary

    def _rebuild_prefix_from_raw_events(
        self,
        entry: CachedSessionState,
        raw_events: list[dict[str, Any]],
    ) -> None:
        raw_event_type_counts = collections.Counter(
            str(raw_event.get("type") or "") for raw_event in raw_events
        )
        latest_snapshot: dict[str, Any] | None = None
        for raw_event in raw_events:
            if raw_event.get("type") != "context_snapshot":
                continue
            if latest_snapshot is None or int(raw_event.get("seq", 0)) >= int(
                latest_snapshot.get("seq", 0)
            ):
                latest_snapshot = raw_event

        if latest_snapshot is None:
            entry.prefix_entries = []
            entry.context_snapshot_seq = 0
            entry.context_snapshot_source = None
            entry.prefix_repair_needed = any(
                str(raw_event.get("type") or "")
                in {"system_message", "developer_message", "user_message", "assistant_message"}
                for raw_event in raw_events
            )
            if entry.prefix_repair_needed:
                logger.warning(
                    "cache: no Intaris context snapshot found while rebuilding immutable prefix",
                    extra={
                        "extra_data": {
                            "session_id": entry.session_id,
                            "intaris_session_id": entry.intaris_session_id,
                            "raw_event_count": len(raw_events),
                            "event_type_counts": dict(raw_event_type_counts),
                        }
                    },
                )
            return

        seq_to_message: dict[int, dict[str, Any]] = {}
        for raw_event in raw_events:
            if raw_event.get("type") not in {"system_message", "developer_message"}:
                continue
            seq_to_message[int(raw_event.get("seq", 0))] = raw_event

        snapshot_data = latest_snapshot.get("data", {})
        rebuilt_entries: list[ImmutablePrefixEntry] = []
        missing_refs: list[int] = []
        for item in snapshot_data.get("entries", []):
            if not isinstance(item, dict):
                continue
            seq = int(item.get("seq", 0))
            message = seq_to_message.get(seq)
            if message is None:
                missing_refs.append(seq)
                continue
            data = message.get("data", {})
            content = data.get("content")
            source = data.get("source") or item.get("source")
            role = data.get("role") or item.get("role")
            if (
                not isinstance(content, str)
                or not isinstance(source, str)
                or not isinstance(role, str)
            ):
                continue
            rebuilt_entries.append(
                ImmutablePrefixEntry(role=role, source=source, content=content, seq=seq)
            )

        entry.prefix_entries = sort_prefix_entries(rebuilt_entries)
        entry.context_snapshot_seq = int(latest_snapshot.get("seq", 0))
        snapshot_source = snapshot_data.get("source")
        entry.context_snapshot_source = (
            snapshot_source if isinstance(snapshot_source, str) else None
        )
        entry.prefix_repair_needed = False
        compaction_summary = entry.last_compaction_summary
        for prefix_entry in entry.prefix_entries:
            if prefix_entry.source == "compaction_summary":
                compaction_summary = prefix_entry.content
                break
        entry.last_compaction_summary = compaction_summary
        if missing_refs:
            logger.warning(
                "cache: Intaris context snapshot references missing prefix messages",
                extra={
                    "extra_data": {
                        "session_id": entry.session_id,
                        "intaris_session_id": entry.intaris_session_id,
                        "context_snapshot_seq": entry.context_snapshot_seq,
                        "context_snapshot_source": entry.context_snapshot_source,
                        "snapshot_entry_count": len(snapshot_data.get("entries", [])),
                        "rebuilt_entry_count": len(entry.prefix_entries),
                        "missing_ref_count": len(missing_refs),
                        "missing_ref_sample": missing_refs[:10],
                        "available_prefix_message_count": len(seq_to_message),
                        "raw_event_count": len(raw_events),
                        "event_type_counts": dict(raw_event_type_counts),
                    }
                },
            )
        elif not entry.prefix_entries:
            logger.warning(
                "cache: Intaris context snapshot rebuilt zero immutable prefix entries",
                extra={
                    "extra_data": {
                        "session_id": entry.session_id,
                        "intaris_session_id": entry.intaris_session_id,
                        "context_snapshot_seq": entry.context_snapshot_seq,
                        "context_snapshot_source": entry.context_snapshot_source,
                        "snapshot_entry_count": len(snapshot_data.get("entries", [])),
                        "available_prefix_message_count": len(seq_to_message),
                        "raw_event_count": len(raw_events),
                        "event_type_counts": dict(raw_event_type_counts),
                    }
                },
            )

    def _apply_cached_event(self, entry: CachedSessionState, event: CachedEvent) -> None:
        if event.type in PREFIX_EVENT_TYPES:
            project_context = project_context_from_event_data(event.data, seq=event.seq)
            if project_context is not None:
                existing = entry.project_contexts.get(project_context.project_root)
                if (
                    existing is None
                    or existing.status != PROJECT_CONTEXT_STATUS_LOADED
                    or existing.seq <= 0
                ):
                    entry.project_contexts[project_context.project_root] = project_context
            entry.last_event_seq = max(entry.last_event_seq, event.seq)
            return
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
