"""Executor-side channel adapter handler.

Manages local adapter instances on the executor.  The controller sends
``channel.start``, ``channel.stop``, and ``channel.send`` commands via
JSON-RPC.  Inbound messages from the adapter are forwarded back to the
controller as ``channel.message`` notifications.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from typing import Any

from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = logging.getLogger("cognis.executor.channel_handler")


class ChannelHandler:
    """Manages channel adapter instances on the executor side."""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}  # account_id → BaseChannelAdapter
        self._ws: Any | None = None
        self._executor_config: dict[str, Any] = {}

    def set_ws(self, ws: Any) -> None:
        """Set the WebSocket for sending notifications back to the controller."""
        self._ws = ws

    def set_executor_config(self, config: dict[str, Any]) -> None:
        """Set executor-level config (used for per-user runtime settings)."""
        self._executor_config = config or {}

    async def start(
        self,
        account_id: str,
        channel_type: str,
        config: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        """Start a channel adapter locally on this executor.

        SECURITY: ``credentials`` contains decrypted secret values
        delivered by the controller over the encrypted WebSocket.
        They MUST NOT be logged or persisted.
        """
        # Stop existing adapter for this account if any
        if account_id in self._adapters:
            await self.stop(account_id)

        adapter = _create_adapter(channel_type)

        # Inject executor-level config into adapter settings
        settings = dict(config.get("settings", {}))
        if channel_type == "signal" and settings.get("transport") == "direct_jsonrpc":
            signal_exec_config = self._executor_config.get("signal", {})
            settings["_signal_cli_command"] = signal_exec_config.get("command", "signal-cli")

        # Build a ChannelAccountConfig from the provided config dict
        account_config = ChannelAccountConfig(
            account_id=account_id,
            channel_type=channel_type,
            display_name=config.get("display_name", ""),
            credential_refs={},
            agent_id=config.get("agent_id", ""),
            user_email=config.get("user_email", ""),
            settings=settings,
            default_conversation_id=config.get("default_conversation_id"),
            allow_new_conversations=config.get("allow_new_conversations", True),
            allowed_senders=config.get("allowed_senders", []),
            dm_policy=config.get("dm_policy", "pairing"),
            group_policy=config.get("group_policy", "pairing"),
            webhook_secret=config.get("webhook_secret"),
        )

        async def on_message(message: InboundMessage) -> None:
            await self._send_channel_message(account_id, message)

        await adapter.start(account_config, credentials, on_message)
        self._adapters[account_id] = adapter

        logger.info("channel_handler: started adapter %s (%s)", account_id, channel_type)
        return {"status": "started", "account_id": account_id}

    async def stop(self, account_id: str) -> dict[str, Any]:
        """Stop a channel adapter."""
        adapter = self._adapters.pop(account_id, None)
        if adapter is not None:
            await adapter.stop()
            logger.info("channel_handler: stopped adapter %s", account_id)
            return {"status": "stopped", "account_id": account_id}
        return {"status": "not_found", "account_id": account_id}

    async def send(self, account_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """Send a message through a local adapter."""
        adapter = self._adapters.get(account_id)
        if adapter is None:
            return {"error": f"No adapter for account {account_id}"}

        outbound = OutboundMessage(
            channel_type=message.get("channel_type", ""),
            account_id=account_id,
            chat_id=message.get("chat_id", ""),
            content=message.get("content", ""),
            reply_to_id=message.get("reply_to_id"),
            thread_id=message.get("thread_id"),
        )
        platform_msg_id = await adapter.send_message(outbound)
        return {"status": "sent", "platform_message_id": platform_msg_id}

    async def fetch_media(
        self,
        account_id: str,
        message: dict[str, Any],
        attachment: dict[str, Any],
        stt_supported_mime_types: list[str] | None = None,
    ) -> dict[str, Any]:
        adapter = self._adapters.get(account_id)
        if adapter is None:
            return {"error": f"No adapter for account {account_id}"}
        inbound = InboundMessage.model_validate(message)
        media = MediaAttachment.model_validate(attachment)
        fetched = await adapter.download_attachment(inbound, media)
        if fetched is None:
            return {"status": "unavailable"}
        content, content_type, filename = fetched
        if str(content_type or "").startswith("audio/") and stt_supported_mime_types:
            try:
                from cognis.channels.inbound import _prepare_audio_for_stt

                content, content_type, filename = await _prepare_audio_for_stt(
                    content,
                    mime_type=content_type,
                    filename=filename,
                    supported_mime_types=stt_supported_mime_types,
                )
            except Exception as exc:
                return {"error": str(exc)[:500]}
        return {
            "status": "ok",
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
            "filename": filename,
        }

    async def send_typing(self, account_id: str, chat_id: str) -> dict[str, Any]:
        """Send a typing indicator through a local adapter."""
        adapter = self._adapters.get(account_id)
        if adapter is None:
            return {"error": f"No adapter for account {account_id}"}
        try:
            await adapter.send_typing(chat_id)
            return {"status": "ok"}
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def mark_read(self, account_id: str, chat_id: str, message_id: str) -> dict[str, Any]:
        """Mark a message as read through a local adapter."""
        adapter = self._adapters.get(account_id)
        if adapter is None:
            return {"error": f"No adapter for account {account_id}"}
        try:
            await adapter.mark_read(chat_id, message_id)
            return {"status": "ok"}
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def sync_profile(self, account_id: str, profile_data: dict[str, Any]) -> dict[str, Any]:
        """Sync agent profile through a local adapter."""
        from cognis.models.channel import AgentProfile

        adapter = self._adapters.get(account_id)
        if adapter is None:
            return {"error": f"No adapter for account {account_id}"}

        avatar_bytes: bytes | None = None
        if profile_data.get("avatar_b64"):
            avatar_bytes = base64.b64decode(profile_data["avatar_b64"])

        profile = AgentProfile(
            name=profile_data.get("name", ""),
            avatar_bytes=avatar_bytes,
            avatar_content_type=profile_data.get("avatar_content_type"),
        )
        try:
            await adapter.sync_profile(profile)
            return {"status": "ok"}
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def stop_all(self) -> None:
        """Stop all running adapters (called on executor shutdown)."""
        for account_id in list(self._adapters.keys()):
            with contextlib.suppress(Exception):
                await self.stop(account_id)

    @property
    def active_count(self) -> int:
        return len(self._adapters)

    # ------------------------------------------------------------------
    # Notifications back to controller
    # ------------------------------------------------------------------

    async def _send_channel_message(
        self,
        account_id: str,
        message: InboundMessage,
    ) -> None:
        """Forward an inbound message to the controller as a notification."""
        if self._ws is None:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "channel.message",
                        "params": {
                            "account_id": account_id,
                            "message": message.model_dump(mode="json"),
                        },
                    }
                )
            )
        except Exception:
            logger.warning(
                "channel_handler: failed to send channel.message notification",
                exc_info=True,
            )

    async def _send_channel_status(
        self,
        account_id: str,
        status: dict[str, Any],
    ) -> None:
        """Forward adapter status to the controller as a notification."""
        if self._ws is None:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "channel.status",
                        "params": {
                            "account_id": account_id,
                            "status": status,
                        },
                    }
                )
            )
        except Exception:
            logger.warning(
                "channel_handler: failed to send channel.status notification",
                exc_info=True,
            )


def _create_adapter(channel_type: str) -> Any:
    """Create an adapter instance via the shared factory."""
    from cognis.channels.factory import create_adapter

    return create_adapter(channel_type)
