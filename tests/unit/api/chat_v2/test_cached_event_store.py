from __future__ import annotations

import asyncio
import base64
import json
import random
import threading
import zlib
from collections.abc import Callable
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.api.chat_v2 import cache_metrics as cache_metrics_module
from cognis.api.chat_v2 import cached_event_store as cache_module
from cognis.api.chat_v2 import shared_snapshot_cache as snapshot_cache_module
from cognis.api.chat_v2.cache_metrics import (
    BYPASSED,
    CACHE_BYTES,
    CACHE_ENTRIES,
    CACHE_ERRORS,
    CACHE_HITS,
    CACHE_MISSES,
    COMPRESSION_OUTCOMES,
    COMPRESSION_RATIO,
    DECODE_FAILURES,
    INVALIDATIONS,
    PAGE_QUERIES,
    RAW_PAYLOAD_BYTES,
    SINGLEFLIGHT_JOINS,
    SLIDING_REFRESH_ERRORS,
    SLIDING_REFRESHES,
    STORED_PAYLOAD_BYTES,
    UPSTREAM_LATENCY,
    UPSTREAM_READS,
    EventCacheMetrics,
)
from cognis.api.chat_v2.cached_event_store import (
    ACTIVE_CACHE_TTL_SECONDS,
    MAX_RAW_VALUE_BYTES,
    BoundSessionEventStore,
    CachedSessionEventStore,
    DerivedEnvelopeDecoding,
    EventCacheBounds,
    EventCachePolicy,
    GenerationFence,
    GenerationFenceEntry,
)
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionWatermark,
)
from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.shared_snapshot_cache import (
    SharedChatSnapshotCache,
    SnapshotCacheResult,
    SnapshotRequestTrace,
)
from cognis.api.chat_v2.snapshot_metrics import (
    INFLIGHT_BUILDS,
    L1_BYTES,
    L1_ENTRIES,
    OWNED_LOCKS,
)
from cognis.api.chat_v2.sync import (
    ConversationSessionRef,
    build_chat_snapshot,
    clear_chat_v2_read_caches,
)
from cognis.api.websocket import WebSocketConnectionManager
from cognis.core.event_append_invalidation import EventAppendInvalidationDispatcher
from cognis.core.events import Event, EventType
from cognis.core.redis_service import RedisService
from cognis.providers.guardrails.events import EventAppendNotification, EventStoreAuthority
from cognis.runtime_context import (
    current_agent_id,
    current_agent_owner_email,
    current_user_email,
)

AUTHORITY = EventStoreAuthority("user@example.com", "agent-a", "owner@example.com")
OTHER_AUTHORITY = EventStoreAuthority("other@example.com", "agent-a", "owner@example.com")


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeRedis:
    """Byte-oriented RedisService fake with the cache Lua semantics."""

    def __init__(self, clock: Callable[[], float], *, configured: bool = True) -> None:
        self.configured = configured
        self._available = configured
        self.availability_epoch = 0
        self.clock = clock
        self.values: dict[str, tuple[bytes, float]] = {}
        self.deleted: list[str] = []
        self.fail = False
        self.scripts: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, available: bool) -> None:
        if self._available != available:
            self._available = available
            self.availability_epoch += 1

    def _key(self, key: str | bytes) -> str:
        return key.decode() if isinstance(key, bytes) else key

    def _purge(self, key: str) -> None:
        value = self.values.get(key)
        if value is not None and value[1] <= self.clock():
            self.values.pop(key, None)

    async def get(self, key: str | bytes) -> bytes | None:
        if self.fail or not self.configured:
            self.available = False
            return None
        normalized = self._key(key)
        self._purge(normalized)
        self.available = True
        value = self.values.get(normalized)
        return value[0] if value is not None else None

    async def set(self, key: str | bytes, value: bytes, *, ttl_seconds: int) -> bool:
        if self.fail or not self.configured:
            self.available = False
            return False
        self.values[self._key(key)] = (bytes(value), self.clock() + ttl_seconds)
        self.available = True
        return True

    async def set_if_absent(
        self, key: str | bytes, value: bytes, *, ttl_seconds: int
    ) -> bool | None:
        if self.fail or not self.configured:
            self.available = False
            return None
        normalized = self._key(key)
        self._purge(normalized)
        if normalized in self.values:
            return False
        return await self.set(normalized, value, ttl_seconds=ttl_seconds)

    async def delete(self, key: str | bytes) -> bool:
        normalized = self._key(key)
        self.deleted.append(normalized)
        self.values.pop(normalized, None)
        return not self.fail

    async def eval(
        self,
        script: str | bytes,
        *,
        keys: list[str | bytes] | tuple[str | bytes, ...] = (),
        args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
    ) -> Any | None:
        if self.fail or not self.configured:
            self.available = False
            return None
        text = script.decode() if isinstance(script, bytes) else script
        self.scripts.append(text)
        normalized_keys = [self._key(key) for key in keys]
        generation_key = normalized_keys[0]
        self._purge(generation_key)
        self.available = True
        if "generation-get-or-init-v1" in text:
            current = self.values.get(generation_key)
            ttl = int(args[1])
            if current is None or _invalid_generation(current[0]):
                supplied = _as_bytes(args[0])
                self.values[generation_key] = (supplied, self.clock() + ttl)
                return [1, supplied]
            self.values[generation_key] = (current[0], self.clock() + ttl)
            return [0, current[0]]
        if "generation-bump-v1" in text:
            current = self.values.get(generation_key)
            supplied = _as_bytes(args[0])
            if current is not None and not _invalid_generation(current[0]):
                epoch, counter = current[0].decode().split(":")
                supplied = f"{epoch}:{int(counter) + 1}".encode()
            self.values[generation_key] = (supplied, self.clock() + int(args[1]))
            return supplied
        if "generation-bump-watermark-v1" in text:
            current = self.values.get(generation_key)
            supplied = _as_bytes(args[0])
            if current is not None and not _invalid_generation(current[0]):
                epoch, counter = current[0].decode().split(":")
                supplied = f"{epoch}:{int(counter) + 1}".encode()
            watermark_key = normalized_keys[1]
            self._purge(watermark_key)
            current_watermark = self.values.get(watermark_key)
            last_seq = int(args[2])
            if current_watermark is not None:
                last_seq = max(last_seq, int(current_watermark[0]))
            expires_at = self.clock() + int(args[1])
            self.values[generation_key] = (supplied, expires_at)
            self.values[watermark_key] = (str(last_seq).encode(), expires_at)
            return [supplied, str(last_seq).encode()]
        if "watermark-floor-v1" in text:
            watermark_key = generation_key
            current_watermark = self.values.get(watermark_key)
            last_seq = int(args[0])
            if current_watermark is not None:
                last_seq = max(last_seq, int(current_watermark[0]))
            self.values[watermark_key] = (
                str(last_seq).encode(),
                self.clock() + int(args[1]),
            )
            return str(last_seq).encode()
        if "generation-validated-get-v1" in text:
            current = self.values.get(generation_key)
            expected = _as_bytes(args[0])
            if current is None or current[0] != expected:
                return [0]
            cache_key = normalized_keys[1]
            self._purge(cache_key)
            cached = self.values.get(cache_key)
            if cached is None:
                return [2]
            if str(args[1]) == "1":
                self.values[generation_key] = (
                    current[0],
                    self.clock() + int(args[3]),
                )
                self.values[cache_key] = (
                    cached[0],
                    self.clock() + int(args[2]),
                )
            return [1, cached[0]]
        if "generation-fenced-get-v1" in text:
            count = int(args[0])
            for index in range(count):
                self._purge(normalized_keys[index])
                current = self.values.get(normalized_keys[index])
                if current is None or current[0] != _as_bytes(args[index + 1]):
                    return [0]
            cache_key = normalized_keys[count]
            self._purge(cache_key)
            cached = self.values.get(cache_key)
            if cached is None:
                return [2]
            if str(args[count + 1]) == "1":
                for index in range(count):
                    current = self.values[normalized_keys[index]]
                    self.values[normalized_keys[index]] = (
                        current[0],
                        self.clock() + int(args[count + 3]),
                    )
                self.values[cache_key] = (
                    cached[0],
                    self.clock() + int(args[count + 2]),
                )
            return [1, cached[0]]
        if "generation-fenced-set-v1" in text:
            count = int(args[0])
            for index in range(count):
                current = self.values.get(normalized_keys[index])
                if current is None or current[0] != _as_bytes(args[index + 1]):
                    return 0
            if len(normalized_keys) == count + 2:
                guard = self.values.get(normalized_keys[count + 1])
                if guard is None or guard[0] != _as_bytes(args[count + 2]):
                    return 0
            self.values[normalized_keys[count]] = (
                _as_bytes(args[count + 1]),
                self.clock() + int(args[count + 3]),
            )
            return 1
        if "compare-generation-and-touch-v1" in text:
            current = self.values.get(generation_key)
            expected = _as_bytes(args[0])
            cache_key = normalized_keys[1]
            self._purge(cache_key)
            cached = self.values.get(cache_key)
            if current is None or current[0] != expected or cached is None:
                return 0
            self.values[generation_key] = (
                current[0],
                self.clock() + int(args[2]),
            )
            self.values[cache_key] = (
                cached[0],
                self.clock() + int(args[1]),
            )
            return 1
        if "compare-generation-and-set-v1" in text:
            current = self.values.get(generation_key)
            expected = _as_bytes(args[0])
            if current is None or current[0] != expected:
                return 0
            self.values[generation_key] = (current[0], self.clock() + int(args[3]))
            self.values[normalized_keys[1]] = (
                _as_bytes(args[1]),
                self.clock() + int(args[2]),
            )
            return 1
        if "snapshot-lock-status-v1" in text:
            self._purge(normalized_keys[0])
            current = self.values.get(normalized_keys[0])
            if current is None:
                return [-2]
            return [max(0, int((current[1] - self.clock()) * 1000))]
        if "snapshot-lock-renew-v1" in text:
            current = self.values.get(normalized_keys[0])
            if current is None or current[0] != _as_bytes(args[0]):
                return 0
            self.values[normalized_keys[0]] = (
                current[0],
                self.clock() + int(args[1]),
            )
            return 1
        if "snapshot-unlock-v1" in text:
            current = self.values.get(normalized_keys[0])
            if current is not None and current[0] == _as_bytes(args[0]):
                self.values.pop(normalized_keys[0], None)
                return 1
            return 0
        raise AssertionError("unknown Lua script")


def _as_bytes(value: str | bytes | int | float) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode()


def _invalid_generation(value: bytes) -> bool:
    try:
        epoch, counter = value.decode().split(":")
    except (UnicodeDecodeError, ValueError):
        return True
    return (
        len(epoch) != 32
        or any(char not in "0123456789abcdef" for char in epoch)
        or not (counter.isdigit())
    )


def _cache_value_keys(redis: FakeRedis) -> list[str]:
    return [
        key for key in redis.values if ":generation:" not in key and ":append-watermark:" not in key
    ]


class Delegate:
    store_id = "intaris"

    def __init__(self) -> None:
        self.page_calls = 0
        self.watermark_calls = 0
        self.last_seq = 1
        self.content = "one"
        self.page_error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.started: asyncio.Event | None = None
        self.authorities: list[tuple[str | None, str | None, str | None]] = []
        self.empty_verified = True
        self.empty = False
        self.large_content: str | None = None

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: str = "forward",
    ) -> SessionEventPage:
        self.page_calls += 1
        self.authorities.append(
            (
                current_user_email.get(),
                current_agent_id.get(),
                current_agent_owner_email.get(),
            )
        )
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.page_error is not None:
            raise self.page_error
        if self.empty:
            return SessionEventPage(
                store_id=self.store_id,
                session_id=session_id,
                last_seq=0,
                verified_empty=self.empty_verified,
            )
        seq = self.last_seq
        if after_seq is not None and seq <= after_seq:
            return SessionEventPage(
                store_id=self.store_id,
                session_id=session_id,
                last_seq=seq,
                verified_empty=True,
            )
        if before_seq is not None and seq >= before_seq:
            seq = max(1, before_seq - 1)
        event = RawSessionEvent(
            store_id=self.store_id,
            session_id=session_id,
            seq=seq,
            type="assistant_message",
            data={"content": self.large_content or self.content},
        )
        return SessionEventPage(
            store_id=self.store_id,
            session_id=session_id,
            events=[event],
            first_seq=seq,
            last_seq=self.last_seq,
            has_more_after=direction == "forward" and self.last_seq > seq,
        )

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        self.watermark_calls += 1
        self.authorities.append(
            (
                current_user_email.get(),
                current_agent_id.get(),
                current_agent_owner_email.get(),
            )
        )
        return SessionWatermark(
            store_id=self.store_id,
            session_id=session_id,
            last_seq=self.last_seq,
        )


def make_cache(
    delegate: Delegate,
    redis: FakeRedis,
    clock: FakeClock,
    *,
    bounds: EventCacheBounds | None = None,
    policy: EventCachePolicy | None = None,
) -> CachedSessionEventStore:
    epochs = (f"{value:032x}" for value in range(1, 100_000))
    return CachedSessionEventStore(
        delegate,
        cast(RedisService, redis),
        "shared-secret",
        bounds=bounds,
        policy=policy,
        clock=clock,
        epoch_factory=lambda: next(epochs),
    )


def make_snapshot_cache(
    event_store: CachedSessionEventStore,
    redis: FakeRedis,
    clock: FakeClock,
    *,
    policy: EventCachePolicy | None = None,
    bounds: EventCacheBounds | None = None,
    lock_lease_seconds: int = 15,
    build_deadline_seconds: float = 120.0,
) -> SharedChatSnapshotCache:
    return SharedChatSnapshotCache(
        event_store=event_store,
        redis_service=cast(RedisService, redis),
        policy=policy or EventCachePolicy(),
        bounds=bounds,
        clock=clock,
        lock_lease_seconds=lock_lease_seconds,
        build_deadline_seconds=build_deadline_seconds,
    )


async def build_test_snapshot(
    bound: BoundSessionEventStore,
    *,
    scope_key: str = "conversation:conversation-a",
):
    return await build_chat_snapshot(
        scope=TimelineScope(
            key=scope_key,
            kind="conversation",
            conversation_id=scope_key.removeprefix("conversation:"),
        ),
        conversation=None,
        session_refs=[
            ConversationSessionRef(
                session_id="session-a",
                event_store_session_id="session-a",
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ],
        event_store=bound,
        cursor_secret="cursor-secret",
    )


def _snapshot_refs(bound: BoundSessionEventStore) -> list[ConversationSessionRef]:
    return [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]


@pytest.mark.anyio
async def test_snapshot_resource_gauges_follow_success_and_shutdown() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def build():
        entered.set()
        await release.wait()
        return await build_test_snapshot(bound)

    pending = asyncio.create_task(
        snapshots.get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=_snapshot_refs(bound),
            cursor_secret="cursor-secret",
            build=build,
        )
    )
    await entered.wait()
    assert INFLIGHT_BUILDS._value.get() == 1
    assert OWNED_LOCKS._value.get() == 1

    release.set()
    await pending
    assert INFLIGHT_BUILDS._value.get() == 0
    assert OWNED_LOCKS._value.get() == 0
    assert L1_ENTRIES._value.get() == 1
    assert L1_BYTES._value.get() > 0

    await snapshots.aclose()
    assert INFLIGHT_BUILDS._value.get() == 0
    assert OWNED_LOCKS._value.get() == 0
    assert L1_ENTRIES._value.get() == 0
    assert L1_BYTES._value.get() == 0


@pytest.mark.anyio
async def test_snapshot_resource_gauges_reset_after_build_exception() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)

    async def fail():
        raise RuntimeError("projection failed")

    with pytest.raises(RuntimeError, match="projection failed"):
        await snapshots.get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=_snapshot_refs(bound),
            cursor_secret="cursor-secret",
            build=fail,
        )

    assert INFLIGHT_BUILDS._value.get() == 0
    assert OWNED_LOCKS._value.get() == 0
    assert L1_ENTRIES._value.get() == 0
    assert L1_BYTES._value.get() == 0
    await snapshots.aclose()


@pytest.mark.anyio
async def test_snapshot_resource_gauges_reset_after_cancellation_and_shutdown() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def build():
        entered.set()
        await release.wait()
        return await build_test_snapshot(bound)

    pending = asyncio.create_task(
        snapshots.get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=_snapshot_refs(bound),
            cursor_secret="cursor-secret",
            build=build,
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert INFLIGHT_BUILDS._value.get() == 1
    assert OWNED_LOCKS._value.get() == 1

    await snapshots.aclose()
    assert INFLIGHT_BUILDS._value.get() == 0
    assert OWNED_LOCKS._value.get() == 0
    assert L1_ENTRIES._value.get() == 0
    assert L1_BYTES._value.get() == 0


@pytest.mark.anyio
async def test_snapshot_join_uses_returned_cache_tier() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    refs = _snapshot_refs(bound)
    identity = await snapshots._identity(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
    )
    assert identity is not None
    snapshot = await build_test_snapshot(bound)

    async def completed_fill() -> SnapshotCacheResult:
        return SnapshotCacheResult(snapshot, "redis")

    task = asyncio.create_task(completed_fill())
    snapshots._inflight[identity.local_key] = task
    trace = SnapshotRequestTrace()
    result = await snapshots.get_or_build_result(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
        request_trace=trace,
    )

    assert result.tier == "redis"
    assert trace.tier == "redis"
    snapshots._inflight.clear()
    await snapshots.aclose()


@pytest.mark.anyio
async def test_snapshot_codec_saturation_is_not_reported_as_redis_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    refs = _snapshot_refs(bound)
    identity = await snapshots._identity(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
    )
    assert identity is not None
    redis.values[identity.value_key] = (b"encoded", clock() + 3600)

    async def saturated(*_args, **_kwargs):
        return snapshot_cache_module._DecodedSnapshot("saturated")

    monkeypatch.setattr(snapshots, "_deserialize", saturated)
    result = await snapshots.get_or_build_result(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
        fail_open=False,
    )

    assert result.snapshot is None
    assert result.warm_failure is None
    assert snapshots.warm_outcome("conversation:conversation-a") == "retry"
    await snapshots.aclose()


@pytest.mark.anyio
async def test_shared_snapshot_hit_avoids_projection_event_reads_across_instances() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first_delegate = Delegate()
    first_events = make_cache(first_delegate, redis, clock)
    first_bound = first_events.bind(AUTHORITY)
    first_snapshots = make_snapshot_cache(first_events, redis, clock)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=first_bound,
            authority_token=first_bound.authority_token,
        )
    ]

    first = await first_snapshots.get_or_build(
        authority_token=first_bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(first_bound),
    )
    page_calls = first_delegate.page_calls
    watermark_calls = first_delegate.watermark_calls

    second_delegate = Delegate()
    second_events = make_cache(second_delegate, redis, clock)
    second_bound = second_events.bind(AUTHORITY)
    second = await make_snapshot_cache(second_events, redis, clock).get_or_build(
        authority_token=second_bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=[
            refs[0].model_copy(
                update={
                    "reader": second_bound,
                    "authority_token": second_bound.authority_token,
                }
            )
        ],
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(second_bound),
    )

    assert second.timeline == first.timeline
    assert first_delegate.page_calls == page_calls
    assert first_delegate.watermark_calls == watermark_calls
    assert second_delegate.page_calls == 0
    assert second_delegate.watermark_calls == 0
    assert all(
        "conversation-a" not in key and "user@example.com" not in key for key in redis.values
    )


@pytest.mark.anyio
async def test_cache_only_snapshot_read_never_builds_locks_waits_or_joins_inflight() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    refs = _snapshot_refs(bound)
    warmed = await snapshots.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )
    assert warmed is not None
    page_calls = delegate.page_calls
    watermark_calls = delegate.watermark_calls
    redis.scripts.clear()

    async def forbidden_inflight() -> SnapshotCacheResult:
        raise AssertionError("cache-only read joined inflight work")

    identity = await snapshots._identity(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
    )
    assert identity is not None
    inflight = asyncio.create_task(forbidden_inflight())
    snapshots._inflight[identity.local_key] = inflight

    result = await snapshots.get_cached_result(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
    )

    assert result.snapshot is not None
    assert result.status == "hit_l1"
    assert delegate.page_calls == page_calls
    assert delegate.watermark_calls == watermark_calls
    assert not any("snapshot-lock" in script for script in redis.scripts)
    assert snapshots._inflight[identity.local_key] is inflight
    snapshots._inflight.clear()
    inflight.cancel()
    await asyncio.gather(inflight, return_exceptions=True)
    await snapshots.aclose()


@pytest.mark.anyio
async def test_append_generation_prevents_stale_snapshot_publish() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock)
    bound = events.bind(AUTHORITY)
    snapshots = make_snapshot_cache(events, redis, clock)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    build_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_build():
        snapshot = await build_test_snapshot(bound)
        build_started.set()
        await release.wait()
        return snapshot

    pending = asyncio.create_task(
        snapshots.get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs,
            cursor_secret="cursor-secret",
            build=slow_build,
        )
    )
    await build_started.wait()
    await events.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))
    delegate.last_seq = 2
    delegate.content = "new"
    release.set()
    raced = await pending
    assert any(getattr(item, "content", None) == "new" for item in raced.timeline.items)

    clear_chat_v2_read_caches()
    current_identity = await snapshots._identity(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
    )
    assert current_identity is not None
    assert current_identity.fence.entries[0].watermark_floor == 2
    refreshed = await make_snapshot_cache(events, redis, clock).get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )
    assert any(getattr(item, "content", None) == "new" for item in refreshed.timeline.items)


@pytest.mark.anyio
async def test_two_controllers_cold_snapshot_use_one_distributed_rebuild() -> None:
    clear_chat_v2_read_caches()
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    first_events = make_cache(delegate, redis, clock)
    second_events = make_cache(delegate, redis, clock)
    first_bound = first_events.bind(AUTHORITY)
    second_bound = second_events.bind(AUTHORITY)
    started = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    def refs(bound: BoundSessionEventStore) -> list[ConversationSessionRef]:
        return [
            ConversationSessionRef(
                session_id="session-a",
                event_store_session_id="session-a",
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ]

    async def build_first():
        nonlocal builds
        builds += 1
        started.set()
        await release.wait()
        return await build_test_snapshot(first_bound)

    async def build_second():
        nonlocal builds
        builds += 1
        return await build_test_snapshot(second_bound)

    first = asyncio.create_task(
        make_snapshot_cache(first_events, redis, clock).get_or_build(
            authority_token=first_bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs(first_bound),
            cursor_secret="cursor-secret",
            build=build_first,
        )
    )
    await started.wait()
    second = asyncio.create_task(
        make_snapshot_cache(second_events, redis, clock).get_or_build(
            authority_token=second_bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs(second_bound),
            cursor_secret="cursor-secret",
            build=build_second,
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    assert builds == 1
    assert delegate.page_calls == 1
    assert delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_append_between_identity_and_atomic_read_never_serves_old_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock)
    bound = events.bind(AUTHORITY)
    first_cache = make_snapshot_cache(events, redis, clock)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    await first_cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )
    second_cache = make_snapshot_cache(events, redis, clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_read = events.generation_fenced_read

    async def delayed_read(*args: Any, **kwargs: Any):
        entered.set()
        await release.wait()
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(events, "generation_fenced_read", delayed_read)
    delegate.last_seq = 2
    delegate.content = "new"
    pending = asyncio.create_task(
        second_cache.get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs,
            cursor_secret="cursor-secret",
            build=lambda: build_test_snapshot(bound),
        )
    )
    await entered.wait()
    await events.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))
    release.set()
    snapshot = await pending

    assert any(getattr(item, "content", None) == "new" for item in snapshot.timeline.items)


@pytest.mark.anyio
async def test_eight_controllers_wait_through_renewed_lease_for_one_build() -> None:
    clear_chat_v2_read_caches()
    clock = monotonic
    redis = FakeRedis(clock)
    delegate = Delegate()
    stores = [make_cache(delegate, redis, cast(Any, clock)) for _ in range(8)]
    bounds = [store.bind(AUTHORITY) for store in stores]
    builds = 0

    async def build():
        nonlocal builds
        builds += 1
        await asyncio.sleep(1.3)
        return await build_test_snapshot(bounds[0])

    tasks = [
        make_snapshot_cache(
            store,
            redis,
            cast(Any, clock),
            lock_lease_seconds=1,
            build_deadline_seconds=4,
        ).get_or_build(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=[
                ConversationSessionRef(
                    session_id="session-a",
                    event_store_session_id="session-a",
                    ordinal=0,
                    reader=bound,
                    authority_token=bound.authority_token,
                )
            ],
            cursor_secret="cursor-secret",
            build=build,
        )
        for store, bound in zip(stores, bounds, strict=True)
    ]
    await asyncio.gather(*tasks)

    assert builds == 1
    assert delegate.page_calls == 1


@pytest.mark.anyio
async def test_snapshot_redis_outage_falls_back_without_retry_loop() -> None:
    clear_chat_v2_read_caches()
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock)
    bound = events.bind(AUTHORITY)
    redis.fail = True
    started = monotonic()

    await make_snapshot_cache(events, redis, clock).get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=[
            ConversationSessionRef(
                session_id="session-a",
                event_store_session_id="session-a",
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ],
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )

    assert monotonic() - started < 0.1
    assert delegate.page_calls == 1


@pytest.mark.anyio
async def test_background_warm_does_not_fail_open_to_event_store() -> None:
    clear_chat_v2_read_caches()
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock)
    bound = events.bind(AUTHORITY)
    redis.fail = True

    result = await make_snapshot_cache(events, redis, clock).get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=[
            ConversationSessionRef(
                session_id="session-a",
                event_store_session_id="session-a",
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ],
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
        fail_open=False,
    )

    assert result is None
    assert delegate.page_calls == 0


@pytest.mark.anyio
async def test_snapshot_key_is_stable_across_ten_thousand_generations() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    keys: set[str] = set()
    for seq in range(1, 10_001):
        await events.handle_append(EventAppendNotification(AUTHORITY, "session-a", seq, seq, 1))
        identity = await cache._identity(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs,
        )
        assert identity is not None
        keys.add(identity.value_key)
        assert await events.generation_fenced_write(
            identity.value_key,
            b"bounded",
            identity.fence,
            ttl_seconds=3600,
        )

    assert len(keys) == 1
    snapshot_values = [
        value for key, (value, _expiry) in redis.values.items() if ":snapshot:" in key
    ]
    assert len(snapshot_values) == 1
    assert sum(len(value) for value in snapshot_values) < 1024


@pytest.mark.anyio
async def test_lineage_growth_keeps_one_value_key_and_separates_local_identity() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    first = events.bind(AUTHORITY)
    second_authority = EventStoreAuthority(
        user_email=AUTHORITY.user_email,
        agent_id="agent-b",
        agent_owner_email=AUTHORITY.agent_owner_email,
    )
    second = events.bind(second_authority)
    cache = make_snapshot_cache(events, redis, clock)
    first_ref = ConversationSessionRef(
        session_id="session-a",
        event_store_session_id="session-a",
        ordinal=0,
        reader=first,
        authority_token=first.authority_token,
    )
    second_ref = ConversationSessionRef(
        session_id="session-b",
        event_store_session_id="session-b",
        ordinal=1,
        reader=second,
        authority_token=second.authority_token,
    )
    stable_authority = events.derived_key_digest(
        "snapshot-conversation-authority",
        AUTHORITY.user_email,
    )
    old_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        session_refs=[first_ref],
    )
    new_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        session_refs=[first_ref, second_ref],
    )
    replaced_authority_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        session_refs=[
            first_ref.model_copy(
                update={
                    "reader": second,
                    "authority_token": second.authority_token,
                }
            )
        ],
    )

    assert (
        old_identity is not None
        and new_identity is not None
        and replaced_authority_identity is not None
    )
    assert old_identity.value_key == new_identity.value_key
    assert old_identity.value_key == replaced_authority_identity.value_key
    assert old_identity.local_key != new_identity.local_key
    assert old_identity.local_key != replaced_authority_identity.local_key
    old_snapshot = await build_test_snapshot(first)
    cache._put_l1(
        old_identity.local_key,
        old_identity.fence,
        old_snapshot,
        b"old",
        accounted_size=3,
    )
    cache._inflight[old_identity.local_key] = asyncio.create_task(asyncio.sleep(0))
    assert new_identity.local_key not in cache._l1
    assert new_identity.local_key not in cache._inflight
    await asyncio.gather(*cache._inflight.values())


@pytest.mark.anyio
async def test_snapshot_session_index_replacement_uses_actual_shared_references() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    bounds = EventCacheBounds(
        l1_max_entries=4,
        l1_max_bytes=4096,
        generation_max_sessions=3,
    )
    cache = make_snapshot_cache(events, redis, clock, bounds=bounds)
    snapshot = await build_test_snapshot(bound)

    def fence(*tokens: str) -> GenerationFence:
        return GenerationFence(
            tuple(
                GenerationFenceEntry(
                    session_token=token,
                    generation="0" * 32 + ":0",
                    backing_session_id=token,
                    watermark_floor=0,
                )
                for token in tokens
            )
        )

    cache._put_l1("key-1", fence("a", "b"), snapshot, b"one", accounted_size=3)
    cache._put_l1("key-2", fence("a", "c"), snapshot, b"two", accounted_size=3)
    cache._put_l1("key-1", fence("d", "e"), snapshot, b"new", accounted_size=3)

    assert len(cache._session_index) <= bounds.generation_max_sessions
    assert set(cache._session_index) == {"d", "e"}
    assert set(cache._l1) == {"key-1"}


@pytest.mark.anyio
async def test_old_lineage_reader_does_not_delete_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    first = events.bind(AUTHORITY)
    second = events.bind(
        EventStoreAuthority(
            user_email=AUTHORITY.user_email,
            agent_id="agent-b",
            agent_owner_email=AUTHORITY.agent_owner_email,
        )
    )
    cache = make_snapshot_cache(events, redis, clock)
    stable_authority = events.derived_key_digest(
        "snapshot-conversation-authority",
        AUTHORITY.user_email,
    )
    first_ref = ConversationSessionRef(
        session_id="session-a",
        event_store_session_id="session-a",
        ordinal=0,
        reader=first,
        authority_token=first.authority_token,
    )
    second_ref = ConversationSessionRef(
        session_id="session-b",
        event_store_session_id="session-b",
        ordinal=1,
        reader=second,
        authority_token=second.authority_token,
    )
    old_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        session_refs=[first_ref],
    )
    new_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        session_refs=[first_ref, second_ref],
    )
    assert old_identity is not None and new_identity is not None
    snapshot = await build_test_snapshot(first)
    first_encoding = await cache._serialize(
        snapshot=snapshot,
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        lineage=new_identity.lineage,
        cursor_secret="cursor-secret",
    )
    second_encoding = await cache._serialize(
        snapshot=snapshot.model_copy(update={"server_time": "newer"}),
        authority_token=stable_authority,
        scope_key="conversation:conversation-a",
        lineage=new_identity.lineage,
        cursor_secret="cursor-secret",
    )
    assert first_encoding is not None and second_encoding is not None
    assert await events.generation_fenced_write(
        new_identity.value_key,
        first_encoding.payload,
        new_identity.fence,
        ttl_seconds=3600,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    decode = events.decode_derived_envelope

    async def delayed_decode(payload: bytes):
        entered.set()
        await release.wait()
        return await decode(payload)

    monkeypatch.setattr(events, "decode_derived_envelope", delayed_decode)
    pending = asyncio.create_task(
        cache._read(
            old_identity,
            authority_token=stable_authority,
            scope_key="conversation:conversation-a",
            cursor_secret="cursor-secret",
        )
    )
    await entered.wait()
    assert await events.generation_fenced_write(
        new_identity.value_key,
        second_encoding.payload,
        new_identity.fence,
        ttl_seconds=3600,
    )
    release.set()
    cached, read_status, _tier = await pending

    assert cached is None and read_status == "available"
    assert redis.values[new_identity.value_key][0] == second_encoding.payload


@pytest.mark.anyio
async def test_snapshot_l1_accounts_uncompressed_envelope_size() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(compression_threshold_bytes=1)
    bounds = EventCacheBounds(l1_max_entries=4, l1_max_bytes=512)
    delegate = Delegate()
    delegate.content = "x" * 20_000
    events = make_cache(delegate, redis, clock, policy=policy)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock, policy=policy, bounds=bounds)

    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=[
            ConversationSessionRef(
                session_id="session-a",
                event_store_session_id="session-a",
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ],
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )

    assert cache._l1_bytes == 0
    assert not cache._l1


@pytest.mark.anyio
async def test_codec_saturation_does_not_delete_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    first = make_snapshot_cache(events, redis, clock)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    await first.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )
    identity = await first._identity(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
    )
    assert identity is not None and identity.value_key in redis.values

    async def saturated(_payload: bytes) -> DerivedEnvelopeDecoding:
        return DerivedEnvelopeDecoding("saturated")

    monkeypatch.setattr(events, "decode_derived_envelope", saturated)
    second = make_snapshot_cache(events, redis, clock)
    await second.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )

    assert identity.value_key in redis.values


@pytest.mark.anyio
async def test_append_overflow_disables_derived_generation_fences() -> None:
    clock = FakeClock()
    events = make_cache(Delegate(), FakeRedis(clock), clock)
    events._append_overflow_until = clock() + 10

    fence = await events.create_generation_fence([("intaris", "session-a")])

    assert fence is None


@pytest.mark.anyio
@pytest.mark.parametrize("sliding", [True, False])
async def test_snapshot_l1_hit_respects_shared_sliding_ttl(sliding: bool) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(ttl_seconds=10, sliding_expiration=sliding)
    delegate = Delegate()
    events = make_cache(delegate, redis, clock, policy=policy)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock, policy=policy)
    refs = [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )
    snapshot_key = next(
        key for key in redis.values if ":snapshot:" in key and not key.endswith(":lock")
    )
    original_expiry = redis.values[snapshot_key][1]
    clock.advance(6)
    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key="conversation:conversation-a",
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound),
    )

    assert (redis.values[snapshot_key][1] > original_expiry) is sliding


async def _exercise_full_codec_capacity(cache: CachedSessionEventStore) -> None:
    release = threading.Event()
    all_workers_started = threading.Event()
    lock = threading.Lock()
    active_workers = 0
    max_active_workers = 0

    def blocking_codec() -> int:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            if active_workers == cache_module._CODEC_WORKERS:
                all_workers_started.set()
        assert release.wait(timeout=10)
        try:
            return 1
        finally:
            with lock:
                active_workers -= 1

    admitted = [
        asyncio.create_task(cache._run_codec(blocking_codec))
        for _ in range(cache_module._CODEC_CAPACITY)
    ]
    for _ in range(10_000):
        if (
            all_workers_started.is_set()
            and cache.diagnostics()["codec_waiting"]
            == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
        ):
            break
        await asyncio.sleep(0)
    assert all_workers_started.is_set()
    assert cache.diagnostics()["codec_tasks"] == cache_module._CODEC_WORKERS
    assert (
        cache.diagnostics()["codec_waiting"]
        == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
    )
    with pytest.raises(cache_module._CodecSaturated):
        await cache._run_codec(lambda: 2)

    release.set()
    assert await asyncio.gather(*admitted) == [1] * cache_module._CODEC_CAPACITY
    assert max_active_workers == cache_module._CODEC_WORKERS
    assert cache.diagnostics()["codec_tasks"] == 0
    assert cache.diagnostics()["codec_waiting"] == 0


@pytest.mark.anyio
async def test_twenty_concurrent_reads_use_one_upstream_read() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.gate = asyncio.Event()
    delegate.started = asyncio.Event()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)

    reads = [
        asyncio.create_task(bound.read_session_events(session_id="session-a")) for _ in range(20)
    ]
    await delegate.started.wait()
    delegate.gate.set()
    pages = await asyncio.gather(*reads)

    assert delegate.page_calls == 1
    assert all(page.events[0].seq == 1 for page in pages)


@pytest.mark.anyio
async def test_two_instances_reuse_shared_redis_value() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first_delegate = Delegate()
    second_delegate = Delegate()
    first = make_cache(first_delegate, redis, clock).bind(AUTHORITY)
    second = make_cache(second_delegate, redis, clock).bind(AUTHORITY)

    await first.read_session_events(session_id="session-a")
    page = await second.read_session_events(session_id="session-a")

    assert page.events[0].seq == 1
    assert first_delegate.page_calls == 1
    assert second_delegate.page_calls == 0


@pytest.mark.anyio
async def test_authority_partitions_never_overlap_and_context_is_exact() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock)

    await cache.bind(AUTHORITY).read_session_events(session_id="same-backing-id")
    await cache.bind(OTHER_AUTHORITY).read_session_events(session_id="same-backing-id")

    assert delegate.page_calls == 2
    assert delegate.authorities == [
        ("user@example.com", "agent-a", "owner@example.com"),
        ("other@example.com", "agent-a", "owner@example.com"),
    ]
    cache_keys = [key for key in redis.values if ":generation:" not in key]
    assert len(cache_keys) == 2
    assert all("example.com" not in key and "same-backing-id" not in key for key in cache_keys)


@pytest.mark.anyio
async def test_unbound_reads_and_missing_authority_are_impossible() -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock), clock)

    with pytest.raises(TypeError):
        cache.bind(cast(Any, None))
    with pytest.raises(RuntimeError, match="must be bound"):
        await cache.read_session_events(session_id="session-a")


@pytest.mark.anyio
async def test_query_and_session_shapes_partition_cache_keys() -> None:
    clock = FakeClock()
    delegate = Delegate()
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)

    await bound.read_session_events(session_id="session-a", limit=10)
    await bound.read_session_events(session_id="session-a", limit=20)
    await bound.read_session_events(session_id="session-a", before_seq=10, direction="backward")
    await bound.read_session_events(session_id="session-b", limit=10)

    assert delegate.page_calls == 4


@pytest.mark.anyio
async def test_append_bumps_generation_and_advances_watermark_monotonically() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.last_seq = 10
    cache = make_cache(delegate, FakeRedis(clock), clock)
    bound = cache.bind(AUTHORITY)
    initial = await bound.read_session_high_watermark(session_id="session-a")
    token = cache.session_token("intaris", "session-a")
    generation_before = cache._generations[token].generation

    assert initial.last_seq == 10
    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 11, 1))
    current = await bound.read_session_high_watermark(session_id="session-a")

    assert current.last_seq == 12
    assert cache._generations[token].generation.counter == generation_before.counter + 2
    assert delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_append_watermark_is_monotonic_across_instances() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first = make_cache(Delegate(), redis, clock)
    second = make_cache(Delegate(), redis, clock)

    assert await first.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    assert await second.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 11, 1))
    watermark = await second.bind(AUTHORITY).read_session_high_watermark(session_id="session-a")

    assert watermark.last_seq == 12


@pytest.mark.asyncio
async def test_slow_redis_append_listener_returns_before_remote_pg_invalidation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    redis_entered = asyncio.Event()
    redis_release = asyncio.Event()

    class SlowAppendRedis(FakeRedis):
        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes] | tuple[str | bytes, ...] = (),
            args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
        ) -> Any | None:
            text = script.decode() if isinstance(script, bytes) else script
            if "generation-bump-watermark-v1" in text:
                redis_entered.set()
                await redis_release.wait()
            return await super().eval(script, keys=keys, args=args)

    redis = SlowAppendRedis(clock)
    writer = make_cache(Delegate(), redis, clock)
    writer_reader = writer.bind(AUTHORITY)
    await writer_reader.read_session_events(session_id="session-a")
    remote_delegate = Delegate()
    remote = make_cache(remote_delegate, redis, clock)
    remote_reader = remote.bind(AUTHORITY)
    initial = await remote_reader.read_session_events(session_id="session-a")
    assert initial.events[0].data["content"] == "one"
    remote_delegate.content = "two"
    remote_delegate.last_seq = 2
    remote_manager = WebSocketConnectionManager(
        SimpleNamespace(state=SimpleNamespace(cached_event_store=remote))
    )
    postgres_signal_received = asyncio.Event()

    async def publish_postgres_signal(session_token: str, revision: int) -> bool:
        await remote_manager._handle_event(  # noqa: SLF001
            Event(
                type=EventType.CLUSTER_SCOPE_INVALIDATED,
                data={
                    "kind": "event_store_session_invalidated",
                    "scope": {
                        "event_store_id": "intaris",
                        "event_session_token": session_token,
                    },
                    "revision": str(revision),
                },
            )
        )
        postgres_signal_received.set()
        return True

    dispatcher = EventAppendInvalidationDispatcher(
        event_store=writer,
        publish_invalidation=publish_postgres_signal,
        backoff_initial_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    await dispatcher.start()

    async def listener(notification: EventAppendNotification) -> None:
        dispatcher.enqueue(writer.invalidate_append_local(notification))

    notification = EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1)
    await asyncio.wait_for(listener(notification), timeout=0.1)
    assert writer.diagnostics()["entries"] == 0
    await asyncio.wait_for(redis_entered.wait(), timeout=0.1)
    await asyncio.sleep(0.11)
    stale = await remote_reader.read_session_events(session_id="session-a")
    assert stale.events[0].data["content"] == "one"

    redis_release.set()
    await asyncio.wait_for(postgres_signal_received.wait(), timeout=1)
    refreshed = await remote_reader.read_session_events(session_id="session-a")
    await dispatcher.stop()

    assert refreshed.events[0].data["content"] == "two"
    assert "user@example.com" not in caplog.text
    assert "owner@example.com" not in caplog.text
    assert "private-content" not in caplog.text


@pytest.mark.anyio
async def test_append_watermark_floor_survives_l1_expiry() -> None:
    clock = FakeClock()
    delegate = Delegate()
    cache = make_cache(delegate, FakeRedis(clock, configured=False), clock)
    bound = cache.bind(AUTHORITY)

    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)
    delegate.last_seq = 10

    watermark = await bound.read_session_high_watermark(session_id="session-a")

    assert watermark.last_seq == 12
    assert delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_append_watermark_floor_survives_l1_expiry_across_instances() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first = make_cache(Delegate(), redis, clock)
    second_delegate = Delegate()
    second_delegate.last_seq = 10
    second = make_cache(second_delegate, redis, clock)

    assert await first.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)

    watermark = await second.bind(AUTHORITY).read_session_high_watermark(session_id="session-a")

    assert watermark.last_seq == 12
    assert second_delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_append_watermark_floor_is_authority_specific() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.last_seq = 10
    cache = make_cache(delegate, redis, clock)

    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)

    watermark = await cache.bind(OTHER_AUTHORITY).read_session_high_watermark(
        session_id="session-a"
    )

    assert watermark.last_seq == 10


@pytest.mark.anyio
async def test_local_watermark_floors_share_generation_registry_bound() -> None:
    clock = FakeClock()
    bounds = EventCacheBounds(generation_max_sessions=2)
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock, bounds=bounds)

    for index in range(3):
        assert await cache.handle_append(
            EventAppendNotification(AUTHORITY, f"session-{index}", 1, 1, 1)
        )

    assert len(cache._local_watermark_floors) <= bounds.generation_max_sessions


@pytest.mark.anyio
async def test_redis_outage_preserves_locally_observed_watermark_floor() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock)
    bound = cache.bind(AUTHORITY)

    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)
    delegate.last_seq = 10
    redis.fail = True
    redis.available = False

    watermark = await bound.read_session_high_watermark(session_id="session-a")

    assert watermark.last_seq == 12
    assert delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_mid_read_redis_failure_preserves_local_watermark_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock)
    bound = cache.bind(AUTHORITY)
    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 11, 12, 2))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)
    delegate.last_seq = 10

    original_eval = redis.eval

    async def fail_validated_get(
        script: str | bytes,
        *,
        keys: list[str | bytes] | tuple[str | bytes, ...] = (),
        args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
    ) -> Any | None:
        text = script.decode() if isinstance(script, bytes) else script
        if "generation-validated-get-v1" in text:
            redis.available = False
            return None
        return await original_eval(script, keys=keys, args=args)

    monkeypatch.setattr(redis, "eval", fail_validated_get)
    watermark = await bound.read_session_high_watermark(session_id="session-a")

    assert watermark.last_seq == 12
    assert delegate.watermark_calls == 1


@pytest.mark.anyio
async def test_slow_fill_cannot_overwrite_append_generation() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.gate = asyncio.Event()
    delegate.started = asyncio.Event()
    cache = make_cache(delegate, FakeRedis(clock), clock)
    bound = cache.bind(AUTHORITY)
    read = asyncio.create_task(bound.read_session_events(session_id="session-a"))
    await delegate.started.wait()

    await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))
    delegate.gate.set()
    await read

    assert cache.diagnostics()["entries"] == 1  # append-seeded watermark only
    await bound.read_session_events(session_id="session-a")
    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_generation_key_loss_creates_new_epoch_and_rejects_old_entries() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    token = cache.session_token("intaris", "session-a")
    generation_before = cache._generations[token].generation
    redis.values.pop(cache._generation_key(token))
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 0.1)

    await bound.read_session_events(session_id="session-a")

    assert delegate.page_calls == 2
    assert cache._generations[token].generation.epoch != generation_before.epoch


@pytest.mark.anyio
async def test_malformed_generation_is_replaced_with_new_epoch() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock)
    token = cache.session_token("intaris", "session-a")
    redis.values[cache._generation_key(token)] = (b"malformed", clock() + 3600)

    await cache.bind(AUTHORITY).read_session_events(session_id="session-a")

    stored = redis.values[cache._generation_key(token)][0]
    assert stored != b"malformed"
    assert len(stored.decode().split(":")[0]) == 32


@pytest.mark.anyio
async def test_local_generation_registry_enforces_count_and_inactivity_bounds() -> None:
    clock = FakeClock()
    bounds = EventCacheBounds(
        generation_max_sessions=2,
        generation_inactivity_seconds=10,
    )
    cache = make_cache(
        Delegate(),
        FakeRedis(clock, configured=False),
        clock,
        bounds=bounds,
        policy=EventCachePolicy(ttl_seconds=5),
    )
    bound = cache.bind(AUTHORITY)

    await bound.read_session_events(session_id="session-a")
    await bound.read_session_events(session_id="session-b")
    await bound.read_session_events(session_id="session-c")
    assert cache.diagnostics()["generation_sessions"] == 2
    clock.advance(11)
    await bound.read_session_events(session_id="session-d")

    assert cache.diagnostics()["generation_sessions"] == 1
    assert cache.diagnostics()["generation_bytes"] <= 1024 * 1024


@pytest.mark.anyio
async def test_l1_reverse_index_tracks_admission_eviction_and_invalidation() -> None:
    clock = FakeClock()
    bounds = EventCacheBounds(l1_max_entries=2)
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock, bounds=bounds)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a", limit=1)
    await bound.read_session_events(session_id="session-a", limit=2)
    await bound.read_session_events(session_id="session-b", limit=1)

    assert len(cache._l1) == 2
    assert sum(len(keys) for keys in cache._session_index.values()) == 2
    await cache.invalidate_session("intaris", "session-a")
    assert all(
        entry.session_token != cache.session_token("intaris", "session-a")
        for entry in cache._l1.values()
    )
    assert sum(len(keys) for keys in cache._session_index.values()) == len(cache._l1)


@pytest.mark.anyio
async def test_l1_and_generation_registry_enforce_byte_bounds() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.large_content = "x" * 300
    bounds = EventCacheBounds(
        l1_max_bytes=1200,
        generation_max_bytes=220,
    )
    cache = make_cache(delegate, FakeRedis(clock, configured=False), clock, bounds=bounds)
    bound = cache.bind(AUTHORITY)

    for index in range(6):
        await bound.read_session_events(session_id=f"session-{index}")

    assert cache.diagnostics()["bytes"] <= 1200
    assert cache.diagnostics()["generation_bytes"] <= 220
    assert sum(len(keys) for keys in cache._session_index.values()) == len(cache._l1)


@pytest.mark.anyio
async def test_append_invalidates_historical_and_tail_pages() -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a", direction="backward")
    await bound.read_session_events(session_id="session-a", before_seq=50, direction="backward")
    assert cache.diagnostics()["entries"] == 2

    await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 0, 0, 0))

    assert cache.diagnostics()["entries"] == 0


@pytest.mark.anyio
async def test_external_append_becomes_visible_after_active_ttl() -> None:
    clock = FakeClock()
    delegate = Delegate()
    cache = make_cache(delegate, FakeRedis(clock), clock)
    bound = cache.bind(AUTHORITY)
    first = await bound.read_session_events(session_id="session-a")
    delegate.last_seq = 2
    delegate.content = "two"

    still_cached = await bound.read_session_events(session_id="session-a")
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 1)
    refreshed = await bound.read_session_events(session_id="session-a")

    assert first.events[0].data["content"] == "one"
    assert still_cached.events[0].data["content"] == "one"
    assert refreshed.events[0].data["content"] == "two"
    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_verified_empty_uses_active_ttl() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.empty = True
    bound = make_cache(
        delegate,
        FakeRedis(clock),
        clock,
        policy=EventCachePolicy(sliding_expiration=False),
    ).bind(AUTHORITY)

    await bound.read_session_events(session_id="session-a")
    clock.advance(ACTIVE_CACHE_TTL_SECONDS - 0.1)
    await bound.read_session_events(session_id="session-a")
    clock.advance(0.2)
    await bound.read_session_events(session_id="session-a")

    assert delegate.page_calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        PermissionError("401"),
        PermissionError("403"),
        TimeoutError("timeout"),
        RuntimeError("429"),
        RuntimeError("500"),
    ],
)
async def test_upstream_failures_are_never_cached(error: BaseException) -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.page_error = error
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)

    with pytest.raises(type(error)):
        await bound.read_session_events(session_id="session-a")
    with pytest.raises(type(error)):
        await bound.read_session_events(session_id="session-a")

    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_unverified_empty_is_not_cached() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.empty = True
    delegate.empty_verified = False
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)

    await bound.read_session_events(session_id="session-a")
    await bound.read_session_events(session_id="session-a")

    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_redis_failure_falls_back_directly_to_upstream() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    redis.fail = True
    delegate = Delegate()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)

    page = await bound.read_session_events(session_id="session-a")

    assert page.events[0].seq == 1
    assert delegate.page_calls == 1


@pytest.mark.anyio
async def test_redis_outage_never_serves_primed_l1_or_stale_on_error() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)
    primed = await bound.read_session_events(session_id="session-a")
    delegate.last_seq = 2
    delegate.content = "two"
    redis.fail = True
    redis.available = False

    refreshed = await bound.read_session_events(session_id="session-a")
    delegate.page_error = RuntimeError("upstream failed")

    assert primed.events[0].data["content"] == "one"
    assert refreshed.events[0].data["content"] == "two"
    with pytest.raises(RuntimeError, match="upstream failed"):
        await bound.read_session_events(session_id="session-a")
    assert delegate.page_calls == 3


@pytest.mark.anyio
async def test_redis_recovery_fences_values_cached_before_outage() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    delegate.last_seq = 2
    delegate.content = "two"
    redis.fail = True
    redis.available = False

    during_outage = await bound.read_session_events(session_id="session-a")
    redis.fail = False
    redis.available = True
    delegate.last_seq = 3
    delegate.content = "three"

    after_recovery = await bound.read_session_events(session_id="session-a")

    assert during_outage.events[0].data["content"] == "two"
    assert after_recovery.events[0].data["content"] == "three"
    assert delegate.page_calls == 3


@pytest.mark.anyio
async def test_unobserved_redis_outage_recovery_fences_old_l1() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)
    primed = await bound.read_session_events(session_id="session-a")

    redis.available = False
    redis.available = True
    delegate.last_seq = 2
    delegate.content = "two"
    refreshed = await bound.read_session_events(session_id="session-a")

    assert primed.events[0].data["content"] == "one"
    assert refreshed.events[0].data["content"] == "two"
    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_append_reports_shared_invalidation_failure_after_local_clear() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    cache = make_cache(Delegate(), redis, clock)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    redis.fail = True

    result = await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))

    assert result is False
    assert cache.diagnostics()["entries"] == 0


@pytest.mark.anyio
async def test_malformed_redis_value_is_deleted_and_treated_as_miss() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first_delegate = Delegate()
    first = make_cache(first_delegate, redis, clock).bind(AUTHORITY)
    await first.read_session_events(session_id="session-a")
    cache_key = next(key for key in redis.values if ":generation:" not in key)
    redis.values[cache_key] = (b'{"version":true}', clock() + 30)
    second_delegate = Delegate()
    second = make_cache(second_delegate, redis, clock).bind(AUTHORITY)

    await second.read_session_events(session_id="session-a")

    assert cache_key in redis.deleted
    assert second_delegate.page_calls == 1


@pytest.mark.anyio
async def test_unverified_empty_redis_value_is_deleted_and_treated_as_miss() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first_delegate = Delegate()
    first_delegate.empty = True
    first = make_cache(first_delegate, redis, clock).bind(AUTHORITY)
    await first.read_session_events(session_id="session-a")
    cache_key = next(
        key for key in redis.values if ":generation:" not in key and ":append-watermark:" not in key
    )
    envelope = json.loads(redis.values[cache_key][0])
    envelope["value"]["verified_empty"] = False
    redis.values[cache_key] = (
        json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(),
        clock() + 30,
    )
    second_delegate = Delegate()
    second_delegate.empty = True
    second = make_cache(second_delegate, redis, clock).bind(AUTHORITY)

    page = await second.read_session_events(session_id="session-a")

    assert page.verified_empty is True
    assert cache_key in redis.deleted
    assert second_delegate.page_calls == 1


@pytest.mark.anyio
async def test_oversized_valid_page_is_returned_but_bypassed() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.large_content = "x" * 5000
    policy = EventCachePolicy(
        compression_enabled=False,
        compression_threshold_bytes=1024,
        redis_value_max_bytes=1024,
        raw_value_max_bytes=6000,
    )
    bound = make_cache(delegate, FakeRedis(clock), clock, policy=policy).bind(AUTHORITY)

    first = await bound.read_session_events(session_id="session-a")
    second = await bound.read_session_events(session_id="session-a")

    assert len(first.events[0].data["content"]) == 5000
    assert len(second.events[0].data["content"]) == 5000
    assert delegate.page_calls == 2


def test_default_and_custom_cache_policy_are_safe_and_immutable() -> None:
    default = EventCachePolicy()

    assert default.ttl_seconds == 3600
    assert default.sliding_expiration is True
    assert default.refresh_after_seconds == 1800
    assert default.compression_enabled is True
    assert default.compression_threshold_bytes == 64 * 1024
    assert default.compression_level == 1
    assert default.redis_value_max_bytes == 2 * 1024 * 1024
    assert default.raw_value_max_bytes == 16 * 1024 * 1024
    assert default.generation_ttl_seconds == 7200
    assert default.redis_page_values_enabled is True
    custom = EventCachePolicy(
        ttl_seconds=120,
        sliding_expiration=False,
        compression_enabled=False,
        compression_threshold_bytes=1024,
        compression_level=6,
        redis_value_max_bytes=2048,
        raw_value_max_bytes=4096,
        redis_page_values_enabled=False,
    )
    assert custom.refresh_after_seconds == 60
    assert custom.generation_ttl_seconds == 7200
    assert custom.redis_page_values_enabled is False
    maximum = EventCachePolicy(ttl_seconds=24 * 60 * 60)
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock), clock, policy=maximum)
    assert cache._policy.generation_ttl_seconds == 2 * maximum.ttl_seconds
    with pytest.raises(AttributeError):
        custom.ttl_seconds = 10  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ttl_seconds": 0},
        {"ttl_seconds": 24 * 60 * 60 + 1},
        {"sliding_expiration": 1},
        {"compression_enabled": 0},
        {"redis_page_values_enabled": 0},
        {"compression_threshold_bytes": 0},
        {"compression_level": 10},
        {"redis_value_max_bytes": 2 * 1024 * 1024 + 1},
        {"redis_value_max_bytes": 2048, "raw_value_max_bytes": 1024},
        {"raw_value_max_bytes": 65 * 1024 * 1024},
        {
            "redis_value_max_bytes": 1024,
            "raw_value_max_bytes": 2048,
            "compression_threshold_bytes": 4096,
        },
    ],
)
def test_cache_policy_rejects_unsafe_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EventCachePolicy(**kwargs)


def test_cache_policy_hard_caps_raw_values_at_sixteen_mib() -> None:
    with pytest.raises(ValueError, match="between Redis max and 16 MiB"):
        EventCachePolicy(raw_value_max_bytes=MAX_RAW_VALUE_BYTES + 1)


@pytest.mark.anyio
async def test_compression_disabled_stores_backward_compatible_raw_json() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.large_content = "x" * 100_000
    policy = EventCachePolicy(compression_enabled=False)
    first = make_cache(delegate, redis, clock, policy=policy)

    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    cache_key = _cache_value_keys(redis)[0]

    assert redis.values[cache_key][0].startswith(b"{")
    second_delegate = Delegate()
    page = (
        await make_cache(second_delegate, redis, clock)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )
    assert len(page.events[0].data["content"]) == 100_000
    assert second_delegate.page_calls == 0


@pytest.mark.anyio
async def test_compressible_page_over_two_mib_is_cached_under_redis_limit() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.large_content = "x" * (2 * 1024 * 1024 + 100_000)
    first = make_cache(delegate, redis, clock)

    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    payload = redis.values[_cache_value_keys(redis)[0]][0]

    assert payload.startswith(cache_module._WIRE_HEADER)
    assert len(payload) < 2 * 1024 * 1024
    second_delegate = Delegate()
    page = (
        await make_cache(second_delegate, redis, clock)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )
    assert len(page.events[0].data["content"]) > 2 * 1024 * 1024
    assert second_delegate.page_calls == 0


@pytest.mark.anyio
async def test_incompressible_value_over_stored_limit_bypasses_safely() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    random_bytes = random.Random(7).randbytes(2_800_000)
    delegate.large_content = base64.b64encode(random_bytes).decode()
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)

    first = await bound.read_session_events(session_id="session-a")
    second = await bound.read_session_events(session_id="session-a")

    assert first.events[0].data["content"] == second.events[0].data["content"]
    assert delegate.page_calls == 2
    assert _cache_value_keys(redis) == []


@pytest.mark.anyio
async def test_disabled_redis_page_values_use_l1_then_upstream_cross_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(redis_page_values_enabled=False)
    first_delegate = Delegate()
    first = make_cache(first_delegate, redis, clock, policy=policy).bind(AUTHORITY)
    first_cache = first._cache

    monkeypatch.setattr(
        first_cache,
        "_redis_generation_validated_get",
        AsyncMock(side_effect=AssertionError("page Redis get")),
    )
    monkeypatch.setattr(
        first_cache,
        "_redis_compare_set",
        AsyncMock(side_effect=AssertionError("page Redis set")),
    )
    monkeypatch.setattr(
        first_cache,
        "_schedule_redis_touch",
        Mock(side_effect=AssertionError("page Redis touch")),
    )
    monkeypatch.setattr(
        redis,
        "delete",
        AsyncMock(side_effect=AssertionError("page Redis delete")),
    )
    monkeypatch.setattr(
        first_cache,
        "_serialize",
        AsyncMock(side_effect=AssertionError("page Redis encode")),
    )
    monkeypatch.setattr(
        first_cache,
        "_deserialize",
        AsyncMock(side_effect=AssertionError("page Redis decode")),
    )

    first_page = await first.read_session_events(session_id="session-a")
    clock.advance((ACTIVE_CACHE_TTL_SECONDS / 2) + 1)
    l1_page = await first.read_session_events(session_id="session-a")
    await asyncio.sleep(0)

    assert first_page.events == l1_page.events
    assert first_delegate.page_calls == 1
    assert _cache_value_keys(redis) == []
    assert not any("compare-generation-and-touch" in script for script in redis.scripts)

    second_delegate = Delegate()
    second_cache = make_cache(second_delegate, redis, clock, policy=policy)
    second = second_cache.bind(AUTHORITY)
    monkeypatch.setattr(
        second_cache,
        "_redis_generation_validated_get",
        AsyncMock(side_effect=AssertionError("page Redis get")),
    )
    monkeypatch.setattr(
        second_cache,
        "_serialize",
        AsyncMock(side_effect=AssertionError("page Redis encode")),
    )
    monkeypatch.setattr(
        second_cache,
        "_deserialize",
        AsyncMock(side_effect=AssertionError("page Redis decode")),
    )

    cross_controller_page = await second.read_session_events(session_id="session-a")

    assert cross_controller_page.events == first_page.events
    assert second_delegate.page_calls == 1
    assert _cache_value_keys(redis) == []
    await first_cache.aclose()
    await second_cache.aclose()


@pytest.mark.anyio
async def test_disabled_page_values_detect_missed_cross_controller_generation_change() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(redis_page_values_enabled=False)
    first_delegate = Delegate()
    first_cache = make_cache(first_delegate, redis, clock, policy=policy)
    first = first_cache.bind(AUTHORITY)
    primed = await first.read_session_events(session_id="session-a")

    second_cache = make_cache(Delegate(), redis, clock, policy=policy)
    token = second_cache.session_token("intaris", "session-a")
    assert await second_cache.invalidate_session_token(token, source="cluster_signal")
    first_delegate.last_seq = 2
    first_delegate.content = "two"
    clock.advance((ACTIVE_CACHE_TTL_SECONDS / 2) + 1)

    stale_once = await first.read_session_events(session_id="session-a")
    await asyncio.gather(*tuple(first_cache._refresh_tasks.values()))
    refreshed = await first.read_session_events(session_id="session-a")

    assert primed.events[0].data["content"] == "one"
    assert stale_once.events[0].data["content"] == "one"
    assert refreshed.events[0].data["content"] == "two"
    assert first_delegate.page_calls == 2
    assert not any("compare-generation-and-touch" in script for script in redis.scripts)
    await first_cache.aclose()
    await second_cache.aclose()


@pytest.mark.anyio
async def test_disabled_page_values_keep_cross_controller_redis_watermarks() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(redis_page_values_enabled=False)
    first_delegate = Delegate()
    first = make_cache(first_delegate, redis, clock, policy=policy).bind(AUTHORITY)

    first_watermark = await first.read_session_high_watermark(session_id="session-a")

    second_delegate = Delegate()
    second = make_cache(second_delegate, redis, clock, policy=policy).bind(AUTHORITY)
    second_watermark = await second.read_session_high_watermark(session_id="session-a")

    assert second_watermark == first_watermark
    assert first_delegate.watermark_calls == 1
    assert second_delegate.watermark_calls == 0
    assert _cache_value_keys(redis)


@pytest.mark.anyio
async def test_disabled_page_values_do_not_serve_l1_during_redis_outage() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    policy = EventCachePolicy(redis_page_values_enabled=False)
    bound = make_cache(delegate, redis, clock, policy=policy).bind(AUTHORITY)
    primed = await bound.read_session_events(session_id="session-a")
    delegate.last_seq = 2
    delegate.content = "two"
    redis.fail = True
    redis.available = False

    refreshed = await bound.read_session_events(session_id="session-a")

    assert primed.events[0].data["content"] == "one"
    assert refreshed.events[0].data["content"] == "two"
    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_raw_value_over_decompressed_limit_bypasses_before_compression() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.large_content = "x" * 5000
    policy = EventCachePolicy(
        compression_threshold_bytes=512,
        redis_value_max_bytes=1024,
        raw_value_max_bytes=4096,
    )
    bound = make_cache(delegate, redis, clock, policy=policy).bind(AUTHORITY)

    await bound.read_session_events(session_id="session-a")
    await bound.read_session_events(session_id="session-a")

    assert delegate.page_calls == 2
    assert _cache_value_keys(redis) == []


@pytest.mark.anyio
async def test_compression_failure_falls_back_to_safe_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    delegate = Delegate()
    delegate.large_content = "x" * 100_000

    def fail_compression(_: bytes, *, level: int) -> bytes:
        raise RuntimeError(f"compression failed at level {level}")

    monkeypatch.setattr(cache_module.zlib, "compress", fail_compression)
    bound = make_cache(delegate, redis, clock).bind(AUTHORITY)

    first = await bound.read_session_events(session_id="session-a")
    second = await bound.read_session_events(session_id="session-a")

    assert first == second
    assert delegate.page_calls == 1
    assert redis.values[_cache_value_keys(redis)[0]][0].startswith(b"{")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        cache_module._WIRE_HEADER + zlib.compress(b"A" * 4097),
        cache_module._WIRE_MAGIC + bytes((99, 1)) + zlib.compress(b"{}"),
        cache_module._WIRE_MAGIC + bytes((1, 99)) + zlib.compress(b"{}"),
        cache_module._WIRE_HEADER + b"not-zlib",
        cache_module._WIRE_HEADER + b"x" * 1025,
    ],
)
async def test_malformed_or_unsafe_wire_values_are_deleted_and_missed(payload: bytes) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(
        compression_threshold_bytes=64,
        redis_value_max_bytes=1024,
        raw_value_max_bytes=4096,
    )
    first = make_cache(Delegate(), redis, clock, policy=policy)
    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    cache_key = _cache_value_keys(redis)[0]
    redis.values[cache_key] = (payload, clock() + 30)
    second_delegate = Delegate()

    page = (
        await make_cache(second_delegate, redis, clock, policy=policy)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )

    assert page.events[0].seq == 1
    assert cache_key in redis.deleted
    assert second_delegate.page_calls == 1


@pytest.mark.anyio
async def test_decompression_rejects_one_byte_over_sixteen_mib() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first = make_cache(Delegate(), redis, clock)
    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    cache_key = _cache_value_keys(redis)[0]
    redis.values[cache_key] = (
        cache_module._WIRE_HEADER + zlib.compress(b"A" * (MAX_RAW_VALUE_BYTES + 1), level=1),
        clock() + 30,
    )
    second_delegate = Delegate()

    page = (
        await make_cache(second_delegate, redis, clock)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )

    assert page.events[0].seq == 1
    assert cache_key in redis.deleted
    assert second_delegate.page_calls == 1


@pytest.mark.anyio
async def test_compressed_deeply_nested_json_is_deleted_and_missed() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    first = make_cache(Delegate(), redis, clock)
    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    cache_key = _cache_value_keys(redis)[0]
    deeply_nested = (b"[" * 2000) + b"0" + (b"]" * 2000)
    redis.values[cache_key] = (
        cache_module._WIRE_HEADER + zlib.compress(deeply_nested, level=1),
        clock() + 30,
    )
    second_delegate = Delegate()

    page = (
        await make_cache(second_delegate, redis, clock)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )

    assert page.events[0].seq == 1
    assert cache_key in redis.deleted
    assert second_delegate.page_calls == 1


@pytest.mark.anyio
async def test_eighteen_concurrent_redis_decodes_wait_with_four_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    producer_delegate = Delegate()
    producer_delegate.large_content = "x" * 100_000
    producer = make_cache(producer_delegate, redis, clock)
    producer_bound = producer.bind(AUTHORITY)
    session_count = 18
    for index in range(session_count):
        await producer_bound.read_session_events(session_id=f"session-{index}")

    consumer_delegate = Delegate()
    consumer = make_cache(consumer_delegate, redis, clock)
    bound = consumer.bind(AUTHORITY)
    original = consumer._deserialize_sync
    lock = threading.Lock()
    release = threading.Event()
    all_workers_started = threading.Event()
    active_workers = 0
    max_active_workers = 0
    worker_threads: set[int] = set()

    def blocking_decode(*args: Any, **kwargs: Any) -> SessionEventPage | SessionWatermark | None:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            worker_threads.add(threading.get_ident())
            if active_workers == cache_module._CODEC_WORKERS:
                all_workers_started.set()
        assert release.wait(timeout=10)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                active_workers -= 1

    monkeypatch.setattr(consumer, "_deserialize_sync", blocking_decode)
    reads = [
        asyncio.create_task(bound.read_session_events(session_id=f"session-{index}"))
        for index in range(session_count)
    ]
    heartbeat = 0
    for _ in range(10_000):
        if all_workers_started.is_set():
            break
        heartbeat += 1
        await asyncio.sleep(0)
    assert all_workers_started.is_set()
    assert heartbeat > 0
    assert consumer.diagnostics()["codec_tasks"] == cache_module._CODEC_WORKERS
    assert consumer.diagnostics()["codec_waiting"] == session_count - cache_module._CODEC_WORKERS
    for _ in range(100):
        heartbeat += 1
        await asyncio.sleep(0)
    assert heartbeat >= 100
    assert max_active_workers == cache_module._CODEC_WORKERS
    assert threading.get_ident() not in worker_threads
    assert not any(task.done() for task in reads)
    assert consumer.diagnostics()["inflight"] == session_count

    release.set()
    pages = await asyncio.gather(*reads)

    assert len(pages) == session_count
    assert all(len(page.events[0].data["content"]) == 100_000 for page in pages)
    assert consumer_delegate.page_calls == 0
    assert max_active_workers == cache_module._CODEC_WORKERS
    assert consumer.diagnostics()["codec_tasks"] == 0
    assert consumer.diagnostics()["codec_waiting"] == 0
    await producer.aclose()
    await consumer.aclose()


@pytest.mark.anyio
async def test_encode_and_decode_pipelines_run_off_controller_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    controller_thread = threading.get_ident()
    encode_threads: set[int] = set()
    decode_threads: set[int] = set()
    producer = make_cache(Delegate(), redis, clock)
    original_encode = producer._serialize_sync

    def observe_encode(
        operation: cache_module.CacheOperation,
        session_id: str,
        generation: cache_module._Generation,
        query_digest: str,
        value: SessionEventPage | SessionWatermark,
    ) -> bytes | None:
        encode_threads.add(threading.get_ident())
        return original_encode(operation, session_id, generation, query_digest, value)

    monkeypatch.setattr(producer, "_serialize_sync", observe_encode)
    await producer.bind(AUTHORITY).read_session_events(session_id="session-a")

    consumer_delegate = Delegate()
    consumer = make_cache(consumer_delegate, redis, clock)
    original_decode = consumer._deserialize_sync

    def observe_decode(*args: Any, **kwargs: Any) -> SessionEventPage | SessionWatermark | None:
        decode_threads.add(threading.get_ident())
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(consumer, "_deserialize_sync", observe_decode)
    page = await consumer.bind(AUTHORITY).read_session_events(session_id="session-a")

    assert page.events[0].seq == 1
    assert consumer_delegate.page_calls == 0
    assert encode_threads and controller_thread not in encode_threads
    assert decode_threads and controller_thread not in decode_threads
    await producer.aclose()
    await consumer.aclose()


@pytest.mark.anyio
async def test_codec_capacity_saturates_then_reuses_cancelled_and_failed_permits() -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock)
    release = threading.Event()
    lock = threading.Lock()
    all_workers_started = threading.Event()
    active_workers = 0
    max_active_workers = 0

    def blocking_codec() -> int:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            if active_workers == cache_module._CODEC_WORKERS:
                all_workers_started.set()
        assert release.wait(timeout=10)
        try:
            return 1
        finally:
            with lock:
                active_workers -= 1

    admitted = [
        asyncio.create_task(cache._run_codec(blocking_codec))
        for _ in range(cache_module._CODEC_CAPACITY)
    ]
    for _ in range(10_000):
        if (
            all_workers_started.is_set()
            and cache.diagnostics()["codec_waiting"]
            == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
        ):
            break
        await asyncio.sleep(0)
    assert all_workers_started.is_set()
    assert cache.diagnostics()["codec_tasks"] == cache_module._CODEC_WORKERS
    assert (
        cache.diagnostics()["codec_waiting"]
        == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
    )
    with pytest.raises(cache_module._CodecSaturated):
        await cache._run_codec(lambda: 2)

    cancelled = admitted.pop()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    replacement = asyncio.create_task(cache._run_codec(blocking_codec))
    for _ in range(10_000):
        if (
            cache.diagnostics()["codec_waiting"]
            == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
        ):
            break
        await asyncio.sleep(0)

    release.set()
    results = await asyncio.gather(*admitted, replacement)

    assert results == [1] * cache_module._CODEC_CAPACITY
    assert max_active_workers == cache_module._CODEC_WORKERS
    assert cache.diagnostics()["codec_tasks"] == 0
    assert cache.diagnostics()["codec_waiting"] == 0

    def fail_codec() -> int:
        raise RuntimeError("codec failed")

    with pytest.raises(RuntimeError, match="codec failed"):
        await cache._run_codec(fail_codec)
    await _exercise_full_codec_capacity(cache)
    await cache.aclose()


@pytest.mark.anyio
async def test_codec_saturation_fails_open_without_deleting_valid_redis_value() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    producer = make_cache(Delegate(), redis, clock)
    await producer.bind(AUTHORITY).read_session_events(session_id="redis-target")
    target_key = _cache_value_keys(redis)[0]
    target_payload = redis.values[target_key]

    consumer_delegate = Delegate()
    consumer_delegate.content = "authoritative-upstream"
    consumer = make_cache(consumer_delegate, redis, clock)
    release = threading.Event()
    all_workers_started = threading.Event()
    lock = threading.Lock()
    active_workers = 0

    def blocking_codec() -> int:
        nonlocal active_workers
        with lock:
            active_workers += 1
            if active_workers == cache_module._CODEC_WORKERS:
                all_workers_started.set()
        assert release.wait(timeout=10)
        try:
            return 1
        finally:
            with lock:
                active_workers -= 1

    admitted = [
        asyncio.create_task(consumer._run_codec(blocking_codec))
        for _ in range(cache_module._CODEC_CAPACITY)
    ]
    for _ in range(10_000):
        if (
            all_workers_started.is_set()
            and consumer.diagnostics()["codec_waiting"]
            == cache_module._CODEC_CAPACITY - cache_module._CODEC_WORKERS
        ):
            break
        await asyncio.sleep(0)
    assert all_workers_started.is_set()
    saturation_before = BYPASSED.labels(reason="codec_saturated")._value.get()

    page = await consumer.bind(AUTHORITY).read_session_events(session_id="redis-target")

    assert page.events[0].data["content"] == "authoritative-upstream"
    assert consumer_delegate.page_calls == 1
    assert redis.values[target_key] == target_payload
    assert target_key not in redis.deleted
    assert BYPASSED.labels(reason="codec_saturated")._value.get() >= saturation_before + 2
    release.set()
    await asyncio.gather(*admitted)
    await producer.aclose()
    await consumer.aclose()


@pytest.mark.anyio
async def test_codec_task_creation_failure_releases_all_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock)
    original_create_task = cache_module.asyncio.create_task

    def fail_create_task(_: Any) -> Any:
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(cache_module.asyncio, "create_task", fail_create_task)
    with pytest.raises(RuntimeError, match="task creation failed"):
        await cache._run_codec(lambda: 1)
    monkeypatch.setattr(cache_module.asyncio, "create_task", original_create_task)

    assert cache.diagnostics()["codec_tasks"] == 0
    assert cache.diagnostics()["codec_waiting"] == 0
    await _exercise_full_codec_capacity(cache)
    await cache.aclose()


@pytest.mark.anyio
async def test_aclose_cancels_waiting_codecs_and_drains_active_workers() -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock, configured=False), clock)
    release = threading.Event()
    all_workers_started = threading.Event()
    lock = threading.Lock()
    active_workers = 0

    def blocking_codec() -> int:
        nonlocal active_workers
        with lock:
            active_workers += 1
            if active_workers == cache_module._CODEC_WORKERS:
                all_workers_started.set()
        assert release.wait(timeout=10)
        try:
            return 1
        finally:
            with lock:
                active_workers -= 1

    call_count = 18
    calls = [asyncio.create_task(cache._run_codec(blocking_codec)) for _ in range(call_count)]
    for _ in range(10_000):
        if (
            all_workers_started.is_set()
            and cache.diagnostics()["codec_waiting"] == call_count - cache_module._CODEC_WORKERS
        ):
            break
        await asyncio.sleep(0)
    assert all_workers_started.is_set()

    close = asyncio.create_task(cache.aclose())
    for _ in range(10_000):
        if cache.diagnostics()["codec_waiting"] == 0:
            break
        await asyncio.sleep(0)
    assert cache.diagnostics()["codec_waiting"] == 0
    assert not close.done()
    release.set()
    await asyncio.wait_for(close, timeout=10)
    results = await asyncio.gather(*calls, return_exceptions=True)

    assert sum(isinstance(result, asyncio.CancelledError) for result in results) == (
        call_count - cache_module._CODEC_WORKERS
    )
    assert sum(result == 1 for result in results) == cache_module._CODEC_WORKERS
    assert cache.diagnostics()["codec_tasks"] == 0
    assert cache.diagnostics()["codec_waiting"] == 0


@pytest.mark.anyio
async def test_aclose_prevents_append_codec_from_repopulating_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    cache = make_cache(Delegate(), redis, clock)
    original = cache._serialize_sync
    entered = threading.Event()
    release = threading.Event()

    def blocking_serialize(
        operation: cache_module.CacheOperation,
        session_id: str,
        generation: cache_module._Generation,
        query_digest: str,
        value: SessionEventPage | SessionWatermark,
    ) -> bytes | None:
        entered.set()
        assert release.wait(timeout=10)
        return original(operation, session_id, generation, query_digest, value)

    monkeypatch.setattr(cache, "_serialize_sync", blocking_serialize)
    append = asyncio.create_task(
        cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 1, 1, 1))
    )
    for _ in range(10_000):
        if entered.is_set():
            break
        await asyncio.sleep(0)
    assert entered.is_set()
    assert _cache_value_keys(redis) == []

    close = asyncio.create_task(cache.aclose())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await asyncio.wait_for(close, timeout=10)
    result = await append

    assert result is False
    assert cache.diagnostics()["entries"] == 0
    assert cache.diagnostics()["codec_tasks"] == 0
    assert _cache_value_keys(redis) == []


@pytest.mark.anyio
async def test_aclose_waits_for_active_append_redis_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock), clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    write_finished = False

    async def blocking_compare_set(*args: Any, **kwargs: Any) -> bool:
        nonlocal write_finished
        entered.set()
        await release.wait()
        write_finished = True
        return True

    monkeypatch.setattr(cache, "_redis_compare_set", blocking_compare_set)
    append = asyncio.create_task(
        cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 1, 1, 1))
    )
    await entered.wait()

    close = asyncio.create_task(cache.aclose())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await asyncio.wait_for(close, timeout=10)

    assert await append is True
    assert write_finished is True
    assert cache.diagnostics()["entries"] == 0


@pytest.mark.anyio
async def test_redis_hit_atomically_refreshes_only_matching_generation() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(ttl_seconds=100)
    first = make_cache(Delegate(), redis, clock, policy=policy)
    await first.bind(AUTHORITY).read_session_events(session_id="session-a")
    token = first.session_token("intaris", "session-a")
    generation = first._generations[token].generation
    cache_key = _cache_value_keys(redis)[0]
    initial_expiry = redis.values[cache_key][1]
    clock.advance(30)

    second_delegate = Delegate()
    await (
        make_cache(second_delegate, redis, clock, policy=policy)
        .bind(AUTHORITY)
        .read_session_events(session_id="session-a")
    )

    assert second_delegate.page_calls == 0
    assert redis.values[cache_key][1] == clock() + policy.ttl_seconds
    assert redis.values[cache_key][1] > initial_expiry
    await first.invalidate_session("intaris", "session-a")
    expiry_after_bump = redis.values[cache_key][1]
    payload, status = await first._redis_generation_validated_get(
        token,
        generation,
        cache_key,
        ttl_seconds=policy.ttl_seconds,
    )
    assert payload is None
    assert status == "generation_changed"
    assert redis.values[cache_key][1] == expiry_after_bump


@pytest.mark.anyio
async def test_l1_sliding_refresh_waits_for_threshold_and_touches_redis_in_background() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    policy = EventCachePolicy(ttl_seconds=100)
    cache = make_cache(Delegate(), redis, clock, policy=policy)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    cache_key = _cache_value_keys(redis)[0]
    initial_expiry = redis.values[cache_key][1]
    initial_script_count = len(redis.scripts)
    clock.advance(49)

    await bound.read_session_events(session_id="session-a")
    assert len(redis.scripts) == initial_script_count
    assert redis.values[cache_key][1] == initial_expiry
    clock.advance(2)

    page = await bound.read_session_events(session_id="session-a")
    await asyncio.sleep(0)

    assert page.events[0].seq == 1
    assert sum("compare-generation-and-touch-v1" in script for script in redis.scripts) == 1
    assert redis.values[cache_key][1] == clock() + policy.ttl_seconds


@pytest.mark.anyio
async def test_l1_sliding_refresh_is_coalesced_and_append_cancels_old_touch() -> None:
    clock = FakeClock()
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowTouchRedis(FakeRedis):
        touch_calls = 0

        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes] | tuple[str | bytes, ...] = (),
            args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
        ) -> Any | None:
            text = script.decode() if isinstance(script, bytes) else script
            if "compare-generation-and-touch-v1" in text:
                self.touch_calls += 1
                entered.set()
                await release.wait()
            return await super().eval(script, keys=keys, args=args)

    redis = SlowTouchRedis(clock)
    delegate = Delegate()
    policy = EventCachePolicy(ttl_seconds=100)
    cache = make_cache(delegate, redis, clock, policy=policy)
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    clock.advance(51)

    await bound.read_session_events(session_id="session-a")
    await entered.wait()
    await asyncio.gather(*(bound.read_session_events(session_id="session-a") for _ in range(10)))
    delegate.last_seq = 2
    delegate.content = "two"
    assert await cache.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))
    release.set()
    await asyncio.sleep(0)
    refreshed = await bound.read_session_events(session_id="session-a")

    assert redis.touch_calls == 1
    assert refreshed.events[0].data["content"] == "two"
    assert delegate.page_calls == 2
    await cache.aclose()


@pytest.mark.anyio
async def test_remote_generation_change_prevents_old_generation_touch() -> None:
    clock = FakeClock()
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowTouchRedis(FakeRedis):
        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes] | tuple[str | bytes, ...] = (),
            args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
        ) -> Any | None:
            text = script.decode() if isinstance(script, bytes) else script
            if "compare-generation-and-touch-v1" in text:
                entered.set()
                await release.wait()
            return await super().eval(script, keys=keys, args=args)

    redis = SlowTouchRedis(clock)
    policy = EventCachePolicy(ttl_seconds=100)
    delegate = Delegate()
    first = make_cache(delegate, redis, clock, policy=policy)
    bound = first.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    old_key = _cache_value_keys(redis)[0]
    old_expiry = redis.values[old_key][1]
    clock.advance(51)

    stale = await bound.read_session_events(session_id="session-a")
    await entered.wait()
    second = make_cache(Delegate(), redis, clock, policy=policy)
    assert await second.handle_append(EventAppendNotification(AUTHORITY, "session-a", 2, 2, 1))
    delegate.last_seq = 2
    delegate.content = "two"
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if first.diagnostics()["refresh_tasks"] == 0:
            break
    assert first.diagnostics()["entries"] == 0
    refreshed = await bound.read_session_events(session_id="session-a")

    assert stale.events[0].data["content"] == "one"
    assert redis.values[old_key][1] == old_expiry
    assert first.diagnostics()["entries"] == 1
    assert refreshed.events[0].data["content"] == "two"
    assert delegate.page_calls == 2
    await first.aclose()
    await second.aclose()


@pytest.mark.anyio
async def test_refresh_tasks_are_globally_bounded_and_cancelled_on_close() -> None:
    clock = FakeClock()
    release = asyncio.Event()

    class BlockingTouchRedis(FakeRedis):
        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes] | tuple[str | bytes, ...] = (),
            args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
        ) -> Any | None:
            text = script.decode() if isinstance(script, bytes) else script
            if "compare-generation-and-touch-v1" in text:
                await release.wait()
            return await super().eval(script, keys=keys, args=args)

    redis = BlockingTouchRedis(clock)
    policy = EventCachePolicy(ttl_seconds=100)
    cache = make_cache(
        Delegate(),
        redis,
        clock,
        bounds=EventCacheBounds(l1_max_entries=200),
        policy=policy,
    )
    bound = cache.bind(AUTHORITY)
    for index in range(cache_module._MAX_REFRESH_TASKS + 1):
        await bound.read_session_events(session_id=f"session-{index}")
    overflow_token = cache.session_token("intaris", f"session-{cache_module._MAX_REFRESH_TASKS}")
    overflow_key = next(iter(cache._session_index[overflow_token]))
    original_overflow_expiry = cache._l1[overflow_key].expires_at
    clock.advance(51)

    for index in range(cache_module._MAX_REFRESH_TASKS + 1):
        await bound.read_session_events(session_id=f"session-{index}")

    assert cache.diagnostics()["refresh_tasks"] == cache_module._MAX_REFRESH_TASKS
    assert cache._l1[overflow_key].expires_at == original_overflow_expiry
    clock.advance(50)
    await bound.read_session_events(session_id=f"session-{cache_module._MAX_REFRESH_TASKS}")
    assert cache._l1[overflow_key].expires_at > original_overflow_expiry
    await cache.aclose()
    assert cache.diagnostics()["refresh_tasks"] == 0


@pytest.mark.anyio
async def test_background_refresh_failure_does_not_affect_l1_read() -> None:
    clock = FakeClock()

    class FailingTouchRedis(FakeRedis):
        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes] | tuple[str | bytes, ...] = (),
            args: list[str | bytes | int | float] | tuple[str | bytes | int | float, ...] = (),
        ) -> Any | None:
            text = script.decode() if isinstance(script, bytes) else script
            if "compare-generation-and-touch-v1" in text:
                raise RuntimeError("refresh failed")
            return await super().eval(script, keys=keys, args=args)

    redis = FailingTouchRedis(clock)
    delegate = Delegate()
    cache = make_cache(delegate, redis, clock, policy=EventCachePolicy(ttl_seconds=100))
    bound = cache.bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    token = cache.session_token("intaris", "session-a")
    key = next(iter(cache._session_index[token]))
    original_expiry = cache._l1[key].expires_at
    clock.advance(51)

    page = await bound.read_session_events(session_id="session-a")
    await asyncio.sleep(0)

    assert page.events[0].seq == 1
    assert delegate.page_calls == 1
    assert cache.diagnostics()["refresh_tasks"] == 0
    assert cache._l1[key].expires_at == original_expiry
    clock.advance(50)
    await bound.read_session_events(session_id="session-a")
    assert delegate.page_calls == 2


@pytest.mark.anyio
async def test_invalid_read_shapes_are_rejected_before_cache_or_upstream() -> None:
    clock = FakeClock()
    delegate = Delegate()
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)

    with pytest.raises(ValueError, match="session_id"):
        await bound.read_session_events(session_id="")
    with pytest.raises(ValueError, match="limit"):
        await bound.read_session_events(session_id="session-a", limit=0)
    with pytest.raises(ValueError, match="after_seq"):
        await bound.read_session_events(session_id="session-a", after_seq=-1)
    with pytest.raises(ValueError, match="direction"):
        await bound.read_session_events(session_id="session-a", direction=cast(Any, "sideways"))

    assert delegate.page_calls == 0


@pytest.mark.anyio
async def test_expired_l1_is_never_served_on_upstream_error() -> None:
    clock = FakeClock()
    delegate = Delegate()
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)
    await bound.read_session_events(session_id="session-a")
    clock.advance(ACTIVE_CACHE_TTL_SECONDS + 1)
    delegate.page_error = RuntimeError("upstream failed")

    with pytest.raises(RuntimeError, match="upstream failed"):
        await bound.read_session_events(session_id="session-a")


@pytest.mark.anyio
async def test_waiter_cancellation_is_shielded_from_shared_fill() -> None:
    clock = FakeClock()
    delegate = Delegate()
    delegate.gate = asyncio.Event()
    delegate.started = asyncio.Event()
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)
    cancelled = asyncio.create_task(bound.read_session_events(session_id="session-a"))
    await delegate.started.wait()
    survivor = asyncio.create_task(bound.read_session_events(session_id="session-a"))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    delegate.gate.set()

    page = await survivor

    assert page.events[0].seq == 1
    assert delegate.page_calls == 1


@pytest.mark.anyio
async def test_backing_session_rotation_naturally_partitions_cache() -> None:
    clock = FakeClock()
    delegate = Delegate()
    bound = make_cache(delegate, FakeRedis(clock), clock).bind(AUTHORITY)

    old_page = await bound.read_session_events(session_id="backing-before-compaction")
    delegate.last_seq = 2
    new_page = await bound.read_session_events(session_id="backing-after-compaction")

    assert old_page.session_id == "backing-before-compaction"
    assert new_page.session_id == "backing-after-compaction"
    assert delegate.page_calls == 2


def test_metric_labels_are_fixed_and_reject_unknown_values() -> None:
    assert CACHE_HITS._name == "cognis_event_read_cache_hits"
    assert CACHE_MISSES._name == "cognis_event_read_cache_misses"
    assert CACHE_ERRORS._name == "cognis_event_read_cache_errors"
    assert SINGLEFLIGHT_JOINS._name == "cognis_event_read_cache_singleflight_joins"
    assert UPSTREAM_READS._name == "cognis_event_read_cache_upstream_reads"
    assert UPSTREAM_LATENCY._name == "cognis_event_read_cache_upstream_latency_seconds"
    assert INVALIDATIONS._name == "cognis_event_read_cache_invalidations"
    assert BYPASSED._name == "cognis_event_read_cache_bypassed"
    assert CACHE_ENTRIES._name == "cognis_event_read_cache_entries"
    assert CACHE_BYTES._name == "cognis_event_read_cache_bytes"
    assert RAW_PAYLOAD_BYTES._name == "cognis_event_read_cache_raw_payload_bytes"
    assert STORED_PAYLOAD_BYTES._name == "cognis_event_read_cache_stored_payload_bytes"
    assert COMPRESSION_RATIO._name == "cognis_event_read_cache_compression_ratio"
    assert COMPRESSION_OUTCOMES._name == "cognis_event_read_cache_compression_outcomes"
    assert SLIDING_REFRESHES._name == "cognis_event_read_cache_sliding_refreshes"
    assert SLIDING_REFRESH_ERRORS._name == "cognis_event_read_cache_sliding_refresh_errors"
    assert PAGE_QUERIES._name == "cognis_event_read_cache_page_queries"
    assert DECODE_FAILURES._name == "cognis_event_read_cache_decode_failures"
    assert CACHE_HITS._labelnames == ("tier", "operation")
    assert CACHE_MISSES._labelnames == ("tier", "operation")
    assert CACHE_ERRORS._labelnames == ("tier", "operation")
    assert SINGLEFLIGHT_JOINS._labelnames == ("operation",)
    assert UPSTREAM_READS._labelnames == ("operation",)
    assert UPSTREAM_LATENCY._labelnames == ("operation",)
    assert INVALIDATIONS._labelnames == ("source",)
    assert BYPASSED._labelnames == ("reason",)
    assert CACHE_ENTRIES._labelnames == ()
    assert CACHE_BYTES._labelnames == ()
    assert RAW_PAYLOAD_BYTES._labelnames == ()
    assert STORED_PAYLOAD_BYTES._labelnames == ()
    assert COMPRESSION_RATIO._labelnames == ()
    assert COMPRESSION_OUTCOMES._labelnames == ("outcome",)
    assert SLIDING_REFRESHES._labelnames == ("tier",)
    assert SLIDING_REFRESH_ERRORS._labelnames == ("tier",)
    assert PAGE_QUERIES._labelnames == ("query_class",)
    assert DECODE_FAILURES._labelnames == ("reason",)
    metrics = EventCacheMetrics()
    with pytest.raises(ValueError, match="unknown tier"):
        metrics.hit(cast(Any, "user@example.com"), "page")
    with pytest.raises(ValueError, match="unknown invalidation source"):
        metrics.invalidation(cast(Any, "session-a"))
    with pytest.raises(ValueError, match="unknown bypass reason"):
        metrics.bypass(cast(Any, "append_pending"))
    with pytest.raises(ValueError, match="unknown compression outcome"):
        metrics.compression(cast(Any, "session-a"))
    with pytest.raises(ValueError, match="unknown refresh tier"):
        metrics.sliding_refresh(cast(Any, "user@example.com"))
    with pytest.raises(ValueError, match="unknown page query class"):
        metrics.page_query(cast(Any, "session-a"))
    with pytest.raises(ValueError, match="unknown decode failure reason"):
        metrics.decode_failure(cast(Any, "cursor"))


def test_metric_backend_failures_do_not_affect_cache_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenMetric:
        def labels(self, **_labels: Any) -> Any:
            raise RuntimeError("metrics unavailable")

    monkeypatch.setattr(cache_metrics_module, "PAGE_QUERIES", BrokenMetric())

    EventCacheMetrics().page_query("forward_delta")


def test_page_query_classes_are_fixed_and_content_free() -> None:
    classify = CachedSessionEventStore._page_query_class

    assert classify(after_seq=10, before_seq=None, direction="forward") == "forward_delta"
    assert classify(after_seq=0, before_seq=None, direction="forward") == "initial_forward"
    assert classify(after_seq=None, before_seq=None, direction="backward") == "backward_tail"
    assert classify(after_seq=None, before_seq=10, direction="backward") == "historical_backward"


@pytest.mark.anyio
async def test_work_overview_fence_changes_snapshot_cache_identity() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock)
    session_ref = ConversationSessionRef(
        session_id="session-a",
        event_store_session_id="session-a",
        ordinal=0,
        reader=bound,
        authority_token=bound.authority_token,
    )
    authority = events.derived_key_digest(
        "snapshot-conversation-authority",
        AUTHORITY.user_email,
    )
    events._advance_local_watermark_floor(
        events.session_token("intaris", "session-a"),
        events._authority_digest(AUTHORITY),
        1,
    )

    before = await cache._identity(
        authority_token=authority,
        scope_key="conversation:conversation-a",
        session_refs=[session_ref],
        overview_fence="covered:10",
        overview_coverage=(("session-a", 0),),
    )
    after = await cache._identity(
        authority_token=authority,
        scope_key="conversation:conversation-a",
        session_refs=[session_ref],
        overview_fence="covered:11",
        overview_coverage=(("session-a", 1),),
    )

    assert before is not None and after is not None
    assert before.value_key != after.value_key
    assert before.lock_key != after.lock_key
    assert before.overview_ready is False
    assert after.overview_ready is True

    builds = 0

    async def forbidden_build() -> Any:
        nonlocal builds
        builds += 1
        raise AssertionError("stale Work coverage must not build a warmed snapshot")

    warm = await cache.get_or_build_result(
        authority_token=authority,
        scope_key="conversation:conversation-a",
        session_refs=[session_ref],
        cursor_secret="cursor-secret",
        overview_fence="covered:10",
        overview_coverage=(("session-a", 0),),
        build=forbidden_build,
        fail_open=False,
    )
    cached = await cache.get_cached_result(
        authority_token=authority,
        scope_key="conversation:conversation-a",
        session_refs=[session_ref],
        cursor_secret="cursor-secret",
        overview_fence="covered:10",
        overview_coverage=(("session-a", 0),),
    )

    assert warm.snapshot is None
    assert warm.warm_failure == "context_changed"
    assert cache.warm_outcome("conversation:conversation-a") == "skipped"
    assert cached.status == "miss"
    assert builds == 0


@pytest.mark.parametrize(
    ("reason", "payload_factory"),
    [
        ("size", lambda cache, _envelope: b"x" * (cache._policy.redis_value_max_bytes + 1)),
        ("wire", lambda _cache, _envelope: cache_module._WIRE_MAGIC),
        (
            "decompression",
            lambda _cache, _envelope: cache_module._WIRE_HEADER + b"not-zlib",
        ),
        ("json", lambda _cache, _envelope: b"{"),
        ("envelope", lambda _cache, _envelope: b"{}"),
        (
            "schema",
            lambda _cache, envelope: json.dumps(
                {**envelope, "value": {"invalid": True}},
                separators=(",", ":"),
            ).encode(),
        ),
        (
            "value",
            lambda _cache, envelope: json.dumps(
                {
                    **envelope,
                    "value": {
                        **envelope["value"],
                        "session_id": "different-session",
                    },
                },
                separators=(",", ":"),
            ).encode(),
        ),
    ],
)
def test_decode_failures_use_fixed_reasons(reason: str, payload_factory: Any) -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock), clock)
    generation = cache_module._Generation("0" * 32, 0)
    query = cache._query_digest(
        "page",
        after_seq=0,
        before_seq=None,
        limit=500,
        direction="forward",
    )
    page = SessionEventPage(
        store_id="intaris",
        session_id="session-a",
        events=[],
        last_seq=0,
        verified_empty=True,
    )
    envelope = {
        "version": cache_module.CACHE_SCHEMA_VERSION,
        "operation": "page",
        "store_id": "intaris",
        "session_id": "session-a",
        "generation": generation.encoded,
        "query": query,
        "value": page.model_dump(mode="json"),
    }
    before = DECODE_FAILURES.labels(reason=reason)._value.get()

    result = cache._deserialize_sync(
        payload_factory(cache, envelope),
        operation="page",
        session_id="session-a",
        generation=generation,
        query_digest=query,
        after_seq=0,
        before_seq=None,
        limit=500,
        direction="forward",
    )

    assert result is None
    assert DECODE_FAILURES.labels(reason=reason)._value.get() == before + 1


def test_diagnostics_and_keys_do_not_expose_identity() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    cache = make_cache(Delegate(), redis, clock)

    diagnostics = cache.diagnostics()
    token = cache.session_token("intaris", "session-a")

    assert set(diagnostics) == {
        "configured",
        "available",
        "redis_configured",
        "redis_available",
        "redis_page_values_enabled",
        "entries",
        "bytes",
        "inflight",
        "refresh_tasks",
        "codec_tasks",
        "codec_waiting",
        "codec_capacity",
        "generation_sessions",
        "generation_bytes",
        "pending_appends",
        "append_overflow_fallback",
    }
    assert "session-a" not in token
    assert "user@example.com" not in repr(diagnostics)


@pytest.mark.anyio
async def test_aclose_does_not_close_shared_redis() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    cache = make_cache(Delegate(), redis, clock)

    await cache.aclose()

    assert redis.configured is True
    assert cache.available is False
    assert cache.diagnostics()["entries"] == 0


def test_bound_store_is_immutable_and_implements_store_identity() -> None:
    clock = FakeClock()
    cache = make_cache(Delegate(), FakeRedis(clock), clock)
    bound = cache.bind(AUTHORITY)

    assert isinstance(bound, BoundSessionEventStore)
    assert bound.store_id == "intaris"
    assert len(bound.authority_token) == 64
    assert AUTHORITY.user_email not in bound.authority_token
    assert AUTHORITY.agent_id not in bound.authority_token
    assert AUTHORITY.agent_owner_email not in bound.authority_token
    tokens = {
        bound.authority_token,
        cache.bind(
            EventStoreAuthority("other@example.com", "agent-a", "owner@example.com")
        ).authority_token,
        cache.bind(
            EventStoreAuthority("user@example.com", "agent-b", "owner@example.com")
        ).authority_token,
        cache.bind(
            EventStoreAuthority("user@example.com", "agent-a", "other@example.com")
        ).authority_token,
    }
    assert len(tokens) == 4
    with pytest.raises(AttributeError):
        bound._authority = OTHER_AUTHORITY  # type: ignore[misc]
