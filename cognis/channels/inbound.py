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
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.audio.preprocessing import (  # noqa: F401 — re-exported for back-compat
    STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES as _STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES,
)
from cognis.audio.preprocessing import _stt_passthrough_target  # noqa: F401 — re-exported for tests
from cognis.audio.preprocessing import (  # noqa: F401 — re-exported for back-compat
    normalized_audio_filename as _normalized_audio_filename,
)
from cognis.audio.preprocessing import (  # noqa: F401 — re-exported for tests
    prepare_audio_for_stt as _prepare_audio_for_stt,
)
from cognis.audio.preprocessing import (
    stt_supported_audio_mime_types as _stt_supported_audio_mime_types,  # noqa: F401 — re-exported for tests
)
from cognis.audio.preprocessing import (  # noqa: F401 — re-exported for back-compat
    transcode_audio_for_stt as _transcode_audio_for_stt,
)
from cognis.audio.transcription import (
    resolve_stt_supported_mime_types,
)
from cognis.channels.delivery import _append_attachment_fallback, prepare_media_attachments
from cognis.channels.group_context import (
    GroupContextPolicy,
    GroupContextSettingsError,
    group_context_policy,
)
from cognis.channels.protocol import CHANNEL_OUTBOUND_TOTAL, BaseChannelAdapter
from cognis.core.attachment_utils import attachment_placeholder_text
from cognis.core.message_envelope import message_metadata
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelDeliveryDescriptor,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)
from cognis.models.session import ConversationContext, SessionEvent
from cognis.store.models import Conversation

logger = get_logger(__name__)

_ASSISTANT_DELIVERY_MODE_FINAL_ONLY = "final_only"
_ASSISTANT_DELIVERY_MODE_CONCATENATED = "concatenated"
_ASSISTANT_DELIVERY_MODE_IMMEDIATE = "immediate"
_LEGACY_ASSISTANT_DELIVERY_MODE_FINAL = "final"
_ASSISTANT_DELIVERY_MODES = frozenset(
    {
        _ASSISTANT_DELIVERY_MODE_FINAL_ONLY,
        _ASSISTANT_DELIVERY_MODE_CONCATENATED,
        _ASSISTANT_DELIVERY_MODE_IMMEDIATE,
    }
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    with contextlib.suppress(ValueError):
        parsed = int(value.strip())
        if parsed >= minimum:
            return parsed
    return default


_MATRIX_THREAD_FORK_LOOKUP_MAX_SESSIONS = _env_int(
    "COGNIS_MATRIX_THREAD_FORK_LOOKUP_MAX_SESSIONS",
    12,
)
_MATRIX_THREAD_FORK_LOOKUP_MAX_EVENTS = _env_int(
    "COGNIS_MATRIX_THREAD_FORK_LOOKUP_MAX_EVENTS",
    5000,
)
_MATRIX_THREAD_FORK_CUTOFF_LOOKAHEAD_EVENTS = _env_int(
    "COGNIS_MATRIX_THREAD_FORK_CUTOFF_LOOKAHEAD_EVENTS",
    1000,
)


@dataclass(frozen=True)
class _ThreadForkMatch:
    rank: int
    chain_index: int
    source_session_row: Any
    cutoff_seq: int
    matched_event_id: str
    source_ref: bool = False


def _normalize_assistant_delivery_mode(value: Any) -> str:
    """Normalize channel assistant delivery mode, preserving legacy semantics."""

    mode = str(value or "").strip().lower()
    if mode == _LEGACY_ASSISTANT_DELIVERY_MODE_FINAL:
        return _ASSISTANT_DELIVERY_MODE_CONCATENATED
    if mode in _ASSISTANT_DELIVERY_MODES:
        return mode
    return _ASSISTANT_DELIVERY_MODE_CONCATENATED


def _assistant_delivery_mode_for_config(config: ChannelAccountConfig) -> str:
    return _normalize_assistant_delivery_mode(
        config.settings.get(
            "assistant_delivery_mode",
            _ASSISTANT_DELIVERY_MODE_CONCATENATED,
        )
    )


_SIGNAL_DEBUG_ENABLED = _env_flag("COGNIS_SIGNAL_DEBUG", False)


def _fallback_attachment_content(
    content: str,
    attachments: list[AttachmentRef],
    media: list[MediaAttachment],
) -> str:
    if content.strip():
        return content
    if attachments:
        return content
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


def _format_system_message_for_channel(channel_type: str, text: str) -> str:
    """Return a compact channel-specific rendering for system command output."""

    if not text:
        return text
    if channel_type == "signal":
        escaped_lines = [
            line.replace("\\", "\\\\").replace("`", "\\`") or " " for line in text.splitlines()
        ]
        formatted = "\n".join(f"`{line}`" for line in escaped_lines)
        return f"*{formatted}*"
    if channel_type != "matrix":
        return text
    longest_backtick_run = 0
    current_run = 0
    for char in text:
        if char == "`":
            current_run += 1
            longest_backtick_run = max(longest_backtick_run, current_run)
        else:
            current_run = 0
    fence = "`" * max(3, longest_backtick_run + 1)
    return f"{fence}text\n{text}\n{fence}"


def _filter_turn_attachments_for_voice_input(
    message: InboundMessage,
    attachments: list[AttachmentRef],
) -> list[AttachmentRef]:
    """Preserve voice audio so the stored artifact remains available to the turn."""

    return attachments


def _conversation_mode_for_message(message: InboundMessage, config: ChannelAccountConfig) -> str:
    key = "dm_conversation_mode" if message.chat_type == "direct" else "group_conversation_mode"
    mode = str(config.settings.get(key) or "default").strip().lower()
    return mode if mode in {"default", "threads"} else "default"


def _thread_start_mode(config: ChannelAccountConfig) -> str:
    mode = str(config.settings.get("thread_start_mode") or "fork").strip().lower()
    return mode if mode in {"fork", "fresh"} else "fork"


def _thread_reference_ids(message: InboundMessage) -> list[str]:
    ids: list[str] = []
    for value in (message.thread_id, message.reply_to_id):
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    root = message.platform_data.get("thread_root_event_id")
    if isinstance(root, str) and root and root not in ids:
        ids.insert(0, root)
    return ids


def _bounded_int_setting(
    settings: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int = 1,
) -> int:
    value = settings.get(name)
    if value is None:
        return default
    with contextlib.suppress(TypeError, ValueError):
        parsed = int(str(value).strip())
        if parsed >= minimum:
            return parsed
    return default


def _effective_thread_message(
    message: InboundMessage,
    config: ChannelAccountConfig,
) -> InboundMessage:
    """Apply channel conversation-mode settings to the inbound message shape."""

    if message.thread_id:
        return message
    if _conversation_mode_for_message(message, config) != "threads":
        return message
    platform_data = dict(message.platform_data)
    platform_data.setdefault("thread_root_event_id", message.message_id)
    platform_data.setdefault("thread_created_by_mode", "threads")
    return message.model_copy(
        update={
            "thread_id": message.message_id,
            "platform_data": platform_data,
        }
    )


def _is_unmentioned_matrix_thread_followup(message: InboundMessage) -> bool:
    return bool(
        message.channel_type == "matrix"
        and message.chat_type == "group"
        and message.thread_id
        and not message.was_mentioned
        and message.platform_data.get("unmentioned_thread_followup_candidate") is True
    )


def _sender_label(message: InboundMessage) -> str:
    return message.sender_name or message.sender_username or message.sender_id


def _fresh_thread_context_message(
    message: InboundMessage,
    *,
    primary_trusted: bool,
) -> dict[str, Any] | None:
    root = message.platform_data.get("thread_root")
    if not isinstance(root, dict):
        return None
    body = root.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    sender = root.get("sender")
    timestamp = root.get("timestamp")
    if isinstance(timestamp, int | float):
        timestamp = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
    if not isinstance(timestamp, datetime | str):
        return None
    sender_label = sender.strip() if isinstance(sender, str) and sender.strip() else "unknown"
    trusted = primary_trusted and sender_label == message.sender_id
    return {
        "content": body.strip(),
        "intention_eligible": False,
        "message_metadata": message_metadata(
            ts=timestamp,
            channel=message.channel_type,
            sender=sender_label,
            untrusted=not trusted,
        ),
    }


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
        credentials_provider: Any = None,
        observed_target_recorder: Any = None,
        managed_channel_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._turn_scheduler = turn_scheduler
        self._llm_provider = llm_provider
        self._session_manager = session_manager
        self._pairing_service = pairing_service
        self._channel_manager_ref = channel_manager_ref
        self._command_dispatcher = command_dispatcher
        self._notification_service = notification_service
        self._credentials_provider = credentials_provider
        self._observed_target_recorder = observed_target_recorder
        self._managed_channel_service = managed_channel_service

    async def process(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        *,
        executor_connection_owner: Any | None = None,
    ) -> None:
        """Process an inbound message through the full pipeline."""
        if not await self._admit_executor_inbound(executor_connection_owner):
            return
        if message.account_id != config.account_id or message.channel_type != config.channel_type:
            logger.warning(
                "channel inbound: message/config binding mismatch",
                extra={
                    "extra_data": {
                        "message_account_id": message.account_id,
                        "config_account_id": config.account_id,
                        "message_channel_type": message.channel_type,
                        "config_channel_type": config.channel_type,
                    }
                },
            )
            return
        message = _effective_thread_message(message, config)

        # 1. Access control
        turn_allowed = self._check_access(message, config)
        try:
            context_policy = group_context_policy(config.settings)
        except GroupContextSettingsError:
            logger.warning(
                "channel inbound: invalid group-context settings; capture disabled",
                extra={"extra_data": {"account_id": config.account_id}},
            )
            context_policy = GroupContextPolicy()
        capture_candidate = self._check_group_context_access(
            message,
            config,
            context_policy,
        )
        if not turn_allowed and not capture_candidate:
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
        user_email = await self._resolve_user(
            message,
            config,
            executor_connection_owner=executor_connection_owner,
            allow_pairing_challenge=turn_allowed,
        )
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
        sender_trusted = message.platform_data.pop("_sender_verified_owner", False) is True
        if turn_allowed:
            try:
                await self._record_observed_target(message, config)
            except Exception:
                logger.warning(
                    "channel inbound: failed to record observed target",
                    exc_info=True,
                    extra={
                        "extra_data": {
                            "channel_type": message.channel_type,
                            "account_id": message.account_id,
                        }
                    },
                )

        pre_normalized_attachments: list[AttachmentRef] | None = None
        pre_normalized_failures = 0
        if turn_allowed and self._managed_channel_service is not None:
            if message.media:
                (
                    pre_normalized_attachments,
                    pre_normalized_failures,
                ) = await self._normalize_media_attachments(
                    message=message,
                    conversation_id=None,
                    user_email=user_email,
                    executor_connection_owner=executor_connection_owner,
                )
            managed = await self._managed_channel_service.admit_inbound(
                message,
                user_email=user_email,
                attachments=pre_normalized_attachments,
            )
            if managed is True:
                return
            if managed is not None:
                observer = self._managed_channel_service.observer(
                    binding_id=managed.binding_id,
                    binding_version=managed.version,
                    owner_epoch=managed.owner_epoch,
                )
                error = await self._turn_scheduler.submit_turn(
                    managed.conversation_id,
                    managed.content,
                    user_email=managed.user_email,
                    intention_eligible=False,
                    attachments=managed.attachments,
                    user_message_metadata=message_metadata(
                        ts=message.timestamp,
                        channel=message.channel_type,
                        sender=_sender_label(message),
                        untrusted=True,
                    ),
                    contextual_messages=managed.contextual_messages,
                    turn_observers=(observer,),
                    client_message_id=managed.message_id,
                    allow_queue=False,
                )
                if error is not None:
                    await self._managed_channel_service.release_after_failure(
                        binding_id=managed.binding_id,
                        admitted_version=managed.version,
                        admitted_owner_epoch=managed.owner_epoch,
                        reason=error.message,
                    )
                return

        capture_allowed = capture_candidate and user_email == config.user_email
        if not turn_allowed:
            if capture_allowed and self._managed_channel_service is not None:
                attachments, _attachment_failures = await self._normalize_media_attachments(
                    message=message,
                    conversation_id=None,
                    user_email=config.user_email,
                )
                await self._managed_channel_service.capture_group_message(
                    message,
                    user_email=config.user_email,
                    policy=context_policy,
                    attachments=attachments,
                )
            return

        # 3. Conversation resolution
        if not await self._admit_executor_inbound(executor_connection_owner):
            return
        conversation_id = await self._resolve_conversation(
            message,
            config,
            user_email,
            executor_connection_owner=executor_connection_owner,
        )
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
        try:
            if message.chat_type == "direct" and self._managed_channel_service is not None:
                await self._managed_channel_service.capture_observed_direct_message(
                    message,
                    user_email=user_email,
                )
        except Exception:
            logger.warning(
                "channel inbound: failed to record observed target",
                exc_info=True,
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                    }
                },
            )

        await self._mark_read(message, config, conversation_id)

        if not await self._admit_executor_inbound(executor_connection_owner):
            return

        command_admitted, cmd_result = await self._run_executor_durable(
            executor_connection_owner,
            self._try_command_dispatch,
            conversation_id=conversation_id,
            content=message.content,
            user_email=user_email,
            channel_default_agent_profile_id=config.default_agent_profile_id,
        )
        if not command_admitted:
            return
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
        if not await self._admit_executor_inbound(executor_connection_owner):
            return
        question_admitted, resolved_question = await self._run_executor_durable(
            executor_connection_owner,
            self._try_resolve_pending_question,
            conversation_id=conversation_id,
            user_email=user_email,
            content=message.content,
        )
        if not question_admitted:
            return
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

        try:
            if not await self._admit_executor_inbound(executor_connection_owner):
                return
            if pre_normalized_attachments is not None:
                attachments = pre_normalized_attachments
                attachment_failures = pre_normalized_failures
            else:
                attachments, attachment_failures = await self._normalize_media_attachments(
                    message=message,
                    conversation_id=conversation_id,
                    user_email=user_email,
                    executor_connection_owner=executor_connection_owner,
                )
        except Exception as exc:
            if self._is_voice_input(message):
                await self._send_error(message, config, str(exc))
                return
            raise
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
        if attachment_failures > 0 and not self._is_voice_input(message):
            noun = "attachment" if attachment_failures == 1 else "attachments"
            await self._send_system_message(
                message,
                config,
                f"I couldn't download {attachment_failures} {noun} from this message. "
                "The file may be unavailable or require authentication. "
                "Please try resending or share the file another way.",
            )
        if message.chat_type == "direct" and self._managed_channel_service is not None:
            await self._managed_channel_service.capture_observed_direct_message(
                message,
                user_email=user_email,
                attachments=attachments,
            )
        user_content = message.content
        turn_attachments = _filter_turn_attachments_for_voice_input(message, attachments)
        if not self._is_voice_input(message):
            user_content = _fallback_attachment_content(user_content, attachments, message.media)
        contextual_messages: list[dict[str, Any]] = []
        group_reservation_token: str | None = None
        group_turn_id: str | None = None
        if capture_allowed and self._managed_channel_service is not None:
            trigger_row = await self._managed_channel_service.capture_group_message(
                message,
                user_email=config.user_email,
                policy=context_policy,
                attachments=attachments,
            )
            if trigger_row is not None:
                group_turn_id = f"turn_{uuid.uuid4().hex[:12]}"
                reservation = await self._managed_channel_service.reserve_group_context(
                    trigger_inbound_id=trigger_row.inbound_id,
                    conversation_id=conversation_id,
                    turn_id=group_turn_id,
                    policy=context_policy,
                )
                if reservation.duplicate_primary:
                    return
                group_reservation_token = reservation.token
                contextual_messages.extend(reservation.contextual_messages)
        if bool(message.platform_data.get("fresh_thread_context")):
            context_message = _fresh_thread_context_message(
                message,
                primary_trusted=sender_trusted,
            )
            if context_message is not None:
                contextual_messages.append(context_message)
        sender = (
            _sender_label(message) if message.chat_type == "group" or not sender_trusted else None
        )
        primary_metadata = message_metadata(
            ts=message.timestamp,
            channel=message.channel_type,
            sender=sender,
            untrusted=not sender_trusted,
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
            channel_delivery=ChannelDeliveryDescriptor(
                channel_type=message.channel_type,
                account_id=message.account_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to_id=message.message_id,
            ),
            channel_manager_ref=self._channel_manager_ref,
            assistant_delivery_mode=_assistant_delivery_mode_for_config(config),
        )

        # 5. Submit turn
        if not await self._admit_executor_inbound(executor_connection_owner):
            if group_reservation_token and group_turn_id:
                await self._managed_channel_service.settle_group_context(
                    group_reservation_token,
                    turn_id=group_turn_id,
                    succeeded=False,
                )
            return

        async def _settle_group_context_admission(
            session: Any,
            admitted_request: Any,
            _created: bool,
        ) -> None:
            if group_reservation_token is None or group_turn_id is None:
                return
            if admitted_request.turn_id != group_turn_id:
                raise RuntimeError("group-context admission turn mismatch")
            await self._managed_channel_service.settle_group_context_in_session(
                session,
                token=group_reservation_token,
                turn_id=group_turn_id,
                require_valid=True,
            )

        turn_admitted, error = await self._run_executor_durable(
            executor_connection_owner,
            self._turn_scheduler.submit_turn,
            conversation_id,
            user_content,
            user_email=user_email,
            intention_eligible=sender_trusted,
            user_message_metadata=primary_metadata,
            contextual_messages=contextual_messages,
            attachments=[item.model_dump(mode="json") for item in turn_attachments],
            turn_observers=[observer],
            channel_deliverable=True,
            client_message_id=message.message_id,
            channel_default_agent_profile_id=config.default_agent_profile_id,
            channel_account_id=config.account_id,
            channel_delivery=ChannelDeliveryDescriptor(
                channel_type=message.channel_type,
                account_id=message.account_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to_id=message.message_id,
            ),
            turn_id=group_turn_id,
            admission_transaction_participant=(
                _settle_group_context_admission if group_reservation_token else None
            ),
        )
        if not turn_admitted:
            if group_reservation_token and group_turn_id:
                await self._managed_channel_service.settle_group_context(
                    group_reservation_token,
                    turn_id=group_turn_id,
                    succeeded=False,
                )
            return

        if error is not None:
            if group_reservation_token and group_turn_id:
                await self._managed_channel_service.settle_group_context(
                    group_reservation_token,
                    turn_id=group_turn_id,
                    succeeded=False,
                )
            # Send error back to channel
            await self._send_error(message, config, error.message)
        elif group_reservation_token and group_turn_id:
            # The durable store already settled this in the admission transaction.
            # This idempotent fallback covers tests and non-durable scheduler modes.
            await self._managed_channel_service.settle_group_context(
                group_reservation_token,
                turn_id=group_turn_id,
                succeeded=True,
            )

        logger.info(
            "channel inbound: turn submitted",
            extra={
                "extra_data": {
                    "channel_type": message.channel_type,
                    "conversation_id": conversation_id,
                }
            },
        )

    async def _record_observed_target(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
    ) -> None:
        """Persist controller-observed routing data for outbound channel tools."""

        if self._observed_target_recorder is not None:
            await self._observed_target_recorder.record(message, config)

    async def _admit_executor_inbound(self, owner: Any | None) -> bool:
        """Transactionally revalidate the originating executor before persistence."""

        if owner is None:
            return True
        from cognis.core.executor_connection_ownership import ExecutorConnectionOwnership

        async with self._session_factory() as session:
            if not await ExecutorConnectionOwnership.lock_current(session, owner):
                await session.rollback()
                return False
            await session.commit()
            return True

    @staticmethod
    def _executor_admission_guard(owner: Any | None) -> Any | None:
        if owner is None:
            return None

        async def _guard(session: Any) -> bool:
            from cognis.core.executor_connection_ownership import (
                ExecutorConnectionOwnership,
            )

            return await ExecutorConnectionOwnership.lock_current(session, owner)

        return _guard

    async def _run_executor_durable(
        self,
        owner: Any | None,
        callback: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, Any]:
        if owner is None:
            return True, await callback(*args, **kwargs)
        ownership = getattr(self, "_executor_connection_ownership", None)
        if ownership is None:
            from cognis.core.executor_connection_ownership import (
                ExecutorConnectionOwnership,
            )

            ownership = ExecutorConnectionOwnership(
                self._session_factory,
                owner.owner_id,
            )
        return await ownership.run_durable_callback_if_current(
            owner,
            callback,
            *args,
            **kwargs,
        )

    async def _mark_read(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        conversation_id: str,
    ) -> None:
        """Best-effort platform read receipt after the inbound message is accepted."""
        try:
            manager = self._channel_manager_ref()
            if manager is None:
                return
            adapter = manager.get_adapter(config.account_id)
            await adapter.mark_read(message.chat_id, message.message_id)
        except Exception:
            logger.warning(
                "channel inbound: failed to mark message as read",
                exc_info=True,
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                        "conversation_id": conversation_id,
                    }
                },
            )

    def _is_voice_input(self, message: InboundMessage) -> bool:
        return bool(message.platform_data.get("voice_input"))

    async def _voice_stt_supported_mime_types(
        self,
        *,
        acting_user_email: str | None = None,
    ) -> list[str] | None:
        if self._llm_provider is None:
            return None
        try:
            return await resolve_stt_supported_mime_types(
                self._llm_provider,
                acting_user_email=acting_user_email,
            )
        except Exception:
            logger.debug("channel inbound: failed to resolve STT audio policy", exc_info=True)
            return None

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
                return message.was_mentioned or _is_unmentioned_matrix_thread_followup(message)
            if config.group_policy == "allowlist":
                return message.sender_id in config.allowed_senders
            # "open" and "pairing" — allow pipeline to continue
            return True

        return True

    @staticmethod
    def _check_group_context_access(
        message: InboundMessage,
        config: ChannelAccountConfig,
        policy: GroupContextPolicy,
    ) -> bool:
        """Allow silent capture only for an enabled, authorized group route."""

        if not policy.enabled or message.chat_type != "group" or message.is_bot_output:
            return False
        if config.group_policy == "disabled":
            return False
        if config.group_policy == "allowlist":
            return message.sender_id in config.allowed_senders
        return True

    # ------------------------------------------------------------------
    # Identity mapping
    # ------------------------------------------------------------------

    async def _resolve_user(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        *,
        executor_connection_owner: Any | None = None,
        allow_pairing_challenge: bool = True,
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
                message.platform_data["_sender_verified_owner"] = (
                    contact.user_email == config.user_email
                )
                return contact.user_email

        if policy == "pairing" and _is_unmentioned_matrix_thread_followup(message):
            return None

        if policy == "pairing" and not allow_pairing_challenge:
            return None

        if policy == "pairing":
            user_email = await self._pairing_service.ensure_verified_sender(
                message=message,
                config=config,
                executor_connection_owner=executor_connection_owner,
            )
            message.platform_data["_sender_verified_owner"] = user_email == config.user_email
            return user_email

        message.platform_data["_sender_verified_owner"] = False
        return config.user_email

    # ------------------------------------------------------------------
    # Conversation resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _base_context_ref(message: InboundMessage) -> str:
        return f"{message.channel_type}:{message.account_id}:{message.chat_id}"

    @staticmethod
    def _context_ref(message: InboundMessage) -> str:
        context_ref = InboundPipeline._base_context_ref(message)
        if message.thread_id:
            return f"{context_ref}:{message.thread_id}"
        return context_ref

    @staticmethod
    def _conversation_context(
        message: InboundMessage,
        *,
        context_ref: str,
        assistant_delivery_mode: str | None = None,
    ) -> ConversationContext:
        platform_data = {
            "channel_type": message.channel_type,
            "account_id": message.account_id,
            "chat_id": message.chat_id,
            "chat_name": message.chat_name,
            "chat_type": message.chat_type,
            "thread_id": message.thread_id,
            "thread_root_event_id": message.platform_data.get("thread_root_event_id")
            or message.thread_id,
            "thread_conversation_mode": message.platform_data.get("thread_created_by_mode")
            or "default",
        }
        if assistant_delivery_mode:
            platform_data["assistant_delivery_mode"] = assistant_delivery_mode
        return ConversationContext(
            type=message.channel_type,
            ref=context_ref,
            platform_data=platform_data,
        )

    @staticmethod
    def _conversation_matches_channel_type(conversation: Conversation, channel_type: str) -> bool:
        context_type = str(getattr(conversation, "context_type", "") or "").strip().lower()
        return bool(context_type) and context_type == channel_type.strip().lower()

    async def _refresh_channel_context_delivery_mode(
        self,
        *,
        conversation_id: str,
        channel_type: str,
        assistant_delivery_mode: str,
        executor_connection_owner: Any | None = None,
    ) -> None:
        """Keep existing channel conversation context aligned with channel delivery settings."""

        from cognis.store.queries import get_conversation, update_conversation_context_data

        async with self._session_factory() as session:
            guard = self._executor_admission_guard(executor_connection_owner)
            if guard is not None and not await guard(session):
                await session.rollback()
                return
            conversation = await get_conversation(session, conversation_id)
            if conversation is None:
                return
            if not self._conversation_matches_channel_type(conversation, channel_type):
                return
            context_data = dict(conversation.context_data or {})
            if context_data.get("assistant_delivery_mode") == assistant_delivery_mode:
                return
            context_data["assistant_delivery_mode"] = assistant_delivery_mode

            updated = await update_conversation_context_data(
                session,
                conversation_id,
                context_data=context_data,
            )
            if updated:
                await session.commit()

    async def _latest_conversation_for_context(
        self,
        *,
        user_email: str,
        agent_id: str,
        context_ref: str,
    ) -> Conversation | None:
        from cognis.store.queries import get_latest_active_conversation_for_context

        async with self._session_factory() as session:
            return await get_latest_active_conversation_for_context(
                session,
                user_email=user_email,
                agent_id=agent_id,
                context_ref=context_ref,
            )

    async def _find_thread_fork_source(
        self,
        *,
        room_conversation: Conversation,
        message: InboundMessage,
        config: ChannelAccountConfig,
    ) -> tuple[Any, int, str] | None:
        """Find the backing session and cutoff seq for the Matrix thread root event."""

        if room_conversation.active_session_id is None:
            return None
        reference_ids = _thread_reference_ids(message)
        if not reference_ids:
            return None

        from cognis.store.queries import get_root_session_chain

        target_rank: dict[str, tuple[int, str]] = {}
        for rank, event_id in enumerate(reference_ids):
            target_rank[event_id] = (rank, event_id)
            target_rank[f"client:{event_id}"] = (rank, event_id)

        async with self._session_factory() as session:
            chain, _truncated = await get_root_session_chain(
                session,
                room_conversation.conversation_id,
                room_conversation.active_session_id,
            )
        if not chain:
            return None

        max_sessions = _bounded_int_setting(
            config.settings,
            "thread_fork_lookup_max_sessions",
            _MATRIX_THREAD_FORK_LOOKUP_MAX_SESSIONS,
        )
        max_events = _bounded_int_setting(
            config.settings,
            "thread_fork_lookup_max_events",
            _MATRIX_THREAD_FORK_LOOKUP_MAX_EVENTS,
        )
        cutoff_lookahead = _bounded_int_setting(
            config.settings,
            "thread_fork_cutoff_lookahead_events",
            _MATRIX_THREAD_FORK_CUTOFF_LOOKAHEAD_EVENTS,
        )
        chain_by_id = {
            row.session_id: row
            for row in chain
            if isinstance(getattr(row, "session_id", None), str)
        }
        chain_index_by_id = {
            row.session_id: index
            for index, row in enumerate(chain)
            if isinstance(getattr(row, "session_id", None), str)
        }

        best: _ThreadForkMatch | None = None
        searched_sessions = 0
        searched_events = 0
        exhausted = False
        for chain_index, source_session in reversed(list(enumerate(chain))):
            if searched_sessions >= max_sessions or searched_events >= max_events:
                exhausted = True
                break
            remaining_events = max_events - searched_events
            if remaining_events <= 0:
                exhausted = True
                break
            session_model = self._session_row_to_model(source_session)
            try:
                events = await self._session_manager._read_history_events(
                    session_model,
                    last_n=remaining_events,
                    allow_missing_stream=True,
                )
            except Exception:
                logger.warning(
                    "channel inbound: failed to read Matrix thread fork source session",
                    extra={
                        "extra_data": {
                            "conversation_id": room_conversation.conversation_id,
                            "session_id": getattr(source_session, "session_id", None),
                            "thread_id": message.thread_id,
                        }
                    },
                    exc_info=True,
                )
                searched_sessions += 1
                continue
            searched_sessions += 1
            searched_events += len(events)
            match = self._find_thread_match_in_events(
                events,
                target_rank=target_rank,
            )
            if match is None:
                continue
            source_ref_match = await self._source_ref_thread_match(
                matched_event=match[1],
                rank=match[0],
                matched_id=match[2],
                chain_by_id=chain_by_id,
                chain_index_by_id=chain_index_by_id,
                cutoff_lookahead=cutoff_lookahead,
            )
            if source_ref_match is not None and source_ref_match.rank == 0:
                best = source_ref_match
                break
            candidate = source_ref_match or _ThreadForkMatch(
                rank=match[0],
                chain_index=chain_index,
                source_session_row=source_session,
                cutoff_seq=self._thread_match_cutoff_seq(match[1], events),
                matched_event_id=match[2],
            )
            if best is None or self._prefer_thread_fork_match(candidate, best):
                best = candidate

        if exhausted:
            message.platform_data["thread_fork_anchor_lookup"] = "exhausted"
            logger.debug(
                "channel inbound: Matrix thread fork source lookup exhausted budget",
                extra={
                    "extra_data": {
                        "conversation_id": room_conversation.conversation_id,
                        "thread_id": message.thread_id,
                        "searched_sessions": searched_sessions,
                        "searched_events": searched_events,
                        "max_sessions": max_sessions,
                        "max_events": max_events,
                    }
                },
            )
        if best is None:
            message.platform_data.setdefault("thread_fork_anchor_lookup", "not_found")
            return None
        message.platform_data["thread_fork_anchor_lookup"] = (
            "source_ref" if best.source_ref else "bounded_scan"
        )
        return best.source_session_row, best.cutoff_seq, best.matched_event_id

    @staticmethod
    def _find_thread_match_in_events(
        events: list[Any],
        *,
        target_rank: dict[str, tuple[int, str]],
    ) -> tuple[int, Any, str] | None:
        event_by_rank: list[tuple[int, Any, str]] = []
        for event in events:
            data = event.data or {}
            candidates = [
                data.get("message_id"),
                data.get("client_message_id"),
                data.get("platform_message_id"),
            ]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate in target_rank:
                    rank, matched = target_rank[candidate]
                    event_by_rank.append((rank, event, matched))
                    break
        if not event_by_rank:
            return None
        return min(event_by_rank, key=lambda item: (item[0], item[1].seq))

    async def _source_ref_thread_match(
        self,
        *,
        matched_event: Any,
        rank: int,
        matched_id: str,
        chain_by_id: dict[str, Any],
        chain_index_by_id: dict[str, int],
        cutoff_lookahead: int,
    ) -> _ThreadForkMatch | None:
        data = matched_event.data or {}
        source_session_id = data.get("source_session_id")
        source_seq_value = data.get("source_seq")
        if not isinstance(source_session_id, str) or source_session_id not in chain_by_id:
            return None
        try:
            source_seq = int(source_seq_value)
            if source_seq <= 0:
                return None
            source_session = chain_by_id[source_session_id]
            source_model = self._session_row_to_model(source_session)
            source_events = await self._session_manager._read_history_events(
                source_model,
                after_seq=source_seq - 1,
                limit=cutoff_lookahead,
                allow_missing_stream=True,
            )
            source_event = next((event for event in source_events if event.seq == source_seq), None)
            if source_event is None:
                return None
            cutoff_seq = self._thread_match_cutoff_seq(source_event, source_events)
            return _ThreadForkMatch(
                rank=rank,
                chain_index=chain_index_by_id[source_session_id],
                source_session_row=source_session,
                cutoff_seq=cutoff_seq,
                matched_event_id=matched_id,
                source_ref=True,
            )
        except (TypeError, ValueError):
            return None
        except Exception:
            logger.warning(
                "channel inbound: failed to read Matrix thread source-ref session",
                extra={"extra_data": {"source_session_id": source_session_id}},
                exc_info=True,
            )
        return None

    @staticmethod
    def _thread_match_cutoff_seq(matched_event: Any, events: list[Any]) -> int:
        turn_id = (matched_event.data or {}).get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return max(
                (event.seq for event in events if (event.data or {}).get("turn_id") == turn_id),
                default=matched_event.seq,
            )
        return matched_event.seq

    @staticmethod
    def _prefer_thread_fork_match(
        candidate: _ThreadForkMatch,
        current: _ThreadForkMatch,
    ) -> bool:
        if candidate.rank != current.rank:
            return candidate.rank < current.rank
        if candidate.source_ref != current.source_ref:
            return candidate.source_ref
        return candidate.chain_index < current.chain_index

    @staticmethod
    def _session_row_to_model(row: Any) -> Any:
        from cognis.core.session import _to_session_model

        return _to_session_model(row)

    @staticmethod
    def _conversation_row_to_model(row: Conversation) -> Any:
        from cognis.core.session import _to_conversation_model

        return _to_conversation_model(row)

    async def _fork_thread_conversation(
        self,
        *,
        room_conversation: Conversation,
        source_session_row: Any,
        cutoff_seq: int,
        matched_event_id: str,
        message: InboundMessage,
        user_email: str,
        context_ref: str,
        assistant_delivery_mode: str,
        executor_connection_owner: Any | None = None,
    ) -> str | None:
        from cognis.api.serializers import agent_to_response
        from cognis.models.agent import AgentDefinition
        from cognis.store.queries import get_agent

        async with self._session_factory() as session:
            agent_row = await get_agent(session, room_conversation.agent_id)
        if agent_row is None:
            return None
        agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation, _session, _copied = await self._session_manager.fork_into_new_conversation(
            source_session=self._session_row_to_model(source_session_row),
            source_conversation=self._conversation_row_to_model(room_conversation),
            agent=agent,
            user_email=user_email,
            title=message.chat_name or f"{message.channel_type} chat",
            intention=f"Matrix thread from {matched_event_id}",
            context=self._conversation_context(
                message,
                context_ref=context_ref,
                assistant_delivery_mode=assistant_delivery_mode,
            ),
            max_source_seq=cutoff_seq,
            snapshot_extras={
                "forked_from_channel": message.channel_type,
                "forked_from_chat_id": message.chat_id,
                "forked_from_platform_event_id": matched_event_id,
                "thread_id": message.thread_id,
            },
            admission_guard=self._executor_admission_guard(executor_connection_owner),
        )
        return conversation.conversation_id

    async def _resolve_conversation(
        self,
        message: InboundMessage,
        config: ChannelAccountConfig,
        user_email: str,
        *,
        executor_connection_owner: Any | None = None,
    ) -> str | None:
        """Find or create a conversation for this channel message.

        Uses the conversation context ref (e.g., "signal:+1234567890:chat123")
        to find an existing conversation, or creates a new one.
        """
        context_ref = self._context_ref(message)
        base_context_ref = self._base_context_ref(message)
        unmentioned_thread_followup = _is_unmentioned_matrix_thread_followup(message)
        assistant_delivery_mode = _assistant_delivery_mode_for_config(config)

        # Check for default conversation
        if config.default_conversation_id and not unmentioned_thread_followup:
            await self._refresh_channel_context_delivery_mode(
                conversation_id=config.default_conversation_id,
                channel_type=message.channel_type,
                assistant_delivery_mode=assistant_delivery_mode,
                executor_connection_owner=executor_connection_owner,
            )
            return config.default_conversation_id

        # Try to find existing conversation by context ref
        existing = await self._latest_conversation_for_context(
            user_email=user_email,
            agent_id=config.agent_id,
            context_ref=context_ref,
        )
        if existing is not None:
            if self._conversation_matches_channel_type(existing, message.channel_type):
                await self._refresh_channel_context_delivery_mode(
                    conversation_id=existing.conversation_id,
                    channel_type=message.channel_type,
                    assistant_delivery_mode=assistant_delivery_mode,
                    executor_connection_owner=executor_connection_owner,
                )
            return existing.conversation_id

        if unmentioned_thread_followup:
            logger.info(
                "channel inbound: unmentioned Matrix thread follow-up has no conversation",
                extra={
                    "extra_data": {
                        "channel_type": message.channel_type,
                        "account_id": message.account_id,
                        "chat_id": message.chat_id,
                        "thread_id": message.thread_id,
                    }
                },
            )
            return None

        # Create new conversation if allowed
        if not config.allow_new_conversations:
            return None

        mode = _conversation_mode_for_message(message, config)
        if message.thread_id and mode == "default":
            room_conversation = await self._latest_conversation_for_context(
                user_email=user_email,
                agent_id=config.agent_id,
                context_ref=base_context_ref,
            )
            if room_conversation is not None and _thread_start_mode(config) == "fork":
                source = await self._find_thread_fork_source(
                    room_conversation=room_conversation,
                    message=message,
                    config=config,
                )
                if source is not None:
                    source_session_row, cutoff_seq, matched_id = source
                    fork_admitted, forked_id = await self._run_executor_durable(
                        executor_connection_owner,
                        self._fork_thread_conversation,
                        room_conversation=room_conversation,
                        source_session_row=source_session_row,
                        cutoff_seq=cutoff_seq,
                        matched_event_id=matched_id,
                        message=message,
                        user_email=user_email,
                        context_ref=context_ref,
                        assistant_delivery_mode=assistant_delivery_mode,
                        executor_connection_owner=None,
                    )
                    if not fork_admitted:
                        return None
                    if forked_id is not None:
                        return forked_id
                logger.info(
                    "channel inbound: thread source event not found; using fresh thread context",
                    extra={
                        "extra_data": {
                            "channel_type": message.channel_type,
                            "account_id": message.account_id,
                            "chat_id": message.chat_id,
                            "thread_id": message.thread_id,
                        }
                    },
                )
            message.platform_data["fresh_thread_context"] = True

        try:
            conversation, _ = await self._session_manager.create_conversation_with_root_session(
                user_email=user_email,
                agent_id=config.agent_id,
                context=self._conversation_context(
                    message,
                    context_ref=context_ref,
                    assistant_delivery_mode=assistant_delivery_mode,
                ),
                title=message.chat_name or f"{message.channel_type} chat",
                title_source="channel_seed",
                admission_guard=self._executor_admission_guard(executor_connection_owner),
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
                    content=_format_system_message_for_channel(message.channel_type, text),
                    reply_to_id=message.message_id,
                )
            )

    async def _try_command_dispatch(
        self,
        *,
        conversation_id: str,
        content: str,
        user_email: str,
        channel_default_agent_profile_id: str | None = None,
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
        session_model.channel_default_agent_profile_id = channel_default_agent_profile_id
        durable_running = getattr(self._turn_scheduler, "durable_running_turn_state", None)
        runtime_turn = (
            await durable_running(conversation_id)
            if callable(durable_running)
            else self._turn_scheduler.running_turn_state(conversation_id)
        )
        has_active = runtime_turn is not None
        has_busy = has_active or self._turn_scheduler.has_active_turn(conversation_id)

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
            if notif.notification_type in {"step_question", "auth_challenge"}
            and notif.task_id is None
            and not (
                notif.notification_type == "auth_challenge"
                and isinstance(notif.payload, dict)
                and notif.payload.get("kind") == "oauth_authorization"
            )
        ]
        if not direct_questions:
            return None

        # Resolve the most recent pending direct-chat question
        target = direct_questions[0]
        if target.notification_type == "auth_challenge":
            from cognis.core.notification_resolution import build_auth_challenge_resolution_data

            try:
                data = await build_auth_challenge_resolution_data(
                    notification=target,
                    decision="continue",
                    user_email=user_email,
                    credentials_provider=self._credentials_provider,
                    response=content,
                )
            except ValueError:
                return False
            resolved = await self._notification_service.resolve(
                target.notification_id,
                "continue",
                data,
                user_email=user_email,
            )
        else:
            from cognis.core.question_sets import plain_text_reply_for_questions

            try:
                data = plain_text_reply_for_questions(
                    content,
                    target.payload.get("questions") if isinstance(target.payload, dict) else [],
                )
            except ValueError:
                return False
            resolved = await self._notification_service.resolve(
                target.notification_id,
                "continue",
                data,
                user_email=user_email,
            )
        return resolved

    async def _normalize_media_attachments(
        self,
        *,
        message: InboundMessage,
        conversation_id: str | None,
        user_email: str,
        executor_connection_owner: Any | None = None,
    ) -> tuple[list[AttachmentRef], int]:
        """Download and store channel media attachments.

        Returns ``(refs, failed_count)`` where ``failed_count`` is the number
        of attachments that could not be downloaded.  Callers should surface a
        non-fatal notice to the user when ``failed_count > 0``.
        """
        if not message.media:
            return [], 0
        manager = self._channel_manager_ref()
        if manager is None:
            return [], 0
        adapter = manager.get_adapter(message.account_id)
        if adapter is None:
            return [], 0

        refs: list[AttachmentRef] = []
        failed_count = 0
        async with self._session_factory() as session:
            from cognis.store.queries import create_artifact_record

            stt_supported_mime_types: list[str] | None = None
            if self._is_voice_input(message):
                stt_supported_mime_types = await self._voice_stt_supported_mime_types(
                    acting_user_email=user_email,
                )
            for attachment in message.media:
                try:
                    if (
                        self._is_voice_input(message)
                        and str(attachment.mime_type or "").startswith("audio/")
                        and hasattr(adapter, "download_attachment_for_stt")
                    ):
                        fetched = await adapter.download_attachment_for_stt(
                            message,
                            attachment,
                            supported_mime_types=stt_supported_mime_types,
                        )
                    else:
                        fetched = await adapter.download_attachment(message, attachment)
                    if fetched is None:
                        failed_count += 1
                        continue
                    content, content_type, filename = fetched
                    kind = _kind_for_media(content_type)
                    artifact_id = manager._artifact_store.generate_id("att")  # noqa: SLF001
                    guard = self._executor_admission_guard(executor_connection_owner)
                    if guard is not None and not await guard(session):
                        await session.rollback()
                        return [], len(message.media)
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
                    await session.commit()
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
                    await session.rollback()
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
                    if self._is_voice_input(message) and str(attachment.mime_type or "").startswith(
                        "audio/"
                    ):
                        raise
                    failed_count += 1
                    continue
        return refs, failed_count


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
        assistant_delivery_mode: str = _ASSISTANT_DELIVERY_MODE_CONCATENATED,
        channel_delivery: ChannelDeliveryDescriptor | None = None,
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
        self._assistant_delivery_mode = _normalize_assistant_delivery_mode(assistant_delivery_mode)
        self._channel_delivery = channel_delivery

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None = None,
        delta: str = "",
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        """Accumulate tokens and send typing indicator."""
        if not delta and turn_id is not None:
            delta = turn_id
            turn_id = None
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
        turn_id: str | None = None,
        assistant_phase_index: int | None = None,
    ) -> None:
        """Flush buffered text (immediate mode) and send typing indicator."""
        del assistant_phase_index
        self._turn_active = True

        # In immediate mode, deliver any accumulated assistant text before
        # the tool starts executing so the user sees the message right away.
        if (
            self._assistant_delivery_mode == _ASSISTANT_DELIVERY_MODE_IMMEDIATE
            and self._accumulated_text
        ):
            if self._channel_delivery is not None:
                try:
                    await self._turn_scheduler._assert_durable_conversation_fence(  # noqa: SLF001
                        self._conversation_id
                    )
                except Exception:
                    self._accumulated_text = ""
                    return
            adapter = self._get_adapter()
            if adapter is not None:
                await self._send_text(
                    self._accumulated_text,
                    adapter=adapter,
                    delivery_result=SimpleNamespace(session_id=session_id, turn_id=turn_id),
                )
                self._accumulated_text = ""
                return  # typing indicator is implicit after a sent message

        adapter = self._get_adapter()
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.send_typing(self._chat_id)

    async def on_tool_progress(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        progress: dict[str, Any],
        turn_id: str | None = None,
    ) -> None:
        """Send typing indicator while a tool input is being prepared."""
        del conversation_id, session_id, call_id, tool_name, progress, turn_id
        self._turn_active = True
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
        file_diffs: list[dict[str, Any]] | None = None,
        turn_id: str | None = None,
        presentation: dict[str, Any] | None = None,
        assistant_phase_index: int | None = None,
    ) -> None:
        """No-op for tool results."""
        del assistant_phase_index

    async def on_tool_output_chunk(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        delta: str,
        stream: str | None,
        turn_id: str | None = None,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        """No-op for live tool output chunks."""

    async def flush_buffered_text(self) -> None:
        """Flush accumulated text to the channel without ending the turn.

        Called by the delivery service before sending a step_question
        notification so the assistant's preceding message arrives first.
        """
        if self._assistant_delivery_mode != _ASSISTANT_DELIVERY_MODE_IMMEDIATE:
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
                    elif hasattr(att, "model_dump"):
                        raw_result_attachments.append(att.model_dump(exclude_none=True))
        manager = self._channel_manager_ref()
        owner_email: str | None = None
        session_factory = (
            getattr(manager, "_session_factory", None) if manager is not None else None
        )
        if raw_result_attachments and session_factory is not None:
            from cognis.store.queries import get_conversation

            try:
                async with session_factory() as session:
                    conversation = await get_conversation(session, self._conversation_id)
                    owner_email = getattr(conversation, "user_email", None)
            except Exception:
                logger.warning(
                    "channel observer: failed to resolve attachment authorization context",
                    extra={"extra_data": {"conversation_id": self._conversation_id}},
                    exc_info=True,
                )
        (
            outbound_media,
            attachment_fallback_lines,
            _had_attachment_failures,
        ) = await prepare_media_attachments(
            raw_result_attachments,
            session_factory=session_factory,
            artifact_store=(
                getattr(manager, "_artifact_store", None) if manager is not None else None
            ),
            owner_email=owner_email,
            conversation_id=self._conversation_id,
        )

        if not self._turn_active and not outbound_media and not attachment_fallback_lines:
            # This observer was registered for a queued message that hasn't
            # started yet. Keep it alive for the next turn.
            return
        # Remove self from observers
        self._turn_scheduler_remove()

        # Durable direct turns publish their terminal content through the
        # fenced outbox. The observer remains responsible only for live
        # partials/typing while the local fence is current.
        if self._channel_delivery is not None:
            self._accumulated_text = ""
            return

        adapter = self._get_adapter()
        if adapter is None:
            return

        content = self._completion_content(result)
        content = _append_attachment_fallback(content, attachment_fallback_lines)
        chat_mode = getattr(result, "chat_mode", None)
        chat_mode_source = getattr(result, "chat_mode_source", None)
        explicit_chat_mode = chat_mode_source in {"one_shot", "conversation_override"}
        if chat_mode in {"plan", "build"} and explicit_chat_mode and content.strip():
            prefix = (
                f"[{chat_mode} mode enabled for this turn]"
                if chat_mode_source == "one_shot"
                else f"[{chat_mode} mode enabled, use /default to disable]"
            )
            content = f"{prefix}\n\n{content}"
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
            await self._record_delivery_mapping(result, delivery_id)
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

    async def _record_delivery_mapping(self, result: Any, delivery_id: str | None) -> None:
        if not isinstance(delivery_id, str) or not delivery_id.strip():
            return
        session_id = getattr(result, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return
        providers = getattr(self._turn_scheduler, "_providers", None)
        guardrails = getattr(providers, "guardrails", None)
        if guardrails is None:
            return
        turn_id = getattr(result, "turn_id", None)
        # NOTE: recorded as a "lifecycle" event — "channel_delivery" is not a
        # valid Intaris event type, so the previous shape was rejected with a
        # 400 on every call and the reply-threading mapping (platform message
        # id -> turn) silently never reached the durable stream. Lifecycle
        # events outside the visible allowlist stay hidden from chat
        # projections while remaining readable by the reply-threading scan.
        event = SessionEvent(
            type="lifecycle",
            data={
                "event": "channel_delivery",
                "channel_type": self._channel_type,
                "account_id": self._account_id,
                "chat_id": self._chat_id,
                "thread_id": self._thread_id,
                "platform_message_id": delivery_id,
                "message_id": delivery_id,
                "reply_to_id": self._reply_to_id,
                "turn_id": turn_id if isinstance(turn_id, str) and turn_id else None,
            },
        )
        try:
            append_result = await guardrails.record_events(
                session_id=session_id,
                events=[event],
                source="cognis:channel",
            )
            if (
                not bool(getattr(append_result, "ok", True))
                or int(getattr(append_result, "count", 1) or 0) < 1
            ):
                return
            session_cache = getattr(self._turn_scheduler, "_session_cache", None)
            manager = self._channel_manager_ref()
            session_factory = (
                getattr(manager, "_session_factory", None) if manager is not None else None
            )
            if session_cache is not None and session_factory is not None:
                from cognis.core.session import _to_session_model
                from cognis.store.queries import get_session_row

                async with session_factory() as db_session:
                    session_row = await get_session_row(db_session, session_id)
                if session_row is not None:
                    await session_cache.append_recorded_events(
                        _to_session_model(session_row),
                        [event],
                        append_result,
                    )
        except Exception:
            logger.warning(
                "channel observer: failed to record delivery mapping",
                extra={
                    "extra_data": {
                        "channel_type": self._channel_type,
                        "account_id": self._account_id,
                        "session_id": session_id,
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

    def _completion_content(self, result: Any) -> str:
        if self._assistant_delivery_mode == _ASSISTANT_DELIVERY_MODE_FINAL_ONLY:
            final_content = getattr(result, "final_content", None) if result is not None else None
            if isinstance(final_content, str):
                return final_content
        return self._accumulated_text

    async def on_turn_error(self, conversation_id: str, error: Any) -> None:
        """Send error message to the channel."""
        if not self._turn_active:
            return
        self._turn_scheduler_remove()
        if self._channel_delivery is not None:
            return

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

    async def on_system_message(
        self,
        conversation_id: str,
        text: str,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
        retry_reason: str | None = None,
        retry_source_turn_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
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

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        source: str | None = None,
        provider_block_index: int | None = None,
    ) -> None:
        """No-op — thinking blocks are not delivered to channels."""

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

    async def _send_text(
        self,
        text: str,
        *,
        adapter: BaseChannelAdapter | None = None,
        delivery_result: Any | None = None,
    ) -> None:
        from cognis.channels.formatting import split_message

        if not text:
            return
        resolved_adapter = adapter or self._get_adapter()
        if resolved_adapter is None:
            return

        chunks = split_message(text, resolved_adapter.capabilities.max_message_length)
        for chunk in chunks:
            with contextlib.suppress(Exception):
                delivery_id = await resolved_adapter.send_message(
                    OutboundMessage(
                        channel_type=self._channel_type,
                        account_id=self._account_id,
                        chat_id=self._chat_id,
                        content=chunk,
                        reply_to_id=self._reply_to_id,
                        thread_id=self._thread_id,
                    )
                )
                await self._record_delivery_mapping(delivery_result, delivery_id)
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
