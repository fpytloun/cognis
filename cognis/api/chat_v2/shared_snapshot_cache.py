"""Shared generation-fenced Redis operation for canonical Chat v2 snapshots."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from cognis.api.chat_v2.cached_event_store import (
    CACHE_SCHEMA_VERSION,
    CachedSessionEventStore,
    DerivedEnvelopeEncoding,
    EventCacheBounds,
    EventCachePolicy,
    GenerationFence,
)
from cognis.api.chat_v2.cursors import validate_cursor
from cognis.api.chat_v2.schemas import ChatSnapshot
from cognis.api.chat_v2.snapshot_metrics import (
    SNAPSHOT_CACHE_METRICS,
    SnapshotRequestTier,
    WarmFailureReason,
)
from cognis.api.chat_v2.sync import ConversationSessionRef, current_projection_version
from cognis.core.redis_service import RedisService

_DEFAULT_LOCK_LEASE_SECONDS = 15
_DEFAULT_BUILD_DEADLINE_SECONDS = 120.0
_LOCK_POLL_MAX_SECONDS = 0.1
_RENEW_LOCK = """
-- cognis-chat-snapshot-lock-renew-v1
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""
_LOCK_STATUS = """
-- cognis-chat-snapshot-lock-status-v1
local value = redis.call('GET', KEYS[1])
if not value then
  return {-2}
end
return {redis.call('PTTL', KEYS[1])}
"""
_UNLOCK = """
-- cognis-chat-snapshot-unlock-v1
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _LeaseLost(Exception):
    pass


class _FenceRejected(Exception):
    pass


@dataclass(slots=True)
class _L1Snapshot:
    snapshot: ChatSnapshot
    payload: bytes
    accounted_size: int
    expires_at: float
    session_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SnapshotIdentity:
    value_key: str
    local_key: str
    lock_key: str
    fence: GenerationFence
    lineage: tuple[dict[str, Any], ...]
    overview_fence: str
    overview_ready: bool


@dataclass(frozen=True, slots=True)
class _DecodedSnapshot:
    status: str
    snapshot: ChatSnapshot | None = None
    raw_size: int = 0


_SnapshotReadStatus = Literal[
    "available",
    "redis_unavailable",
    "codec_saturated",
    "indeterminate",
]
CachedSnapshotStatus = Literal["hit_l1", "hit_redis", "miss", "unavailable", "error"]


@dataclass(frozen=True, slots=True)
class SnapshotCacheResult:
    snapshot: ChatSnapshot | None
    tier: SnapshotRequestTier
    warm_failure: WarmFailureReason | None = None


@dataclass(frozen=True, slots=True)
class CachedSnapshotResult:
    snapshot: ChatSnapshot | None
    status: CachedSnapshotStatus


@dataclass(slots=True)
class SnapshotRequestTrace:
    """Request-scoped cache path selected before potentially failing work."""

    tier: SnapshotRequestTier = "unknown"

    def select(self, tier: SnapshotRequestTier) -> None:
        self.tier = tier


class SharedChatSnapshotCache:
    """Application-scoped snapshot operation owned by the canonical event cache."""

    def __init__(
        self,
        *,
        event_store: CachedSessionEventStore,
        redis_service: RedisService,
        policy: EventCachePolicy,
        bounds: EventCacheBounds | None = None,
        clock: Callable[[], float],
        lock_lease_seconds: int = _DEFAULT_LOCK_LEASE_SECONDS,
        build_deadline_seconds: float = _DEFAULT_BUILD_DEADLINE_SECONDS,
    ) -> None:
        if lock_lease_seconds < 1:
            raise ValueError("lock_lease_seconds must be positive")
        if build_deadline_seconds <= lock_lease_seconds:
            raise ValueError("build_deadline_seconds must exceed the lock lease")
        self._event_store = event_store
        self._redis = redis_service
        self._policy = policy
        self._bounds = bounds or EventCacheBounds()
        self._clock = clock
        self._lock_lease_seconds = lock_lease_seconds
        self._build_deadline_seconds = build_deadline_seconds
        self._l1: OrderedDict[str, _L1Snapshot] = OrderedDict()
        self._session_index: dict[str, set[str]] = {}
        self._l1_bytes = 0
        self._inflight: dict[str, asyncio.Task[SnapshotCacheResult]] = {}
        self._owned_locks = 0
        self._warm_scope_keys: OrderedDict[str, None] = OrderedDict()
        self._scope_outcomes: OrderedDict[str, str] = OrderedDict()
        event_store.add_generation_invalidation_listener(self.invalidate_session_token)

    async def aclose(self) -> None:
        self._event_store.remove_generation_invalidation_listener(self.invalidate_session_token)
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._l1.clear()
        self._session_index.clear()
        self._l1_bytes = 0
        self._owned_locks = 0
        SNAPSHOT_CACHE_METRICS.l1_resident(0, 0)
        SNAPSHOT_CACHE_METRICS.inflight_builds(0)
        SNAPSHOT_CACHE_METRICS.owned_locks(0)

    def has_warm_scope(self, scope_key: str) -> bool:
        """Return whether this process has successfully observed a warm canonical scope."""

        return scope_key in self._warm_scope_keys

    def warm_outcome(self, scope_key: str) -> Literal["succeeded", "skipped", "retry"]:
        outcome = self._scope_outcomes.get(scope_key, "retry")
        if outcome == "succeeded":
            return "succeeded"
        if outcome == "skipped":
            return "skipped"
        return "retry"

    @property
    def warming_configured(self) -> bool:
        return bool(self._redis.configured)

    @property
    def warming_available(self) -> bool:
        return bool(self._redis.available)

    def invalidate_session_token(self, session_token: str) -> None:
        """Synchronously evict derived L1 entries reached by one session generation."""

        for key in tuple(self._session_index.get(session_token, ())):
            self._remove_l1(key)

    async def get_or_build(
        self,
        *,
        authority_token: str,
        scope_key: str,
        session_refs: Sequence[ConversationSessionRef],
        cursor_secret: str,
        build: Callable[[], Awaitable[ChatSnapshot]],
        overview_fence: str = "",
        overview_coverage: Sequence[tuple[str, int]] | None = None,
        fail_open: bool = True,
    ) -> ChatSnapshot | None:
        result = await self.get_or_build_result(
            authority_token=authority_token,
            scope_key=scope_key,
            session_refs=session_refs,
            cursor_secret=cursor_secret,
            build=build,
            overview_fence=overview_fence,
            overview_coverage=overview_coverage,
            fail_open=fail_open,
        )
        return result.snapshot

    async def get_cached_result(
        self,
        *,
        authority_token: str,
        scope_key: str,
        session_refs: Sequence[ConversationSessionRef],
        cursor_secret: str,
        overview_fence: str = "",
        overview_coverage: Sequence[tuple[str, int]] | None = None,
    ) -> CachedSnapshotResult:
        """Read an identity- and generation-fenced snapshot without coordinating a fill."""

        if not self._redis.configured or not self._redis.available:
            return CachedSnapshotResult(None, "unavailable")
        try:
            identity = await self._identity(
                authority_token=authority_token,
                scope_key=scope_key,
                session_refs=session_refs,
                overview_fence=overview_fence,
                overview_coverage=overview_coverage,
            )
            if identity is None:
                return CachedSnapshotResult(None, "unavailable")
            if not identity.overview_ready:
                return CachedSnapshotResult(None, "miss")
            cached, read_status, cache_tier = await self._read(
                identity,
                authority_token=authority_token,
                scope_key=scope_key,
                cursor_secret=cursor_secret,
            )
        except Exception:
            return CachedSnapshotResult(None, "error")
        if cached is None:
            if read_status in {"redis_unavailable", "indeterminate"}:
                return CachedSnapshotResult(None, "unavailable")
            return CachedSnapshotResult(None, "miss")
        self._remember_warm_scope(scope_key)
        self._remember_outcome(scope_key, "succeeded")
        return CachedSnapshotResult(
            cached,
            "hit_l1" if cache_tier == "l1" else "hit_redis",
        )

    async def get_or_build_result(
        self,
        *,
        authority_token: str,
        scope_key: str,
        session_refs: Sequence[ConversationSessionRef],
        cursor_secret: str,
        build: Callable[[], Awaitable[ChatSnapshot]],
        overview_fence: str = "",
        overview_coverage: Sequence[tuple[str, int]] | None = None,
        fail_open: bool = True,
        request_trace: SnapshotRequestTrace | None = None,
    ) -> SnapshotCacheResult:
        if not self._redis.configured or not self._redis.available:
            if request_trace is not None:
                request_trace.select("bypass")
            SNAPSHOT_CACHE_METRICS.redis_bypass()
            self._remember_outcome(scope_key, "retry")
            return SnapshotCacheResult(
                await build() if fail_open else None,
                "bypass",
                "redis_unavailable",
            )
        identity = await self._identity(
            authority_token=authority_token,
            scope_key=scope_key,
            session_refs=session_refs,
            overview_fence=overview_fence,
            overview_coverage=overview_coverage,
        )
        if identity is None:
            if request_trace is not None:
                request_trace.select("bypass")
            SNAPSHOT_CACHE_METRICS.redis_bypass()
            self._remember_outcome(scope_key, "retry")
            return SnapshotCacheResult(
                await build() if fail_open else None,
                "bypass",
                "internal",
            )
        if not identity.overview_ready:
            if request_trace is not None:
                request_trace.select("bypass")
            self._remember_outcome(scope_key, "retry" if fail_open else "skipped")
            return SnapshotCacheResult(
                await build() if fail_open else None,
                "bypass",
                "context_changed",
            )
        cached, read_status, cache_tier = await self._read(
            identity,
            authority_token=authority_token,
            scope_key=scope_key,
            cursor_secret=cursor_secret,
        )
        if cached is not None:
            if request_trace is not None:
                request_trace.select(cache_tier or "redis")
            self._remember_warm_scope(scope_key)
            self._remember_outcome(scope_key, "succeeded")
            return SnapshotCacheResult(cached, cache_tier or "redis")
        if read_status != "available":
            if request_trace is not None:
                request_trace.select("bypass")
            SNAPSHOT_CACHE_METRICS.redis_bypass()
            self._remember_outcome(scope_key, "retry")
            return SnapshotCacheResult(
                await build() if fail_open else None,
                "bypass",
                "redis_unavailable" if read_status == "redis_unavailable" else None,
            )
        existing = self._inflight.get(identity.local_key)
        if existing is not None:
            if request_trace is not None:
                request_trace.select("build")
            result = await asyncio.shield(existing)
            if request_trace is not None:
                request_trace.select(result.tier)
            if result.snapshot is None and fail_open:
                if request_trace is not None:
                    request_trace.select("bypass")
                return SnapshotCacheResult(await build(), "bypass", result.warm_failure)
            return result
        if len(self._inflight) >= self._bounds.l1_max_entries:
            if request_trace is not None:
                request_trace.select("bypass")
            SNAPSHOT_CACHE_METRICS.overflow("inflight")
            self._remember_outcome(scope_key, "retry")
            return SnapshotCacheResult(
                await build() if fail_open else None,
                "bypass",
                "internal",
            )

        async def fill() -> SnapshotCacheResult:
            try:
                current_identity = identity
                for _attempt in range(3):
                    try:
                        return await self._coordinate_fill(
                            identity=current_identity,
                            authority_token=authority_token,
                            scope_key=scope_key,
                            cursor_secret=cursor_secret,
                            build=build,
                            fail_open=fail_open,
                            request_trace=request_trace,
                        )
                    except _FenceRejected:
                        refreshed = await self._identity(
                            authority_token=authority_token,
                            scope_key=scope_key,
                            session_refs=session_refs,
                            overview_fence=overview_fence,
                            overview_coverage=overview_coverage,
                        )
                        if refreshed is None:
                            break
                        current_identity = refreshed
                if request_trace is not None:
                    request_trace.select("bypass")
                return SnapshotCacheResult(
                    await build() if fail_open else None,
                    "bypass",
                    "context_changed",
                )
            finally:
                current = asyncio.current_task()
                if self._inflight.get(identity.local_key) is current:
                    self._inflight.pop(identity.local_key, None)
                    SNAPSHOT_CACHE_METRICS.inflight_builds(len(self._inflight))

        task = asyncio.create_task(fill())
        self._inflight[identity.local_key] = task
        SNAPSHOT_CACHE_METRICS.inflight_builds(len(self._inflight))
        result = await asyncio.shield(task)
        if result.snapshot is None and fail_open:
            if request_trace is not None:
                request_trace.select("bypass")
            return SnapshotCacheResult(await build(), "bypass", result.warm_failure)
        return result

    async def _coordinate_fill(
        self,
        *,
        identity: _SnapshotIdentity,
        authority_token: str,
        scope_key: str,
        cursor_secret: str,
        build: Callable[[], Awaitable[ChatSnapshot]],
        fail_open: bool,
        request_trace: SnapshotRequestTrace | None,
    ) -> SnapshotCacheResult:
        token = secrets.token_hex(16).encode()
        while True:
            acquired = await self._redis.set_if_absent(
                identity.lock_key,
                token,
                ttl_seconds=self._lock_lease_seconds,
            )
            if acquired is None:
                if request_trace is not None:
                    request_trace.select("bypass")
                SNAPSHOT_CACHE_METRICS.redis_bypass()
                self._remember_outcome(scope_key, "retry")
                return SnapshotCacheResult(
                    await build() if fail_open else None,
                    "bypass",
                    "redis_unavailable",
                )
            if acquired:
                if request_trace is not None:
                    request_trace.select("build")
                self._owned_locks += 1
                SNAPSHOT_CACHE_METRICS.owned_locks(self._owned_locks)
                try:
                    snapshot = await self._owner_fill(
                        identity=identity,
                        lock_token=token,
                        authority_token=authority_token,
                        scope_key=scope_key,
                        cursor_secret=cursor_secret,
                        build=build,
                    )
                    return SnapshotCacheResult(
                        snapshot,
                        "build",
                        (
                            "redis_unavailable"
                            if self._scope_outcomes.get(scope_key) == "retry"
                            and not self._redis.available
                            else None
                        ),
                    )
                except TimeoutError:
                    self._remember_outcome(scope_key, "retry")
                    raise
                except _LeaseLost:
                    if not self._redis.available:
                        if request_trace is not None:
                            request_trace.select("bypass")
                        return SnapshotCacheResult(
                            await build() if fail_open else None,
                            "bypass",
                            "redis_unavailable",
                        )
                    continue
                finally:
                    self._owned_locks -= 1
                    SNAPSHOT_CACHE_METRICS.owned_locks(self._owned_locks)
            SNAPSHOT_CACHE_METRICS.lock_wait()
            cached, read_status, cache_tier = await self._wait_for_owner(
                identity,
                authority_token=authority_token,
                scope_key=scope_key,
                cursor_secret=cursor_secret,
            )
            if cached is not None:
                if request_trace is not None:
                    request_trace.select(cache_tier or "redis")
                self._remember_warm_scope(scope_key)
                self._remember_outcome(scope_key, "succeeded")
                return SnapshotCacheResult(cached, cache_tier or "redis")
            if read_status != "available":
                if request_trace is not None:
                    request_trace.select("bypass")
                SNAPSHOT_CACHE_METRICS.redis_bypass()
                self._remember_outcome(scope_key, "retry")
                return SnapshotCacheResult(
                    await build() if fail_open else None,
                    "bypass",
                    "redis_unavailable" if read_status == "redis_unavailable" else None,
                )

    async def _wait_for_owner(
        self,
        identity: _SnapshotIdentity,
        *,
        authority_token: str,
        scope_key: str,
        cursor_secret: str,
    ) -> tuple[ChatSnapshot | None, _SnapshotReadStatus, SnapshotRequestTier | None]:
        while True:
            cached, read_status, cache_tier = await self._read(
                identity,
                authority_token=authority_token,
                scope_key=scope_key,
                cursor_secret=cursor_secret,
            )
            if cached is not None or read_status != "available":
                return cached, read_status, cache_tier
            status = await self._redis.eval(_LOCK_STATUS, keys=[identity.lock_key])
            if (
                status is None
                or not isinstance(status, (list, tuple))
                or len(status) != 1
                or isinstance(status[0], bool)
            ):
                return (
                    None,
                    "redis_unavailable" if not self._redis.available else "indeterminate",
                    None,
                )
            try:
                remaining_ms = int(status[0])
            except (TypeError, ValueError):
                return (
                    None,
                    "redis_unavailable" if not self._redis.available else "indeterminate",
                    None,
                )
            if remaining_ms < 0:
                SNAPSHOT_CACHE_METRICS.lock_fallback()
                return None, "available", None
            await asyncio.sleep(min(_LOCK_POLL_MAX_SECONDS, max(0.01, remaining_ms / 1000)))

    async def _owner_fill(
        self,
        *,
        identity: _SnapshotIdentity,
        lock_token: bytes,
        authority_token: str,
        scope_key: str,
        cursor_secret: str,
        build: Callable[[], Awaitable[ChatSnapshot]],
    ) -> ChatSnapshot:
        stop_renewal = asyncio.Event()
        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_lease(
                identity.lock_key,
                lock_token,
                stop_renewal,
                lease_lost,
            )
        )
        work: asyncio.Task[ChatSnapshot] | None = None
        lost_wait: asyncio.Task[bool] | None = None
        try:

            async def build_and_publish() -> ChatSnapshot:
                SNAPSHOT_CACHE_METRICS.build()
                snapshot = await build()
                cursor = validate_cursor(
                    snapshot.cursor,
                    cursor_secret,
                    scope_key=scope_key,
                    projection_version=current_projection_version(),
                )
                watermarks = {item.session_id: item.last_seq for item in cursor.session_watermarks}
                if any(
                    watermarks.get(entry.backing_session_id, -1) < entry.watermark_floor
                    for entry in identity.fence.entries
                ):
                    raise _FenceRejected
                encoding = await self._serialize(
                    snapshot=snapshot,
                    authority_token=authority_token,
                    scope_key=scope_key,
                    lineage=identity.lineage,
                    overview_fence=identity.overview_fence,
                    cursor_secret=cursor_secret,
                )
                if encoding is None:
                    SNAPSHOT_CACHE_METRICS.oversized_or_invalid()
                    self._remember_outcome(scope_key, "skipped")
                    return snapshot
                wrote = await self._event_store.generation_fenced_write(
                    identity.value_key,
                    encoding.payload,
                    identity.fence,
                    ttl_seconds=self._policy.ttl_seconds,
                    guard_key=identity.lock_key,
                    guard_value=lock_token,
                )
                if wrote:
                    SNAPSHOT_CACHE_METRICS.redis_value(len(encoding.payload))
                    self._put_l1(
                        identity.local_key,
                        identity.fence,
                        snapshot,
                        encoding.payload,
                        accounted_size=encoding.raw_size,
                    )
                    self._remember_warm_scope(scope_key)
                    self._remember_outcome(scope_key, "succeeded")
                elif wrote is None:
                    SNAPSHOT_CACHE_METRICS.redis_bypass()
                    self._remember_outcome(scope_key, "retry")
                else:
                    self._remember_outcome(scope_key, "retry")
                    raise _FenceRejected
                return snapshot

            work = asyncio.create_task(build_and_publish())
            lost_wait = asyncio.create_task(lease_lost.wait())
            async with asyncio.timeout(self._build_deadline_seconds):
                done, _pending = await asyncio.wait(
                    {work, lost_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lost_wait in done and lease_lost.is_set():
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise _LeaseLost
                return await work
        finally:
            stop_renewal.set()
            if lost_wait is not None:
                lost_wait.cancel()
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            renewal.cancel()
            await asyncio.gather(
                renewal,
                *(item for item in (lost_wait,) if item is not None),
                return_exceptions=True,
            )
            await self._redis.eval(_UNLOCK, keys=[identity.lock_key], args=[lock_token])

    async def _renew_lease(
        self,
        lock_key: str,
        lock_token: bytes,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(0.1, self._lock_lease_seconds / 3)
        while not stop.is_set():
            await asyncio.sleep(interval)
            renewed = await self._redis.eval(
                _RENEW_LOCK,
                keys=[lock_key],
                args=[lock_token, self._lock_lease_seconds],
            )
            if renewed != 1:
                lease_lost.set()
                return

    async def _identity(
        self,
        *,
        authority_token: str,
        scope_key: str,
        session_refs: Sequence[ConversationSessionRef],
        overview_fence: str = "",
        overview_coverage: Sequence[tuple[str, int]] | None = None,
    ) -> _SnapshotIdentity | None:
        fence = await self._event_store.create_generation_fence(
            [(ref.store, ref.event_store_session_id, ref.authority_token) for ref in session_refs]
        )
        if fence is None:
            return None
        lineage = tuple(
            {
                "session_id": ref.session_id,
                "event_store_session_id": ref.event_store_session_id,
                "store": ref.store,
                "role": ref.role,
                "ordinal": ref.ordinal,
                "authority_token": ref.authority_token,
                "generation": entry.generation,
            }
            for ref, entry in zip(session_refs, fence.entries, strict=True)
        )
        digest = self._event_store.derived_key_digest(
            "snapshot",
            authority_token,
            scope_key,
            current_projection_version(),
            overview_fence,
        )
        value_key = f"cognis:chat-event-cache:v{CACHE_SCHEMA_VERSION}:snapshot:{digest}"
        local_digest = self._event_store.derived_key_digest(
            "snapshot-local",
            digest,
            repr(lineage),
        )
        local_key = f"{value_key}:local:{local_digest}"
        covered_by_session = dict(overview_coverage or ())
        overview_ready = overview_coverage is None or all(
            covered_by_session.get(entry.backing_session_id, -1) >= entry.watermark_floor
            for entry in fence.entries
        )
        return _SnapshotIdentity(
            value_key=value_key,
            local_key=local_key,
            lock_key=f"{value_key}:lock",
            fence=fence,
            lineage=lineage,
            overview_fence=overview_fence,
            overview_ready=overview_ready,
        )

    async def _read(
        self,
        identity: _SnapshotIdentity,
        *,
        authority_token: str,
        scope_key: str,
        cursor_secret: str,
    ) -> tuple[ChatSnapshot | None, _SnapshotReadStatus, SnapshotRequestTier | None]:
        result = await self._event_store.generation_fenced_read(
            identity.value_key,
            identity.fence,
            ttl_seconds=self._policy.ttl_seconds,
            sliding=self._policy.sliding_expiration,
        )
        if result.status == "unavailable":
            return None, "redis_unavailable", None
        if result.status == "stale":
            self._remove_l1(identity.local_key)
            SNAPSHOT_CACHE_METRICS.miss()
            return None, "available", None
        if result.status == "miss" or result.payload is None:
            self._remove_l1(identity.local_key)
            SNAPSHOT_CACHE_METRICS.miss()
            return None, "available", None
        SNAPSHOT_CACHE_METRICS.redis_value(len(result.payload))

        local = self._l1.get(identity.local_key)
        now = self._clock()
        if (
            local is not None
            and local.expires_at > now
            and hmac.compare_digest(local.payload, result.payload)
        ):
            self._l1.move_to_end(identity.local_key)
            if self._policy.sliding_expiration:
                local.expires_at = now + self._policy.ttl_seconds
            SNAPSHOT_CACHE_METRICS.hit("l1")
            return local.snapshot, "available", "l1"
        self._remove_l1(identity.local_key)
        decoded = await self._deserialize(
            result.payload,
            authority_token=authority_token,
            scope_key=scope_key,
            lineage=identity.lineage,
            overview_fence=identity.overview_fence,
            cursor_secret=cursor_secret,
        )
        if decoded.status == "saturated":
            SNAPSHOT_CACHE_METRICS.codec_saturated()
            return None, "codec_saturated", None
        if decoded.status == "lineage_mismatch":
            SNAPSHOT_CACHE_METRICS.miss()
            return None, "available", None
        if decoded.snapshot is None:
            await self._redis.delete(identity.value_key)
            SNAPSHOT_CACHE_METRICS.miss()
            return None, "available", None
        self._put_l1(
            identity.local_key,
            identity.fence,
            decoded.snapshot,
            result.payload,
            accounted_size=decoded.raw_size,
        )
        SNAPSHOT_CACHE_METRICS.hit("redis")
        return decoded.snapshot, "available", "redis"

    async def _serialize(
        self,
        *,
        snapshot: ChatSnapshot,
        authority_token: str,
        scope_key: str,
        lineage: tuple[dict[str, Any], ...],
        cursor_secret: str,
        overview_fence: str = "",
    ) -> DerivedEnvelopeEncoding | None:
        try:
            cursor = validate_cursor(
                snapshot.cursor,
                cursor_secret,
                scope_key=scope_key,
                projection_version=current_projection_version(),
            )
            envelope = {
                "version": CACHE_SCHEMA_VERSION,
                "operation": "snapshot",
                "authority": authority_token,
                "conversation": self._event_store.derived_key_digest("conversation", scope_key),
                "projection_version": current_projection_version(),
                "overview_fence": overview_fence,
                "lineage": lineage,
                "watermarks": [item.model_dump(mode="json") for item in cursor.session_watermarks],
                "value": snapshot.model_dump(mode="json"),
            }
        except (RecursionError, TypeError, ValueError):
            return None
        return await self._event_store.encode_derived_envelope(envelope)

    async def _deserialize(
        self,
        payload: bytes,
        *,
        authority_token: str,
        scope_key: str,
        lineage: tuple[dict[str, Any], ...],
        cursor_secret: str,
        overview_fence: str = "",
    ) -> _DecodedSnapshot:
        decoding = await self._event_store.decode_derived_envelope(payload)
        if decoding.status != "decoded":
            return _DecodedSnapshot(decoding.status)
        raw = decoding.value
        try:
            if (
                raw is None
                or raw.get("version") != CACHE_SCHEMA_VERSION
                or raw.get("operation") != "snapshot"
                or not hmac.compare_digest(str(raw.get("authority")), authority_token)
                or raw.get("conversation")
                != self._event_store.derived_key_digest("conversation", scope_key)
                or raw.get("projection_version") != current_projection_version()
                or raw.get("overview_fence") != overview_fence
            ):
                return _DecodedSnapshot("invalid")
            if raw.get("lineage") != list(lineage):
                return _DecodedSnapshot("lineage_mismatch")
            snapshot = ChatSnapshot.model_validate(raw.get("value"))
            cursor = validate_cursor(
                snapshot.cursor,
                cursor_secret,
                scope_key=scope_key,
                projection_version=current_projection_version(),
            )
            watermarks = [item.model_dump(mode="json") for item in cursor.session_watermarks]
            if raw.get("watermarks") != watermarks:
                return _DecodedSnapshot("invalid")
            return _DecodedSnapshot("decoded", snapshot, decoding.raw_size)
        except (RecursionError, ValueError, ValidationError):
            return _DecodedSnapshot("invalid")

    def _put_l1(
        self,
        key: str,
        fence: GenerationFence,
        snapshot: ChatSnapshot,
        payload: bytes,
        *,
        accounted_size: int,
    ) -> None:
        if accounted_size > self._bounds.l1_max_bytes:
            return
        tokens = tuple(entry.session_token for entry in fence.entries)
        if len(set(tokens)) > self._bounds.generation_max_sessions:
            SNAPSHOT_CACHE_METRICS.overflow("l1_index")
            return
        self._remove_l1(key)
        while (
            len(set(self._session_index).union(tokens)) > self._bounds.generation_max_sessions
            and self._l1
        ):
            self._remove_l1(next(iter(self._l1)))
        if len(set(self._session_index).union(tokens)) > self._bounds.generation_max_sessions:
            SNAPSHOT_CACHE_METRICS.overflow("l1_index")
            return
        self._l1[key] = _L1Snapshot(
            snapshot=snapshot,
            payload=payload,
            accounted_size=accounted_size,
            expires_at=self._clock() + self._policy.ttl_seconds,
            session_tokens=tokens,
        )
        self._l1_bytes += accounted_size
        for token in tokens:
            self._session_index.setdefault(token, set()).add(key)
        while (
            len(self._l1) > self._bounds.l1_max_entries
            or self._l1_bytes > self._bounds.l1_max_bytes
        ):
            self._remove_l1(next(iter(self._l1)))
        SNAPSHOT_CACHE_METRICS.l1_resident(len(self._l1), self._l1_bytes)

    def _remove_l1(self, key: str) -> None:
        entry = self._l1.pop(key, None)
        if entry is None:
            return
        self._l1_bytes -= entry.accounted_size
        for token in entry.session_tokens:
            keys = self._session_index.get(token)
            if keys is None:
                continue
            keys.discard(key)
            if not keys:
                self._session_index.pop(token, None)
        SNAPSHOT_CACHE_METRICS.l1_resident(len(self._l1), self._l1_bytes)

    def _remember_warm_scope(self, scope_key: str) -> None:
        self._warm_scope_keys.pop(scope_key, None)
        self._warm_scope_keys[scope_key] = None
        while len(self._warm_scope_keys) > self._bounds.generation_max_sessions:
            self._warm_scope_keys.popitem(last=False)

    def _remember_outcome(self, scope_key: str, outcome: str) -> None:
        self._scope_outcomes.pop(scope_key, None)
        self._scope_outcomes[scope_key] = outcome
        while len(self._scope_outcomes) > self._bounds.generation_max_sessions:
            self._scope_outcomes.popitem(last=False)
