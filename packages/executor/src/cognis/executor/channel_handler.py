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
import re
from collections.abc import Awaitable, Callable
from typing import Any

from cognis.channels.adapters.signal_cli_install import (
    ensure_signal_cli,
    resolve_signal_cli_runtime_config,
)
from cognis.channels.protocol import NonRetryableChannelError
from cognis.channels.signal_failures import SignalDeliveryFailure
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelCapabilities,
    ChannelRecipient,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
    ResolvedChannelTarget,
)

logger = logging.getLogger("cognis.executor.channel_handler")


class ChannelHandler:
    """Manages channel adapter instances on the executor side."""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}  # account_id → BaseChannelAdapter
        self._ws: Any | None = None
        self._notification_sender: Callable[[str], Awaitable[None]] | None = None
        self._pending_notifications: list[tuple[str, str]] = []
        self._notification_ready_accounts: set[str] | None = set()
        self._executor_config: dict[str, Any] = {}

    def set_ws(self, ws: Any) -> None:
        """Set the WebSocket for sending notifications back to the controller."""
        self._ws = ws
        self._notification_ready_accounts = None

    def set_notification_sender(self, sender: Callable[[str], Awaitable[None]]) -> None:
        """Set the connection-scoped serialized notification sender."""

        self._notification_sender = sender
        self._notification_ready_accounts = None

    def pause_notifications(self) -> None:
        """Queue inbound notifications until the controller reinstalls callbacks."""

        self._ws = None
        self._notification_sender = None
        self._notification_ready_accounts = set()

    async def activate_notification_sender(
        self,
        account_id: str,
        sender: Callable[[str], Awaitable[None]],
    ) -> None:
        """Install the sender and flush notifications queued during reconnect."""

        self._notification_sender = sender
        if self._notification_ready_accounts is None:
            self._notification_ready_accounts = set()
        self._notification_ready_accounts.add(account_id)
        pending = [
            payload
            for pending_account_id, payload in self._pending_notifications
            if pending_account_id == account_id
        ]
        self._pending_notifications = [
            entry for entry in self._pending_notifications if entry[0] != account_id
        ]
        for index, payload in enumerate(pending):
            try:
                await sender(payload)
            except Exception:
                self._pending_notifications.extend((account_id, item) for item in pending[index:])
                raise

    async def _send_notification(self, account_id: str, payload: str) -> None:
        """Send a notification or retain it until reconnect setup completes."""

        ready = self._notification_ready_accounts
        if self._notification_sender is None:
            if self._ws is not None and ready is None:
                await self._ws.send(payload)
                return
            self._pending_notifications.append((account_id, payload))
            return
        if ready is not None and account_id not in ready:
            self._pending_notifications.append((account_id, payload))
            return
        try:
            await self._notification_sender(payload)
        except Exception:
            self._pending_notifications.append((account_id, payload))
            self.pause_notifications()
            raise

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
            signal_cli = await ensure_signal_cli(
                resolve_signal_cli_runtime_config(self._executor_config)
            )
            if not signal_cli.available or not signal_cli.command:
                raise RuntimeError(signal_cli.error or "signal-cli runtime unavailable")
            settings["_signal_cli_command"] = signal_cli.command
            settings["_signal_cli_runtime"] = signal_cli.metadata()

        # Build a ChannelAccountConfig from the provided config dict
        account_config = ChannelAccountConfig(
            account_id=account_id,
            channel_type=channel_type,
            display_name=config.get("display_name", ""),
            credential_refs={},
            agent_id=config.get("agent_id", ""),
            default_agent_profile_id=config.get("default_agent_profile_id"),
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
        result: dict[str, Any] = {"status": "started", "account_id": account_id}
        capabilities = getattr(adapter, "capabilities", None)
        if isinstance(capabilities, ChannelCapabilities):
            result["capabilities"] = capabilities.model_dump(mode="json")
        return result

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
            return {
                "error": {
                    "code": "account_not_found",
                    "retryable": False,
                    "side_effect_certainty": "none",
                }
            }

        outbound = OutboundMessage(
            channel_type=message.get("channel_type", ""),
            account_id=account_id,
            chat_id=message.get("chat_id", ""),
            content=message.get("content", ""),
            reply_to_id=message.get("reply_to_id"),
            thread_id=message.get("thread_id"),
            media=message.get("media") or [],
            platform_data=message.get("platform_data") or {},
        )
        try:
            platform_msg_id = await adapter.send_message(outbound)
        except SignalDeliveryFailure as exc:
            return {"error": exc.safe_metadata()}
        return {"status": "sent", "platform_message_id": platform_msg_id}

    async def resolve_recipient(
        self,
        account_id: Any,
        recipient: Any,
        resolution_key: Any,
    ) -> dict[str, Any]:
        """Resolve one typed recipient and return only safe error envelopes."""
        if not isinstance(account_id, str) or not account_id or len(account_id) > 255:
            return _recipient_error("malformed_request", retryable=False)
        if not isinstance(resolution_key, str) or not resolution_key or len(resolution_key) > 255:
            return _recipient_error("malformed_request", retryable=False)
        try:
            typed_recipient = ChannelRecipient.model_validate(recipient)
        except Exception:
            return _recipient_error("malformed_recipient", retryable=False)

        adapter = self._adapters.get(account_id)
        if adapter is None:
            return _recipient_error("account_not_found", retryable=False)
        capabilities = getattr(adapter, "capabilities", None)
        recipient_capabilities = getattr(capabilities, "recipient_capabilities", None)
        if recipient_capabilities is None:
            return _recipient_error("unsupported_resolution", retryable=False)
        adapter_channel_type = getattr(adapter, "channel_type", None)
        if (
            isinstance(adapter_channel_type, str)
            and adapter_channel_type
            and typed_recipient.channel_type != adapter_channel_type
        ):
            return _recipient_error("channel_mismatch", retryable=False)
        if typed_recipient.address_kind not in recipient_capabilities.address_kinds:
            return _recipient_error(
                "unsupported_address_kind"
                if typed_recipient.address_kind is not None
                else "unsupported_resolution",
                retryable=False,
            )
        if typed_recipient.chat_kind not in recipient_capabilities.chat_kinds:
            return _recipient_error("unsupported_chat_kind", retryable=False)
        if typed_recipient.allow_resolution and not recipient_capabilities.supports_resolution:
            return _recipient_error("resolution_unsupported", retryable=False)
        if typed_recipient.allow_creation and not recipient_capabilities.supports_creation:
            return _recipient_error("creation_unsupported", retryable=False)

        try:
            target = await adapter.resolve_recipient(
                typed_recipient,
                resolution_key=resolution_key,
            )
            resolved = ResolvedChannelTarget.model_validate(target)
        except NonRetryableChannelError as exc:
            return _recipient_error(
                _safe_error_code(exc),
                retryable=False,
                side_effect_certainty=_safe_side_effect_certainty(
                    exc, allow_creation=typed_recipient.allow_creation
                ),
            )
        except Exception as exc:
            return _recipient_error(
                _safe_error_code(exc),
                retryable=True,
                side_effect_certainty=_safe_side_effect_certainty(
                    exc, allow_creation=typed_recipient.allow_creation
                ),
            )
        if resolved.account_id != account_id:
            return _recipient_error(
                "invalid_target",
                retryable=False,
                side_effect_certainty=("uncertain" if typed_recipient.allow_creation else "none"),
            )
        return resolved.model_dump(mode="json")

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
                from cognis.executor.audio_preprocessing import prepare_audio_for_stt

                content, content_type, filename = await prepare_audio_for_stt(
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
        try:
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "channel.message",
                    "params": {
                        "account_id": account_id,
                        "message": message.model_dump(mode="json"),
                    },
                }
            )
            await self._send_notification(account_id, payload)
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
        try:
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "channel.status",
                    "params": {
                        "account_id": account_id,
                        "status": status,
                    },
                }
            )
            await self._send_notification(account_id, payload)
        except Exception:
            logger.warning(
                "channel_handler: failed to send channel.status notification",
                exc_info=True,
            )


def _create_adapter(channel_type: str) -> Any:
    """Create an adapter instance via the shared factory."""
    from cognis.channels.factory import create_adapter

    return create_adapter(channel_type)


_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_SIDE_EFFECT_CERTAINTIES = {"none", "uncertain", "known"}


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return (
        code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else "resolution_failed"
    )


def _safe_side_effect_certainty(exc: Exception, *, allow_creation: bool) -> str:
    certainty = getattr(exc, "side_effect_certainty", None)
    return (
        certainty
        if isinstance(certainty, str) and certainty in _SAFE_SIDE_EFFECT_CERTAINTIES
        else ("uncertain" if allow_creation else "none")
    )


def _recipient_error(
    code: str,
    *,
    retryable: bool,
    side_effect_certainty: str = "none",
) -> dict[str, Any]:
    safe_code = code if _SAFE_ERROR_CODE.fullmatch(code) else "resolution_failed"
    return {
        "error": {
            "code": safe_code,
            "message": _recipient_error_message(safe_code),
            "retryable": retryable,
            "side_effect_certainty": side_effect_certainty,
        }
    }


def _recipient_error_message(code: str) -> str:
    messages = {
        "account_not_found": "Recipient account is unavailable",
        "malformed_request": "Recipient resolution request is malformed",
        "malformed_recipient": "Recipient data is malformed",
        "channel_mismatch": "Recipient channel does not match the account",
        "unsupported_address_kind": "Recipient address kind is unsupported",
        "unsupported_chat_kind": "Recipient chat kind is unsupported",
        "unsupported_resolution": "Recipient resolution is unsupported",
        "resolution_not_authorized": "Recipient resolution is not authorized",
        "resolution_unsupported": "Recipient resolution is unsupported",
        "creation_unsupported": "Recipient creation is unsupported",
        "invalid_target": "Executor returned an invalid recipient target",
        "resolution_failed": "Recipient resolution failed",
    }
    return messages.get(code, "Recipient resolution failed")
