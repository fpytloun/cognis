"""Discord adapter via Bot Gateway WebSocket.

Uses the Discord Gateway for inbound events and the REST API for outbound
messages. Resume support is implemented so reconnects can continue an existing
session when Discord allows it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import random
import zlib
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import DISCORD_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json&compress=zlib-stream"
_ZLIB_SUFFIX = b"\x00\x00\xff\xff"

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

_INTENT_GUILDS = 1 << 0
_INTENT_GUILD_MESSAGES = 1 << 9
_INTENT_GUILD_MESSAGE_CONTENT = 1 << 15
_INTENT_DIRECT_MESSAGES = 1 << 12


class DiscordAdapter(BaseChannelAdapter):
    channel_type = "discord"
    capabilities: ChannelCapabilities = DISCORD_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._rest_client: httpx.AsyncClient | None = None
        self._bot_token = ""
        self._bot_user_id = ""
        self._bot_username = ""
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._sequence: int | None = None
        self._decompressor = zlib.decompressobj()

    async def _connect(self) -> None:
        self._bot_token = self._credentials.get("bot_token", "")
        if not self._bot_token:
            raise ValueError("Discord adapter requires bot_token credential")

        self._rest_client = httpx.AsyncClient(
            base_url=_DISCORD_API_BASE,
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=30.0,
        )
        resp = await self._rest_client.get("/users/@me")
        resp.raise_for_status()
        user_data = resp.json()
        self._bot_user_id = user_data.get("id", "")
        self._bot_username = user_data.get("username", "")
        self._decompressor = zlib.decompressobj()

    async def _disconnect(self) -> None:
        if self._rest_client is not None:
            await self._rest_client.aclose()
            self._rest_client = None

    async def _run(self) -> None:
        import websockets

        gateway_url = self._resume_url or _DISCORD_GATEWAY_URL
        async with websockets.connect(gateway_url, max_size=10 * 1024 * 1024) as ws:
            hello = await self._recv_gateway_payload(ws)
            if int(hello.get("op", -1)) != _OP_HELLO:
                raise RuntimeError("Discord gateway did not send HELLO")

            heartbeat_interval_ms = int((hello.get("d") or {}).get("heartbeat_interval", 45000))
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(ws, heartbeat_interval_ms / 1000.0),
                name=f"discord-heartbeat-{self.account_id}",
            )
            try:
                await self._identify_or_resume(ws)
                while not self._stop_event.is_set():
                    payload = await self._recv_gateway_payload(ws)
                    op = int(payload.get("op", -1))
                    data = payload.get("d") or {}
                    seq = payload.get("s")
                    if seq is not None:
                        self._sequence = int(seq)

                    if op == _OP_DISPATCH:
                        event_type = payload.get("t")
                        if event_type == "READY":
                            self._session_id = data.get("session_id")
                            self._resume_url = data.get("resume_gateway_url")
                        elif event_type == "MESSAGE_CREATE":
                            await self._handle_message(data, data.get("channel_id", ""))
                    elif op == _OP_HEARTBEAT:
                        await self._send_heartbeat(ws)
                    elif op == _OP_RECONNECT:
                        raise RuntimeError("Discord requested reconnect")
                    elif op == _OP_INVALID_SESSION:
                        self._session_id = None
                        self._resume_url = None
                        self._sequence = None
                        raise RuntimeError("Discord invalidated the session")
                    elif op == _OP_HEARTBEAT_ACK:
                        continue
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _identify_or_resume(self, ws: Any) -> None:
        if self._session_id and self._sequence is not None:
            await ws.send(
                json.dumps(
                    {
                        "op": _OP_RESUME,
                        "d": {
                            "token": self._bot_token,
                            "session_id": self._session_id,
                            "seq": self._sequence,
                        },
                    }
                )
            )
            return

        intents = (
            _INTENT_GUILDS
            | _INTENT_GUILD_MESSAGES
            | _INTENT_GUILD_MESSAGE_CONTENT
            | _INTENT_DIRECT_MESSAGES
        )
        await ws.send(
            json.dumps(
                {
                    "op": _OP_IDENTIFY,
                    "d": {
                        "token": self._bot_token,
                        "intents": intents,
                        "properties": {
                            "os": platform.system().lower(),
                            "browser": "cognis",
                            "device": "cognis",
                        },
                    },
                }
            )
        )

    async def _heartbeat_loop(self, ws: Any, interval_seconds: float) -> None:
        await asyncio.sleep(random.random() * interval_seconds)
        while not self._stop_event.is_set():
            await self._send_heartbeat(ws)
            await asyncio.sleep(interval_seconds)

    async def _send_heartbeat(self, ws: Any) -> None:
        await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._sequence}))

    async def _recv_gateway_payload(self, ws: Any) -> dict[str, Any]:
        buffer = b""
        while True:
            frame = await ws.recv()
            if isinstance(frame, str):
                return json.loads(frame)
            buffer += frame
            if buffer.endswith(_ZLIB_SUFFIX):
                data = self._decompressor.decompress(buffer)
                return json.loads(data.decode("utf-8"))

    async def send_message(self, message: OutboundMessage) -> str | None:
        if self._rest_client is None:
            return None
        payload: dict[str, Any] = {"content": message.content}
        if message.reply_to_id:
            payload["message_reference"] = {"message_id": message.reply_to_id}
        resp = await self._rest_client.post(f"/channels/{message.chat_id}/messages", json=payload)
        resp.raise_for_status()
        return resp.json().get("id")

    async def send_typing(self, chat_id: str) -> None:
        if self._rest_client is None:
            return
        with contextlib.suppress(Exception):
            await self._rest_client.post(f"/channels/{chat_id}/typing")

    async def _handle_message(self, msg: dict[str, Any], channel_id: str) -> None:
        author = msg.get("author", {})
        content = msg.get("content", "")
        if not content:
            return
        if author.get("id") == self._bot_user_id:
            return

        sender_id = author.get("id", "")
        sender_name = author.get("global_name") or author.get("username", "")
        sender_username = author.get("username")
        chat_type = "direct" if not msg.get("guild_id") else "group"

        was_mentioned = False
        if self._bot_user_id:
            for mention in msg.get("mentions", []):
                if mention.get("id") == self._bot_user_id:
                    was_mentioned = True
                    content = (
                        content.replace(f"<@{self._bot_user_id}>", "")
                        .replace(f"<@!{self._bot_user_id}>", "")
                        .strip()
                    )
                    break

        media = [
            MediaAttachment(
                url=attachment.get("url"),
                filename=attachment.get("filename"),
                mime_type=attachment.get("content_type"),
                size_bytes=attachment.get("size"),
            )
            for attachment in msg.get("attachments", [])
        ]

        reply_to_id = (msg.get("message_reference") or {}).get("message_id")
        thread_id = msg.get("thread", {}).get("id") if msg.get("thread") else None
        timestamp_str = msg.get("timestamp", "")
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now(UTC)

        await self._dispatch_inbound(
            InboundMessage(
                channel_type="discord",
                account_id=self.account_id,
                message_id=msg.get("id", ""),
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                chat_id=channel_id,
                chat_type=chat_type,
                content=content,
                reply_to_id=reply_to_id,
                thread_id=thread_id,
                media=media,
                was_mentioned=was_mentioned,
                timestamp=timestamp,
                platform_data={"raw_message": msg},
            )
        )
