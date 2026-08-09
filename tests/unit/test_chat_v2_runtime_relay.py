from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from cognis.api.chat_v2.schemas import MessageTimelineItem, RuntimeActiveTurn
from cognis.core import chat_v2_runtime_relay as relay_module
from cognis.core.chat_v2_runtime_relay import (
    ACTIVE_TTL_SECONDS,
    ADMIT_AND_PUBLISH_LUA,
    CODEC_MAX_PENDING,
    CODEC_OUTCOMES,
    DROP_REASONS,
    FUTURE_SKEW_SECONDS,
    MAX_PAYLOAD_BYTES,
    MAX_RAW_PAYLOAD_BYTES,
    MAX_REDIS_SAFE_INTEGER,
    RELAY_CHANNEL,
    TERMINAL_TTL_SECONDS,
    AdmissionDecision,
    ChatV2RuntimeRedisRelay,
    ChatV2RuntimeRelayEnvelope,
    RelayGenerationContext,
    RelayKind,
    RelayOrigin,
    RelayOwner,
    compare_admission,
)


class _FakePubSub:
    def __init__(self, messages: list[bytes] | None = None, *, subscribe_ok: bool = True) -> None:
        self.messages = list(messages or [])
        self.subscribe_ok = subscribe_ok
        self.closed = False
        self.subscriptions: list[str | bytes] = []

    async def subscribe(self, *channels: str | bytes) -> bool:
        self.subscriptions.extend(channels)
        return self.subscribe_ok

    async def listen(self) -> Any:
        for payload in self.messages:
            yield {"type": "message", "data": payload}

    async def aclose(self) -> None:
        self.closed = True


class _BlockingPubSub(_FakePubSub):
    def __init__(self) -> None:
        super().__init__()
        self.subscribe_started = asyncio.Event()

    async def subscribe(self, *channels: str | bytes) -> bool:
        self.subscriptions.extend(channels)
        self.subscribe_started.set()
        await asyncio.Event().wait()
        return True


class _FailingPubSub(_FakePubSub):
    async def listen(self) -> Any:
        if False:
            yield None
        raise ConnectionError


class _FakeRedis:
    def __init__(self) -> None:
        self.eval_results: list[Any] = [1]
        self.eval_calls: list[tuple[str | bytes, list[str | bytes], list[Any]]] = []
        self.get_value: bytes | None = None
        self.pubsubs: list[_FakePubSub | None] = []

    async def eval(
        self,
        script: str | bytes,
        *,
        keys: list[str | bytes],
        args: list[Any],
    ) -> Any:
        self.eval_calls.append((script, keys, args))
        result = self.eval_results.pop(0) if self.eval_results else 1
        if isinstance(result, Exception):
            raise result
        return result

    async def get(self, _key: str | bytes) -> bytes | None:
        return self.get_value

    def create_pubsub(self) -> _FakePubSub | None:
        if self.pubsubs:
            return self.pubsubs.pop(0)
        return None


class _AdmissionFakeRedis(_FakeRedis):
    """Focused eval harness mirroring the Lua script's result contract."""

    def __init__(self) -> None:
        super().__init__()
        self.current: ChatV2RuntimeRelayEnvelope | None = None

    async def eval(
        self,
        script: str | bytes,
        *,
        keys: list[str | bytes],
        args: list[Any],
    ) -> Any:
        self.eval_calls.append((script, keys, args))
        candidate = ChatV2RuntimeRelayEnvelope.decode(args[0])
        decision = compare_admission(self.current, candidate)
        result = {
            AdmissionDecision.ACCEPT: 1,
            AdmissionDecision.STALE: -1,
            AdmissionDecision.WRONG_TURN: -2,
            AdmissionDecision.WRONG_FENCE: -3,
        }[decision]
        if decision == AdmissionDecision.ACCEPT:
            self.current = candidate
        return result


class _RaisingMetric:
    def inc(self) -> None:
        raise RuntimeError("metrics unavailable")

    def set(self, _value: int) -> None:
        raise RuntimeError("metrics unavailable")

    def observe(self, _value: int) -> None:
        raise RuntimeError("metrics unavailable")

    def labels(self, **_labels: str) -> _RaisingMetric:
        raise RuntimeError("metrics unavailable")


async def _accept(_: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
    return AdmissionDecision.ACCEPT


async def _noop(_: ChatV2RuntimeRelayEnvelope) -> None:
    return None


def _context(*, fence: int = 7, turn: str = "turn-1") -> RelayGenerationContext:
    return RelayGenerationContext(
        direct_request_id="request-1",
        turn_id=turn,
        session_id="session-1",
        conversation_id="conversation-user@example.com",
        owner_controller_id="controller-owner",
        owner_incarnation_id="owner-incarnation",
        fencing_token=fence,
    )


def _relay(
    redis: _FakeRedis | None = None,
    *,
    apply: Callable[[ChatV2RuntimeRelayEnvelope], Awaitable[None]] = _noop,
    validator: Callable[
        [ChatV2RuntimeRelayEnvelope], Awaitable[AdmissionDecision | bool]
    ] = _accept,
    subscriber: Callable[[str], bool] = lambda _: True,
    queue_max_items: int = 8,
    queue_max_bytes: int = 2 * MAX_PAYLOAD_BYTES,
) -> ChatV2RuntimeRedisRelay:
    return ChatV2RuntimeRedisRelay(
        redis_service=redis or _FakeRedis(),  # type: ignore[arg-type]
        shared_secret="relay-shared-secret",
        controller_id="controller-local",
        incarnation_id="local-incarnation",
        durable_validator=validator,
        apply_callback=apply,
        has_subscriber=subscriber,
        queue_max_items=queue_max_items,
        queue_max_bytes=queue_max_bytes,
        reconnect_min_seconds=0.001,
        reconnect_max_seconds=0.002,
    )


def _envelope(
    *,
    kind: RelayKind = RelayKind.RUNTIME,
    fence: int = 7,
    revision: int = 1,
    turn: str = "turn-1",
    request: str = "request-1",
    epoch: str = "remote-epoch",
    event_id: str | None = None,
    generated_at: datetime | None = None,
    active: bool | None = None,
) -> ChatV2RuntimeRelayEnvelope:
    has_active = kind == RelayKind.RUNTIME if active is None else active
    return ChatV2RuntimeRelayEnvelope(
        kind=kind,
        event_id=event_id or f"event-{fence}-{revision}-{turn}-{kind}",
        generated_at=generated_at or datetime.now(UTC),
        origin=RelayOrigin(
            controller_id="controller-remote",
            incarnation_id="remote-incarnation",
            runtime_epoch=epoch,
        ),
        conversation_id="conversation-user@example.com",
        session_id="session-1",
        turn_id=turn,
        direct_request_id=request,
        owner=RelayOwner(
            controller_id="controller-owner",
            incarnation_id="owner-incarnation",
        ),
        fencing_token=fence,
        source_revision=revision,
        has_active_turn=has_active,
        active_turn=(
            RuntimeActiveTurn(turn_id=turn, session_id="session-1", status="running")
            if has_active
            else None
        ),
        volatile_items=[],
    )


def _large_envelope(content: str) -> ChatV2RuntimeRelayEnvelope:
    return _envelope().model_copy(
        update={
            "volatile_items": [
                MessageTimelineItem(
                    id="volatile-message",
                    sort_key="runtime:1",
                    stable=False,
                    role="assistant",
                    content=content,
                    message_id="message-1",
                    turn_id="turn-1",
                )
            ]
        }
    )


def _wire_wrapper(
    envelope: ChatV2RuntimeRelayEnvelope,
    *,
    raw: bytes | None = None,
    compressed: bytes | None = None,
) -> dict[str, Any]:
    raw = raw or envelope.raw_encoded()
    compressed = compressed or zlib.compress(raw, level=1)
    return {
        "wire_version": 2,
        "codec": "zlib",
        "raw_size": len(raw),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "payload_b64": base64.b64encode(compressed).decode(),
        "kind": envelope.kind,
        "direct_request_id": envelope.direct_request_id,
        "turn_id": envelope.turn_id,
        "owner": envelope.owner.model_dump(mode="json"),
        "origin": envelope.origin.model_dump(mode="json"),
        "fencing_token": envelope.fencing_token,
        "source_revision": envelope.source_revision,
    }


def _wire_bytes(wrapper: dict[str, Any]) -> bytes:
    return json.dumps(wrapper, separators=(",", ":")).encode()


def test_envelope_is_strict_coherent_utc_and_size_bounded() -> None:
    envelope = _envelope()
    assert ChatV2RuntimeRelayEnvelope.decode(envelope.encoded()) == envelope

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChatV2RuntimeRelayEnvelope.model_validate(
            {**envelope.model_dump(), "unknown_field": "rejected"}
        )
    with pytest.raises(ValidationError, match="generated_at must be UTC"):
        ChatV2RuntimeRelayEnvelope.model_validate(
            {**envelope.model_dump(), "generated_at": datetime.now()}
        )
    with pytest.raises(ValidationError, match="active_turn.turn_id"):
        ChatV2RuntimeRelayEnvelope.model_validate(
            {
                **envelope.model_dump(),
                "active_turn": {
                    "turn_id": "other-turn",
                    "session_id": "session-1",
                    "status": "running",
                },
            }
        )
    with pytest.raises(ValueError, match="512KiB"):
        ChatV2RuntimeRelayEnvelope.decode(b"{" + b"x" * MAX_PAYLOAD_BYTES + b"}")
    assert _envelope(fence=MAX_REDIS_SAFE_INTEGER, revision=MAX_REDIS_SAFE_INTEGER)
    with pytest.raises(ValidationError):
        _envelope(fence=MAX_REDIS_SAFE_INTEGER + 1)
    with pytest.raises(ValidationError):
        _envelope(revision=MAX_REDIS_SAFE_INTEGER + 1)
    assert _context(fence=MAX_REDIS_SAFE_INTEGER)
    with pytest.raises(ValueError, match="Redis-safe"):
        _context(fence=MAX_REDIS_SAFE_INTEGER + 1)


def test_wire_codec_preserves_legacy_bytes_and_compresses_large_envelope() -> None:
    small = _envelope()
    legacy = small.encoded()
    assert relay_module._encode_wire_sync(small, small.raw_encoded()) == legacy
    assert relay_module._decode_wire_sync(legacy) == small

    large = _large_envelope("x" * (MAX_PAYLOAD_BYTES + 128 * 1024))
    raw = large.raw_encoded()
    assert len(raw) > MAX_PAYLOAD_BYTES
    wire = relay_module._encode_wire_sync(large, raw)
    assert wire is not None
    assert len(wire) <= MAX_PAYLOAD_BYTES
    assert wire != raw
    assert relay_module._decode_wire_sync(wire) == large
    with pytest.raises(ValidationError):
        ChatV2RuntimeRelayEnvelope.model_validate_json(wire)


def test_wire_codec_rejects_incompressible_stored_oversized_and_raw_ceiling() -> None:
    incompressible = base64.b64encode(os.urandom(600 * 1024)).decode()
    envelope = _large_envelope(incompressible)
    raw = envelope.raw_encoded()
    assert len(raw) > MAX_PAYLOAD_BYTES
    assert relay_module._encode_wire_sync(envelope, raw) is None

    above_ceiling = _large_envelope("x" * (MAX_RAW_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="2MiB"):
        above_ceiling.raw_encoded()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "truncated",
        "trailing",
        "hash",
        "metadata",
        "base64",
        "size",
    ],
)
def test_compressed_wire_rejects_malformed_integrity_and_metadata(
    mutation: str,
) -> None:
    envelope = _large_envelope("x" * (MAX_PAYLOAD_BYTES + 64 * 1024))
    wrapper = _wire_wrapper(envelope)
    if mutation == "unknown":
        wrapper["unknown"] = True
    elif mutation == "truncated":
        compressed = base64.b64decode(wrapper["payload_b64"])
        wrapper["payload_b64"] = base64.b64encode(compressed[:-1]).decode()
    elif mutation == "trailing":
        compressed = base64.b64decode(wrapper["payload_b64"])
        wrapper["payload_b64"] = base64.b64encode(compressed + b"trailing").decode()
    elif mutation == "hash":
        wrapper["raw_sha256"] = "0" * 64
    elif mutation == "metadata":
        wrapper["source_revision"] = envelope.source_revision + 1
    elif mutation == "base64":
        wrapper["payload_b64"] = "@@@"
    elif mutation == "size":
        wrapper["raw_size"] = wrapper["raw_size"] - 1
    assert relay_module._decode_wire_sync(_wire_bytes(wrapper)) is None


def test_wire_decode_rejects_duplicate_keys_and_decompression_bomb() -> None:
    legacy = _envelope().encoded()
    duplicate = legacy[:-1] + b',"version":1}'
    assert relay_module._decode_wire_sync(duplicate) is None

    envelope = _envelope()
    bomb_raw = b"x" * (MAX_RAW_PAYLOAD_BYTES + 1)
    wrapper = _wire_wrapper(
        envelope,
        raw=envelope.raw_encoded(),
        compressed=zlib.compress(bomb_raw, level=1),
    )
    wrapper["raw_size"] = MAX_RAW_PAYLOAD_BYTES
    assert len(_wire_bytes(wrapper)) <= MAX_PAYLOAD_BYTES
    assert relay_module._decode_wire_sync(_wire_bytes(wrapper)) is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"fence": 8}, AdmissionDecision.ACCEPT),
        ({"fence": 6}, AdmissionDecision.WRONG_FENCE),
        ({"turn": "turn-2"}, AdmissionDecision.WRONG_TURN),
        ({"request": "request-2"}, AdmissionDecision.WRONG_TURN),
        ({"epoch": "new-epoch"}, AdmissionDecision.STALE),
        ({"revision": 1}, AdmissionDecision.STALE),
        ({"revision": 2}, AdmissionDecision.ACCEPT),
    ],
)
def test_pure_admission_ordering_matrix(
    change: dict[str, Any], expected: AdmissionDecision
) -> None:
    assert compare_admission(_envelope(), _envelope(**change)) == expected


def test_terminal_precedence_and_higher_fence_replaces_tombstone() -> None:
    terminal = _envelope(kind=RelayKind.TERMINAL)
    assert compare_admission(terminal, _envelope(revision=2)) == AdmissionDecision.STALE
    assert (
        compare_admission(
            terminal,
            _envelope(kind=RelayKind.TERMINAL, revision=2),
        )
        == AdmissionDecision.ACCEPT
    )
    assert compare_admission(terminal, _envelope(fence=8)) == AdmissionDecision.ACCEPT
    assert (
        compare_admission(
            _envelope(revision=10),
            _envelope(kind=RelayKind.TERMINAL, revision=1),
        )
        == AdmissionDecision.ACCEPT
    )


def test_origin_epoch_is_stable_and_revision_is_per_exact_context() -> None:
    relay = _relay()
    epoch = relay.origin.runtime_epoch
    assert relay.next_revision(_context()) == 1
    assert relay.next_revision(_context()) == 2
    assert relay.next_revision(_context(turn="turn-2")) == 1
    assert relay.origin.runtime_epoch == epoch
    key = next(iter(relay._revisions))  # noqa: SLF001
    relay._revisions[key] = MAX_REDIS_SAFE_INTEGER  # noqa: SLF001
    with pytest.raises(ValueError, match="source_revision"):
        relay.next_revision(_context())


def test_hmac_latest_key_hides_raw_conversation_identifier() -> None:
    relay = _relay()
    key = relay.latest_key("private-user@example.com")
    assert key.startswith(f"{RELAY_CHANNEL}:latest:")
    assert "private-user" not in key
    assert "@" not in key
    assert key == relay.latest_key("private-user@example.com")
    assert key != _relay().latest_key("another-user@example.com")


def test_nonblocking_queue_coalesces_progress_and_obeys_count_and_byte_bounds() -> None:
    relay = _relay(queue_max_items=2, queue_max_bytes=1800)
    assert relay.enqueue(_envelope(revision=1))
    assert relay.enqueue(_envelope(revision=2))
    assert len(relay._queue.items) == 1  # noqa: SLF001
    assert relay._queue.items[0].envelope.source_revision == 2  # noqa: SLF001

    other = _envelope(turn="turn-2", revision=1)
    other = other.model_copy(update={"conversation_id": "conversation-2"})
    assert relay.enqueue(other)
    third = _envelope(turn="turn-3", revision=1)
    third = third.model_copy(update={"conversation_id": "conversation-3"})
    relay.enqueue(third)
    assert len(relay._queue.items) <= 2  # noqa: SLF001
    assert relay._queue.bytes <= 1800  # noqa: SLF001


def test_boundary_and_terminal_preserve_latest_corresponding_progress() -> None:
    relay = _relay(queue_max_items=2, queue_max_bytes=1800)
    relay.enqueue(_envelope(revision=1))
    relay.enqueue(_envelope(revision=2), cumulative_boundary=True)
    assert [item.envelope.source_revision for item in relay._queue.items] == [1, 2]  # noqa: SLF001

    relay = _relay(queue_max_items=2, queue_max_bytes=1800)
    relay.enqueue(_envelope(revision=3))
    relay.enqueue(_envelope(kind=RelayKind.TERMINAL, revision=4))
    queued = list(relay._queue.items)  # noqa: SLF001
    assert [(item.envelope.source_revision, item.envelope.kind) for item in queued] == [
        (3, RelayKind.RUNTIME),
        (4, RelayKind.TERMINAL),
    ]


def test_runtime_overflow_never_evicts_terminal() -> None:
    relay = _relay(queue_max_items=1)
    terminal = _envelope(kind=RelayKind.TERMINAL, revision=1)
    assert relay.enqueue(terminal)

    runtime = _envelope(turn="turn-2", request="request-2", revision=1)
    assert not relay.enqueue(runtime)
    assert [item.envelope for item in relay._queue.items] == [terminal]  # noqa: SLF001


def test_terminal_overflow_evicts_progress_before_boundary() -> None:
    relay = _relay(queue_max_items=2)
    progress = _envelope(revision=1)
    boundary = _envelope(turn="turn-2", request="request-2", revision=1)
    terminal = _envelope(
        kind=RelayKind.TERMINAL,
        turn="turn-3",
        request="request-3",
        revision=1,
    )
    assert relay.enqueue(progress)
    assert relay.enqueue(boundary, cumulative_boundary=True)

    assert relay.enqueue(terminal)
    queued = [item.envelope for item in relay._queue.items]  # noqa: SLF001
    assert queued == [boundary, terminal]


def test_runtime_boundary_cannot_coalesce_queued_terminal() -> None:
    relay = _relay()
    terminal = _envelope(kind=RelayKind.TERMINAL, revision=2)
    assert relay.enqueue(terminal)

    assert relay.enqueue(_envelope(revision=3), cumulative_boundary=True)
    queued = list(relay._queue.items)  # noqa: SLF001
    assert [(item.envelope.kind, item.envelope.source_revision) for item in queued] == [
        (RelayKind.TERMINAL, 2),
        (RelayKind.RUNTIME, 3),
    ]


def test_newer_terminal_supersedes_terminal_only_within_exact_generation() -> None:
    relay = _relay()
    assert relay.enqueue(_envelope(kind=RelayKind.TERMINAL, revision=1))
    assert relay.enqueue(_envelope(kind=RelayKind.TERMINAL, revision=2))
    assert [item.envelope.source_revision for item in relay._queue.items] == [2]  # noqa: SLF001

    other = _envelope(kind=RelayKind.TERMINAL, turn="turn-2", request="request-2")
    assert relay.enqueue(other)
    assert len(relay._queue.items) == 2  # noqa: SLF001


def test_failed_terminal_replacement_preserves_previous_terminal() -> None:
    relay = _relay(queue_max_items=4)
    old = _envelope(kind=RelayKind.TERMINAL, revision=1)
    other = _envelope(kind=RelayKind.TERMINAL, turn="turn-2", request="request-2")
    candidate = _large_envelope("x" * 400).model_copy(
        update={
            "kind": RelayKind.TERMINAL,
            "source_revision": 2,
            "has_active_turn": False,
            "active_turn": None,
        }
    )
    assert relay.enqueue(old)
    assert relay.enqueue(other)
    relay._queue.max_bytes = len(candidate.raw_encoded()) + len(other.raw_encoded()) - 1  # noqa: SLF001

    assert not relay.enqueue(candidate)
    assert [item.envelope for item in relay._queue.items] == [old, other]  # noqa: SLF001


def test_progress_is_coalesced_only_within_boundary_delimited_generation_segment() -> None:
    relay = _relay()
    relay.enqueue(_envelope(revision=1))
    relay.enqueue(_envelope(revision=2), cumulative_boundary=True)
    relay.enqueue(_envelope(revision=3))
    relay.enqueue(_envelope(revision=4))
    assert [item.envelope.source_revision for item in relay._queue.items] == [1, 2, 4]  # noqa: SLF001


@pytest.mark.asyncio
async def test_publisher_compresses_large_envelope_and_receiver_and_hydration_share_decode() -> (
    None
):
    redis = _FakeRedis()
    applied: list[str] = []

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        applied.append(envelope.event_id)

    relay = _relay(redis, apply=apply, queue_max_bytes=3 * MAX_RAW_PAYLOAD_BYTES)
    envelope = _large_envelope("x" * (MAX_PAYLOAD_BYTES + 128 * 1024))
    assert relay.enqueue(envelope)
    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=2)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)

    wire = redis.eval_calls[0][2][0]
    assert isinstance(wire, bytes)
    assert len(wire) <= MAX_PAYLOAD_BYTES
    assert await relay.receive(wire)
    redis.get_value = wire
    hydrated = await _relay(redis).hydrate_latest(_context())
    assert hydrated == envelope
    assert applied == [envelope.event_id]


@pytest.mark.asyncio
async def test_publisher_drops_incompressible_stored_oversized_without_redis_io() -> None:
    redis = _FakeRedis()
    relay = _relay(redis, queue_max_bytes=3 * MAX_RAW_PAYLOAD_BYTES)
    envelope = _large_envelope(base64.b64encode(os.urandom(600 * 1024)).decode())
    assert relay.enqueue(envelope)
    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=2)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)
    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_receive_and_hydration_reject_the_same_malformed_compressed_wire() -> None:
    redis = _FakeRedis()
    relay = _relay(redis)
    envelope = _large_envelope("x" * (MAX_PAYLOAD_BYTES + 64 * 1024))
    wrapper = _wire_wrapper(envelope)
    wrapper["raw_sha256"] = "0" * 64
    wire = _wire_bytes(wrapper)
    redis.get_value = wire
    assert not await relay.receive(wire)
    assert await relay.hydrate_latest(_context()) is None


@pytest.mark.asyncio
async def test_codec_admission_is_bounded_and_cancellation_releases_capacity() -> None:
    relay = _relay()
    entered = 0
    entered_lock = threading.Lock()
    release = threading.Event()

    def blocked() -> int:
        nonlocal entered
        with entered_lock:
            entered += 1
        release.wait(timeout=2)
        return 1

    calls = [asyncio.create_task(relay._run_codec(blocked)) for _ in range(CODEC_MAX_PENDING)]  # noqa: SLF001
    while relay._codec_admitted < CODEC_MAX_PENDING:  # noqa: SLF001
        await asyncio.sleep(0)
    while entered < relay_module.CODEC_WORKERS:
        await asyncio.sleep(0)
    assert entered == relay_module.CODEC_WORKERS
    dropped_before = relay_module.RELAY_DROPPED.labels(reason="codec_saturated")._value.get()  # noqa: SLF001
    codec_before = relay_module.RELAY_CODEC.labels(outcome="saturated")._value.get()  # noqa: SLF001
    assert not await relay.receive(_envelope().encoded())
    assert (
        relay_module.RELAY_DROPPED.labels(reason="codec_saturated")._value.get()  # noqa: SLF001
        == dropped_before + 1
    )
    assert (
        relay_module.RELAY_CODEC.labels(outcome="saturated")._value.get()  # noqa: SLF001
        == codec_before + 1
    )

    calls[-1].cancel()
    await asyncio.gather(calls[-1], return_exceptions=True)
    assert relay._codec_admitted == CODEC_MAX_PENDING - 1  # noqa: SLF001
    release.set()
    await asyncio.gather(*calls[:-1])
    assert relay._codec_admitted == 0  # noqa: SLF001
    assert not relay._codec_tasks  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancelled_codec_caller_does_not_orphan_worker_task() -> None:
    relay = _relay()
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> int:
        entered.set()
        release.wait(timeout=2)
        return 1

    caller = asyncio.create_task(relay._run_codec(blocked))  # noqa: SLF001
    await asyncio.to_thread(entered.wait, 1)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    assert relay._codec_tasks  # noqa: SLF001
    release.set()
    await asyncio.gather(*list(relay._codec_tasks))  # noqa: SLF001
    assert relay._codec_admitted == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_codec_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = _relay()
    entered = threading.Event()
    release = threading.Event()
    original = relay_module._encode_wire_sync

    def blocked(
        envelope: ChatV2RuntimeRelayEnvelope,
        raw: bytes,
    ) -> bytes | None:
        entered.set()
        release.wait(timeout=2)
        return original(envelope, raw)

    monkeypatch.setattr(relay_module, "_encode_wire_sync", blocked)
    await relay.start()
    assert relay.enqueue(_envelope())
    await asyncio.to_thread(entered.wait, 1)
    stopping = asyncio.create_task(relay.stop(drain_timeout_seconds=0.01))
    await asyncio.sleep(0.02)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    assert not relay._codec_tasks  # noqa: SLF001
    assert relay._codec_admitted == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_receive_ignores_same_origin_duplicate_regression_and_stale_without_republish() -> (
    None
):
    redis = _FakeRedis()
    applied: list[ChatV2RuntimeRelayEnvelope] = []

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        applied.append(envelope)

    relay = _relay(redis, apply=apply)
    same_origin = _envelope().model_copy(update={"origin": relay.origin})
    assert not await relay.receive(same_origin.encoded())
    assert await relay.receive(_envelope(event_id="accepted", revision=2).encoded())
    assert not await relay.receive(_envelope(event_id="accepted", revision=2).encoded())
    assert not await relay.receive(_envelope(event_id="regressed", revision=1).encoded())
    assert await relay.receive(_envelope(event_id="new-fence", fence=8, revision=1).encoded())
    stale = _envelope(
        event_id="old",
        revision=3,
        generated_at=datetime.now(UTC) - timedelta(seconds=31),
    )
    assert not await relay.receive(stale.encoded())
    future = _envelope(
        event_id="future",
        revision=4,
        generated_at=datetime.now(UTC) + timedelta(seconds=FUTURE_SKEW_SECONDS + 1),
    )
    assert not await relay.receive(future.encoded())
    assert [item.event_id for item in applied] == ["accepted", "new-fence"]
    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_receive_terminal_outranks_runtime_without_regressing_terminal_revision() -> None:
    applied: list[tuple[RelayKind, int]] = []

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        applied.append((envelope.kind, envelope.source_revision))

    relay = _relay(apply=apply)
    assert await relay.receive(_envelope(revision=10).encoded())
    assert await relay.receive(_envelope(kind=RelayKind.TERMINAL, revision=5).encoded())
    assert not await relay.receive(
        _envelope(
            kind=RelayKind.TERMINAL,
            revision=4,
            event_id="regressing-terminal",
        ).encoded()
    )
    assert await relay.receive(
        _envelope(
            kind=RelayKind.TERMINAL,
            revision=6,
            event_id="newer-terminal",
        ).encoded()
    )
    assert not await relay.receive(_envelope(revision=11, event_id="late-runtime").encoded())
    assert applied == [
        (RelayKind.RUNTIME, 10),
        (RelayKind.TERMINAL, 5),
        (RelayKind.TERMINAL, 6),
    ]


@pytest.mark.asyncio
async def test_publisher_eval_contract_preserves_terminal_precedence() -> None:
    redis = _AdmissionFakeRedis()
    relay = _relay(redis)
    relay.enqueue(_envelope(revision=10))
    relay.enqueue(_envelope(kind=RelayKind.TERMINAL, revision=1))
    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=1)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)
    assert redis.current is not None
    assert redis.current.kind == RelayKind.TERMINAL
    assert len(redis.eval_calls) == 2


@pytest.mark.asyncio
async def test_no_subscriber_short_circuits_durable_validation() -> None:
    validated = False

    async def validator(_: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
        nonlocal validated
        validated = True
        return AdmissionDecision.ACCEPT

    relay = _relay(validator=validator, subscriber=lambda _: False)
    assert not await relay.receive(_envelope().encoded())
    assert not validated


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [AdmissionDecision.WRONG_TURN, AdmissionDecision.WRONG_FENCE])
async def test_receive_maps_wrong_turn_and_fence(
    decision: AdmissionDecision,
) -> None:
    async def reject(_: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
        return decision

    relay = _relay(validator=reject)
    assert not await relay.receive(_envelope().encoded())


@pytest.mark.asyncio
async def test_boundary_publish_failure_retries_in_place_and_ttls_are_selected() -> None:
    redis = _FakeRedis()
    redis.eval_results = [RuntimeError("redis down"), 1]
    relay = _relay(redis)
    relay.enqueue(_envelope(), cumulative_boundary=True)
    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=1)  # noqa: SLF001
    relay.enqueue(_envelope(kind=RelayKind.TERMINAL, revision=2))
    await asyncio.wait_for(relay._drain(), timeout=1)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)

    assert len(redis.eval_calls) == 3
    assert redis.eval_calls[0][2][1] == ACTIVE_TTL_SECONDS
    assert redis.eval_calls[1][2][1] == ACTIVE_TTL_SECONDS
    assert redis.eval_calls[2][2][1] == TERMINAL_TTL_SECONDS
    assert redis.eval_calls[0][2][0] == redis.eval_calls[1][2][0]
    assert redis.eval_calls[0][0] == ADMIT_AND_PUBLISH_LUA
    assert redis.eval_calls[0][2][2] == RELAY_CHANNEL


@pytest.mark.asyncio
async def test_progress_publish_failure_does_not_block_next_conversation() -> None:
    redis = _FakeRedis()
    redis.eval_results = [RuntimeError("redis down"), 1]
    relay = _relay(redis)
    first = _envelope()
    second = first.model_copy(
        update={
            "event_id": "other-conversation",
            "conversation_id": "conversation-other@example.com",
            "source_revision": 2,
        }
    )
    relay.enqueue(first)
    relay.enqueue(second)

    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=1)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)

    assert len(redis.eval_calls) == 2
    published = ChatV2RuntimeRelayEnvelope.decode(redis.eval_calls[1][2][0])
    assert published.conversation_id == "conversation-other@example.com"


@pytest.mark.asyncio
async def test_blocked_progress_publish_is_time_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockedOnceRedis(_FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes],
            args: list[Any],
        ) -> Any:
            self.eval_calls.append((script, keys, args))
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return 1

    monkeypatch.setattr(relay_module, "PROGRESS_PUBLISH_TIMEOUT_SECONDS", 0.01)
    redis = _BlockedOnceRedis()
    relay = _relay(redis)
    first = _envelope()
    relay.enqueue(first)
    relay.enqueue(
        first.model_copy(
            update={
                "event_id": "other-conversation",
                "conversation_id": "conversation-other@example.com",
                "source_revision": 2,
            }
        )
    )

    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=0.2)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)

    assert redis.calls == 2


@pytest.mark.asyncio
async def test_publisher_does_not_attempt_expired_envelope() -> None:
    redis = _FakeRedis()
    relay = _relay(redis)
    expired = _envelope(generated_at=datetime.now(UTC) - timedelta(seconds=31))
    assert relay.enqueue(expired)
    publisher = asyncio.create_task(relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(relay._drain(), timeout=1)  # noqa: SLF001
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)

    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_receiver_reconnects_and_applies_without_republishing() -> None:
    redis = _FakeRedis()
    consumers = [
        _FakePubSub(subscribe_ok=False),
        _FakePubSub([_envelope().encoded()]),
    ]
    redis.pubsubs = list(consumers)
    applied = asyncio.Event()

    async def apply(_: ChatV2RuntimeRelayEnvelope) -> None:
        applied.set()

    relay = _relay(redis, apply=apply)
    receiver = asyncio.create_task(relay._receiver_loop())  # noqa: SLF001
    await asyncio.wait_for(applied.wait(), timeout=1)
    relay._stop.set()  # noqa: SLF001
    receiver.cancel()
    await asyncio.gather(receiver, return_exceptions=True)
    assert redis.eval_calls == []
    assert all(pubsub.closed for pubsub in consumers)


@pytest.mark.asyncio
async def test_receiver_validates_different_conversations_concurrently() -> None:
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def validator(envelope: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
        entered.add(envelope.conversation_id)
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return AdmissionDecision.ACCEPT

    relay = _relay(validator=validator)
    first = _envelope(event_id="first")
    second = _envelope(turn="turn-2", request="request-2", event_id="second").model_copy(
        update={"conversation_id": "conversation-2"}
    )

    await relay._schedule_receive(first.encoded())  # noqa: SLF001
    await relay._schedule_receive(second.encoded())  # noqa: SLF001
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    await asyncio.gather(*list(relay._receiver_deliveries))  # noqa: SLF001

    assert entered == {"conversation-user@example.com", "conversation-2"}


@pytest.mark.asyncio
async def test_receiver_preserves_order_within_one_conversation() -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[int] = []

    async def validator(envelope: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
        order.append(envelope.source_revision)
        if envelope.source_revision == 1:
            first_entered.set()
            await release_first.wait()
        return AdmissionDecision.ACCEPT

    relay = _relay(validator=validator)
    await relay._schedule_receive(_envelope(revision=1).encoded())  # noqa: SLF001
    await first_entered.wait()
    await relay._schedule_receive(_envelope(revision=2).encoded())  # noqa: SLF001
    await asyncio.sleep(0)

    assert order == [1]
    release_first.set()
    await asyncio.gather(*list(relay._receiver_deliveries))  # noqa: SLF001
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_receiver_reconnects_after_subscribed_connection_failure() -> None:
    redis = _FakeRedis()
    consumers = [
        _FailingPubSub(),
        _FakePubSub([_envelope().encoded()]),
    ]
    redis.pubsubs = list(consumers)
    applied = asyncio.Event()

    async def apply(_: ChatV2RuntimeRelayEnvelope) -> None:
        applied.set()

    relay = _relay(redis, apply=apply)
    receiver = asyncio.create_task(relay._receiver_loop())  # noqa: SLF001
    await asyncio.wait_for(applied.wait(), timeout=1)
    relay._stop.set()  # noqa: SLF001
    receiver.cancel()
    await asyncio.gather(receiver, return_exceptions=True)

    assert all(pubsub.closed for pubsub in consumers)
    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_failed_subscriptions_close_every_created_consumer() -> None:
    redis = _FakeRedis()
    consumers = [_FakePubSub(subscribe_ok=False) for _ in range(3)]
    redis.pubsubs = list(consumers)
    relay = _relay(redis)
    receiver = asyncio.create_task(relay._receiver_loop())  # noqa: SLF001
    while redis.pubsubs:
        await asyncio.sleep(0)
    relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(receiver, timeout=1)
    assert all(consumer.closed for consumer in consumers)


@pytest.mark.asyncio
async def test_cancelled_subscription_closes_created_consumer() -> None:
    redis = _FakeRedis()
    consumer = _BlockingPubSub()
    redis.pubsubs = [consumer]
    relay = _relay(redis)
    receiver = asyncio.create_task(relay._receiver_loop())  # noqa: SLF001
    await consumer.subscribe_started.wait()
    receiver.cancel()
    await asyncio.gather(receiver, return_exceptions=True)
    assert consumer.closed


@pytest.mark.asyncio
async def test_stop_drain_waits_for_in_flight_eval() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockedRedis(_FakeRedis):
        async def eval(
            self,
            script: str | bytes,
            *,
            keys: list[str | bytes],
            args: list[Any],
        ) -> Any:
            entered.set()
            await release.wait()
            return 1

    relay = _relay(_BlockedRedis())
    await relay.start()
    relay.enqueue(_envelope())
    await entered.wait()
    stopping = asyncio.create_task(relay.stop(drain_timeout_seconds=1))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await stopping


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["validator", "apply"])
async def test_callback_exceptions_are_isolated_per_message(
    failure_at: str, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0
    applied: list[str] = []

    async def validator(_: ChatV2RuntimeRelayEnvelope) -> AdmissionDecision:
        nonlocal attempts
        attempts += 1
        if failure_at == "validator" and attempts == 1:
            raise RuntimeError("sensitive validator detail")
        return AdmissionDecision.ACCEPT

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        if failure_at == "apply" and not applied:
            applied.append("failed")
            raise RuntimeError("sensitive apply detail")
        applied.append(envelope.event_id)

    relay = _relay(validator=validator, apply=apply)
    assert not await relay.receive(_envelope(event_id="first").encoded())
    assert await relay.receive(_envelope(event_id="second", revision=2).encoded())
    assert applied[-1] == "second"
    assert "sensitive" not in caplog.text
    assert "first" not in caplog.text


@pytest.mark.asyncio
async def test_latest_hydration_validates_generation_tombstone_and_failures() -> None:
    redis = _FakeRedis()
    relay = _relay(redis)
    redis.get_value = _envelope(kind=RelayKind.TERMINAL).encoded()
    assert await relay.hydrate_latest(_context()) is not None
    assert await relay.hydrate_latest(_context(turn="turn-2")) is None
    assert await relay.hydrate_latest(_context(fence=8)) is None

    async def reject(_: ChatV2RuntimeRelayEnvelope) -> bool:
        return False

    assert await relay.hydrate_latest(_context(), durable_validator=reject) is None
    redis.get_value = None
    assert await relay.hydrate_latest(_context()) is None
    redis.get_value = b"not-json"
    assert await relay.hydrate_latest(_context()) is None


@pytest.mark.asyncio
async def test_latest_hydration_records_envelope_before_delayed_pubsub_delivery() -> None:
    redis = _FakeRedis()
    applied: list[str] = []

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        applied.append(envelope.event_id)

    relay = _relay(redis, apply=apply)
    payload = _envelope(event_id="hydrated").encoded()
    redis.get_value = payload

    hydrated = await relay.hydrate_latest(_context())

    assert hydrated is not None
    assert hydrated.event_id == "hydrated"
    assert not await relay.receive(payload)
    assert applied == []


def test_invalidation_clears_bounded_generation_state() -> None:
    relay = _relay()
    assert relay.next_revision(_context()) == 1
    relay.invalidate(_context().conversation_id)
    assert relay.next_revision(_context()) == 1


def test_metric_drop_reasons_are_exact_and_identity_free() -> None:
    assert {
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
    } == DROP_REASONS
    assert {
        "legacy",
        "compressed",
        "raw_oversized",
        "stored_oversized",
        "invalid",
        "saturated",
        "error",
    } == CODEC_OUTCOMES
    from cognis.core import chat_v2_runtime_relay as module

    metrics = [
        module.RELAY_CONNECTED,
        module.RELAY_ENQUEUED,
        module.RELAY_PUBLISHED,
        module.RELAY_RECEIVED,
        module.RELAY_APPLIED,
        module.RELAY_PUBLISH_ERRORS,
        module.RELAY_RECONNECTS,
        module.RELAY_DROPPED,
        module.RELAY_QUEUE_DEPTH,
        module.RELAY_PAYLOAD_BYTES,
        module.RELAY_RAW_PAYLOAD_BYTES,
        module.RELAY_CODEC,
    ]
    for metric in metrics:
        labelnames = set(metric._labelnames)  # noqa: SLF001
        assert not labelnames & {
            "conversation_id",
            "session_id",
            "turn_id",
            "request_id",
            "email",
            "user_id",
        }
    assert set(module.RELAY_DROPPED._labelnames) == {"reason"}  # noqa: SLF001
    assert set(module.RELAY_CODEC._labelnames) == {"outcome"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_all_relay_metrics_fail_open_without_changing_core_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "RELAY_CONNECTED",
        "RELAY_ENQUEUED",
        "RELAY_PUBLISHED",
        "RELAY_RECEIVED",
        "RELAY_APPLIED",
        "RELAY_PUBLISH_ERRORS",
        "RELAY_RECONNECTS",
        "RELAY_DROPPED",
        "RELAY_QUEUE_DEPTH",
        "RELAY_PAYLOAD_BYTES",
        "RELAY_RAW_PAYLOAD_BYTES",
        "RELAY_CODEC",
    ):
        monkeypatch.setattr(relay_module, name, _RaisingMetric())

    redis = _FakeRedis()
    applied: list[str] = []

    async def apply(envelope: ChatV2RuntimeRelayEnvelope) -> None:
        applied.append(envelope.event_id)

    publisher_relay = _relay(redis, apply=apply)
    envelope = _envelope(event_id="published")
    assert publisher_relay.enqueue(envelope)
    assert len(publisher_relay._queue.items) == 1  # noqa: SLF001
    publisher = asyncio.create_task(publisher_relay._publisher_loop())  # noqa: SLF001
    await asyncio.wait_for(publisher_relay._drain(), timeout=1)  # noqa: SLF001
    publisher_relay._stop.set()  # noqa: SLF001
    await asyncio.wait_for(publisher, timeout=1)
    assert len(redis.eval_calls) == 1

    receive_relay = _relay(apply=apply)
    received = _envelope(event_id="received", revision=2)
    assert await receive_relay.receive(received.encoded())
    assert applied == ["received"]

    hydrate_redis = _FakeRedis()
    hydrate_redis.get_value = _envelope(event_id="hydrated", revision=3).encoded()
    hydrate_relay = _relay(hydrate_redis)
    hydrated = await hydrate_relay.hydrate_latest(_context())
    assert hydrated is not None
    assert hydrated.event_id == "hydrated"

    stopping_relay = _relay()
    await stopping_relay.start()
    assert stopping_relay.enqueue(_envelope(event_id="stopping", revision=4))
    await stopping_relay.stop()
    assert stopping_relay._publisher_task is None  # noqa: SLF001
    assert stopping_relay._receiver_task is None  # noqa: SLF001
