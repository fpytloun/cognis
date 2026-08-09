from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from cognis.api.chat_v2.schemas import MessageTimelineItem, RuntimeActiveTurn
from cognis.core.chat_v2_runtime_relay import (
    ADMIT_AND_PUBLISH_LUA,
    MAX_PAYLOAD_BYTES,
    ChatV2RuntimeRelayEnvelope,
    RelayKind,
    RelayOrigin,
    RelayOwner,
    _decode_wire_sync,
    _encode_wire_sync,
)


class _RespConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, url: str) -> _RespConnection:
        parsed = urlparse(url)
        reader, writer = await asyncio.open_connection(
            parsed.hostname or "127.0.0.1",
            parsed.port or 6379,
        )
        connection = cls(reader, writer)
        database = int((parsed.path or "/0").removeprefix("/"))
        if database:
            assert await connection.command("SELECT", database) == b"OK"
        return connection

    async def command(self, *parts: str | bytes | int) -> object:
        encoded = [part if isinstance(part, bytes) else str(part).encode() for part in parts]
        payload = [f"*{len(encoded)}\r\n".encode()]
        for part in encoded:
            payload.extend((f"${len(part)}\r\n".encode(), part, b"\r\n"))
        self.writer.write(b"".join(payload))
        await self.writer.drain()
        return await self.read()

    async def read(self) -> object:
        marker = await self.reader.readexactly(1)
        line = await self.reader.readline()
        if marker == b"+":
            return line.rstrip(b"\r\n")
        if marker == b":":
            return int(line)
        if marker == b"$":
            length = int(line)
            if length == -1:
                return None
            value = await self.reader.readexactly(length)
            assert await self.reader.readexactly(2) == b"\r\n"
            return value
        if marker == b"*":
            return [await self.read() for _ in range(int(line))]
        if marker == b"-":
            raise RuntimeError("Redis command failed")
        raise RuntimeError("unsupported Redis response")

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


def _envelope(
    revision: int,
    *,
    kind: RelayKind = RelayKind.RUNTIME,
    fence: int = 7,
    large: bool = False,
) -> ChatV2RuntimeRelayEnvelope:
    return ChatV2RuntimeRelayEnvelope(
        kind=kind,
        event_id=f"event-{revision}-{kind}",
        generated_at=datetime.now(UTC),
        origin=RelayOrigin(
            controller_id="controller-a",
            incarnation_id="incarnation-a",
            runtime_epoch="epoch-a",
        ),
        conversation_id="conversation-a",
        session_id="session-a",
        turn_id="turn-a",
        direct_request_id="request-a",
        owner=RelayOwner(
            controller_id="controller-a",
            incarnation_id="incarnation-a",
        ),
        fencing_token=fence,
        source_revision=revision,
        has_active_turn=kind == RelayKind.RUNTIME,
        active_turn=(
            RuntimeActiveTurn(turn_id="turn-a", session_id="session-a", status="running")
            if kind == RelayKind.RUNTIME
            else None
        ),
        volatile_items=(
            [
                MessageTimelineItem(
                    id="message-a",
                    sort_key="runtime:1",
                    stable=False,
                    role="assistant",
                    content="x" * (MAX_PAYLOAD_BYTES + 64 * 1024),
                    message_id="message-a",
                    turn_id="turn-a",
                )
            ]
            if large
            else []
        ),
    )


@pytest.mark.asyncio
async def test_real_redis_lua_orders_legacy_and_compressed_frames_atomically() -> None:
    redis_url = os.environ.get("COGNIS_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("COGNIS_TEST_REDIS_URL is not configured")
    redis = await _RespConnection.connect(redis_url)
    subscriber = await _RespConnection.connect(redis_url)
    suffix = secrets.token_hex(8)
    key = f"test:chat-v2-runtime-relay:{suffix}:latest"
    channel = f"test:chat-v2-runtime-relay:{suffix}:channel"
    subscribed = await subscriber.command("SUBSCRIBE", channel)
    assert subscribed == [b"subscribe", channel.encode(), 1]
    try:
        legacy = _envelope(1).encoded()
        compressed_envelope = _envelope(2, large=True)
        compressed = _encode_wire_sync(
            compressed_envelope,
            compressed_envelope.raw_encoded(),
        )
        assert compressed is not None
        assert compressed != compressed_envelope.raw_encoded()

        assert await redis.command("EVAL", ADMIT_AND_PUBLISH_LUA, 1, key, legacy, 60, channel) == 1
        assert (
            await redis.command("EVAL", ADMIT_AND_PUBLISH_LUA, 1, key, compressed, 60, channel) == 1
        )
        stored = await redis.command("GET", key)
        assert isinstance(stored, bytes)
        assert stored == compressed
        assert _decode_wire_sync(stored) == compressed_envelope

        newer_legacy = _envelope(3).encoded()
        assert (
            await redis.command("EVAL", ADMIT_AND_PUBLISH_LUA, 1, key, newer_legacy, 60, channel)
            == 1
        )
        assert await redis.command("GET", key) == newer_legacy

        rejected = _envelope(4, fence=6).encoded()
        assert (
            await redis.command("EVAL", ADMIT_AND_PUBLISH_LUA, 1, key, rejected, 60, channel) == -3
        )
        assert await redis.command("GET", key) == newer_legacy

        messages: list[bytes] = []
        for _ in range(3):
            message = await asyncio.wait_for(subscriber.read(), timeout=1.0)
            assert isinstance(message, list)
            if len(message) == 3 and isinstance(message[2], bytes):
                messages.append(message[2])
        assert messages == [legacy, compressed, newer_legacy]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber.read(), timeout=0.1)
    finally:
        await redis.command("DEL", key)
        await subscriber.close()
        await redis.close()
