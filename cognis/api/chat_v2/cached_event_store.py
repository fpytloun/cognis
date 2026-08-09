"""Authority-bound L1/Redis cache for canonical session event reads."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import zlib
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Any, Literal, TypeVar, cast

from pydantic import ValidationError

from cognis.api.chat_v2.cache_metrics import (
    EVENT_CACHE_METRICS,
    CacheOperation,
    DecodeFailureReason,
    EventCacheMetrics,
    InvalidationSource,
    PageQueryClass,
)
from cognis.api.chat_v2.event_store import (
    SessionEventPage,
    SessionEventStore,
    SessionWatermark,
)
from cognis.core.redis_service import RedisService
from cognis.providers.guardrails.events import EventAppendNotification, EventStoreAuthority
from cognis.runtime_context import scoped_runtime_context

CACHE_SCHEMA_VERSION = 1
MAX_REDIS_VALUE_BYTES = 2 * 1024 * 1024
MAX_RAW_VALUE_BYTES = 16 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 60 * 60
ACTIVE_CACHE_TTL_SECONDS = DEFAULT_CACHE_TTL_SECONDS
MAX_CANONICAL_CACHE_TTL_SECONDS = DEFAULT_CACHE_TTL_SECONDS
_MIN_GENERATION_TTL_SECONDS = 2 * 60 * 60
_MAX_CACHE_TTL_SECONDS = 24 * 60 * 60
_MAX_REFRESH_TASKS = 128
_CODEC_WORKERS = 4
_CODEC_CAPACITY = 64
_DECOMPRESSION_CHUNK_BYTES = 64 * 1024
_WIRE_MAGIC = b"\x89CGEC"
_WIRE_VERSION = 1
_CODEC_ZLIB = 1
_WIRE_HEADER = _WIRE_MAGIC + bytes((_WIRE_VERSION, _CODEC_ZLIB))
_CodecResult = TypeVar("_CodecResult")


class _CodecSaturated(Exception):
    """The bounded codec worker set has no admission capacity."""


_GENERATION_GET_OR_INIT = """
-- cognis-event-cache-generation-get-or-init-v1
local current = redis.call('GET', KEYS[1])
if not current then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return {1, ARGV[1]}
end
local epoch, counter = string.match(current, '^([0-9a-f]+):([0-9]+)$')
if not epoch or string.len(epoch) ~= 32 or not counter then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return {1, ARGV[1]}
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return {0, current}
"""

_GENERATION_BUMP = """
-- cognis-event-cache-generation-bump-v1
local current = redis.call('GET', KEYS[1])
local next_value = ARGV[1]
if current then
  local epoch, counter = string.match(current, '^([0-9a-f]+):([0-9]+)$')
  if epoch and string.len(epoch) == 32 and counter then
    next_value = epoch .. ':' .. tostring(tonumber(counter) + 1)
  end
end
redis.call('SET', KEYS[1], next_value, 'EX', ARGV[2])
return next_value
"""

_GENERATION_BUMP_AND_ADVANCE_WATERMARK = """
-- cognis-event-cache-generation-bump-watermark-v1
local current = redis.call('GET', KEYS[1])
local next_generation = ARGV[1]
if current then
  local epoch, counter = string.match(current, '^([0-9a-f]+):([0-9]+)$')
  if epoch and string.len(epoch) == 32 and counter then
    next_generation = epoch .. ':' .. tostring(tonumber(counter) + 1)
  end
end
local next_watermark = tonumber(ARGV[3])
local current_watermark = tonumber(redis.call('GET', KEYS[2]))
if current_watermark and current_watermark > next_watermark then
  next_watermark = current_watermark
end
redis.call('SET', KEYS[1], next_generation, 'EX', ARGV[2])
redis.call('SET', KEYS[2], tostring(next_watermark), 'EX', ARGV[2])
return {next_generation, tostring(next_watermark)}
"""

_COMPARE_GENERATION_AND_SET = """
-- cognis-event-cache-compare-generation-and-set-v1
local current = redis.call('GET', KEYS[1])
if current then
  redis.call('EXPIRE', KEYS[1], ARGV[4])
end
if not current or current ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

_GENERATION_VALIDATED_GET = """
-- cognis-event-cache-generation-validated-get-v1
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
  return {0}
end
local value = redis.call('GET', KEYS[2])
if not value then
  return {2}
end
if ARGV[2] == '1' then
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  redis.call('EXPIRE', KEYS[2], ARGV[3])
end
return {1, value}
"""

_COMPARE_GENERATION_AND_TOUCH = """
-- cognis-event-cache-compare-generation-and-touch-v1
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
  return 0
end
if redis.call('EXISTS', KEYS[2]) == 0 then
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return 1
"""

_ADVANCE_WATERMARK_FLOOR = """
-- cognis-event-cache-watermark-floor-v1
local next_watermark = tonumber(ARGV[1])
if not next_watermark then
  return false
end
local current_watermark = tonumber(redis.call('GET', KEYS[1]))
if current_watermark and current_watermark > next_watermark then
  next_watermark = current_watermark
end
redis.call('SET', KEYS[1], tostring(next_watermark), 'EX', ARGV[2])
return tostring(next_watermark)
"""

_GENERATION_FENCED_GET = """
-- cognis-event-cache-generation-fenced-get-v1
local count = tonumber(ARGV[1])
for index = 1, count do
  local current = redis.call('GET', KEYS[index])
  if not current or current ~= ARGV[index + 1] then
    return {0}
  end
end
local value = redis.call('GET', KEYS[count + 1])
if not value then
  return {2}
end
if ARGV[count + 2] == '1' then
  for index = 1, count do
    redis.call('EXPIRE', KEYS[index], ARGV[count + 4])
  end
  redis.call('EXPIRE', KEYS[count + 1], ARGV[count + 3])
end
return {1, value}
"""

_GENERATION_FENCED_SET = """
-- cognis-event-cache-generation-fenced-set-v1
local count = tonumber(ARGV[1])
for index = 1, count do
  local current = redis.call('GET', KEYS[index])
  if not current or current ~= ARGV[index + 1] then
    return 0
  end
end
local guard_key_index = count + 2
local value_key_index = count + 1
local guard_value_index = count + 3
if #KEYS == guard_key_index then
  local guard = redis.call('GET', KEYS[guard_key_index])
  if not guard or guard ~= ARGV[guard_value_index] then
    return 0
  end
end
for index = 1, count do
  redis.call('EXPIRE', KEYS[index], ARGV[count + 5])
end
redis.call('SET', KEYS[value_key_index], ARGV[count + 2], 'EX', ARGV[count + 4])
return 1
"""


@dataclass(frozen=True, slots=True)
class EventCacheBounds:
    """Validated app-local cache and generation-registry bounds."""

    l1_max_entries: int = 1024
    l1_max_bytes: int = 16 * 1024 * 1024
    generation_max_sessions: int = 4096
    generation_max_bytes: int = 1024 * 1024
    generation_inactivity_seconds: float = 2 * _MAX_CACHE_TTL_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "l1_max_entries",
            "l1_max_bytes",
            "generation_max_sessions",
            "generation_max_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.generation_max_sessions > 4096:
            raise ValueError("generation_max_sessions must be <= 4096")
        if self.generation_max_bytes > 1024 * 1024:
            raise ValueError("generation_max_bytes must be <= 1 MiB")
        if self.generation_inactivity_seconds <= 0:
            raise ValueError("generation_inactivity_seconds must be positive")


@dataclass(frozen=True, slots=True)
class EventCachePolicy:
    """Validated immutable retention and Redis value policy."""

    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    sliding_expiration: bool = True
    compression_enabled: bool = True
    compression_threshold_bytes: int = 64 * 1024
    compression_level: int = 1
    redis_value_max_bytes: int = MAX_REDIS_VALUE_BYTES
    raw_value_max_bytes: int = MAX_RAW_VALUE_BYTES
    redis_page_values_enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "ttl_seconds",
            "compression_threshold_bytes",
            "compression_level",
            "redis_value_max_bytes",
            "raw_value_max_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
        for name in (
            "sliding_expiration",
            "compression_enabled",
            "redis_page_values_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if not 1 <= self.ttl_seconds <= _MAX_CACHE_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 1 second and 24 hours")
        if self.compression_threshold_bytes <= 0:
            raise ValueError("compression_threshold_bytes must be positive")
        if not 0 <= self.compression_level <= 9:
            raise ValueError("compression_level must be between 0 and 9")
        if not 1 <= self.redis_value_max_bytes <= MAX_REDIS_VALUE_BYTES:
            raise ValueError("redis_value_max_bytes must be between 1 byte and 2 MiB")
        if not self.redis_value_max_bytes <= self.raw_value_max_bytes <= MAX_RAW_VALUE_BYTES:
            raise ValueError("raw_value_max_bytes must be between Redis max and 16 MiB")
        if self.compression_enabled and self.compression_threshold_bytes > self.raw_value_max_bytes:
            raise ValueError("compression_threshold_bytes must not exceed raw_value_max_bytes")

    @property
    def refresh_after_seconds(self) -> float:
        return self.ttl_seconds / 2

    @property
    def generation_ttl_seconds(self) -> int:
        return max(_MIN_GENERATION_TTL_SECONDS, 2 * self.ttl_seconds)


@dataclass(frozen=True, slots=True)
class _Generation:
    epoch: str
    counter: int

    @property
    def encoded(self) -> str:
        return f"{self.epoch}:{self.counter}"


@dataclass(slots=True)
class _GenerationRecord:
    generation: _Generation
    last_access: float
    estimated_bytes: int


@dataclass(slots=True)
class _L1Entry:
    value: SessionEventPage | SessionWatermark
    payload: bytes
    expires_at: float
    refresh_after: float
    ttl_seconds: int
    session_token: str
    authority_digest: str


@dataclass(frozen=True, slots=True)
class AppendInvalidation:
    """Content-free append work safe to retain outside the provider callback."""

    session_token: str
    authority_token: str
    last_seq: int
    has_events: bool
    local_revision: int


@dataclass(frozen=True, slots=True)
class GenerationFenceEntry:
    """Opaque session generation used by a derived cache operation."""

    session_token: str
    generation: str
    backing_session_id: str
    watermark_floor: int


@dataclass(frozen=True, slots=True)
class GenerationFence:
    """Ordered multi-session generation fence for one derived value."""

    entries: tuple[GenerationFenceEntry, ...]


@dataclass(frozen=True, slots=True)
class GenerationFencedRead:
    """Result of an atomic generation-validated Redis read."""

    status: Literal["hit", "miss", "stale", "unavailable"]
    payload: bytes | None = None


@dataclass(frozen=True, slots=True)
class DerivedEnvelopeEncoding:
    payload: bytes
    raw_size: int


@dataclass(frozen=True, slots=True)
class DerivedEnvelopeDecoding:
    status: Literal["decoded", "invalid", "saturated"]
    value: dict[str, Any] | None = None
    raw_size: int = 0


def _canonical_parts(*parts: str) -> bytes:
    encoded = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        encoded.extend(len(raw).to_bytes(8, "big"))
        encoded.extend(raw)
    return bytes(encoded)


def _decode_generation(value: Any) -> _Generation | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    epoch, separator, counter_text = value.partition(":")
    if (
        separator != ":"
        or len(epoch) != 32
        or any(char not in "0123456789abcdef" for char in epoch)
        or not counter_text.isdigit()
    ):
        return None
    return _Generation(epoch=epoch, counter=int(counter_text))


class CachedSessionEventStore:
    """App-scoped factory for immutable authority-bound read views."""

    def __init__(
        self,
        delegate: SessionEventStore,
        redis_service: RedisService,
        hmac_secret: str | bytes,
        *,
        bounds: EventCacheBounds | None = None,
        policy: EventCachePolicy | None = None,
        clock: Callable[[], float] = monotonic,
        epoch_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        metrics: EventCacheMetrics = EVENT_CACHE_METRICS,
        enabled: bool = True,
    ) -> None:
        store_id = str(getattr(delegate, "store_id", "")).strip()
        if not store_id:
            raise ValueError("delegate must expose a fixed non-empty store_id")
        secret = hmac_secret.encode() if isinstance(hmac_secret, str) else bytes(hmac_secret)
        if not secret:
            raise ValueError("hmac_secret must not be empty")
        self.store_id = store_id
        self._delegate = delegate
        self._redis = redis_service
        self._secret = secret
        self._bounds = bounds or EventCacheBounds()
        self._policy = policy or EventCachePolicy()
        if self._bounds.generation_inactivity_seconds <= self._policy.ttl_seconds:
            raise ValueError("generation inactivity must be greater than cache TTL")
        self._clock = clock
        self._epoch_factory = epoch_factory
        self._metrics = metrics
        self._enabled = enabled
        self._closed = False
        self._l1: OrderedDict[str, _L1Entry] = OrderedDict()
        self._l1_bytes = 0
        self._session_index: dict[str, set[str]] = {}
        self._generations: OrderedDict[str, _GenerationRecord] = OrderedDict()
        self._generation_bytes = 0
        self._local_watermark_floors: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._redis_availability_epoch = redis_service.availability_epoch
        self._redis_outage_active = False
        self._redis_recovery_required = False
        self._redis_recovered_tokens: OrderedDict[str, None] = OrderedDict()
        self._invalidation_revisions: OrderedDict[str, int] = OrderedDict()
        self._pending_append_revisions: OrderedDict[str, int] = OrderedDict()
        self._append_overflow_until = 0.0
        self._inflight: dict[str, asyncio.Task[SessionEventPage | SessionWatermark]] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._append_tasks: set[asyncio.Task[Any]] = set()
        self._codec_slots = asyncio.Semaphore(_CODEC_WORKERS)
        self._codec_admission_slots = asyncio.Semaphore(_CODEC_CAPACITY)
        self._codec_tasks: set[asyncio.Task[Any]] = set()
        self._codec_waiters: set[asyncio.Task[Any]] = set()
        self._generation_invalidation_listeners: set[Callable[[str], None]] = set()
        self._metrics.resident(0, 0)

    @property
    def configured(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        return self._enabled and not self._closed

    def diagnostics(self) -> dict[str, int | bool]:
        """Return content-free app-local diagnostics."""

        return {
            "configured": self.configured,
            "available": self.available,
            "redis_configured": self._redis.configured,
            "redis_available": self._redis.available,
            "redis_page_values_enabled": self._policy.redis_page_values_enabled,
            "entries": len(self._l1),
            "bytes": self._l1_bytes,
            "inflight": len(self._inflight),
            "refresh_tasks": len(self._refresh_tasks),
            "codec_tasks": len(self._codec_tasks),
            "codec_waiting": len(self._codec_waiters),
            "codec_capacity": _CODEC_CAPACITY,
            "generation_sessions": len(self._generations),
            "generation_bytes": self._generation_bytes,
            "pending_appends": len(self._pending_append_revisions),
            "append_overflow_fallback": self._append_overflow_until > self._clock(),
        }

    def bind(self, authority: EventStoreAuthority) -> BoundSessionEventStore:
        if not isinstance(authority, EventStoreAuthority):
            raise TypeError("authority must be an EventStoreAuthority")
        return BoundSessionEventStore(self, authority)

    async def read_session_events(self, **_: Any) -> SessionEventPage:
        raise RuntimeError("CachedSessionEventStore must be bound to an authority before reads")

    async def read_session_high_watermark(self, **_: Any) -> SessionWatermark:
        raise RuntimeError("CachedSessionEventStore must be bound to an authority before reads")

    def session_token(self, store_id: str, backing_session_id: str) -> str:
        if store_id != self.store_id:
            raise ValueError("store_id does not match the configured delegate")
        if not backing_session_id:
            raise ValueError("backing_session_id must not be empty")
        return self._digest("session", store_id, backing_session_id)

    def derived_key_digest(self, domain: str, *parts: str) -> str:
        """Return an HMAC digest in the existing cache ownership namespace."""

        if not domain or any(not isinstance(part, str) for part in parts):
            raise ValueError("derived cache key parts must be strings")
        return self._digest(f"derived:{domain}", *parts)

    def add_generation_invalidation_listener(self, listener: Callable[[str], None]) -> None:
        """Register a synchronous content-free listener for local generation invalidation."""

        self._generation_invalidation_listeners.add(listener)

    def remove_generation_invalidation_listener(self, listener: Callable[[str], None]) -> None:
        self._generation_invalidation_listeners.discard(listener)

    async def create_generation_fence(
        self,
        sessions: Sequence[tuple[str, str] | tuple[str, str, str | None]],
    ) -> GenerationFence | None:
        """Capture an ordered Redis generation fence for derived cache operations."""

        if self._append_overflow_until > self._clock():
            return None
        entries: list[GenerationFenceEntry] = []
        for session in sessions:
            store_id, backing_session_id = session[:2]
            authority_token = session[2] if len(session) == 3 else None
            token = self.session_token(store_id, backing_session_id)
            if token in self._pending_append_revisions:
                return None
            generation = await self._generation(token)
            if generation is None or token in self._pending_append_revisions:
                return None
            floor = 0
            if authority_token is not None:
                floor = self._local_watermark_floors.get((token, authority_token), 0)
                raw_floor = await self._redis.get(
                    self._append_watermark_key(token, authority_token)
                )
                if raw_floor is None and not self._redis.available:
                    return None
                if raw_floor is not None:
                    try:
                        floor = max(floor, int(raw_floor))
                    except (TypeError, ValueError):
                        return None
            entries.append(
                GenerationFenceEntry(
                    token,
                    generation.encoded,
                    backing_session_id,
                    floor,
                )
            )
        return GenerationFence(tuple(entries))

    async def generation_fenced_read(
        self,
        key: str,
        fence: GenerationFence,
        *,
        ttl_seconds: int,
        sliding: bool,
    ) -> GenerationFencedRead:
        """Atomically validate all generations and read/touch one derived Redis value."""

        if (
            not self._redis.configured
            or not self._redis.available
            or self._append_overflow_until > self._clock()
            or any(entry.session_token in self._pending_append_revisions for entry in fence.entries)
        ):
            return GenerationFencedRead("unavailable")
        result = await self._redis.eval(
            _GENERATION_FENCED_GET,
            keys=[
                *[self._generation_key(entry.session_token) for entry in fence.entries],
                key,
            ],
            args=[
                len(fence.entries),
                *[entry.generation for entry in fence.entries],
                1 if sliding else 0,
                ttl_seconds,
                self._policy.generation_ttl_seconds,
            ],
        )
        if result is None or not isinstance(result, (list, tuple)) or not result:
            return GenerationFencedRead("unavailable")
        if any(entry.session_token in self._pending_append_revisions for entry in fence.entries):
            return GenerationFencedRead("stale")
        if result[0] == 0:
            return GenerationFencedRead("stale")
        if result[0] == 2:
            return GenerationFencedRead("miss")
        payload = result[1] if result[0] == 1 and len(result) == 2 else None
        if not isinstance(payload, bytes):
            return GenerationFencedRead("unavailable")
        return GenerationFencedRead("hit", payload)

    async def generation_fenced_write(
        self,
        key: str,
        payload: bytes,
        fence: GenerationFence,
        *,
        ttl_seconds: int,
        guard_key: str | None = None,
        guard_value: bytes | None = None,
    ) -> bool | None:
        """Compare all generations and optionally a token guard before one Redis write."""

        if (
            not self._redis.configured
            or not self._redis.available
            or self._append_overflow_until > self._clock()
            or any(entry.session_token in self._pending_append_revisions for entry in fence.entries)
        ):
            return None
        if (guard_key is None) != (guard_value is None):
            raise ValueError("guard_key and guard_value must be provided together")
        keys: list[str] = [
            *[self._generation_key(entry.session_token) for entry in fence.entries],
            key,
        ]
        args: list[str | bytes | int] = [
            len(fence.entries),
            *[entry.generation for entry in fence.entries],
            payload,
            guard_value or b"",
            ttl_seconds,
            self._policy.generation_ttl_seconds,
        ]
        if guard_key is not None:
            keys.append(guard_key)
        result = await self._redis.eval(_GENERATION_FENCED_SET, keys=keys, args=args)
        if result is None:
            return None
        return isinstance(result, int) and not isinstance(result, bool) and result == 1

    async def encode_derived_envelope(
        self, envelope: dict[str, Any]
    ) -> DerivedEnvelopeEncoding | None:
        """Encode a derived value with the bounded event-cache codec workers and policy."""

        try:
            return await self._run_codec(lambda: self._encode_derived_envelope_sync(envelope))
        except _CodecSaturated:
            self._metrics.bypass("codec_saturated")
            return None
        except Exception:
            self._metrics.compression("error")
            return None

    async def decode_derived_envelope(self, payload: bytes) -> DerivedEnvelopeDecoding:
        """Decode a derived value with bounded decompression and strict JSON parsing."""

        try:
            return await self._run_codec(lambda: self._decode_derived_envelope_sync(payload))
        except _CodecSaturated:
            self._metrics.bypass("codec_saturated")
            return DerivedEnvelopeDecoding("saturated")
        except Exception:
            return DerivedEnvelopeDecoding("invalid")

    async def handle_append(self, notification: EventAppendNotification) -> bool:
        """Invalidate immediately, then best-effort bump and seed the watermark."""

        task = asyncio.current_task()
        if task is None:
            return await self._handle_append(notification)
        self._append_tasks.add(task)
        try:
            return await self._handle_append(notification)
        finally:
            self._append_tasks.discard(task)

    async def _handle_append(self, notification: EventAppendNotification) -> bool:
        work = self.invalidate_append_local(notification)
        try:
            generation, last_seq = await self._advance_append_shared(work)
        except Exception:
            self._append_shared_failed(work)
            self.abandon_append_invalidation(work)
            return False
        if generation is None:
            self.abandon_append_invalidation(work)
            return False
        self._complete_append_invalidation(work)
        if self._redis.configured and not self._redis.available:
            return False
        if not work.has_events:
            return True
        watermark = SessionWatermark(
            store_id=self.store_id,
            session_id=notification.session_id,
            last_seq=last_seq,
        )
        query_digest = self._query_digest("watermark")
        key = self._cache_key(
            authority_digest=work.authority_token,
            session_token=work.session_token,
            generation=generation,
            query_digest=query_digest,
        )
        try:
            payload = await self._serialize(
                "watermark",
                notification.session_id,
                generation,
                query_digest,
                watermark,
            )
        except _CodecSaturated:
            self._metrics.bypass("codec_saturated")
            return True
        if self._closed:
            return False
        if payload is None:
            return True
        self._l1_put(
            key,
            work.session_token,
            work.authority_token,
            watermark,
            payload,
            ttl_seconds=self._policy.ttl_seconds,
        )
        try:
            wrote_redis = await self._redis_compare_set(
                work.session_token,
                generation,
                key,
                payload,
                ttl_seconds=self._policy.ttl_seconds,
            )
        except Exception:
            if self._redis.configured:
                self._mark_redis_outage(work.session_token)
            return False
        if self._redis.configured and not wrote_redis:
            if not self._redis.available:
                self._mark_redis_outage(work.session_token)
            return False
        return True

    def invalidate_append_local(self, notification: EventAppendNotification) -> AppendInvalidation:
        """Synchronously invalidate L1 and return opaque shared work."""

        token = self.session_token(self.store_id, notification.session_id)
        authority_token = self._authority_digest(notification.authority)
        last_seq = notification.last_seq
        for key in self._session_index.get(token, ()):
            entry = self._l1.get(key)
            if (
                entry is not None
                and entry.authority_digest == authority_token
                and isinstance(entry.value, SessionWatermark)
            ):
                last_seq = max(last_seq, entry.value.last_seq)
        if notification.event_count:
            last_seq = self._advance_local_watermark_floor(
                token,
                authority_token,
                last_seq,
            )
        self._invalidate_l1_token(token, "append")
        self._notify_generation_invalidation(token)
        revision = self._advance_invalidation_revision(token)
        self._pending_append_revisions.pop(token, None)
        self._pending_append_revisions[token] = revision
        while len(self._pending_append_revisions) > self._bounds.generation_max_sessions:
            self._pending_append_revisions.popitem(last=False)
            self.activate_append_overflow_fallback()
        return AppendInvalidation(
            session_token=token,
            authority_token=authority_token,
            last_seq=last_seq,
            has_events=notification.event_count > 0,
            local_revision=revision,
        )

    async def process_append_invalidation(self, work: AppendInvalidation) -> bool:
        """Best-effort shared generation/watermark work for the dispatcher."""

        try:
            generation, _ = await self._advance_append_shared(work)
        except Exception:
            self._append_shared_failed(work)
            return False
        if generation is None:
            return False
        self._complete_append_invalidation(work)
        return not self._redis.configured or self._redis.available

    def abandon_append_invalidation(self, work: AppendInvalidation) -> None:
        """Fall back to TTL-bounded direct reads after bounded work is dropped."""

        pending = self._pending_append_revisions.get(work.session_token)
        if pending is not None and pending <= work.local_revision:
            self._pending_append_revisions.pop(work.session_token, None)
        self.activate_append_overflow_fallback()

    def activate_append_overflow_fallback(self) -> None:
        """Bypass shared cache until every derived entry has expired."""

        self._append_overflow_until = max(
            self._append_overflow_until,
            self._clock() + self._policy.ttl_seconds,
        )
        for key in tuple(self._l1):
            self._l1_remove(key)

    async def invalidate_session(
        self,
        store_id: str,
        backing_session_id: str,
        *,
        source: InvalidationSource = "cluster_signal",
    ) -> bool:
        token = self.session_token(store_id, backing_session_id)
        return await self.invalidate_session_token(token, source=source)

    async def invalidate_session_token(
        self,
        session_token: str,
        *,
        source: InvalidationSource = "cluster_signal",
    ) -> bool:
        self._invalidate_l1_token(session_token, source)
        self._notify_generation_invalidation(session_token)
        self._advance_invalidation_revision(session_token)
        generation = await self._bump_generation(session_token)
        return generation is not None and (not self._redis.configured or self._redis.available)

    async def aclose(self) -> None:
        """Cancel owned fill tasks without closing the shared Redis service."""

        if self._closed:
            return
        self._closed = True
        current_task = asyncio.current_task()
        tasks = list(
            {
                *self._inflight.values(),
                *self._refresh_tasks.values(),
                *self._codec_waiters,
            }
            - ({current_task} if current_task is not None else set())
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        codec_tasks = tuple(self._codec_tasks)
        if codec_tasks:
            await asyncio.gather(*codec_tasks, return_exceptions=True)
        append_tasks = tuple(
            task for task in self._append_tasks if task is not asyncio.current_task()
        )
        if append_tasks:
            await asyncio.gather(*append_tasks, return_exceptions=True)
        self._inflight.clear()
        self._refresh_tasks.clear()
        self._append_tasks.clear()
        self._codec_tasks.clear()
        self._codec_waiters.clear()
        self._l1.clear()
        self._session_index.clear()
        self._l1_bytes = 0
        self._generations.clear()
        self._generation_bytes = 0
        self._local_watermark_floors.clear()
        self._redis_recovered_tokens.clear()
        self._invalidation_revisions.clear()
        self._pending_append_revisions.clear()
        self._generation_invalidation_listeners.clear()
        self._metrics.resident(0, 0)

    async def _read(
        self,
        *,
        authority: EventStoreAuthority,
        operation: CacheOperation,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage | SessionWatermark:
        self._validate_read_request(
            operation=operation,
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        if operation == "page":
            self._metrics.page_query(
                self._page_query_class(
                    after_seq=after_seq,
                    before_seq=before_seq,
                    direction=direction,
                )
            )
        token = self.session_token(self.store_id, session_id)
        if not self.available:
            self._metrics.bypass("cache_disabled")
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        if token in self._pending_append_revisions:
            self._metrics.bypass("generation_changed")
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        if self._append_overflow_until > self._clock():
            self._metrics.bypass("generation_changed")
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        self._observe_redis_availability()
        if self._redis.configured and not self._redis.available:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        invalidation_revision = self._invalidation_revisions.get(token, 0)
        recovery_required = (
            self._redis.configured
            and self._redis_recovery_required
            and token not in self._redis_recovered_tokens
        )
        authority_digest = self._authority_digest(authority)
        query_digest = self._query_digest(
            operation,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        local_record = None if recovery_required else self._generations.get(token)
        local_key: str | None = None
        if local_record is not None:
            local_generation = local_record.generation
            local_key = self._cache_key(
                authority_digest=authority_digest,
                session_token=token,
                generation=local_generation,
                query_digest=query_digest,
            )
            cached = self._l1_get(
                local_key,
                token,
                generation=local_generation,
                invalidation_revision=invalidation_revision,
            )
            if cached is not None:
                self._remember_generation(token, local_generation)
                self._metrics.hit("local", operation)
                return cached
        if recovery_required:
            self._redis_outage_active = False
            generation = await self._bump_generation(token)
            if generation is not None:
                self._remember_redis_recovered_token(token)
        else:
            generation = await self._generation(token)
        if generation is None:
            self._metrics.bypass("redis_unavailable")
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        key = self._cache_key(
            authority_digest=authority_digest,
            session_token=token,
            generation=generation,
            query_digest=query_digest,
        )
        if self._redis.configured and not self._redis.available:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        if key != local_key:
            cached = self._l1_get(
                key,
                token,
                generation=generation,
                invalidation_revision=invalidation_revision,
            )
            if cached is not None:
                self._metrics.hit("local", operation)
                return cached
        self._metrics.miss("local", operation)
        if self._redis.configured and not self._redis.available:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return await self._read_uncached(
                authority=authority,
                operation=operation,
                token=token,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        existing = self._inflight.get(key)
        if existing is not None:
            self._metrics.singleflight_join(operation)
            return await asyncio.shield(existing)

        async def fill() -> SessionEventPage | SessionWatermark:
            try:
                return await self._fill(
                    key=key,
                    token=token,
                    authority_digest=authority_digest,
                    generation=generation,
                    invalidation_revision=invalidation_revision,
                    query_digest=query_digest,
                    authority=authority,
                    operation=operation,
                    session_id=session_id,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
            finally:
                self._inflight.pop(key, None)

        task = asyncio.create_task(fill())
        task.add_done_callback(self._consume_fill_result)
        self._inflight[key] = task
        return await asyncio.shield(task)

    @staticmethod
    def _consume_fill_result(task: asyncio.Task[Any]) -> None:
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _fill(
        self,
        *,
        key: str,
        token: str,
        authority_digest: str,
        generation: _Generation,
        invalidation_revision: int,
        query_digest: str,
        authority: EventStoreAuthority,
        operation: CacheOperation,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage | SessionWatermark:
        if operation == "page" and not self._policy.redis_page_values_enabled:
            self._metrics.bypass("redis_value_disabled")
            return await self._fill_page_l1_only(
                key=key,
                token=token,
                authority_digest=authority_digest,
                generation=generation,
                invalidation_revision=invalidation_revision,
                authority=authority,
                session_id=session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                direction=direction,
            )
        redis_status: Literal["hit", "miss", "generation_changed", "error"] = "miss"
        redis_payload: bytes | None = None
        if self._redis.configured:
            redis_payload, redis_status = await self._redis_generation_validated_get(
                token,
                generation,
                key,
                ttl_seconds=self._policy.ttl_seconds,
            )
        if redis_payload is None:
            self._metrics.miss("redis", operation)
            if redis_status == "generation_changed":
                self._metrics.bypass("generation_changed")
                return await self._read_uncached(
                    authority=authority,
                    operation=operation,
                    token=token,
                    session_id=session_id,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
            if redis_status == "error" or (self._redis.configured and not self._redis.available):
                self._metrics.bypass("redis_unavailable")
                self._metrics.error("redis", operation)
                self._mark_redis_outage(token)
                return await self._read_uncached(
                    authority=authority,
                    operation=operation,
                    token=token,
                    session_id=session_id,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
        else:
            try:
                cached = await self._deserialize(
                    redis_payload,
                    operation=operation,
                    session_id=session_id,
                    generation=generation,
                    query_digest=query_digest,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
            except _CodecSaturated:
                self._metrics.bypass("codec_saturated")
            else:
                if cached is not None:
                    generation_now = await self._generation(token)
                    if (
                        generation_now is not None
                        and generation_now == generation
                        and self._invalidation_revisions.get(token, 0) == invalidation_revision
                    ):
                        self._metrics.hit("redis", operation)
                        ttl = self._ttl(cached, operation, before_seq, direction)
                        self._l1_put(
                            key,
                            token,
                            authority_digest,
                            cached,
                            redis_payload,
                            ttl_seconds=ttl,
                        )
                        return cached
                    self._metrics.bypass("generation_changed")
                self._metrics.error("redis", operation)
                with suppress(Exception):
                    await self._redis.delete(key)
                if self._redis.configured and not self._redis.available:
                    self._metrics.bypass("redis_unavailable")
                    self._mark_redis_outage(token)
                    return await self._read_uncached(
                        authority=authority,
                        operation=operation,
                        token=token,
                        session_id=session_id,
                        after_seq=after_seq,
                        before_seq=before_seq,
                        limit=limit,
                        direction=direction,
                    )

        self._metrics.miss("upstream", operation)
        value = await self._read_upstream(
            authority=authority,
            operation=operation,
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        if not self._validate_value(
            value,
            operation=operation,
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        ):
            self._metrics.error("upstream", operation)
            return value
        if isinstance(value, SessionEventPage) and not value.events and not value.verified_empty:
            self._metrics.bypass("unverified_empty")
            return value
        if isinstance(value, SessionWatermark):
            value, floor_available = await self._apply_watermark_floor(
                token,
                authority_digest,
                value,
            )
            if not floor_available:
                self._metrics.bypass("redis_unavailable")
                self._mark_redis_outage(token)
                return value
        try:
            payload = await self._serialize(
                operation,
                session_id,
                generation,
                query_digest,
                value,
            )
        except _CodecSaturated:
            self._metrics.bypass("codec_saturated")
            return value
        if payload is None:
            self._metrics.bypass("oversized")
            return value
        generation_after = await self._generation(token)
        if generation_after is None and self._redis.configured:
            return value
        if (
            generation_after != generation
            or self._invalidation_revisions.get(token, 0) != invalidation_revision
        ):
            self._metrics.bypass("generation_changed")
            return value
        ttl = self._ttl(value, operation, before_seq, direction)
        wrote_redis = await self._redis_compare_set(
            token, generation, key, payload, ttl_seconds=ttl
        )
        if self._redis.configured and not wrote_redis and not self._redis.available:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return value
        if self._redis.configured and not wrote_redis:
            generation_now = await self._generation(token)
            if generation_now != generation:
                self._metrics.bypass("generation_changed")
                return value
        if self._invalidation_revisions.get(token, 0) != invalidation_revision:
            self._metrics.bypass("generation_changed")
            return value
        self._l1_put(
            key,
            token,
            authority_digest,
            value,
            payload,
            ttl_seconds=ttl,
        )
        return value

    async def _fill_page_l1_only(
        self,
        *,
        key: str,
        token: str,
        authority_digest: str,
        generation: _Generation,
        invalidation_revision: int,
        authority: EventStoreAuthority,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage:
        """Fill one L1 page without reading or writing a Redis page value."""

        self._metrics.miss("upstream", "page")
        value = await self._read_upstream(
            authority=authority,
            operation="page",
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        if not isinstance(value, SessionEventPage):
            self._metrics.error("upstream", "page")
            raise TypeError("page read returned a non-page value")
        if not self._validate_value(
            value,
            operation="page",
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        ):
            self._metrics.error("upstream", "page")
            return value
        if not value.events and not value.verified_empty:
            self._metrics.bypass("unverified_empty")
            return value
        try:
            payload = await self._run_codec(lambda: value.model_dump_json().encode("utf-8"))
        except _CodecSaturated:
            self._metrics.bypass("codec_saturated")
            return value
        except (RecursionError, TypeError, ValueError):
            return value
        if len(payload) > self._bounds.l1_max_bytes:
            self._metrics.bypass("oversized")
            return value
        generation_after = await self._generation(token)
        if (
            generation_after != generation
            or self._invalidation_revisions.get(token, 0) != invalidation_revision
        ):
            self._metrics.bypass("generation_changed")
            return value
        self._l1_put(
            key,
            token,
            authority_digest,
            value,
            payload,
            ttl_seconds=self._ttl(value, "page", before_seq, direction),
        )
        return value

    async def _read_upstream(
        self,
        *,
        authority: EventStoreAuthority,
        operation: CacheOperation,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage | SessionWatermark:
        self._metrics.upstream_read(operation)
        started = perf_counter()
        try:
            with scoped_runtime_context(
                user_email=authority.user_email,
                agent_id=authority.agent_id,
                agent_owner_email=authority.agent_owner_email,
            ):
                if operation == "watermark":
                    return await self._delegate.read_session_high_watermark(session_id=session_id)
                return await self._delegate.read_session_events(
                    session_id=session_id,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
        except BaseException:
            self._metrics.error("upstream", operation)
            raise
        finally:
            self._metrics.observe_upstream(operation, perf_counter() - started)

    async def _read_uncached(
        self,
        *,
        authority: EventStoreAuthority,
        operation: CacheOperation,
        token: str,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage | SessionWatermark:
        value = await self._read_upstream(
            authority=authority,
            operation=operation,
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        if isinstance(value, SessionWatermark):
            last_seq = self._advance_local_watermark_floor(
                token,
                self._authority_digest(authority),
                value.last_seq,
            )
            return value.model_copy(update={"last_seq": last_seq})
        return value

    async def _generation(self, token: str) -> _Generation | None:
        self._prune_generation_registry()
        if self._redis.configured:
            supplied = _Generation(self._new_epoch(), 0)
            result = await self._redis.eval(
                _GENERATION_GET_OR_INIT,
                keys=[self._generation_key(token)],
                args=[supplied.encoded, self._policy.generation_ttl_seconds],
            )
            raw = result[1] if isinstance(result, (list, tuple)) and len(result) == 2 else None
            generation = _decode_generation(raw)
            if generation is None:
                self._metrics.bypass("redis_unavailable")
                self._mark_redis_outage(token)
                return None
        else:
            record = self._generations.get(token)
            generation = (
                record.generation if record is not None else _Generation(self._new_epoch(), 0)
            )
        self._remember_generation(token, generation)
        return generation

    async def _bump_generation(self, token: str) -> _Generation | None:
        self._prune_generation_registry()
        generation: _Generation | None = None
        if self._redis.configured:
            supplied = _Generation(self._new_epoch(), 0)
            result = await self._redis.eval(
                _GENERATION_BUMP,
                keys=[self._generation_key(token)],
                args=[supplied.encoded, self._policy.generation_ttl_seconds],
            )
            generation = _decode_generation(result)
            if generation is None:
                self._metrics.bypass("redis_unavailable")
                self._mark_redis_outage(token)
                return None
        else:
            record = self._generations.get(token)
            generation = (
                _Generation(record.generation.epoch, record.generation.counter + 1)
                if record is not None
                else _Generation(self._new_epoch(), 0)
            )
        self._remember_generation(token, generation)
        return generation

    async def _bump_generation_and_watermark(
        self,
        token: str,
        authority_digest: str,
        last_seq: int,
    ) -> tuple[_Generation | None, int]:
        local_last_seq = self._advance_local_watermark_floor(
            token,
            authority_digest,
            last_seq,
        )
        if not self._redis.configured:
            generation = await self._bump_generation(token)
            return generation, local_last_seq
        supplied = _Generation(self._new_epoch(), 0)
        result = await self._redis.eval(
            _GENERATION_BUMP_AND_ADVANCE_WATERMARK,
            keys=[
                self._generation_key(token),
                self._append_watermark_key(token, authority_digest),
            ],
            args=[supplied.encoded, self._policy.generation_ttl_seconds, local_last_seq],
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return None, local_last_seq
        generation = _decode_generation(result[0])
        try:
            advanced_last_seq = int(result[1])
        except (TypeError, ValueError):
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return None, local_last_seq
        if generation is None or advanced_last_seq < local_last_seq:
            self._metrics.bypass("redis_unavailable")
            self._mark_redis_outage(token)
            return None, local_last_seq
        self._remember_generation(token, generation)
        return generation, self._advance_local_watermark_floor(
            token,
            authority_digest,
            advanced_last_seq,
        )

    async def _advance_append_shared(
        self, work: AppendInvalidation
    ) -> tuple[_Generation | None, int]:
        if not self.available:
            return None, work.last_seq
        if not work.has_events:
            return await self._bump_generation(work.session_token), work.last_seq
        return await self._bump_generation_and_watermark(
            work.session_token,
            work.authority_token,
            work.last_seq,
        )

    def _append_shared_failed(self, work: AppendInvalidation) -> None:
        if self._redis.configured:
            self._mark_redis_outage(work.session_token)

    def _complete_append_invalidation(self, work: AppendInvalidation) -> None:
        pending = self._pending_append_revisions.get(work.session_token)
        if pending is not None and pending <= work.local_revision:
            self._pending_append_revisions.pop(work.session_token, None)

    async def _apply_watermark_floor(
        self,
        token: str,
        authority_digest: str,
        watermark: SessionWatermark,
    ) -> tuple[SessionWatermark, bool]:
        local_last_seq = self._advance_local_watermark_floor(
            token,
            authority_digest,
            watermark.last_seq,
        )
        if not self._redis.configured:
            return watermark.model_copy(update={"last_seq": local_last_seq}), True
        result = await self._redis.eval(
            _ADVANCE_WATERMARK_FLOOR,
            keys=[self._append_watermark_key(token, authority_digest)],
            args=[local_last_seq, self._policy.generation_ttl_seconds],
        )
        if result is None or isinstance(result, bool):
            return watermark.model_copy(update={"last_seq": local_last_seq}), False
        try:
            last_seq = int(result)
        except (TypeError, ValueError):
            return watermark.model_copy(update={"last_seq": local_last_seq}), False
        if last_seq < local_last_seq:
            return watermark.model_copy(update={"last_seq": local_last_seq}), False
        last_seq = self._advance_local_watermark_floor(
            token,
            authority_digest,
            last_seq,
        )
        return watermark.model_copy(update={"last_seq": last_seq}), True

    def _advance_local_watermark_floor(
        self,
        token: str,
        authority_digest: str,
        last_seq: int,
    ) -> int:
        key = (token, authority_digest)
        current = self._local_watermark_floors.pop(key, None)
        advanced = max(last_seq, current or 0)
        self._local_watermark_floors[key] = advanced
        while len(self._local_watermark_floors) > self._bounds.generation_max_sessions:
            self._local_watermark_floors.popitem(last=False)
        return advanced

    async def _redis_compare_set(
        self,
        token: str,
        generation: _Generation,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int,
    ) -> bool:
        if not self._redis.configured:
            return False
        result = await self._redis.eval(
            _COMPARE_GENERATION_AND_SET,
            keys=[self._generation_key(token), key],
            args=[
                generation.encoded,
                payload,
                min(ttl_seconds, self._policy.ttl_seconds),
                self._policy.generation_ttl_seconds,
            ],
        )
        return isinstance(result, int) and not isinstance(result, bool) and result == 1

    async def _redis_generation_validated_get(
        self,
        token: str,
        generation: _Generation,
        key: str,
        *,
        ttl_seconds: int,
    ) -> tuple[bytes | None, Literal["hit", "miss", "generation_changed", "error"]]:
        try:
            result = await self._redis.eval(
                _GENERATION_VALIDATED_GET,
                keys=[self._generation_key(token), key],
                args=[
                    generation.encoded,
                    "1" if self._policy.sliding_expiration else "0",
                    ttl_seconds,
                    self._policy.generation_ttl_seconds,
                ],
            )
        except Exception:
            return None, "error"
        if not isinstance(result, (list, tuple)) or not result:
            return None, "error"
        status = result[0]
        if status == 0:
            return None, "generation_changed"
        if status == 2:
            return None, "miss"
        if status != 1 or len(result) != 2:
            return None, "error"
        payload = result[1]
        if isinstance(payload, str):
            payload = payload.encode()
        if not isinstance(payload, bytes):
            return None, "error"
        if self._policy.sliding_expiration:
            self._metrics.sliding_refresh("redis")
        return payload, "hit"

    def _remember_generation(self, token: str, generation: _Generation) -> None:
        current = self._generations.pop(token, None)
        if current is not None:
            self._generation_bytes -= current.estimated_bytes
            if current.generation != generation:
                self._invalidate_l1_token(token, "generation_change")
        estimated = len(token) + len(generation.encoded) + 32
        self._generations[token] = _GenerationRecord(
            generation=generation,
            last_access=self._clock(),
            estimated_bytes=estimated,
        )
        self._generation_bytes += estimated
        self._prune_generation_registry()

    def _prune_generation_registry(self) -> None:
        now = self._clock()
        while self._generations:
            token, record = next(iter(self._generations.items()))
            expired = now - record.last_access > self._bounds.generation_inactivity_seconds
            over_bounds = (
                len(self._generations) > self._bounds.generation_max_sessions
                or self._generation_bytes > self._bounds.generation_max_bytes
            )
            if not expired and not over_bounds:
                break
            self._generations.pop(token)
            self._generation_bytes -= record.estimated_bytes
            self._drop_local_watermark_floors(token)
            self._redis_recovered_tokens.pop(token, None)
            self._invalidation_revisions.pop(token, None)
            self._invalidate_l1_token(token, "local_eviction")

    def _drop_local_watermark_floors(self, token: str) -> None:
        for key in tuple(self._local_watermark_floors):
            if key[0] == token:
                self._local_watermark_floors.pop(key, None)

    def _mark_redis_outage(self, token: str) -> None:
        if not self._redis_outage_active:
            self._redis_outage_active = True
            self._redis_recovery_required = True
            self._redis_recovered_tokens.clear()
        self._invalidate_l1_token(token, "generation_change")
        self._advance_invalidation_revision(token)

    def _observe_redis_availability(self) -> None:
        if not self._redis.configured:
            return
        availability_epoch = self._redis.availability_epoch
        if availability_epoch == self._redis_availability_epoch:
            return
        self._redis_availability_epoch = availability_epoch
        self._redis_outage_active = not self._redis.available
        self._redis_recovery_required = True
        self._redis_recovered_tokens.clear()
        tokens = set(self._generations) | set(self._session_index)
        for token in tokens:
            self._invalidate_l1_token(token, "generation_change")
            self._advance_invalidation_revision(token)

    def _remember_redis_recovered_token(self, token: str) -> None:
        self._redis_recovered_tokens.pop(token, None)
        self._redis_recovered_tokens[token] = None
        while len(self._redis_recovered_tokens) > self._bounds.generation_max_sessions:
            self._redis_recovered_tokens.popitem(last=False)

    def _advance_invalidation_revision(self, token: str) -> int:
        revision = self._invalidation_revisions.pop(token, 0) + 1
        self._invalidation_revisions[token] = revision
        while len(self._invalidation_revisions) > self._bounds.generation_max_sessions:
            self._invalidation_revisions.popitem(last=False)
        return revision

    def _l1_get(
        self,
        key: str,
        token: str,
        *,
        generation: _Generation,
        invalidation_revision: int,
    ) -> SessionEventPage | SessionWatermark | None:
        entry = self._l1.get(key)
        if entry is None:
            return None
        now = self._clock()
        if entry.session_token != token or entry.expires_at <= now:
            self._l1_remove(key)
            return None
        self._l1.move_to_end(key)
        if self._policy.sliding_expiration and entry.refresh_after <= now:
            original_expires_at = entry.expires_at
            local_only_page = (
                isinstance(entry.value, SessionEventPage)
                and not self._policy.redis_page_values_enabled
            )
            refresh_started = (
                (
                    self._schedule_redis_generation_check(
                        key,
                        token,
                        generation,
                        invalidation_revision=invalidation_revision,
                        original_expires_at=original_expires_at,
                        expected_entry=entry,
                    )
                    if local_only_page
                    else False
                )
                or not self._redis.configured
                or self._schedule_redis_touch(
                    key,
                    token,
                    generation,
                    invalidation_revision=invalidation_revision,
                    ttl_seconds=entry.ttl_seconds,
                    original_expires_at=original_expires_at,
                    expected_entry=entry,
                )
            )
            if refresh_started:
                entry.expires_at = now + entry.ttl_seconds
                entry.refresh_after = now + (entry.ttl_seconds / 2)
                self._metrics.sliding_refresh("local")
        return entry.value

    def _l1_put(
        self,
        key: str,
        token: str,
        authority_digest: str,
        value: SessionEventPage | SessionWatermark,
        payload: bytes,
        *,
        ttl_seconds: int,
    ) -> None:
        if len(payload) > self._bounds.l1_max_bytes:
            return
        self._l1_remove(key)
        now = self._clock()
        self._l1[key] = _L1Entry(
            value=value,
            payload=payload,
            expires_at=now + ttl_seconds,
            refresh_after=now + (ttl_seconds / 2),
            ttl_seconds=ttl_seconds,
            session_token=token,
            authority_digest=authority_digest,
        )
        self._l1_bytes += len(payload)
        self._session_index.setdefault(token, set()).add(key)
        while (
            len(self._l1) > self._bounds.l1_max_entries
            or self._l1_bytes > self._bounds.l1_max_bytes
        ):
            oldest = next(iter(self._l1))
            self._l1_remove(oldest)
        self._metrics.resident(len(self._l1), self._l1_bytes)

    def _l1_remove(self, key: str) -> None:
        entry = self._l1.pop(key, None)
        if entry is None:
            return
        self._l1_bytes -= len(entry.payload)
        keys = self._session_index.get(entry.session_token)
        if keys is not None:
            keys.discard(key)
            if not keys:
                self._session_index.pop(entry.session_token, None)
        self._metrics.resident(len(self._l1), self._l1_bytes)

    def _invalidate_l1_token(self, token: str, source: InvalidationSource) -> None:
        self._metrics.invalidation(source)
        for key in tuple(self._session_index.get(token, ())):
            task = self._refresh_tasks.get(key)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            self._l1_remove(key)

    def _notify_generation_invalidation(self, token: str) -> None:
        for listener in tuple(self._generation_invalidation_listeners):
            try:
                listener(token)
            except Exception:
                continue

    def _schedule_redis_touch(
        self,
        key: str,
        token: str,
        generation: _Generation,
        *,
        invalidation_revision: int,
        ttl_seconds: int,
        original_expires_at: float,
        expected_entry: _L1Entry,
    ) -> bool:
        if key in self._refresh_tasks:
            return True
        if (
            not self._redis.configured
            or not self._redis.available
            or self._closed
            or len(self._refresh_tasks) >= _MAX_REFRESH_TASKS
        ):
            return False

        async def touch() -> None:
            try:
                result = await self._redis.eval(
                    _COMPARE_GENERATION_AND_TOUCH,
                    keys=[self._generation_key(token), key],
                    args=[
                        generation.encoded,
                        ttl_seconds,
                        self._policy.generation_ttl_seconds,
                    ],
                )
                if (
                    result == 1
                    and self._invalidation_revisions.get(token, 0) == invalidation_revision
                ):
                    self._metrics.sliding_refresh("redis")
                elif (
                    result == 0
                    and self._invalidation_revisions.get(token, 0) == invalidation_revision
                ):
                    self._invalidate_l1_token(token, "generation_change")
                    self._advance_invalidation_revision(token)
                elif self._invalidation_revisions.get(token, 0) == invalidation_revision:
                    self._restore_l1_expiry(
                        key,
                        token,
                        original_expires_at,
                        expected_entry,
                    )
                    self._metrics.sliding_refresh_error("redis")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._restore_l1_expiry(
                    key,
                    token,
                    original_expires_at,
                    expected_entry,
                )
                self._metrics.sliding_refresh_error("redis")
            finally:
                current = asyncio.current_task()
                if self._refresh_tasks.get(key) is current:
                    self._refresh_tasks.pop(key, None)

        task = asyncio.create_task(touch())
        self._refresh_tasks[key] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._refresh_tasks.get(key) is completed:
                self._refresh_tasks.pop(key, None)

        task.add_done_callback(discard)
        return True

    def _schedule_redis_generation_check(
        self,
        key: str,
        token: str,
        generation: _Generation,
        *,
        invalidation_revision: int,
        original_expires_at: float,
        expected_entry: _L1Entry,
    ) -> bool:
        """Validate a local-only page without touching a Redis page value."""

        if key in self._refresh_tasks:
            return True
        if (
            not self._redis.configured
            or not self._redis.available
            or self._closed
            or len(self._refresh_tasks) >= _MAX_REFRESH_TASKS
        ):
            return False

        async def validate_generation() -> None:
            try:
                current_generation = await self._generation(token)
                revision_unchanged = (
                    self._invalidation_revisions.get(token, 0) == invalidation_revision
                )
                if current_generation == generation and revision_unchanged:
                    self._metrics.sliding_refresh("redis")
                elif current_generation is not None and revision_unchanged:
                    self._invalidate_l1_token(token, "generation_change")
                    self._advance_invalidation_revision(token)
                elif revision_unchanged:
                    self._restore_l1_expiry(
                        key,
                        token,
                        original_expires_at,
                        expected_entry,
                    )
                    self._metrics.sliding_refresh_error("redis")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._restore_l1_expiry(
                    key,
                    token,
                    original_expires_at,
                    expected_entry,
                )
                self._metrics.sliding_refresh_error("redis")
            finally:
                current = asyncio.current_task()
                if self._refresh_tasks.get(key) is current:
                    self._refresh_tasks.pop(key, None)

        task = asyncio.create_task(validate_generation())
        self._refresh_tasks[key] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._refresh_tasks.get(key) is completed:
                self._refresh_tasks.pop(key, None)

        task.add_done_callback(discard)
        return True

    def _restore_l1_expiry(
        self,
        key: str,
        token: str,
        original_expires_at: float,
        expected_entry: _L1Entry,
    ) -> None:
        entry = self._l1.get(key)
        if entry is not expected_entry or entry.session_token != token:
            return
        entry.expires_at = min(entry.expires_at, original_expires_at)
        entry.refresh_after = min(entry.refresh_after, original_expires_at)

    async def _run_codec(self, codec: Callable[[], _CodecResult]) -> _CodecResult:
        if self._codec_admission_slots.locked():
            raise _CodecSaturated
        await self._codec_admission_slots.acquire()
        caller = asyncio.current_task()
        if caller is not None:
            self._codec_waiters.add(caller)
        try:
            await self._codec_slots.acquire()
        except BaseException:
            self._codec_admission_slots.release()
            raise
        finally:
            if caller is not None:
                self._codec_waiters.discard(caller)
        if self._closed:
            self._codec_slots.release()
            self._codec_admission_slots.release()
            raise asyncio.CancelledError

        async def run() -> _CodecResult:
            try:
                return await asyncio.to_thread(codec)
            finally:
                self._codec_slots.release()
                self._codec_admission_slots.release()

        worker = run()
        try:
            task = asyncio.create_task(worker)
        except BaseException:
            worker.close()
            self._codec_slots.release()
            self._codec_admission_slots.release()
            raise
        self._codec_tasks.add(task)

        def discard(completed: asyncio.Task[_CodecResult]) -> None:
            self._codec_tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                completed.exception()

        task.add_done_callback(discard)
        return await asyncio.shield(task)

    async def _serialize(
        self,
        operation: CacheOperation,
        session_id: str,
        generation: _Generation,
        query_digest: str,
        value: SessionEventPage | SessionWatermark,
    ) -> bytes | None:
        try:
            return await self._run_codec(
                lambda: self._serialize_sync(
                    operation,
                    session_id,
                    generation,
                    query_digest,
                    value,
                )
            )
        except _CodecSaturated:
            raise
        except Exception:
            self._metrics.compression("error")
            return None

    def _serialize_sync(
        self,
        operation: CacheOperation,
        session_id: str,
        generation: _Generation,
        query_digest: str,
        value: SessionEventPage | SessionWatermark,
    ) -> bytes | None:
        try:
            envelope = {
                "version": CACHE_SCHEMA_VERSION,
                "operation": operation,
                "store_id": self.store_id,
                "session_id": session_id,
                "generation": generation.encoded,
                "query": query_digest,
                "value": value.model_dump(mode="json"),
            }
            raw_payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        except (RecursionError, TypeError, ValueError):
            return None
        raw_size = len(raw_payload)
        if raw_size > self._policy.raw_value_max_bytes:
            self._metrics.compression("raw_oversized")
            return None
        payload = raw_payload
        if not self._policy.compression_enabled:
            self._metrics.compression("disabled")
        elif raw_size < self._policy.compression_threshold_bytes:
            self._metrics.compression("below_threshold")
        else:
            try:
                compressed = zlib.compress(raw_payload, level=self._policy.compression_level)
            except Exception:
                self._metrics.compression("error")
            else:
                wrapped = _WIRE_HEADER + compressed
                if len(wrapped) < raw_size:
                    payload = wrapped
                    self._metrics.compression("compressed", ratio=len(payload) / raw_size)
                else:
                    self._metrics.compression("not_smaller")
        if len(payload) > self._policy.redis_value_max_bytes:
            self._metrics.compression("stored_oversized")
            return None
        self._metrics.payload_sizes(raw_size, len(payload))
        return payload

    def _encode_derived_envelope_sync(
        self, envelope: dict[str, Any]
    ) -> DerivedEnvelopeEncoding | None:
        try:
            raw_payload = json.dumps(
                envelope,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError):
            return None
        raw_size = len(raw_payload)
        if raw_size > self._policy.raw_value_max_bytes:
            self._metrics.compression("raw_oversized")
            return None
        payload = raw_payload
        if not self._policy.compression_enabled:
            self._metrics.compression("disabled")
        elif raw_size < self._policy.compression_threshold_bytes:
            self._metrics.compression("below_threshold")
        else:
            try:
                compressed = zlib.compress(raw_payload, level=self._policy.compression_level)
            except Exception:
                self._metrics.compression("error")
            else:
                wrapped = _WIRE_HEADER + compressed
                if len(wrapped) < raw_size:
                    payload = wrapped
                    self._metrics.compression("compressed", ratio=len(payload) / raw_size)
                else:
                    self._metrics.compression("not_smaller")
        if len(payload) > self._policy.redis_value_max_bytes:
            self._metrics.compression("stored_oversized")
            return None
        self._metrics.payload_sizes(raw_size, len(payload))
        return DerivedEnvelopeEncoding(payload=payload, raw_size=raw_size)

    def _decode_derived_envelope_sync(self, payload: bytes) -> DerivedEnvelopeDecoding:
        if len(payload) > self._policy.redis_value_max_bytes:
            return DerivedEnvelopeDecoding("invalid")
        decoded, _reason = self._decode_wire_payload_sync(payload)
        if decoded is None:
            return DerivedEnvelopeDecoding("invalid")
        try:
            raw = json.loads(decoded, object_pairs_hook=self._strict_object)
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return DerivedEnvelopeDecoding("invalid")
        if not isinstance(raw, dict):
            return DerivedEnvelopeDecoding("invalid")
        return DerivedEnvelopeDecoding("decoded", value=raw, raw_size=len(decoded))

    async def _deserialize(
        self,
        payload: bytes,
        *,
        operation: CacheOperation,
        session_id: str,
        generation: _Generation,
        query_digest: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage | SessionWatermark | None:
        try:
            return await self._run_codec(
                lambda: self._deserialize_sync(
                    payload,
                    operation=operation,
                    session_id=session_id,
                    generation=generation,
                    query_digest=query_digest,
                    after_seq=after_seq,
                    before_seq=before_seq,
                    limit=limit,
                    direction=direction,
                )
            )
        except _CodecSaturated:
            raise
        except Exception:
            self._metrics.decode_failure("value")
            return None

    def _deserialize_sync(
        self,
        payload: bytes,
        *,
        operation: CacheOperation,
        session_id: str,
        generation: _Generation,
        query_digest: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> SessionEventPage | SessionWatermark | None:
        if len(payload) > self._policy.redis_value_max_bytes:
            self._metrics.decode_failure("size")
            return None
        decoded, failure = self._decode_wire_payload_sync(payload)
        if decoded is None:
            self._metrics.decode_failure(failure or "wire")
            return None
        try:
            raw = json.loads(decoded, object_pairs_hook=self._strict_object)
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._metrics.decode_failure("json")
            return None
        if not isinstance(raw, dict) or set(raw) != {
            "version",
            "operation",
            "store_id",
            "session_id",
            "generation",
            "query",
            "value",
        }:
            self._metrics.decode_failure("envelope")
            return None
        if (
            type(raw["version"]) is not int
            or raw["version"] != CACHE_SCHEMA_VERSION
            or raw["operation"] != operation
            or raw["store_id"] != self.store_id
            or raw["session_id"] != session_id
            or raw["generation"] != generation.encoded
            or raw["query"] != query_digest
        ):
            self._metrics.decode_failure("envelope")
            return None
        try:
            value = (
                SessionWatermark.model_validate(raw["value"])
                if operation == "watermark"
                else SessionEventPage.model_validate(raw["value"])
            )
        except (RecursionError, ValidationError):
            self._metrics.decode_failure("schema")
            return None
        if isinstance(value, SessionEventPage) and not value.events and not value.verified_empty:
            self._metrics.decode_failure("value")
            return None
        if not self._validate_value(
            value,
            operation=operation,
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        ):
            self._metrics.decode_failure("value")
            return None
        return value

    def _decode_wire_payload_sync(
        self,
        payload: bytes,
    ) -> tuple[bytes | None, DecodeFailureReason | None]:
        if not payload.startswith(_WIRE_MAGIC):
            if len(payload) > self._policy.raw_value_max_bytes:
                return None, "size"
            return payload, None
        if len(payload) < len(_WIRE_HEADER):
            return None, "wire"
        version = payload[len(_WIRE_MAGIC)]
        codec = payload[len(_WIRE_MAGIC) + 1]
        if version != _WIRE_VERSION or codec != _CODEC_ZLIB:
            return None, "wire"
        compressed = payload[len(_WIRE_HEADER) :]
        if not compressed:
            return None, "wire"
        decompressor = zlib.decompressobj()
        output = bytearray()
        try:
            for offset in range(0, len(compressed), _DECOMPRESSION_CHUNK_BYTES):
                chunk = compressed[offset : offset + _DECOMPRESSION_CHUNK_BYTES]
                remaining = self._policy.raw_value_max_bytes - len(output)
                decoded = decompressor.decompress(chunk, remaining + 1)
                output.extend(decoded)
                if len(output) > self._policy.raw_value_max_bytes or decompressor.unconsumed_tail:
                    return None, "size"
            remaining = self._policy.raw_value_max_bytes - len(output)
            output.extend(decompressor.flush(remaining + 1))
        except (MemoryError, zlib.error):
            return None, "decompression"
        if len(output) > self._policy.raw_value_max_bytes:
            return None, "size"
        if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
            return None, "decompression"
        return bytes(output), None

    @staticmethod
    def _page_query_class(
        *,
        after_seq: int | None,
        before_seq: int | None,
        direction: Literal["forward", "backward"],
    ) -> PageQueryClass:
        if direction == "forward":
            return "initial_forward" if after_seq in {None, 0} else "forward_delta"
        return "backward_tail" if before_seq is None else "historical_backward"

    @staticmethod
    def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def _validate_value(
        self,
        value: SessionEventPage | SessionWatermark,
        *,
        operation: CacheOperation,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: Literal["forward", "backward"],
    ) -> bool:
        if value.store_id != self.store_id or value.session_id != session_id:
            return False
        if operation == "watermark":
            return (
                isinstance(value, SessionWatermark)
                and isinstance(value.last_seq, int)
                and not isinstance(value.last_seq, bool)
                and value.last_seq >= 0
            )
        if not isinstance(value, SessionEventPage) or len(value.events) > limit:
            return False
        if direction == "forward" and value.has_more_before:
            return False
        if direction == "backward" and value.has_more_after:
            return False
        if not value.events:
            return (
                value.first_seq is None and not value.has_more_before and not value.has_more_after
            )
        if value.verified_empty:
            return False
        seqs = [event.seq for event in value.events]
        if (
            any(
                event.store_id != self.store_id or event.session_id != session_id or event.seq <= 0
                for event in value.events
            )
            or seqs != sorted(set(seqs))
            or value.first_seq != seqs[0]
            or value.last_seq is None
            or value.last_seq < seqs[-1]
        ):
            return False
        if (direction == "backward" or not value.has_more_after) and value.last_seq != seqs[-1]:
            return False
        if after_seq is not None and any(seq <= after_seq for seq in seqs):
            return False
        return not (before_seq is not None and any(seq >= before_seq for seq in seqs))

    def _ttl(
        self,
        value: SessionEventPage | SessionWatermark,
        operation: CacheOperation,
        before_seq: int | None,
        direction: Literal["forward", "backward"],
    ) -> int:
        if operation == "watermark":
            return self._policy.ttl_seconds
        page = cast(SessionEventPage, value)
        if not page.events:
            return self._policy.ttl_seconds
        if direction == "backward" and before_seq is None:
            return self._policy.ttl_seconds
        if direction == "forward" and not page.has_more_after:
            return self._policy.ttl_seconds
        return self._policy.ttl_seconds

    def _validate_read_request(
        self,
        *,
        operation: CacheOperation,
        session_id: str,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        direction: str,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must not be empty")
        if operation == "watermark":
            return
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be >= 1")
        for name, value in (("after_seq", after_seq), ("before_seq", before_seq)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer")
        if direction not in {"forward", "backward"}:
            raise ValueError("direction must be 'forward' or 'backward'")

    def _authority_digest(self, authority: EventStoreAuthority) -> str:
        return self._digest(
            "authority",
            authority.user_email,
            authority.agent_id,
            authority.agent_owner_email,
        )

    def _query_digest(
        self,
        operation: CacheOperation,
        *,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> str:
        return self._digest(
            "query",
            operation,
            "none" if after_seq is None else str(after_seq),
            "none" if before_seq is None else str(before_seq),
            str(limit),
            direction,
        )

    def _digest(self, domain: str, *parts: str) -> str:
        return hmac.new(
            self._secret,
            _canonical_parts(f"cognis-chat-event-cache:{domain}", *parts),
            hashlib.sha256,
        ).hexdigest()

    def _cache_key(
        self,
        *,
        authority_digest: str,
        session_token: str,
        generation: _Generation,
        query_digest: str,
    ) -> str:
        return (
            f"cognis:chat-event-cache:v{CACHE_SCHEMA_VERSION}:{authority_digest}:"
            f"{self.store_id}:{session_token}:{generation.epoch}:{generation.counter}:"
            f"{query_digest}"
        )

    def _generation_key(self, token: str) -> str:
        return f"cognis:chat-event-cache:v{CACHE_SCHEMA_VERSION}:generation:{self.store_id}:{token}"

    def _append_watermark_key(self, token: str, authority_digest: str) -> str:
        return (
            f"cognis:chat-event-cache:v{CACHE_SCHEMA_VERSION}:append-watermark:"
            f"{authority_digest}:{self.store_id}:{token}"
        )

    def _new_epoch(self) -> str:
        epoch = self._epoch_factory()
        if (
            not isinstance(epoch, str)
            or len(epoch) != 32
            or any(char not in "0123456789abcdef" for char in epoch)
        ):
            raise ValueError("epoch_factory must return 128-bit lowercase hex")
        return epoch


@dataclass(frozen=True, slots=True)
class BoundSessionEventStore:
    """Immutable authority-bound SessionEventStore read view."""

    _cache: CachedSessionEventStore
    _authority: EventStoreAuthority

    @property
    def store_id(self) -> str:
        return self._cache.store_id

    @property
    def authority_token(self) -> str:
        """Return the opaque complete-authority token used by shared caches."""

        return self._cache._authority_digest(self._authority)

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage:
        value = await self._cache._read(
            authority=self._authority,
            operation="page",
            session_id=session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            direction=direction,
        )
        return cast(SessionEventPage, value)

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        value = await self._cache._read(
            authority=self._authority,
            operation="watermark",
            session_id=session_id,
        )
        return cast(SessionWatermark, value)


__all__ = [
    "ACTIVE_CACHE_TTL_SECONDS",
    "AppendInvalidation",
    "BoundSessionEventStore",
    "CACHE_SCHEMA_VERSION",
    "CachedSessionEventStore",
    "DEFAULT_CACHE_TTL_SECONDS",
    "EventCacheBounds",
    "EventCachePolicy",
    "MAX_CANONICAL_CACHE_TTL_SECONDS",
    "MAX_RAW_VALUE_BYTES",
    "MAX_REDIS_VALUE_BYTES",
]
