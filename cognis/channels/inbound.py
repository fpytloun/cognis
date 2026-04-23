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

import asyncio
import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.delivery import _append_attachment_fallback, prepare_media_attachments
from cognis.channels.protocol import CHANNEL_OUTBOUND_TOTAL, BaseChannelAdapter
from cognis.core.attachment_utils import attachment_placeholder_text
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)
from cognis.models.session import ConversationContext

logger = get_logger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_SIGNAL_DEBUG_ENABLED = _env_flag("COGNIS_SIGNAL_DEBUG", False)

_STT_SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/mpeg": ("audio/mpeg", ".mp3"),
    "audio/mp3": ("audio/mpeg", ".mp3"),
    "audio/mp4": ("audio/mp4", ".m4a"),
    "audio/x-m4a": ("audio/mp4", ".m4a"),
    "audio/wav": ("audio/wav", ".wav"),
    "audio/x-wav": ("audio/wav", ".wav"),
    "audio/webm": ("audio/webm", ".webm"),
    "audio/ogg": ("audio/ogg", ".ogg"),
}

_STT_TRANSCODE_TARGET_MIME = "audio/wav"
_STT_TRANSCODE_TARGET_EXTENSION = ".wav"


def _fallback_attachment_content(
    content: str,
    attachments: list[AttachmentRef],
    media: list[MediaAttachment],
) -> str:
    if content.strip():
        return content
    if attachments:
        return attachment_placeholder_text(attachment.kind for attachment in attachments)
    if media:
        kinds: list[ArtifactKind] = []
        for item in media:
            mime_type = str(item.mime_type or "")
            if mime_type.startswith("image/"):
                kinds.append(ArtifactKind.IMAGE)
            elif mime_type.startswith("audio/"):
                kinds.append(ArtifactKind.AUDIO)
            elif mime_type.startswith("video/"):
                kinds.append(ArtifactKind.VIDEO)
            elif mime_type == "application/pdf":
                kinds.append(ArtifactKind.PDF)
            else:
                kinds.append(ArtifactKind.FILE)
        return attachment_placeholder_text(kinds)
    return content


def _filter_turn_attachments_for_voice_input(
    message: InboundMessage,
    attachments: list[AttachmentRef],
) -> list[AttachmentRef]:
    if not bool(message.platform_data.get("voice_input")):
        return attachments
    return [attachment for attachment in attachments if attachment.kind != ArtifactKind.AUDIO]


def _normalized_audio_filename(filename: str, default_stem: str = "attachment") -> str:
    candidate = Path(filename).name if filename else default_stem
    if candidate:
        return candidate
    return default_stem


def _stt_passthrough_target(mime_type: str, filename: str) -> tuple[str, str] | None:
    normalized = _STT_SUPPORTED_AUDIO_MIME_TYPES.get(mime_type.lower())
    if normalized is None:
        return None
    normalized_mime, extension = normalized
    path = Path(_normalized_audio_filename(filename))
    if path.suffix.lower() == extension:
        return normalized_mime, path.name
    return normalized_mime, f"{path.stem or 'attachment'}{extension}"


async def _transcode_audio_for_stt(
    content: bytes,
    *,
    mime_type: str,
    filename: str,
) -> tuple[bytes, str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "channel inbound: ffmpeg required for voice transcription but unavailable",
            extra={"extra_data": {"mime_type": mime_type, "filename": filename}},
        )
        raise RuntimeError(
            "I couldn't transcribe that voice message because its audio format requires ffmpeg conversion "
            "and ffmpeg is not installed on the host. Install ffmpeg or send MP3/M4A/WAV/OGG/WebM audio."
        )

    input_suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="cognis_stt_") as tmp_dir:
        input_path = Path(tmp_dir) / f"input{input_suffix}"
        output_path = Path(tmp_dir) / f"output{_STT_TRANSCODE_TARGET_EXTENSION}"
        input_path.write_bytes(content)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists():
            logger.warning(
                "channel inbound: ffmpeg failed to normalize voice audio",
                extra={
                    "extra_data": {
                        "mime_type": mime_type,
                        "filename": filename,
                        "returncode": proc.returncode,
                    }
                },
            )
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "I couldn't transcribe that voice message because its audio format could not be converted "
                f"with ffmpeg. {detail[:200]}"
            )
        return output_path.read_bytes(), _STT_TRANSCODE_TARGET_MIME, "voice-input.wav"


async def _prepare_audio_for_stt(
    content: bytes,
    *,
    mime_type: str,
    filename: str,
) -> tuple[bytes, str, str]:
    passthrough = _stt_passthrough_target(mime_type, filename)
    if passthrough is not None:
        normalized_mime, normalized_filename = passthrough
        return content, normalized_mime, normalized_filename
    return await _transcode_audio_for_stt(content, mime_type=mime_type, filename=filename)


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
        llm_provider: Any | None = None,
        session_manager: Any,  # SessionManager
        pairing_service: Any,
        channel_manager_ref: Any,  # Callable[[], ChannelManager] — lazy ref
        command_dispatcher: Any = None,
        notification_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._turn_scheduler = turn_scheduler
        self._llm_provider = llm_provider
        self._session_manager = session_manager
        self._pairing_service = pairing_service
        self._channel_manager_ref = channel_manager_ref
        self._command_dispatcher = command_dispatcher
        self._notification_service = notification_service

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

        cmd_result = await self._try_command_dispatch(
            conversation_id=conversation_id,
            content=message.content,
            user_email=user_email,
        )
        if cmd_result is not None:
            if cmd_result.text:
                await self._send_system_message(message, config, cmd_result.text)
            logger.info(
                "channel inbound: slash command handled",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "conversation_id": conversation_id,
                        "command_type": cmd_result.type,
                    }
                },
            )
            return

        # Check for pending direct-chat step questions and auto-resolve
        resolved_question = await self._try_resolve_pending_question(
            conversation_id=conversation_id,
            user_email=user_email,
            content=message.content,
        )
        if resolved_question is not None:
            if resolved_question:
                logger.info(
                    "channel inbound: auto-resolved pending step question",
                    extra={
                        "extra_data": {
                            "channel_type": message.channel_type,
                            "conversation_id": conversation_id,
                        }
                    },
                )
            else:
                await self._send_system_message(
                    message,
                    config,
                    "Could not resolve the pending question. Please try again or use /stop to cancel it.",
                )
            return

        attachments = await self._normalize_media_attachments(
            message=message,
            conversation_id=conversation_id,
            user_email=user_email,
        )
        if _SIGNAL_DEBUG_ENABLED and message.channel_type == "signal":
            logger.info(
                "channel inbound: attachment normalization result",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                        "voice_input": self._is_voice_input(message),
                        "media_count": len(message.media),
                        "normalized_attachment_count": len(attachments),
                        "normalized_kinds": [attachment.kind.value for attachment in attachments],
                    }
                },
            )
        user_content = message.content
        if self._is_voice_input(message):
            try:
                user_content = await self._transcribe_voice_input(
                    message=message,
                    attachments=attachments,
                )
            except Exception as exc:
                await self._send_error(message, config, str(exc))
                return
        turn_attachments = _filter_turn_attachments_for_voice_input(message, attachments)
        user_content = _fallback_attachment_content(user_content, attachments, message.media)

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
            assistant_delivery_mode=str(config.settings.get("assistant_delivery_mode", "final")),
        )

        # 5. Submit turn
        error = await self._turn_scheduler.submit_turn(
            conversation_id,
            user_content,
            user_email=user_email,
            attachments=[item.model_dump(mode="json") for item in turn_attachments],
            turn_observers=[observer],
        )

        if error is not None:
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

    def _is_voice_input(self, message: InboundMessage) -> bool:
        return bool(message.platform_data.get("voice_input"))

    async def _transcribe_voice_input(
        self,
        *,
        message: InboundMessage,
        attachments: list[AttachmentRef],
    ) -> str:
        if self._llm_provider is None:
            raise RuntimeError(
                "I couldn't transcribe that voice message because speech-to-text is not configured."
            )
        audio_attachment = next(
            (item for item in attachments if item.kind == ArtifactKind.AUDIO), None
        )
        if audio_attachment is None:
            raise RuntimeError(
                "I couldn't transcribe that voice message because no audio attachment was available."
            )

        manager = self._channel_manager_ref()
        if manager is None:
            raise RuntimeError(
                "I couldn't transcribe that voice message because the channel runtime is unavailable."
            )
        content, _ = await manager._artifact_store.async_load(  # noqa: SLF001
            "attachments", audio_attachment.artifact_id, audio_attachment.filename
        )
        prepared_content, prepared_mime, prepared_filename = await _prepare_audio_for_stt(
            content,
            mime_type=audio_attachment.mime_type,
            filename=audio_attachment.filename,
        )
        try:
            result = await self._llm_provider.transcribe(
                prepared_content,
                mime_type=prepared_mime,
                filename=prepared_filename,
            )
        except Exception as exc:
            raise RuntimeError(f"I couldn't transcribe that voice message. {exc}") from exc
        return result.text

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
                title_source="channel_seed",
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
        content = error_text.strip() if error_text else ""
        if not content:
            content = "Sorry, I couldn't process your message right now. Please try again."
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=message.channel_type,
                    account_id=message.account_id,
                    chat_id=message.chat_id,
                    content=content,
                    reply_to_id=message.message_id,
                )
            )

    async def _send_system_message(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        text: str,
    ) -> None:
        """Send a command/system response back to the channel."""
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
                    content=text,
                    reply_to_id=message.message_id,
                )
            )

    async def _try_command_dispatch(
        self,
        *,
        conversation_id: str,
        content: str,
        user_email: str,
    ) -> Any | None:
        """Try to handle slash commands for channel integrations before turn submission."""
        if self._command_dispatcher is None:
            return None
        if not content.strip().startswith("/"):
            return None

        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.models.agent import AgentDefinition
        from cognis.store.queries import get_agent, get_conversation, get_session_row

        async with self._session_factory() as session:
            conversation_row = await get_conversation(session, conversation_id)
            if conversation_row is None or conversation_row.active_session_id is None:
                return None
            agent_row = await get_agent(session, conversation_row.agent_id)
            if agent_row is None:
                return None
            session_row = await get_session_row(session, conversation_row.active_session_id)
            if session_row is None:
                return None

        agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation_model = _to_conversation_model(conversation_row)
        session_model = _to_session_model(session_row)
        has_active = self._turn_scheduler.has_running_turn(conversation_id)
        has_busy = self._turn_scheduler.has_active_turn(conversation_id)

        return await self._command_dispatcher.dispatch(
            content,
            conversation=conversation_model,
            session=session_model,
            agent=agent_model,
            user_email=user_email,
            has_active_turn=has_active,
            has_busy_turn=has_busy,
        )

    async def _try_resolve_pending_question(
        self,
        *,
        conversation_id: str,
        user_email: str,
        content: str,
    ) -> bool | None:
        """Auto-resolve a pending direct-chat step question with the user's reply.

        Returns:
            ``True`` if a question was resolved successfully.
            ``False`` if a question was found but resolution failed.
            ``None`` if no pending direct-chat question exists.
        """
        if self._notification_service is None:
            return None

        pending = await self._notification_service.list_pending(
            user_email, conversation_id=conversation_id
        )
        direct_questions = [
            notif
            for notif in pending
            if notif.notification_type == "step_question" and notif.task_id is None
        ]
        if not direct_questions:
            return None

        # Resolve the most recent pending direct-chat question
        target = direct_questions[0]
        resolved = await self._notification_service.resolve(
            target.notification_id,
            "continue",
            {"response": content},
        )
        return resolved

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
                            url=await manager._artifact_store.async_get_public_url(  # noqa: SLF001
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

    supports_mid_turn_absorb = True

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
        assistant_delivery_mode: str = "final",
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
        self._turn_active = False
        self._assistant_delivery_mode = assistant_delivery_mode

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        delta: str,
    ) -> None:
        """Accumulate tokens and send typing indicator."""
        self._turn_active = True
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
        """Flush buffered text (immediate mode) and send typing indicator."""
        self._turn_active = True

        # In immediate mode, deliver any accumulated assistant text before
        # the tool starts executing so the user sees the message right away.
        if self._assistant_delivery_mode == "immediate" and self._accumulated_text:
            adapter = self._get_adapter()
            if adapter is not None:
                await self._send_text(self._accumulated_text, adapter=adapter)
                self._accumulated_text = ""
                return  # typing indicator is implicit after a sent message

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
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """No-op for tool results."""

    async def flush_buffered_text(self) -> None:
        """Flush accumulated text to the channel without ending the turn.

        Called by the delivery service before sending a step_question
        notification so the assistant's preceding message arrives first.
        """
        if self._assistant_delivery_mode != "immediate":
            return
        if not self._accumulated_text:
            return
        adapter = self._get_adapter()
        if adapter is None:
            return
        await self._send_text(self._accumulated_text, adapter=adapter)
        self._accumulated_text = ""

    async def on_turn_complete(self, result: Any) -> None:
        """Send the accumulated response to the channel."""
        raw_result_attachments: list[dict[str, Any]] = []
        if result is not None:
            result_attachments = getattr(result, "attachments", None)
            if isinstance(result_attachments, list):
                for att in result_attachments:
                    if isinstance(att, dict):
                        raw_result_attachments.append(att)
        manager = self._channel_manager_ref()
        outbound_media, attachment_fallback_lines, _had_attachment_failures = (
            await prepare_media_attachments(
                raw_result_attachments,
                session_factory=(getattr(manager, "_session_factory", None) if manager is not None else None),
                artifact_store=(getattr(manager, "_artifact_store", None) if manager is not None else None),
            )
        )

        if not self._turn_active and not outbound_media and not attachment_fallback_lines:
            # This observer was registered for a queued message that hasn't
            # started yet. Keep it alive for the next turn.
            return
        # Remove self from observers
        self._turn_scheduler_remove()

        adapter = self._get_adapter()
        if adapter is None:
            return

        content = _append_attachment_fallback(self._accumulated_text, attachment_fallback_lines)
        self._accumulated_text = ""
        if not content and not outbound_media:
            return

        try:
            delivery_id = await adapter.send_message(
                OutboundMessage(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                    chat_id=self._chat_id,
                    content=content,
                    reply_to_id=self._reply_to_id,
                    thread_id=self._thread_id,
                    media=outbound_media,
                )
            )
            if self._channel_type == "signal" and (
                not isinstance(delivery_id, str) or not delivery_id.strip()
            ):
                logger.warning(
                    "channel observer: primary Signal delivery returned no message id",
                    extra={
                        "extra_data": {
                            "channel_type": self._channel_type,
                            "account_id": self._account_id,
                            "conversation_id": self._conversation_id,
                            "has_text": bool(content),
                            "media_count": len(outbound_media),
                            "fallback_candidate_count": len(raw_result_attachments),
                        }
                    },
                )
                fallback_sent = await self._send_signal_image_fallback(
                    adapter=adapter,
                    text=content,
                    attachments=raw_result_attachments,
                )
                if not fallback_sent:
                    return
            CHANNEL_OUTBOUND_TOTAL.labels(
                channel_type=self._channel_type,
                account_id=self._account_id,
            ).inc()
        except Exception:
            logger.warning(
                "channel observer: final delivery failed",
                extra={
                    "extra_data": {
                        "channel_type": self._channel_type,
                        "account_id": self._account_id,
                        "conversation_id": self._conversation_id,
                        "has_text": bool(content),
                        "media_count": len(outbound_media),
                    }
                },
                exc_info=True,
            )

    async def _send_signal_image_fallback(
        self,
        *,
        adapter: BaseChannelAdapter,
        text: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        if self._channel_type != "signal":
            return False
        preview = _signal_image_preview_payload(attachments)
        if preview is None:
            return False
        fallback_text = text.strip()
        if preview["url"] not in fallback_text:
            fallback_text = (
                f"{fallback_text}\n\n{preview['url']}" if fallback_text else preview["url"]
            )
        logger.info(
            "channel observer: using Signal image fallback preview",
            extra={
                "extra_data": {
                    "channel_type": self._channel_type,
                    "account_id": self._account_id,
                    "conversation_id": self._conversation_id,
                    "has_text": bool(text.strip()),
                    "preview_url_present": True,
                }
            },
        )
        try:
            fallback_id = await adapter.send_message(
                OutboundMessage(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                    chat_id=self._chat_id,
                    content=fallback_text,
                    reply_to_id=self._reply_to_id,
                    thread_id=self._thread_id,
                    platform_data={"signal_preview": preview},
                )
            )
        except Exception:
            logger.warning(
                "channel observer: Signal image fallback failed",
                extra={
                    "extra_data": {
                        "channel_type": self._channel_type,
                        "account_id": self._account_id,
                        "conversation_id": self._conversation_id,
                    }
                },
                exc_info=True,
            )
            return False
        return isinstance(fallback_id, str) and bool(fallback_id.strip())

    async def on_turn_error(self, conversation_id: str, error: Any) -> None:
        """Send error message to the channel."""
        if not self._turn_active:
            return
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

    def absorb_queued_observer(self, observer: Any) -> bool:
        """Adopt the latest reply anchor from a queued sibling observer."""

        if not isinstance(observer, ChannelTurnObserver):
            return False
        if observer is self:
            return True
        if (
            observer._conversation_id != self._conversation_id  # noqa: SLF001
            or observer._channel_type != self._channel_type  # noqa: SLF001
            or observer._account_id != self._account_id  # noqa: SLF001
            or observer._chat_id != self._chat_id  # noqa: SLF001
            or observer._thread_id != self._thread_id  # noqa: SLF001
        ):
            return False
        if observer._reply_to_id:  # noqa: SLF001
            self._reply_to_id = observer._reply_to_id  # noqa: SLF001
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_adapter(self) -> BaseChannelAdapter | None:
        manager = self._channel_manager_ref()
        if manager is None:
            return None
        return manager.get_adapter(self._account_id)

    async def _send_text(self, text: str, *, adapter: BaseChannelAdapter | None = None) -> None:
        from cognis.channels.formatting import format_for_channel

        if not text:
            return
        resolved_adapter = adapter or self._get_adapter()
        if resolved_adapter is None:
            return

        if self._channel_type == "signal":
            with contextlib.suppress(Exception):
                await resolved_adapter.send_message(
                    OutboundMessage(
                        channel_type=self._channel_type,
                        account_id=self._account_id,
                        chat_id=self._chat_id,
                        content=text,
                        reply_to_id=self._reply_to_id,
                        thread_id=self._thread_id,
                    )
                )
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=self._channel_type,
                    account_id=self._account_id,
                ).inc()
                self._reply_to_id = None
            return

        chunks = format_for_channel(text, resolved_adapter.capabilities)
        for chunk in chunks:
            with contextlib.suppress(Exception):
                await resolved_adapter.send_message(
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
                self._reply_to_id = None

    def _turn_scheduler_remove(self) -> None:
        """Remove self from turn scheduler observers.

        Called after each turn completes so a new observer created for
        the next inbound message starts with a clean list.
        """
        self._turn_scheduler.remove_observer(self._conversation_id, self)
def _signal_image_preview_payload(attachments: list[dict[str, Any]]) -> dict[str, str] | None:
    for attachment in attachments:
        mime_type = attachment.get("mime_type")
        url = attachment.get("url")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            continue
        if not isinstance(url, str) or not url:
            continue
        filename = attachment.get("filename")
        preview = {
            "url": url,
            "image": url,
        }
        if isinstance(filename, str) and filename:
            preview["title"] = filename
        return preview
    return None
