"""Inbound message processing pipeline.

Handles the flow from a normalized ``InboundMessage`` to a
``TurnScheduler.submit_turn()`` call:

1. Access control (allowlist, DM/group policy)
2. Identity mapping (external sender → Cognis user)
3. Conversation resolution (find or create)
4. Turn submission
5. Observer registration for response delivery
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.protocol import CHANNEL_OUTBOUND_TOTAL, BaseChannelAdapter
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
    OutboundMessage,
)
from cognis.models.session import ConversationContext

logger = get_logger(__name__)


class InboundPipeline:
    """Processes inbound messages from channel adapters.

    Routes messages through access control, identity mapping,
    conversation resolution, and turn submission.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        turn_scheduler: Any,  # TurnScheduler (avoid circular import)
        session_manager: Any,  # SessionManager
        pairing_service: Any,
        channel_manager_ref: Any,  # Callable[[], ChannelManager] — lazy ref
    ) -> None:
        self._session_factory = session_factory
        self._turn_scheduler = turn_scheduler
        self._session_manager = session_manager
        self._pairing_service = pairing_service
        self._channel_manager_ref = channel_manager_ref

    async def process(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
    ) -> None:
        """Process an inbound message through the full pipeline."""
        # 1. Access control
        if not self._check_access(message, config):
            logger.info(
                "channel inbound: access denied",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                        "sender_id": message.sender_id,
                        "chat_type": message.chat_type,
                    }
                },
            )
            return

        # 2. Identity mapping / pairing (external sender → Cognis user)
        user_email = await self._resolve_user(message, config)
        if user_email is None:
            logger.info(
                "channel inbound: awaiting verified sender mapping",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "sender_id": message.sender_id,
                    }
                },
            )
            return

        # 3. Conversation resolution
        conversation_id = await self._resolve_conversation(message, config, user_email)
        if conversation_id is None:
            logger.warning(
                "channel inbound: failed to resolve conversation",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                    }
                },
            )
            return

        attachments = await self._normalize_media_attachments(
            message=message,
            conversation_id=conversation_id,
            user_email=user_email,
        )

        # 4. Register observer for response delivery
        observer = ChannelTurnObserver(
            channel_type=message.channel_type,
            account_id=message.account_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            conversation_id=conversation_id,
            turn_scheduler=self._turn_scheduler,
            reply_to_id=message.message_id,
            channel_manager_ref=self._channel_manager_ref,
        )
        self._turn_scheduler.add_observer(conversation_id, observer)

        # 5. Submit turn
        error = await self._turn_scheduler.submit_turn(
            conversation_id,
            message.content,
            user_email=user_email,
            attachments=[item.model_dump(mode="json") for item in attachments],
        )

        if error is not None:
            # Remove observer on immediate error
            self._turn_scheduler.remove_observer(conversation_id, observer)
            # Send error back to channel
            await self._send_error(message, config, error.message)

        logger.info(
            "channel inbound: turn submitted",
            extra={
                "extra_data": {
                    "channel_type": message.channel_type,
                    "conversation_id": conversation_id,
                }
            },
        )

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def _check_access(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
    ) -> bool:
        """Check if the sender is allowed to send messages."""
        if message.chat_type == "direct":
            if config.dm_policy == "disabled":
                return False
            if config.dm_policy == "allowlist":
                return message.sender_id in config.allowed_senders
            # "open" and "pairing" — allow pipeline to continue
            return True

        if message.chat_type == "group":
            if config.group_policy == "disabled":
                return False
            if config.group_policy == "mention":
                return message.was_mentioned
            if config.group_policy == "allowlist":
                return message.sender_id in config.allowed_senders
            # "open" and "pairing" — allow pipeline to continue
            return True

        return True

    # ------------------------------------------------------------------
    # Identity mapping
    # ------------------------------------------------------------------

    async def _resolve_user(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
    ) -> str | None:
        """Map an external sender to a Cognis user email.

        Resolution depends on channel policy:
        - ``pairing``: require a verified channel contact or issue a challenge
        - ``open`` / ``mention``: use a verified contact if present, otherwise
          fall back to the account owner
        - ``allowlist``: sender must already pass access control; use verified
          contact if present, otherwise fall back to account owner
        """
        from cognis.store.queries import get_channel_contact

        policy = config.dm_policy if message.chat_type == "direct" else config.group_policy

        async with self._session_factory() as session:
            contact = await get_channel_contact(session, message.channel_type, message.sender_id)
            if contact is not None and contact.verified:
                return contact.user_email

        if policy == "pairing":
            return await self._pairing_service.ensure_verified_sender(
                message=message, config=config
            )

        return config.user_email

    # ------------------------------------------------------------------
    # Conversation resolution
    # ------------------------------------------------------------------

    async def _resolve_conversation(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        user_email: str,
    ) -> str | None:
        """Find or create a conversation for this channel message.

        Uses the conversation context ref (e.g., "signal:+1234567890:chat123")
        to find an existing conversation, or creates a new one.
        """
        context_ref = f"{message.channel_type}:{message.account_id}:{message.chat_id}"
        if message.thread_id:
            context_ref = f"{context_ref}:{message.thread_id}"

        # Check for default conversation
        if config.default_conversation_id:
            return config.default_conversation_id

        # Try to find existing conversation by context ref
        from cognis.store.queries import get_latest_active_conversation_for_context

        async with self._session_factory() as session:
            existing = await get_latest_active_conversation_for_context(
                session,
                user_email=user_email,
                agent_id=config.agent_id,
                context_ref=context_ref,
            )
            if existing is not None:
                return existing.conversation_id

        # Create new conversation if allowed
        if not config.allow_new_conversations:
            return None

        try:
            conversation, _ = await self._session_manager.create_conversation_with_root_session(
                user_email=user_email,
                agent_id=config.agent_id,
                context=ConversationContext(
                    type=message.channel_type,
                    ref=context_ref,
                    platform_data={
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                        "chat_id": message.chat_id,
                        "chat_name": message.chat_name,
                        "chat_type": message.chat_type,
                        "thread_id": message.thread_id,
                    },
                ),
                title=message.chat_name or f"{message.channel_type} chat",
            )
            return conversation.conversation_id
        except Exception:
            logger.exception(
                "channel inbound: failed to create conversation",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                    }
                },
            )
            return None

    # ------------------------------------------------------------------
    # Error delivery
    # ------------------------------------------------------------------

    async def _send_error(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        error_text: str,
    ) -> None:
        """Send an error message back to the channel."""
        manager = self._channel_manager_ref()
        if manager is None:
            return
        adapter = manager.get_adapter(config.account_id)
        if adapter is None:
            return
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=message.channel_type,
                    account_id=message.account_id,
                    chat_id=message.chat_id,
                    content="Sorry, I couldn't process your message right now. Please try again.",
                    reply_to_id=message.message_id,
                )
            )

    async def _normalize_media_attachments(
        self,
        *,
        message: InboundMessage,
        conversation_id: str,
        user_email: str,
    ) -> list[AttachmentRef]:
        if not message.media:
            return []
        manager = self._channel_manager_ref()
        if manager is None:
            return []
        adapter = manager.get_adapter(message.account_id)
        if adapter is None:
            return []

        refs: list[AttachmentRef] = []
        async with self._session_factory() as session:
            from cognis.store.queries import create_artifact_record

            for attachment in message.media:
                try:
                    fetched = await adapter.download_attachment(message, attachment)
                    if fetched is None:
                        continue
                    content, content_type, filename = fetched
                    kind = _kind_for_media(content_type)
                    artifact_id = manager._artifact_store.generate_id("att")  # noqa: SLF001
                    await manager._artifact_store.async_save(  # noqa: SLF001
                        "attachments",
                        artifact_id,
                        filename,
                        content,
                        content_type,
                        owner_email=user_email,
                    )
                    await create_artifact_record(
                        session,
                        artifact_id=artifact_id,
                        namespace="attachments",
                        object_id=artifact_id,
                        filename=filename,
                        owner_email=user_email,
                        purpose="channel_input",
                        kind=kind.value,
                        mime_type=content_type,
                        size_bytes=len(content),
                        status="attached",
                        conversation_id=conversation_id,
                        message_role="user",
                    )
                    refs.append(
                        AttachmentRef(
                            artifact_id=artifact_id,
                            kind=kind,
                            mime_type=content_type,
                            filename=filename,
                            size_bytes=len(content),
                            url=await manager._artifact_store.async_get_signed_url(  # noqa: SLF001
                                "attachments", artifact_id, filename
                            ),
                        )
                    )
                except Exception:
                    logger.warning(
                        "channel inbound: failed to normalize attachment",
                        extra={
                            "extra_data": {
                                "account_id": message.account_id,
                                "channel_type": message.channel_type,
                            }
                        },
                        exc_info=True,
                    )
                    continue
            await session.commit()
        return refs


def _kind_for_media(content_type: str) -> ArtifactKind:
    if content_type.startswith("image/"):
        return ArtifactKind.IMAGE
    if content_type.startswith("audio/"):
        return ArtifactKind.AUDIO
    if content_type.startswith("video/"):
        return ArtifactKind.VIDEO
    if content_type == "application/pdf":
        return ArtifactKind.PDF
    return ArtifactKind.FILE


# ---------------------------------------------------------------------------
# ChannelTurnObserver — bridges TurnObserver to channel delivery
# ---------------------------------------------------------------------------


class ChannelTurnObserver:
    """TurnObserver implementation that delivers responses to a channel.

    Accumulates streaming tokens and sends the complete response when
    the turn finishes.  Also sends typing indicators during processing.
    """

    def __init__(
        self,
        *,
        channel_type: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None = None,
        conversation_id: str,
        turn_scheduler: Any,
        reply_to_id: str | None = None,
        channel_manager_ref: Any,
    ) -> None:
        self._channel_type = channel_type
        self._account_id = account_id
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._conversation_id = conversation_id
        self._turn_scheduler = turn_scheduler
        self._reply_to_id = reply_to_id
        self._channel_manager_ref = channel_manager_ref
        self._accumulated_text = ""
        self._typing_sent = False

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        delta: str,
    ) -> None:
        """Accumulate tokens and send typing indicator."""
        self._accumulated_text += delta

        # Send typing indicator on first token
        if not self._typing_sent:
            self._typing_sent = True
            adapter = self._get_adapter()
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.send_typing(self._chat_id)

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> None:
        """Send typing indicator during tool execution."""
        adapter = self._get_adapter()
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.send_typing(self._chat_id)

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
    ) -> None:
        """No-op for tool results."""

    async def on_turn_complete(self, result: Any) -> None:
        """Send the accumulated response to the channel."""
        from cognis.channels.formatting import format_for_channel

        # Remove self from observers
        self._turn_scheduler_remove()

        if not self._accumulated_text:
            return

        adapter = self._get_adapter()
        if adapter is None:
            return

        # Format and split for channel
        chunks = format_for_channel(self._accumulated_text, adapter.capabilities)

        for chunk in chunks:
            with contextlib.suppress(Exception):
                await adapter.send_message(
                    OutboundMessage(
                        channel_type=self._channel_type,
                        account_id=self._account_id,
                        chat_id=self._chat_id,
                        content=chunk,
                        reply_to_id=self._reply_to_id,
                        thread_id=self._thread_id,
                    )
                )
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                ).inc()
                # Only reply to the original message for the first chunk
                self._reply_to_id = None

    async def on_turn_error(self, conversation_id: str, error: Any) -> None:
        """Send error message to the channel."""
        self._turn_scheduler_remove()

        adapter = self._get_adapter()
        if adapter is None:
            return

        error_text = (
            f"Sorry, something went wrong: {error.message}" if error else "An error occurred."
        )
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                    chat_id=self._chat_id,
                    content=error_text,
                    reply_to_id=self._reply_to_id,
                    thread_id=self._thread_id,
                )
            )

    async def on_system_message(self, conversation_id: str, text: str) -> None:
        """Forward system messages to the channel."""
        adapter = self._get_adapter()
        if adapter is None:
            return
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                    chat_id=self._chat_id,
                    content=text,
                    thread_id=self._thread_id,
                )
            )

    async def on_queued(self, conversation_id: str, queued_count: int) -> None:
        """No-op for queue notifications."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_adapter(self) -> BaseChannelAdapter | None:
        manager = self._channel_manager_ref()
        if manager is None:
            return None
        return manager.get_adapter(self._account_id)

    def _turn_scheduler_remove(self) -> None:
        """Remove self from turn scheduler observers.

        Called after each turn completes so a new observer created for
        the next inbound message starts with a clean list.
        """
        self._turn_scheduler.remove_observer(self._conversation_id, self)
