"""Best-effort Redis relay for native Chat v2 runtime overlay state."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import secrets
import zlib
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Annotated, Any, Literal, TypeVar

from prometheus_client import Counter, Gauge, Histogram
from pydantic import Field, ValidationError, model_validator

from cognis.api.chat_v2.schemas import (
    RuntimeActiveTurn,
    StrictModel,
    TimelineItem,
)
from cognis.core.redis_service import RedisService
from cognis.logging import get_logger
from cognis.models.config import GenerationPerformanceSnapshot

logger = get_logger(__name__)

RELAY_CHANNEL = "cognis:chat-v2-runtime:v1"
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_RAW_PAYLOAD_BYTES = 2 * 1024 * 1024
COMPRESSION_LEVEL = 1
CODEC_WORKERS = 2
CODEC_MAX_PENDING = 4
DECOMPRESSION_CHUNK_BYTES = 64 * 1024
ACTIVE_TTL_SECONDS = 600
TERMINAL_TTL_SECONDS = 120
RUNTIME_MAX_AGE_SECONDS = 30
PROGRESS_PUBLISH_TIMEOUT_SECONDS = 0.5
FUTURE_SKEW_SECONDS = 5
DEFAULT_QUEUE_MAX_ITEMS = 256
DEFAULT_QUEUE_MAX_BYTES = 8 * MAX_PAYLOAD_BYTES
DEFAULT_RECEIVE_CONCURRENCY = 32
DEFAULT_STATE_MAX_ENTRIES = 4096
DEFAULT_STATE_TTL_SECONDS = 900
MAX_REDIS_SAFE_INTEGER = (1 << 53) - 1

_Identifier = Annotated[str, Field(min_length=1, max_length=256)]

RELAY_CONNECTED = Gauge(
    "cognis_chat_v2_runtime_relay_connected",
    "Whether the Chat v2 runtime relay Pub/Sub receiver is connected.",
)
RELAY_ENQUEUED = Counter(
    "cognis_chat_v2_runtime_relay_enqueued_total",
    "Chat v2 runtime relay envelopes accepted into the local queue.",
)
RELAY_PUBLISHED = Counter(
    "cognis_chat_v2_runtime_relay_published_total",
    "Chat v2 runtime relay envelopes admitted and published by Redis.",
)
RELAY_RECEIVED = Counter(
    "cognis_chat_v2_runtime_relay_received_total",
    "Chat v2 runtime relay envelopes received from Redis Pub/Sub.",
)
RELAY_APPLIED = Counter(
    "cognis_chat_v2_runtime_relay_applied_total",
    "Chat v2 runtime relay envelopes applied locally.",
)
RELAY_PUBLISH_ERRORS = Counter(
    "cognis_chat_v2_runtime_relay_publish_errors_total",
    "Chat v2 runtime relay local publish failures.",
)
RELAY_RECONNECTS = Counter(
    "cognis_chat_v2_runtime_relay_reconnects_total",
    "Chat v2 runtime relay Pub/Sub reconnect attempts.",
)
RELAY_DROPPED = Counter(
    "cognis_chat_v2_runtime_relay_dropped_total",
    "Chat v2 runtime relay envelopes dropped by bounded best-effort handling.",
    labelnames=("reason",),
)
RELAY_QUEUE_DEPTH = Gauge(
    "cognis_chat_v2_runtime_relay_queue_depth",
    "Current Chat v2 runtime relay local queue depth.",
)
RELAY_PAYLOAD_BYTES = Histogram(
    "cognis_chat_v2_runtime_relay_payload_bytes",
    "Serialized Chat v2 runtime relay envelope size.",
    buckets=(256, 1024, 4096, 16384, 65536, 262144, MAX_PAYLOAD_BYTES),
)
RELAY_RAW_PAYLOAD_BYTES = Histogram(
    "cognis_chat_v2_runtime_relay_raw_payload_bytes",
    "Uncompressed Chat v2 runtime relay envelope size.",
    buckets=(256, 1024, 4096, 16384, 65536, 262144, 524288, 1048576, 2097152),
)
RELAY_QUEUE_AGE_SECONDS = Histogram(
    "cognis_chat_v2_runtime_relay_queue_age_seconds",
    "Age of a Chat v2 runtime relay envelope when publishing starts.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
RELAY_PUBLISH_LATENCY_SECONDS = Histogram(
    "cognis_chat_v2_runtime_relay_publish_latency_seconds",
    "Latency of one Redis publish attempt for a Chat v2 runtime relay envelope.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
RELAY_APPLY_AGE_SECONDS = Histogram(
    "cognis_chat_v2_runtime_relay_apply_age_seconds",
    "Age of a Chat v2 runtime relay envelope when a receiving controller applies it.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
RELAY_CODEC = Counter(
    "cognis_chat_v2_runtime_relay_codec_total",
    "Chat v2 runtime relay wire codec outcomes.",
    labelnames=("outcome",),
)


class _RelayMetrics:
    """Narrow fail-open facade around optional relay instrumentation."""

    @staticmethod
    def _call(operation: Callable[[], None]) -> None:
        with contextlib.suppress(Exception):
            operation()

    def connected(self, value: int) -> None:
        self._call(lambda: RELAY_CONNECTED.set(value))

    def enqueued(self) -> None:
        self._call(RELAY_ENQUEUED.inc)

    def published(self) -> None:
        self._call(RELAY_PUBLISHED.inc)

    def received(self) -> None:
        self._call(RELAY_RECEIVED.inc)

    def applied(self) -> None:
        self._call(RELAY_APPLIED.inc)

    def publish_error(self) -> None:
        self._call(RELAY_PUBLISH_ERRORS.inc)

    def reconnect(self) -> None:
        self._call(RELAY_RECONNECTS.inc)

    def dropped(self, reason: str) -> None:
        self._call(lambda: RELAY_DROPPED.labels(reason=reason).inc())

    def queue_depth(self, value: int) -> None:
        self._call(lambda: RELAY_QUEUE_DEPTH.set(value))

    def payload_bytes(self, value: int) -> None:
        self._call(lambda: RELAY_PAYLOAD_BYTES.observe(value))

    def raw_payload_bytes(self, value: int) -> None:
        self._call(lambda: RELAY_RAW_PAYLOAD_BYTES.observe(value))

    def queue_age(self, value: float) -> None:
        self._call(lambda: RELAY_QUEUE_AGE_SECONDS.observe(value))

    def publish_latency(self, value: float) -> None:
        self._call(lambda: RELAY_PUBLISH_LATENCY_SECONDS.observe(value))

    def apply_age(self, value: float) -> None:
        self._call(lambda: RELAY_APPLY_AGE_SECONDS.observe(value))

    def codec(self, outcome: str) -> None:
        self._call(lambda: RELAY_CODEC.labels(outcome=outcome).inc())


_METRICS = _RelayMetrics()


DROP_REASONS = frozenset(
    {
        "queue_full",
        "superseded",
        "oversized",
        "invalid",
        "duplicate",
        "stale",
        "wrong_turn",
        "wrong_fence",
        "no_subscriber",
        "codec_saturated",
        "publish_failed",
    }
)
CODEC_OUTCOMES = frozenset(
    {
        "legacy",
        "compressed",
        "raw_oversized",
        "stored_oversized",
        "invalid",
        "saturated",
        "error",
    }
)


class RelayKind(StrEnum):
    RUNTIME = "runtime"
    TERMINAL = "terminal"


class RelayOrigin(StrictModel):
    controller_id: _Identifier
    incarnation_id: _Identifier
    runtime_epoch: _Identifier


class RelayOwner(StrictModel):
    controller_id: _Identifier
    incarnation_id: _Identifier


class ChatV2RuntimeRelayEnvelope(StrictModel):
    """Strict cumulative runtime state exchanged between controllers."""

    version: Literal[1] = 1
    kind: RelayKind
    event_id: _Identifier
    generated_at: datetime
    origin: RelayOrigin
    conversation_id: _Identifier
    session_id: _Identifier
    turn_id: _Identifier
    direct_request_id: _Identifier
    owner: RelayOwner
    fencing_token: int = Field(ge=0, le=MAX_REDIS_SAFE_INTEGER)
    source_revision: int = Field(ge=0, le=MAX_REDIS_SAFE_INTEGER)
    has_active_turn: bool
    active_turn: RuntimeActiveTurn | None = None
    volatile_items: list[TimelineItem] = Field(default_factory=list)
    context_usage: dict[str, Any] | None = None
    last_generation: GenerationPerformanceSnapshot | None = None

    @model_validator(mode="after")
    def _validate_coherence(self) -> ChatV2RuntimeRelayEnvelope:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() != timedelta(0):
            raise ValueError("generated_at must be UTC")
        if self.has_active_turn != (self.active_turn is not None):
            raise ValueError("has_active_turn must exactly match active_turn presence")
        if self.active_turn is not None:
            if self.active_turn.turn_id != self.turn_id:
                raise ValueError("active_turn.turn_id must match turn_id")
            if self.active_turn.session_id != self.session_id:
                raise ValueError("active_turn.session_id must match session_id")
        if self.kind == RelayKind.TERMINAL and self.has_active_turn:
            raise ValueError("terminal envelopes cannot contain an active turn")
        for item in self.volatile_items:
            if item.stable:
                raise ValueError("volatile_items must have stable=false")
            item_turn_id = getattr(item, "turn_id", None)
            if item_turn_id is not None and item_turn_id != self.turn_id:
                raise ValueError("volatile item turn_id must match turn_id")
        return self

    def encoded(self) -> bytes:
        payload = self.model_dump_json().encode()
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("relay envelope exceeds 512KiB")
        return payload

    def raw_encoded(self) -> bytes:
        payload = self.model_dump_json().encode()
        if len(payload) > MAX_RAW_PAYLOAD_BYTES:
            raise ValueError("relay envelope exceeds 2MiB raw limit")
        return payload

    @classmethod
    def decode(cls, payload: bytes) -> ChatV2RuntimeRelayEnvelope:
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("relay envelope exceeds 512KiB")
        return cls.model_validate_json(payload)


class _CompressedRelayWireFrame(StrictModel):
    wire_version: Literal[2] = 2
    codec: Literal["zlib"] = "zlib"
    raw_size: int = Field(ge=1, le=MAX_RAW_PAYLOAD_BYTES)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_b64: str = Field(min_length=1, max_length=2 * MAX_RAW_PAYLOAD_BYTES)
    kind: RelayKind
    direct_request_id: _Identifier
    turn_id: _Identifier
    owner: RelayOwner
    origin: RelayOrigin
    fencing_token: int = Field(ge=0, le=MAX_REDIS_SAFE_INTEGER)
    source_revision: int = Field(ge=0, le=MAX_REDIS_SAFE_INTEGER)

    @classmethod
    def from_envelope(
        cls,
        envelope: ChatV2RuntimeRelayEnvelope,
        *,
        raw: bytes,
        compressed: bytes,
    ) -> _CompressedRelayWireFrame:
        return cls(
            raw_size=len(raw),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            payload_b64=base64.b64encode(compressed).decode("ascii"),
            kind=envelope.kind,
            direct_request_id=envelope.direct_request_id,
            turn_id=envelope.turn_id,
            owner=envelope.owner,
            origin=envelope.origin,
            fencing_token=envelope.fencing_token,
            source_revision=envelope.source_revision,
        )

    def matches(self, envelope: ChatV2RuntimeRelayEnvelope) -> bool:
        return (
            self.kind == envelope.kind
            and self.direct_request_id == envelope.direct_request_id
            and self.turn_id == envelope.turn_id
            and self.owner == envelope.owner
            and self.origin == envelope.origin
            and self.fencing_token == envelope.fencing_token
            and self.source_revision == envelope.source_revision
        )


class _CodecSaturated(Exception):
    """The bounded relay codec has no admission capacity."""


_CodecResult = TypeVar("_CodecResult")


@dataclass(frozen=True, slots=True)
class RelayGenerationContext:
    direct_request_id: str
    turn_id: str
    session_id: str
    conversation_id: str
    owner_controller_id: str
    owner_incarnation_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        for value in (
            self.direct_request_id,
            self.turn_id,
            self.session_id,
            self.conversation_id,
            self.owner_controller_id,
            self.owner_incarnation_id,
        ):
            if not value or len(value) > 256:
                raise ValueError("generation context identifiers must contain 1..256 characters")
        if not 0 <= self.fencing_token <= MAX_REDIS_SAFE_INTEGER:
            raise ValueError("fencing_token must be a Redis-safe non-negative integer")


class AdmissionDecision(StrEnum):
    ACCEPT = "accept"
    STALE = "stale"
    WRONG_TURN = "wrong_turn"
    WRONG_FENCE = "wrong_fence"


def compare_admission(
    current: ChatV2RuntimeRelayEnvelope | None,
    candidate: ChatV2RuntimeRelayEnvelope,
) -> AdmissionDecision:
    """Mirror the Redis Lua latest-state admission ordering."""

    if current is None:
        return AdmissionDecision.ACCEPT
    if candidate.fencing_token > current.fencing_token:
        return AdmissionDecision.ACCEPT
    if candidate.fencing_token < current.fencing_token:
        return AdmissionDecision.WRONG_FENCE
    same_generation = (
        candidate.direct_request_id == current.direct_request_id
        and candidate.turn_id == current.turn_id
        and candidate.owner == current.owner
    )
    if not same_generation:
        return AdmissionDecision.WRONG_TURN
    if candidate.origin.runtime_epoch != current.origin.runtime_epoch:
        return AdmissionDecision.STALE
    if current.kind == RelayKind.TERMINAL and candidate.kind == RelayKind.RUNTIME:
        return AdmissionDecision.STALE
    if current.kind == RelayKind.RUNTIME and candidate.kind == RelayKind.TERMINAL:
        return AdmissionDecision.ACCEPT
    if candidate.source_revision <= current.source_revision:
        return AdmissionDecision.STALE
    return AdmissionDecision.ACCEPT


ADMIT_AND_PUBLISH_LUA = r"""
local candidate = cjson.decode(ARGV[1])
local raw = redis.call('GET', KEYS[1])
if raw then
  local current = cjson.decode(raw)
  if candidate.fencing_token < current.fencing_token then return -3 end
  if candidate.fencing_token == current.fencing_token then
    if candidate.direct_request_id ~= current.direct_request_id
       or candidate.turn_id ~= current.turn_id
       or candidate.owner.controller_id ~= current.owner.controller_id
       or candidate.owner.incarnation_id ~= current.owner.incarnation_id then
      return -2
    end
    if candidate.origin.runtime_epoch ~= current.origin.runtime_epoch then return -1 end
    if current.kind == 'terminal' and candidate.kind == 'runtime' then return -1 end
    if current.kind == 'runtime' and candidate.kind == 'terminal' then
      redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
      redis.call('PUBLISH', ARGV[3], ARGV[1])
      return 1
    end
    if candidate.source_revision <= current.source_revision then return -1 end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('PUBLISH', ARGV[3], ARGV[1])
return 1
"""


@dataclass(slots=True)
class _QueuedEnvelope:
    envelope: ChatV2RuntimeRelayEnvelope
    payload: bytes
    cumulative_boundary: bool

    @property
    def is_progress(self) -> bool:
        return self.envelope.kind == RelayKind.RUNTIME and not self.cumulative_boundary

    @property
    def is_terminal(self) -> bool:
        return self.envelope.kind == RelayKind.TERMINAL


class _BoundedRelayQueue:
    def __init__(self, *, max_items: int, max_bytes: int) -> None:
        if max_items < 1 or max_bytes < 1:
            raise ValueError("queue bounds must be positive")
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.items: deque[_QueuedEnvelope] = deque()
        self.bytes = 0
        self.event = asyncio.Event()

    def put(self, item: _QueuedEnvelope) -> bool:
        if len(item.payload) > self.max_bytes:
            _drop("queue_full")
            return False
        removed = self._coalesce(item)
        removed_ids = {id(queued) for queued in removed}
        survivors = [queued for queued in self.items if id(queued) not in removed_ids]
        survivor_bytes = sum(len(queued.payload) for queued in survivors)
        victims: list[_QueuedEnvelope] = []
        while survivors and (
            len(survivors) >= self.max_items or survivor_bytes + len(item.payload) > self.max_bytes
        ):
            victim = next((queued for queued in survivors if queued.is_progress), None)
            if victim is None:
                victim = next((queued for queued in survivors if not queued.is_terminal), None)
            if victim is None:
                _drop("queue_full")
                return False
            survivors.remove(victim)
            survivor_bytes -= len(victim.payload)
            victims.append(victim)
        if len(survivors) >= self.max_items or survivor_bytes + len(item.payload) > self.max_bytes:
            _drop("queue_full")
            return False
        for _old in removed:
            _drop("superseded")
        for victim in victims:
            _drop("queue_full" if not victim.is_progress else "superseded")
        self.items = deque(survivors)
        self.bytes = survivor_bytes
        self.items.append(item)
        self.bytes += len(item.payload)
        self.event.set()
        _METRICS.queue_depth(len(self.items))
        return True

    def _coalesce(self, candidate: _QueuedEnvelope) -> list[_QueuedEnvelope]:
        generation = self._generation_key(candidate)
        if candidate.is_progress:
            removed: list[_QueuedEnvelope] = []
            for queued in reversed(self.items):
                if self._generation_key(queued) != generation:
                    continue
                if queued.cumulative_boundary:
                    break
                if queued.is_progress:
                    removed.append(queued)
            return removed
        last_boundary = next(
            (
                index
                for index in range(len(self.items) - 1, -1, -1)
                if self._generation_key(self.items[index]) == generation
                and self.items[index].cumulative_boundary
            ),
            None,
        )
        if last_boundary is None:
            return []
        removed = []
        for index, queued in enumerate(self.items):
            if index > last_boundary or self._generation_key(queued) != generation:
                continue
            if queued.is_terminal:
                if (
                    candidate.is_terminal
                    and queued.envelope.source_revision < candidate.envelope.source_revision
                ):
                    removed.append(queued)
                continue
            removed.append(queued)
        return removed

    @staticmethod
    def _generation_key(item: _QueuedEnvelope) -> tuple[str, ...]:
        envelope = item.envelope
        return (
            envelope.conversation_id,
            envelope.session_id,
            envelope.turn_id,
            envelope.direct_request_id,
            envelope.owner.controller_id,
            envelope.owner.incarnation_id,
            str(envelope.fencing_token),
            envelope.origin.controller_id,
            envelope.origin.incarnation_id,
            envelope.origin.runtime_epoch,
        )

    def _remove(self, item: _QueuedEnvelope) -> None:
        self.items.remove(item)
        self.bytes -= len(item.payload)

    async def get(self) -> _QueuedEnvelope:
        while not self.items:
            self.event.clear()
            await self.event.wait()
        item = self.items.popleft()
        self.bytes -= len(item.payload)
        if not self.items:
            self.event.clear()
        _METRICS.queue_depth(len(self.items))
        return item

    def empty(self) -> bool:
        return not self.items


DurableValidator = Callable[[ChatV2RuntimeRelayEnvelope], Awaitable[AdmissionDecision | bool]]
ApplyCallback = Callable[[ChatV2RuntimeRelayEnvelope], Awaitable[None]]
SubscriberCallback = Callable[[str], bool]


class ChatV2RuntimeRedisRelay:
    """Bounded, non-authoritative relay; Redis failures never block observers."""

    def __init__(
        self,
        *,
        redis_service: RedisService,
        shared_secret: str | bytes,
        controller_id: str,
        incarnation_id: str,
        durable_validator: DurableValidator,
        apply_callback: ApplyCallback,
        has_subscriber: SubscriberCallback,
        queue_max_items: int = DEFAULT_QUEUE_MAX_ITEMS,
        queue_max_bytes: int = DEFAULT_QUEUE_MAX_BYTES,
        state_max_entries: int = DEFAULT_STATE_MAX_ENTRIES,
        state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
        reconnect_min_seconds: float = 0.05,
        reconnect_max_seconds: float = 2.0,
    ) -> None:
        if not shared_secret:
            raise ValueError("shared_secret is required")
        self.redis_service = redis_service
        self._secret = (
            shared_secret.encode() if isinstance(shared_secret, str) else bytes(shared_secret)
        )
        self.origin = RelayOrigin(
            controller_id=controller_id,
            incarnation_id=incarnation_id,
            runtime_epoch=secrets.token_urlsafe(18),
        )
        self._durable_validator = durable_validator
        self._apply_callback = apply_callback
        self._has_subscriber = has_subscriber
        self._queue = _BoundedRelayQueue(max_items=queue_max_items, max_bytes=queue_max_bytes)
        self._state_max_entries = state_max_entries
        self._state_ttl = state_ttl_seconds
        self._seen: OrderedDict[str, tuple[float, int, RelayKind, str, tuple[str, ...]]] = (
            OrderedDict()
        )
        self._revisions: OrderedDict[tuple[str, ...], int] = OrderedDict()
        self._publisher_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._receiver_deliveries: set[asyncio.Task[None]] = set()
        self._receiver_tails: dict[str, asyncio.Task[None]] = {}
        self._receiver_slots = asyncio.Semaphore(DEFAULT_RECEIVE_CONCURRENCY)
        self._stop = asyncio.Event()
        self._publisher_idle = asyncio.Event()
        self._publisher_idle.set()
        self._codec_slots = asyncio.Semaphore(CODEC_WORKERS)
        self._codec_admitted = 0
        self._codec_tasks: set[asyncio.Task[Any]] = set()
        self._reconnect_min = reconnect_min_seconds
        self._reconnect_max = reconnect_max_seconds

    def latest_key(self, conversation_id: str) -> str:
        digest = hmac.new(self._secret, conversation_id.encode(), hashlib.sha256).hexdigest()
        return f"{RELAY_CHANNEL}:latest:{digest}"

    def next_revision(self, context: RelayGenerationContext) -> int:
        key = (
            context.conversation_id,
            context.session_id,
            context.turn_id,
            context.direct_request_id,
            context.owner_controller_id,
            context.owner_incarnation_id,
            str(context.fencing_token),
            self.origin.runtime_epoch,
        )
        current = self._revisions.pop(key, 0)
        if current >= MAX_REDIS_SAFE_INTEGER:
            self._revisions[key] = current
            raise ValueError("source_revision exceeds Redis-safe integer range")
        revision = current + 1
        self._revisions[key] = revision
        while len(self._revisions) > self._state_max_entries:
            self._revisions.popitem(last=False)
        return revision

    def make_envelope(
        self,
        context: RelayGenerationContext,
        *,
        kind: RelayKind = RelayKind.RUNTIME,
        has_active_turn: bool,
        active_turn: RuntimeActiveTurn | None,
        volatile_items: list[TimelineItem] | None = None,
        context_usage: Mapping[str, Any] | None = None,
        last_generation: GenerationPerformanceSnapshot | None = None,
        event_id: str | None = None,
    ) -> ChatV2RuntimeRelayEnvelope:
        return ChatV2RuntimeRelayEnvelope(
            kind=kind,
            event_id=event_id or secrets.token_urlsafe(18),
            generated_at=datetime.now(UTC),
            origin=self.origin,
            conversation_id=context.conversation_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            direct_request_id=context.direct_request_id,
            owner=RelayOwner(
                controller_id=context.owner_controller_id,
                incarnation_id=context.owner_incarnation_id,
            ),
            fencing_token=context.fencing_token,
            source_revision=self.next_revision(context),
            has_active_turn=has_active_turn,
            active_turn=active_turn,
            volatile_items=volatile_items or [],
            context_usage=dict(context_usage) if context_usage is not None else None,
            last_generation=last_generation,
        )

    def enqueue(
        self,
        envelope: ChatV2RuntimeRelayEnvelope,
        *,
        cumulative_boundary: bool = False,
    ) -> bool:
        try:
            payload = envelope.raw_encoded()
        except ValueError:
            _codec("raw_oversized")
            _drop("oversized")
            return False
        _METRICS.raw_payload_bytes(len(payload))
        accepted = self._queue.put(
            _QueuedEnvelope(
                envelope=envelope,
                payload=payload,
                cumulative_boundary=cumulative_boundary or envelope.kind == RelayKind.TERMINAL,
            )
        )
        if accepted:
            _METRICS.enqueued()
        return accepted

    async def start(self) -> None:
        if self._publisher_task is not None:
            return
        self._stop.clear()
        self._publisher_task = asyncio.create_task(self._publisher_loop())
        self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def stop(self, *, drain_timeout_seconds: float = 1.0) -> None:
        self._stop.set()
        self._queue.event.set()
        if self._publisher_task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._drain(), timeout=drain_timeout_seconds)
        tasks = [task for task in (self._publisher_task, self._receiver_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        deliveries = list(self._receiver_deliveries)
        for task in deliveries:
            task.cancel()
        if deliveries:
            await asyncio.gather(*deliveries, return_exceptions=True)
        self._receiver_deliveries.clear()
        self._receiver_tails.clear()
        codec_tasks = list(self._codec_tasks)
        if codec_tasks:
            await asyncio.gather(*codec_tasks, return_exceptions=True)
        self._publisher_task = None
        self._receiver_task = None
        _METRICS.connected(0)

    async def _drain(self) -> None:
        while not self._queue.empty() or not self._publisher_idle.is_set():
            await asyncio.sleep(0)

    async def _publisher_loop(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                queued = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            self._publisher_idle.clear()
            _METRICS.queue_age(
                max(0.0, (datetime.now(UTC) - queued.envelope.generated_at).total_seconds())
            )
            ttl = (
                TERMINAL_TTL_SECONDS
                if queued.envelope.kind == RelayKind.TERMINAL
                else ACTIVE_TTL_SECONDS
            )
            try:
                try:
                    wire_payload = await self._run_codec(
                        partial(_encode_wire_sync, queued.envelope, queued.payload)
                    )
                except _CodecSaturated:
                    _codec("saturated")
                    _drop("codec_saturated")
                    continue
                except Exception:
                    _codec("error")
                    _drop("invalid")
                    continue
                if wire_payload is None:
                    _codec("stored_oversized")
                    _drop("oversized")
                    continue
                _METRICS.payload_bytes(len(wire_payload))
                retry_delay = self._reconnect_min
                while True:
                    age = (datetime.now(UTC) - queued.envelope.generated_at).total_seconds()
                    remaining = RUNTIME_MAX_AGE_SECONDS - age
                    if remaining <= 0:
                        _drop("stale")
                        break
                    started_at = asyncio.get_running_loop().time()
                    try:
                        attempt_timeout = (
                            min(remaining, PROGRESS_PUBLISH_TIMEOUT_SECONDS)
                            if queued.is_progress
                            else remaining
                        )
                        result = await asyncio.wait_for(
                            self.redis_service.eval(
                                ADMIT_AND_PUBLISH_LUA,
                                keys=[self.latest_key(queued.envelope.conversation_id)],
                                args=[wire_payload, ttl, RELAY_CHANNEL],
                            ),
                            timeout=attempt_timeout,
                        )
                    except Exception:
                        result = None
                    finally:
                        _METRICS.publish_latency(asyncio.get_running_loop().time() - started_at)
                    if result == 1 or result == b"1":
                        _METRICS.published()
                        break
                    if result in (-1, b"-1"):
                        _drop("stale")
                        break
                    if result in (-2, b"-2"):
                        _drop("wrong_turn")
                        break
                    if result in (-3, b"-3"):
                        _drop("wrong_fence")
                        break
                    _METRICS.publish_error()
                    if queued.is_progress:
                        # Progress is cumulative and a newer frame replaces it.
                        # Do not block unrelated conversations behind one
                        # unavailable Redis publish for up to 30 seconds.
                        _drop("publish_failed")
                        break
                    age = (datetime.now(UTC) - queued.envelope.generated_at).total_seconds()
                    remaining = RUNTIME_MAX_AGE_SECONDS - age
                    if remaining <= 0:
                        _drop("stale")
                        break
                    await asyncio.sleep(min(retry_delay, remaining))
                    retry_delay = min(retry_delay * 2, self._reconnect_max)
            finally:
                self._publisher_idle.set()

    async def _receiver_loop(self) -> None:
        delay = self._reconnect_min
        first = True
        while not self._stop.is_set():
            consumer = self.redis_service.create_pubsub()
            if consumer is None:
                subscribed = False
            else:
                try:
                    try:
                        subscribed = await consumer.subscribe(RELAY_CHANNEL)
                    except Exception:
                        subscribed = False
                    if subscribed:
                        _METRICS.connected(1)
                        delay = self._reconnect_min
                        try:
                            async for message in consumer.listen():
                                if self._stop.is_set():
                                    break
                                data = message.get("data")
                                if isinstance(data, str):
                                    data = data.encode()
                                if isinstance(data, bytes):
                                    await self._schedule_receive(data)
                        except Exception:
                            subscribed = False
                finally:
                    _METRICS.connected(0)
                    await consumer.aclose()
            if self._stop.is_set():
                break
            if not first:
                _METRICS.reconnect()
            first = False
            await asyncio.sleep(delay)
            if not subscribed:
                delay = min(delay * 2, self._reconnect_max)

    async def _schedule_receive(self, payload: bytes) -> None:
        """Start one bounded relay delivery without serializing other conversations."""
        envelope = await self._decode_payload(payload)
        if envelope is None:
            return
        await self._receiver_slots.acquire()
        conversation_id = envelope.conversation_id
        previous = self._receiver_tails.get(conversation_id)
        task = asyncio.create_task(self._receive_after(previous, envelope))
        self._receiver_deliveries.add(task)
        self._receiver_tails[conversation_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            self._receiver_deliveries.discard(completed)
            if self._receiver_tails.get(conversation_id) is completed:
                self._receiver_tails.pop(conversation_id, None)

        task.add_done_callback(discard)

    async def _receive_after(
        self,
        previous: asyncio.Task[None] | None,
        envelope: ChatV2RuntimeRelayEnvelope,
    ) -> None:
        try:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._receive_envelope(envelope)
        finally:
            self._receiver_slots.release()

    async def receive(self, payload: bytes) -> bool:
        envelope = await self._decode_payload(payload)
        if envelope is None:
            return False
        return await self._receive_envelope(envelope)

    async def _receive_envelope(self, envelope: ChatV2RuntimeRelayEnvelope) -> bool:
        _METRICS.received()
        if envelope.origin == self.origin:
            _drop("duplicate")
            return False
        now = datetime.now(UTC)
        age = (now - envelope.generated_at).total_seconds()
        if age > RUNTIME_MAX_AGE_SECONDS or age < -FUTURE_SKEW_SECONDS:
            _drop("stale")
            return False
        if not self._has_subscriber(envelope.conversation_id):
            _drop("no_subscriber")
            return False
        seen = self._seen_decision(envelope)
        if seen is not None:
            _drop(seen)
            return False
        try:
            verdict = await self._durable_validator(envelope)
        except Exception:
            _drop("invalid")
            logger.warning("Relay durable validation failed")
            return False
        if verdict is not True and verdict != AdmissionDecision.ACCEPT:
            reason = str(verdict)
            if reason not in DROP_REASONS:
                reason = "invalid"
            _drop(reason)
            return False
        try:
            await self._apply_callback(envelope)
        except Exception:
            _drop("invalid")
            logger.warning("Relay apply callback failed")
            return False
        _METRICS.apply_age(max(0.0, age))
        self._remember(envelope)
        _METRICS.applied()
        return True

    async def hydrate_latest(
        self,
        context: RelayGenerationContext,
        *,
        durable_validator: DurableValidator | None = None,
    ) -> ChatV2RuntimeRelayEnvelope | None:
        try:
            payload = await self.redis_service.get(self.latest_key(context.conversation_id))
        except Exception:
            return None
        if payload is None:
            return None
        envelope = await self._decode_payload(payload)
        if envelope is None:
            return None
        expected = self._matches_context(envelope, context)
        if expected != AdmissionDecision.ACCEPT:
            _drop(expected.value)
            return None
        if envelope.kind == RelayKind.TERMINAL and (
            envelope.turn_id != context.turn_id
            or envelope.direct_request_id != context.direct_request_id
        ):
            _drop("wrong_turn")
            return None
        seen = self._seen_decision(envelope)
        if seen is not None:
            _drop(seen)
            return None
        verdict = await (durable_validator or self._durable_validator)(envelope)
        if verdict is not True and verdict != AdmissionDecision.ACCEPT:
            reason = str(verdict)
            _drop(reason if reason in DROP_REASONS else "invalid")
            return None
        self._remember(envelope)
        return envelope

    async def _decode_payload(self, payload: bytes) -> ChatV2RuntimeRelayEnvelope | None:
        if len(payload) > MAX_PAYLOAD_BYTES:
            _codec("stored_oversized")
            _drop("oversized")
            return None
        try:
            envelope = await self._run_codec(lambda: _decode_wire_sync(payload))
        except _CodecSaturated:
            _codec("saturated")
            _drop("codec_saturated")
            return None
        except Exception:
            _codec("error")
            _drop("invalid")
            return None
        if envelope is None:
            _codec("invalid")
            _drop("invalid")
            return None
        _METRICS.payload_bytes(len(payload))
        return envelope

    async def _run_codec(self, codec: Callable[[], _CodecResult]) -> _CodecResult:
        if self._codec_admitted >= CODEC_MAX_PENDING:
            raise _CodecSaturated
        self._codec_admitted += 1
        try:
            await self._codec_slots.acquire()
        except BaseException:
            self._codec_admitted -= 1
            raise

        async def run() -> _CodecResult:
            try:
                return await asyncio.to_thread(codec)
            finally:
                self._codec_admitted -= 1
                self._codec_slots.release()

        try:
            task = asyncio.create_task(run())
        except BaseException:
            self._codec_admitted -= 1
            self._codec_slots.release()
            raise
        self._codec_tasks.add(task)

        def discard(completed: asyncio.Task[_CodecResult]) -> None:
            self._codec_tasks.discard(completed)
            with contextlib.suppress(asyncio.CancelledError):
                completed.exception()

        task.add_done_callback(discard)
        return await asyncio.shield(task)

    def invalidate(self, conversation_id: str) -> None:
        for event_id, (_, _, __, stored_conversation_id, ___) in list(self._seen.items()):
            if stored_conversation_id == conversation_id:
                self._seen.pop(event_id, None)
        for key in list(self._revisions):
            if key[0] == conversation_id:
                self._revisions.pop(key, None)

    def _seen_decision(self, envelope: ChatV2RuntimeRelayEnvelope) -> str | None:
        self._expire_seen()
        if envelope.event_id in self._seen:
            return "duplicate"
        generation = self._generation_key(envelope)
        matching = [
            (revision, kind)
            for _, (__, revision, kind, ___, stored_generation) in self._seen.items()
            if stored_generation == generation
        ]
        terminal_revisions = [revision for revision, kind in matching if kind == RelayKind.TERMINAL]
        if terminal_revisions:
            if envelope.kind == RelayKind.RUNTIME:
                return "stale"
            return "stale" if envelope.source_revision <= max(terminal_revisions) else None
        if envelope.kind == RelayKind.TERMINAL:
            return None
        if matching and envelope.source_revision <= max(revision for revision, _ in matching):
            return "stale"
        return None

    def _remember(self, envelope: ChatV2RuntimeRelayEnvelope) -> None:
        loop = asyncio.get_running_loop()
        self._seen[envelope.event_id] = (
            loop.time() + self._state_ttl,
            envelope.source_revision,
            envelope.kind,
            envelope.conversation_id,
            self._generation_key(envelope),
        )
        self._seen.move_to_end(envelope.event_id)
        while len(self._seen) > self._state_max_entries:
            self._seen.popitem(last=False)

    def _expire_seen(self) -> None:
        now = asyncio.get_running_loop().time()
        for event_id, (expires_at, _, __, ___, ____) in list(self._seen.items()):
            if expires_at > now:
                break
            self._seen.pop(event_id, None)

    @staticmethod
    def _generation_key(envelope: ChatV2RuntimeRelayEnvelope) -> tuple[str, ...]:
        return (
            envelope.conversation_id,
            envelope.session_id,
            envelope.turn_id,
            envelope.direct_request_id,
            envelope.owner.controller_id,
            envelope.owner.incarnation_id,
            str(envelope.fencing_token),
            envelope.origin.controller_id,
            envelope.origin.incarnation_id,
            envelope.origin.runtime_epoch,
        )

    @staticmethod
    def _matches_context(
        envelope: ChatV2RuntimeRelayEnvelope,
        context: RelayGenerationContext,
    ) -> AdmissionDecision:
        if envelope.fencing_token != context.fencing_token:
            return AdmissionDecision.WRONG_FENCE
        if (
            envelope.conversation_id != context.conversation_id
            or envelope.session_id != context.session_id
            or envelope.turn_id != context.turn_id
            or envelope.direct_request_id != context.direct_request_id
            or envelope.owner.controller_id != context.owner_controller_id
            or envelope.owner.incarnation_id != context.owner_incarnation_id
        ):
            return AdmissionDecision.WRONG_TURN
        return AdmissionDecision.ACCEPT


def _drop(reason: str) -> None:
    if reason not in DROP_REASONS:
        raise ValueError(f"unsupported relay drop reason: {reason}")
    _METRICS.dropped(reason)


def _codec(outcome: str) -> None:
    if outcome not in CODEC_OUTCOMES:
        raise ValueError(f"unsupported relay codec outcome: {outcome}")
    _METRICS.codec(outcome)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _encode_wire_sync(
    envelope: ChatV2RuntimeRelayEnvelope,
    raw: bytes,
) -> bytes | None:
    if len(raw) > MAX_RAW_PAYLOAD_BYTES:
        return None
    if len(raw) <= MAX_PAYLOAD_BYTES:
        _codec("legacy")
        return raw
    compressed = zlib.compress(raw, level=COMPRESSION_LEVEL)
    frame = _CompressedRelayWireFrame.from_envelope(
        envelope,
        raw=raw,
        compressed=compressed,
    )
    wire = frame.model_dump_json().encode()
    if len(wire) > MAX_PAYLOAD_BYTES:
        return None
    _codec("compressed")
    return wire


def _decode_wire_sync(payload: bytes) -> ChatV2RuntimeRelayEnvelope | None:
    if len(payload) > MAX_PAYLOAD_BYTES:
        return None
    try:
        value = json.loads(payload, object_pairs_hook=_strict_object)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if "wire_version" not in value:
        try:
            envelope = ChatV2RuntimeRelayEnvelope.model_validate_json(
                json.dumps(value, separators=(",", ":"))
            )
        except (RecursionError, ValidationError):
            return None
        _codec("legacy")
        return envelope
    try:
        frame = _CompressedRelayWireFrame.model_validate_json(
            json.dumps(value, separators=(",", ":"))
        )
        compressed = base64.b64decode(frame.payload_b64, validate=True)
    except (RecursionError, ValidationError, ValueError, binascii.Error):
        return None
    raw = _bounded_decompress(compressed)
    if raw is None or len(raw) != frame.raw_size:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, frame.raw_sha256):
        return None
    try:
        decoded = json.loads(raw, object_pairs_hook=_strict_object)
        if not isinstance(decoded, dict):
            return None
        envelope = ChatV2RuntimeRelayEnvelope.model_validate_json(
            json.dumps(decoded, separators=(",", ":"))
        )
    except (
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ):
        return None
    if not frame.matches(envelope):
        return None
    _codec("compressed")
    return envelope


def _bounded_decompress(compressed: bytes) -> bytes | None:
    if not compressed:
        return None
    decompressor = zlib.decompressobj()
    output = bytearray()
    try:
        for offset in range(0, len(compressed), DECOMPRESSION_CHUNK_BYTES):
            chunk = compressed[offset : offset + DECOMPRESSION_CHUNK_BYTES]
            remaining = MAX_RAW_PAYLOAD_BYTES - len(output)
            output.extend(decompressor.decompress(chunk, remaining + 1))
            if len(output) > MAX_RAW_PAYLOAD_BYTES or decompressor.unconsumed_tail:
                return None
        remaining = MAX_RAW_PAYLOAD_BYTES - len(output)
        output.extend(decompressor.flush(remaining + 1))
    except (MemoryError, zlib.error):
        return None
    if (
        len(output) > MAX_RAW_PAYLOAD_BYTES
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        return None
    return bytes(output)


def envelope_from_json(payload: str | bytes) -> ChatV2RuntimeRelayEnvelope:
    """Parse a relay envelope while retaining the same byte-size contract."""

    encoded = payload.encode() if isinstance(payload, str) else payload
    return ChatV2RuntimeRelayEnvelope.decode(encoded)


def envelope_to_json(envelope: ChatV2RuntimeRelayEnvelope) -> str:
    """Encode a relay envelope to deterministic compact JSON."""

    return json.dumps(
        envelope.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )
