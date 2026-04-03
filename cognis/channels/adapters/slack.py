"""Slack adapter via Web API, Socket Mode, or HTTP Events API."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import SLACK_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter(BaseChannelAdapter):
    channel_type = "slack"
    capabilities: ChannelCapabilities = SLACK_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._bot_token = ""
        self._app_token = ""
        self._signing_secret = ""
        self._bot_user_id = ""
        self._use_socket_mode = True

    async def _connect(self) -> None:
        self._bot_token = self._credentials.get("bot_token", "")
        self._app_token = self._credentials.get("app_token", "")
        self._signing_secret = self._credentials.get("signing_secret", "")
        self._use_socket_mode = self._credentials.get("use_socket_mode", "true").lower() in {
            "true",
            "1",
            "yes",
        }
        if not self._bot_token:
            raise ValueError("Slack adapter requires bot_token credential")
        self._client = httpx.AsyncClient(
            base_url=_SLACK_API_BASE,
            headers={"Authorization": f"Bearer {self._bot_token}"},
            timeout=30.0,
        )
        resp = await self._client.post("/auth.test")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(f"Slack auth.test failed: {data.get('error')}")
        self._bot_user_id = data.get("user_id", "")

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        if self._use_socket_mode:
            if not self._app_token:
                logger.warning(
                    "slack adapter: socket mode requested but app_token missing; falling back to webhook mode",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
                await self._stop_event.wait()
                return
            try:
                await self._run_socket_mode()
                return
            except Exception:
                logger.warning(
                    "slack adapter: socket mode failed; falling back to webhook mode",
                    extra={"extra_data": {"account_id": self.account_id}},
                    exc_info=True,
                )
        await self._stop_event.wait()

    async def _run_socket_mode(self) -> None:
        if self._client is None:
            return
        import websockets

        while not self._stop_event.is_set():
            resp = await self._client.post(
                "/apps.connections.open",
                headers={"Authorization": f"Bearer {self._app_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack apps.connections.open failed: {data.get('error')}")
            url = data.get("url")
            if not isinstance(url, str) or not url:
                raise RuntimeError("Slack Socket Mode did not return a URL")

            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                while not self._stop_event.is_set():
                    envelope = json.loads(await ws.recv())
                    envelope_id = envelope.get("envelope_id")
                    if envelope_id:
                        await ws.send(json.dumps({"envelope_id": envelope_id}))

                    if envelope.get("type") == "disconnect":
                        break

                    if envelope.get("type") == "events_api":
                        payload = envelope.get("payload") or {}
                        event = payload.get("event") or {}
                        if event.get("type") == "message":
                            await self._handle_message_event(event)
            await asyncio.sleep(1)

    async def send_message(self, message: OutboundMessage) -> str | None:
        if self._client is None:
            return None
        payload: dict[str, Any] = {"channel": message.chat_id, "text": message.content}
        if message.thread_id:
            payload["thread_ts"] = message.thread_id
        if message.reply_to_id:
            payload["thread_ts"] = message.reply_to_id
        resp = await self._client.post("/chat.postMessage", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning(
                "slack adapter: send failed",
                extra={"extra_data": {"account_id": self.account_id, "error": data.get("error")}},
            )
            return None
        return data.get("ts")

    async def send_typing(self, chat_id: str) -> None:
        return None

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                "/conversations.mark", json={"channel": chat_id, "ts": message_id}
            )

    async def verify_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        signing_secret = secret or self._signing_secret
        if not signing_secret:
            return False
        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            return False
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > 300:
                return False
        except ValueError:
            return False
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(signature, expected)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge")}
        if data.get("type") == "event_callback":
            event = data.get("event", {})
            if event.get("type") == "message":
                await self._handle_message_event(event)
        return {"ok": True}

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        if event.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
            return
        if event.get("bot_id") or event.get("user") == self._bot_user_id:
            return
        user_id = event.get("user", "")
        text = event.get("text", "")
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        if not text:
            return

        chat_type = "direct" if event.get("channel_type", "") == "im" else "group"
        was_mentioned = False
        if self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            was_mentioned = True
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()
        sender_name = await self._resolve_user_name(user_id)
        media = [
            MediaAttachment(
                url=file_info.get("url_private"),
                filename=file_info.get("name"),
                mime_type=file_info.get("mimetype"),
                size_bytes=file_info.get("size"),
            )
            for file_info in event.get("files", [])
        ]
        try:
            timestamp = datetime.fromtimestamp(float(ts), tz=UTC)
        except (ValueError, TypeError):
            timestamp = datetime.now(UTC)

        await self._dispatch_inbound(
            InboundMessage(
                channel_type="slack",
                account_id=self.account_id,
                message_id=ts,
                sender_id=user_id,
                sender_name=sender_name,
                chat_id=channel_id,
                chat_type=chat_type,
                content=text,
                thread_id=thread_ts,
                media=media,
                was_mentioned=was_mentioned,
                timestamp=timestamp,
                platform_data={"event": event},
            )
        )

    async def _resolve_user_name(self, user_id: str) -> str | None:
        if self._client is None:
            return None
        try:
            resp = await self._client.get("/users.info", params={"user": user_id})
            data = resp.json()
            if data.get("ok"):
                user = data.get("user", {})
                return user.get("real_name") or user.get("name")
        except Exception:
            return None
        return None
