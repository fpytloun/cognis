"""Outbound delivery service — EventBus → channel.

Subscribes to EventBus events (TASK_COMPLETED, ESCALATION_CREATED, etc.)
and delivers notifications to the originating channel by looking up the
conversation's channel context in ``ConversationContext.platform_data``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.channels.formatting import split_message
from cognis.channels.protocol import (
    CHANNEL_DELIVERY_ERRORS,
    CHANNEL_OUTBOUND_TOTAL,
    NonRetryableChannelError,
)
from cognis.channels.rich_markdown import (
    render_rich_markdown,
    render_rich_media_fallbacks,
    render_text_markdown,
    rich_media_manifest,
)
from cognis.channels.route_admission import active_managed_binding_id, lock_channel_route
from cognis.core.artifact_inputs import (
    authorize_outbound_artifact_refs_in_session,
    outbound_artifact_grant_is_valid,
    safe_attachment_metadata,
    validate_outbound_attachment_batch,
)
from cognis.core.deliverable_links import (
    DeliverableShareUnavailable,
    DeliverableViewLink,
    private_deliverable_view_url,
    signed_deliverable_view_link,
)
from cognis.core.deliverable_media import resolve_deliverable_media
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.channel import ChannelDeliveryDescriptor, MediaAttachment, OutboundMessage
from cognis.store.models import ChannelDeliveryOutboxRow
from cognis.store.queries import (
    get_agent_direct_conversation,
    get_conversation,
    get_conversation_channel_route,
    get_latest_active_conversation_for_channel_account,
    get_preferred_channel_account_for_agent,
)

logger = get_logger(__name__)

ChunkStartCallback = Callable[[int, int, str, bool], Awaitable[bool]]
ChunkProgressCallback = Callable[[int, int, str, dict[str, Any]], Awaitable[bool]]

_DELIVERY_LEASE_DURATION = timedelta(minutes=2)
_DELIVERY_LEASE_HEARTBEAT_SECONDS = 30.0


@dataclass(frozen=True)
class ChannelProjection:
    """Canonical Markdown chunks plus stable delivery identity and media."""

    chunks: list[str]
    identity: str
    media: tuple[MediaAttachment, ...] = ()
    media_complete: bool = True
    rich: bool = False


class ChannelDeliveryStatus(StrEnum):
    """Outcome of one channel delivery attempt."""

    SENT = "sent"
    INCOMPLETE = "incomplete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    PERMANENT = "permanent"


_TASK_FINAL_SOURCE_TYPE = "task_final_result"
_TASK_RESULT_FOLLOW_UP_SOURCE_TYPE = "task_result_follow_up"
_FOLLOW_UP_RESULT_SOURCE_TYPE = "follow_up_result"
_MANAGED_DELIVERY_MAX_ATTEMPTS = 3
_MANAGED_DELIVERY_MAX_PENDING_AGE = timedelta(minutes=5)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _append_attachment_fallback(content: str, fallback_lines: list[str]) -> str:
    if not fallback_lines:
        return content
    fallback_text = "\n\n".join(line for line in fallback_lines if line)
    if not fallback_text:
        return content
    if not content.strip():
        return fallback_text
    return f"{content}\n\n{fallback_text}"


def _attachment_fallback_text(raw: dict[str, Any]) -> str | None:
    raw_filename = raw.get("filename")
    filename = raw_filename if isinstance(raw_filename, str) else None
    return f"[Attachment unavailable: {filename}]" if filename else None


async def prepare_media_attachments(
    media: list[dict[str, Any]],
    *,
    session_factory: async_sessionmaker[Any] | None,
    artifact_store: Any | None,
    owner_email: str | None = None,
    conversation_id: str | None = None,
) -> tuple[list[MediaAttachment], list[str], bool]:
    try:
        validate_outbound_attachment_batch(media)
    except ValueError:
        logger.warning("channel delivery: outbound attachment policy rejected the batch")
        return [], [], True
    prepared: list[MediaAttachment] = []
    fallback_lines: list[str] = []
    had_failures = False
    for item in media:
        attachment, fallback_line, materialized = await _materialize_media_attachment(
            item,
            session_factory=session_factory,
            artifact_store=artifact_store,
            owner_email=owner_email,
            conversation_id=conversation_id,
        )
        if attachment is not None:
            prepared.append(attachment)
        if fallback_line:
            fallback_lines.append(fallback_line)
        if not materialized:
            had_failures = True
    return prepared, fallback_lines, had_failures


async def _materialize_media_attachment(
    raw: dict[str, Any],
    *,
    session_factory: async_sessionmaker[Any] | None,
    artifact_store: Any | None,
    owner_email: str | None = None,
    conversation_id: str | None = None,
) -> tuple[MediaAttachment | None, str | None, bool]:
    content_b64 = raw.get("content_b64") if isinstance(raw.get("content_b64"), str) else None
    artifact_id = raw.get("artifact_id")
    if content_b64 is None and isinstance(artifact_id, str) and artifact_id:
        from cognis.store.queries import get_artifact_record

        if session_factory is None or artifact_store is None:
            return None, None, False
        try:
            async with session_factory() as session:
                row = await get_artifact_record(session, artifact_id)
                authorized = await _artifact_authorized_for_delivery(
                    session,
                    attachment=raw,
                    artifact=row,
                    owner_email=owner_email,
                )
        except Exception:
            logger.warning("channel delivery: attachment authorization failed", exc_info=True)
            return None, None, False
        expires_at = getattr(row, "expires_at", None)
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        safe_image_only = raw.get("safe_image_only") is True
        safe_raster_mime = str(getattr(row, "mime_type", "")).lower() in {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }
        if (
            row is None
            or row.status == "deleted"
            or not authorized
            or (expires_at is not None and expires_at <= datetime.now(UTC))
            or (safe_image_only and not safe_raster_mime)
        ):
            return None, None, False
        try:
            content, _ct = await artifact_store.async_load(
                row.namespace,
                row.object_id,
                row.filename,
            )
        except Exception:
            logger.warning("channel delivery: failed to materialize attachment", exc_info=True)
            return None, _attachment_fallback_text(raw), False
        if (
            len(content) != row.size_bytes
            or len(content) != raw.get("size_bytes")
            or _ct.casefold() != row.mime_type.casefold()
        ):
            logger.warning("channel delivery: attachment metadata/content mismatch")
            return None, None, False
        return (
            MediaAttachment(
                url=None,
                mime_type=getattr(row, "mime_type", None),
                filename=(raw.get("filename") if isinstance(raw.get("filename"), str) else None)
                or row.filename,
                size_bytes=(
                    raw.get("size_bytes") if isinstance(raw.get("size_bytes"), int) else None
                )
                or len(content),
                content_b64=base64.b64encode(content).decode("ascii"),
            ),
            None,
            True,
        )
    if content_b64 is not None:
        try:
            decoded = base64.b64decode(content_b64, validate=True)
        except Exception:
            return None, None, False
        if len(decoded) != raw.get("size_bytes"):
            return None, None, False
    if content_b64 is None and not isinstance(raw.get("url"), str):
        return None, _attachment_fallback_text(raw), False
    return (
        MediaAttachment(
            url=raw.get("url") if isinstance(raw.get("url"), str) else None,
            mime_type=raw.get("mime_type") if isinstance(raw.get("mime_type"), str) else None,
            filename=raw.get("filename") if isinstance(raw.get("filename"), str) else None,
            size_bytes=raw.get("size_bytes") if isinstance(raw.get("size_bytes"), int) else None,
            content_b64=content_b64,
        ),
        None,
        True,
    )


async def _artifact_authorized_for_delivery(
    session: AsyncSession,
    *,
    attachment: dict[str, Any],
    artifact: Any | None,
    owner_email: str | None,
) -> bool:
    """Authorize a channel attachment before loading its stored bytes."""
    if not isinstance(owner_email, str):
        return False
    return await outbound_artifact_grant_is_valid(
        session,
        attachment=attachment,
        artifact=artifact,
        owner_email=owner_email,
    )


class ChannelDeliveryService:
    """Delivers async notifications to channels via EventBus.

    Subscribes to lifecycle events and routes notifications to the
    originating channel based on conversation context.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        event_bus: EventBus,
        channel_manager_ref: Any,  # Callable[[], ChannelManager]
        turn_scheduler: Any | None = None,  # TurnScheduler (for observer flush)
        public_base_url: str = "",
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._channel_manager_ref = channel_manager_ref
        self._turn_scheduler = turn_scheduler
        self._public_base_url = public_base_url.rstrip("/")
        self._retry_task: asyncio.Task[None] | None = None
        self._managed_channel_service: Any | None = None
        self._recipient_resolution_service: Any | None = None

        # Subscribe to relevant events
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_task_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_task_event)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._handle_task_event)
        event_bus.subscribe(EventType.ESCALATION_CREATED, self._handle_escalation_event)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._handle_notification_event)
        event_bus.subscribe(EventType.TURN_COMPLETED, self._handle_turn_completed_event)
        event_bus.subscribe(EventType.TURN_ERROR, self._handle_turn_error_event)
        event_bus.subscribe(EventType.SCHEDULE_ERROR, self._handle_schedule_event)
        event_bus.subscribe(EventType.SCHEDULE_DISABLED, self._handle_schedule_event)

    def set_managed_channel_service(self, service: Any) -> None:
        """Attach managed-final lifecycle settlement without a constructor cycle."""

        self._managed_channel_service = service

    def set_recipient_resolution_service(self, service: Any) -> None:
        """Attach the singleton explicit-recipient recovery service."""

        self._recipient_resolution_service = service

    async def start(self) -> None:
        """Start lightweight in-process retry loop."""

        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_loop())

    async def stop(self) -> None:
        """Stop retry loop."""

        if self._retry_task is None:
            return
        self._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._retry_task
        self._retry_task = None

    async def send_to_conversation(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        deliverable_id: str | None = None,
    ) -> bool:
        """Send a message to a conversation's channel.

        Web conversations are already updated by the workflow/event path, so
        they count as delivered for caller fallback purposes. External channel
        conversations are delivered via their adapter route.
        """
        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
            return bool(conversation is not None and conversation.context_type == "web")

        channel_type, account_id, chat_id, thread_id = channel_info
        owner_email: str | None = None
        if deliverable_id or (
            attachments
            and any(isinstance(attachment.get("artifact_id"), str) for attachment in attachments)
        ):
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
            owner_email = conversation.user_email if conversation is not None else None
        status = await self._send_to_route(
            channel_type=channel_type,
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            content=content,
            media=attachments,
            deliverable_id=deliverable_id,
            delivery_owner_email=owner_email,
            delivery_conversation_id=conversation_id,
        )
        return status == ChannelDeliveryStatus.SENT

    async def deliver_fenced_direct_turn(
        self,
        *,
        request_id: str,
        lease: Any,
        descriptor: ChannelDeliveryDescriptor,
        content: str,
        attachments: list[dict[str, Any]] | None,
        error: bool = False,
    ) -> ChannelDeliveryStatus:
        """Persist and signal a fenced direct-turn terminal delivery."""
        from cognis.store.direct_turns import DirectTurnStore
        from cognis.store.queries import get_channel_delivery_outbox

        store = DirectTurnStore(self._session_factory)
        request = await store.get(request_id)
        if request is None:
            return ChannelDeliveryStatus.UNAVAILABLE
        async with self._session_factory() as session:
            authorized_attachments = await authorize_outbound_artifact_refs_in_session(
                session,
                attachments or [],
                user_email=request.user_id,
                conversation_id=request.conversation_id,
            )
        suffix = "error" if error else "final"
        delivery_id = f"direct-turn:{request_id}:{suffix}"
        row = await store.create_fenced_channel_delivery(
            request_id=request_id,
            lease=lease,
            delivery_id=delivery_id,
            descriptor=descriptor.model_dump(mode="json"),
            content=content,
            attachments=authorized_attachments,
        )
        if row is None:
            return ChannelDeliveryStatus.UNAVAILABLE
        await self._deliver_outbox(
            delivery_id=row.delivery_id,
            final_content=None,
            fallback_text=None,
            attachments=None,
            ignore_next_attempt=True,
        )
        async with self._session_factory() as session:
            current = await get_channel_delivery_outbox(session, row.delivery_id)
        if current is None:
            return ChannelDeliveryStatus.UNAVAILABLE
        try:
            return ChannelDeliveryStatus(current.status)
        except ValueError:
            return ChannelDeliveryStatus.FAILED

    async def deliver_task_to_conversation(
        self,
        conversation_id: str,
        *,
        task_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        deliverable_id: str | None = None,
    ) -> ChannelDeliveryStatus:
        """Persist and immediately attempt a resumable direct task delivery."""

        from cognis.store.queries import (
            create_channel_delivery_outbox,
            get_channel_delivery_outbox,
            get_channel_delivery_outbox_for_source,
        )

        async with self._session_factory() as session:
            route = await get_conversation_channel_route(session, conversation_id)
            if route is None:
                conversation = await get_conversation(session, conversation_id)
                return (
                    ChannelDeliveryStatus.SENT
                    if conversation is not None and conversation.context_type == "web"
                    else ChannelDeliveryStatus.FAILED
                )
            existing = await get_channel_delivery_outbox_for_source(
                session,
                conversation_id=conversation_id,
                source_type=_TASK_FINAL_SOURCE_TYPE,
                source_id=task_id,
            )
            if existing is None:
                channel_type, account_id, chat_id, thread_id, user_email = route
                authorized_attachments = await authorize_outbound_artifact_refs_in_session(
                    session,
                    attachments or [],
                    user_email=user_email,
                    conversation_id=conversation_id,
                )
                stable_key = hashlib.sha256(
                    f"{_TASK_FINAL_SOURCE_TYPE}:{task_id}:{conversation_id}".encode()
                ).hexdigest()[:20]
                existing = await create_channel_delivery_outbox(
                    session,
                    delivery_id=f"cdel_{stable_key}",
                    user_email=user_email,
                    conversation_id=conversation_id,
                    session_id=None,
                    source_type=_TASK_FINAL_SOURCE_TYPE,
                    source_id=task_id,
                    channel_type=channel_type,
                    account_id=account_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    fallback_text=content,
                    attachments=authorized_attachments,
                    deliverable_id=deliverable_id,
                    next_attempt_at=datetime.now(UTC),
                )
                await session.commit()
                attachments = authorized_attachments
            delivery_id = existing.delivery_id

        await self._deliver_outbox(
            delivery_id=delivery_id,
            final_content=None,
            fallback_text=None,
            attachments=None,
            deliverable_id=None,
            ignore_next_attempt=True,
        )
        async with self._session_factory() as session:
            row = await get_channel_delivery_outbox(session, delivery_id)
        if row is None:
            return ChannelDeliveryStatus.FAILED
        try:
            return ChannelDeliveryStatus(row.status)
        except ValueError:
            return ChannelDeliveryStatus.FAILED

    async def deliver_managed_channel_final(self, delivery_id: str) -> None:
        """Attempt one already-persisted, fenced managed-channel final."""

        await self._deliver_outbox(
            delivery_id=delivery_id,
            final_content=None,
            fallback_text=None,
            ignore_next_attempt=True,
        )

    async def _send_to_route(
        self,
        *,
        channel_type: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None,
        reply_to_id: str | None = None,
        content: str,
        media: list[dict[str, Any]] | None = None,
        deliverable_id: str | None = None,
        start_chunk_index: int = 0,
        expected_projection_digest: str | None = None,
        expected_projected_chunk_count: int | None = None,
        on_chunk_start: ChunkStartCallback | None = None,
        on_chunk_sent: ChunkProgressCallback | None = None,
        delivery_idempotency_key: str | None = None,
        delivery_owner_email: str | None = None,
        delivery_conversation_id: str | None = None,
        workflow_task_id: str | None = None,
        reject_active_managed_binding: bool = False,
    ) -> ChannelDeliveryStatus:
        """Send content to a resolved channel route.

        Returns ``sent`` when all chunks were delivered, ``failed`` when
        nothing was sent, and ``partial`` when some chunks were sent
        before a later chunk failed.
        """

        manager = self._channel_manager_ref()
        if manager is None:
            logger.warning(
                "channel delivery: channel manager unavailable",
                extra={"extra_data": {"channel_type": channel_type, "account_id": account_id}},
            )
            return ChannelDeliveryStatus.UNAVAILABLE

        result = manager.find_adapter_for_channel(channel_type, account_id)
        if result is None:
            logger.warning(
                "channel delivery: channel adapter unavailable",
                extra={"extra_data": {"channel_type": channel_type, "account_id": account_id}},
            )
            return ChannelDeliveryStatus.UNAVAILABLE

        adapter, config = result

        outbound_media: list[MediaAttachment] = []
        if start_chunk_index == 0:
            (
                outbound_media,
                _attachment_fallback_lines,
                had_attachment_failures,
            ) = await self._prepare_media_attachments(
                media or [],
                owner_email=delivery_owner_email,
                conversation_id=delivery_conversation_id,
            )
            if had_attachment_failures:
                return ChannelDeliveryStatus.INCOMPLETE
        deliverable_projection: ChannelProjection | None = None
        if deliverable_id is not None:
            deliverable_projection = await self._deliverable_channel_projection(
                deliverable_id=deliverable_id,
                fallback_text=content,
                capabilities=adapter.capabilities,
                artifact_store=getattr(manager, "_artifact_store", None),
                owner_email=delivery_owner_email,
                conversation_id=delivery_conversation_id,
                workflow_task_id=workflow_task_id,
                materialize_media=start_chunk_index == 0,
            )
            if not deliverable_projection.media_complete:
                return ChannelDeliveryStatus.INCOMPLETE
            outbound_media.extend(deliverable_projection.media)
        if not content and not outbound_media and deliverable_projection is None:
            return ChannelDeliveryStatus.FAILED

        chunks = (
            deliverable_projection.chunks
            if deliverable_projection is not None
            else split_message(content, adapter.capabilities.max_message_length)
        )
        if not chunks and outbound_media:
            chunks = [""]
        stable_projection_identity = (
            deliverable_projection.identity
            if deliverable_projection is not None
            else f"text:{hashlib.sha256(content.encode()).hexdigest()}"
        )
        projection_digest = hashlib.sha256(
            (
                f"{channel_type}:{adapter.capabilities.model_dump_json()}:"
                f"{stable_projection_identity}"
            ).encode()
        ).hexdigest()
        if (
            expected_projection_digest
            and expected_projection_digest != projection_digest
            and start_chunk_index > 0
        ):
            logger.error(
                "channel delivery: projection changed after multipart progress",
                extra={
                    "extra_data": {
                        "expected_digest": expected_projection_digest,
                        "actual_digest": projection_digest,
                        "completed_chunks": start_chunk_index,
                    }
                },
            )
            return ChannelDeliveryStatus.UNCERTAIN
        if (
            expected_projected_chunk_count is not None
            and expected_projected_chunk_count != len(chunks)
            and start_chunk_index > 0
        ):
            logger.error(
                "channel delivery: chunk count changed after multipart progress",
                extra={
                    "extra_data": {
                        "expected_chunks": expected_projected_chunk_count,
                        "actual_chunks": len(chunks),
                        "completed_chunks": start_chunk_index,
                    }
                },
            )
            return ChannelDeliveryStatus.UNCERTAIN
        if start_chunk_index >= len(chunks) and chunks:
            return ChannelDeliveryStatus.SENT

        delivered = start_chunk_index > 0
        for index, chunk in enumerate(chunks[start_chunk_index:], start=start_chunk_index):
            chunk_has_media = index == 0 and bool(outbound_media)
            chunk_idempotent = bool(
                adapter.capabilities.supports_idempotent_send
                and delivery_idempotency_key
                and not chunk_has_media
            )
            if on_chunk_start is not None:
                try:
                    started = await on_chunk_start(
                        index,
                        len(chunks),
                        projection_digest,
                        chunk_idempotent,
                    )
                except Exception:
                    logger.exception("channel delivery: failed to persist in-flight chunk")
                    return ChannelDeliveryStatus.FAILED
                if not started:
                    return ChannelDeliveryStatus.FAILED
            idempotency_key = None
            if chunk_idempotent:
                idempotency_key = hashlib.sha256(
                    f"{delivery_idempotency_key}:{projection_digest}:{index}".encode()
                ).hexdigest()
            try:
                outbound = OutboundMessage(
                    channel_type=channel_type,
                    account_id=account_id,
                    chat_id=chat_id,
                    content=chunk,
                    reply_to_id=reply_to_id,
                    thread_id=thread_id,
                    media=outbound_media if index == 0 else [],
                    platform_data=(
                        {
                            **(
                                {"idempotency_key": idempotency_key}
                                if idempotency_key is not None
                                else {}
                            ),
                            **(
                                {"canonical_rich_markdown": True}
                                if deliverable_projection is not None
                                and deliverable_projection.rich
                                else {}
                            ),
                        }
                    ),
                )
                if reject_active_managed_binding:
                    if delivery_owner_email is None:
                        return ChannelDeliveryStatus.PERMANENT
                    async with self._session_factory() as session:
                        try:
                            route_account = await lock_channel_route(session, account_id)
                        except ValueError:
                            return ChannelDeliveryStatus.PERMANENT
                        if (
                            route_account.user_email != delivery_owner_email
                            or not route_account.enabled
                            or route_account.channel_type != channel_type
                            or not route_account.allow_new_conversations
                            or await active_managed_binding_id(
                                session,
                                user_email=delivery_owner_email,
                                account_id=account_id,
                                chat_id=chat_id,
                                thread_id=thread_id,
                            )
                            is not None
                        ):
                            return ChannelDeliveryStatus.PERMANENT
                        message_id = await adapter.send_message(outbound)
                        await session.commit()
                else:
                    message_id = await adapter.send_message(outbound)
                if (channel_type == "signal" or chunk_idempotent) and (
                    not isinstance(message_id, str) or not message_id.strip()
                ):
                    raise RuntimeError("channel send returned no message id")
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                delivered = True
                if on_chunk_sent is not None:
                    try:
                        progress_saved = await on_chunk_sent(
                            index + 1,
                            len(chunks),
                            projection_digest,
                            {
                                "chunk_index": index,
                                "content": chunk,
                                "sent_at": datetime.now(UTC).isoformat(),
                                "external_message_id": message_id,
                                "attachments_delivered": chunk_has_media,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "channel delivery: sent chunk progress could not be persisted"
                        )
                        return (
                            ChannelDeliveryStatus.PARTIAL
                            if chunk_idempotent
                            else ChannelDeliveryStatus.UNCERTAIN
                        )
                    if not progress_saved:
                        return (
                            ChannelDeliveryStatus.PARTIAL
                            if chunk_idempotent
                            else ChannelDeliveryStatus.UNCERTAIN
                        )
            except NonRetryableChannelError as exc:
                logger.error(
                    "channel delivery: permanent adapter failure",
                    extra={
                        "extra_data": {
                            "channel_type": channel_type,
                            "account_id": account_id,
                            "exception": str(exc)[:500],
                        }
                    },
                )
                CHANNEL_DELIVERY_ERRORS.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return ChannelDeliveryStatus.PERMANENT
            except Exception as exc:
                logger.warning(
                    "channel delivery: adapter send failed",
                    extra={
                        "extra_data": {
                            "channel_type": channel_type,
                            "account_id": account_id,
                            "exception_type": type(exc).__name__,
                            "exception": str(exc)[:500],
                        }
                    },
                    exc_info=True,
                )
                CHANNEL_DELIVERY_ERRORS.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                if not chunk_idempotent:
                    return ChannelDeliveryStatus.UNCERTAIN
                return ChannelDeliveryStatus.PARTIAL if delivered else ChannelDeliveryStatus.FAILED

        if not delivered:
            CHANNEL_DELIVERY_ERRORS.labels(
                channel_type=channel_type,
                account_id=account_id,
            ).inc()

        return ChannelDeliveryStatus.SENT if delivered else ChannelDeliveryStatus.FAILED

    async def _prepare_media_attachments(
        self,
        media: list[dict[str, Any]],
        *,
        owner_email: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[list[MediaAttachment], list[str], bool]:
        manager = self._channel_manager_ref()
        return await prepare_media_attachments(
            media,
            session_factory=self._session_factory,
            artifact_store=getattr(manager, "_artifact_store", None)
            if manager is not None
            else None,
            owner_email=owner_email,
            conversation_id=conversation_id,
        )

    async def _materialize_media_attachment(
        self,
        raw: dict[str, Any],
        *,
        owner_email: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[MediaAttachment | None, str | None, bool]:
        manager = self._channel_manager_ref()
        return await _materialize_media_attachment(
            raw,
            session_factory=self._session_factory,
            artifact_store=getattr(manager, "_artifact_store", None)
            if manager is not None
            else None,
            owner_email=owner_email,
            conversation_id=conversation_id,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _handle_task_event(self, event: Event) -> None:
        """Handle task completion/failure events."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            return

        # Skip direct notification if a follow-up delivery turn is expected.
        # The follow-up turn will deliver the result through the normal
        # outbox path, so sending a direct notification here would duplicate.
        delivery_id = event.data.get("channel_follow_up_delivery_id")
        if isinstance(delivery_id, str):
            return
        if event.data.get("direct_delivery") is True:
            return

        # Only deliver to channel conversations (not web)
        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            return

        task_title = event.data.get("task_title", "Background task")
        event.data.get("status", "completed")
        result_summary = event.data.get("result_summary", "")
        attachments = event.data.get("attachments")
        if not isinstance(attachments, list):
            attachments = None

        if event.type == EventType.TASK_COMPLETED:
            content = f'Task "{task_title}" completed.'
            if result_summary:
                content += f"\n\n{result_summary}"
        elif event.type == EventType.TASK_CANCELLED:
            content = f'Task "{task_title}" was cancelled.'
        else:
            content = f'Task "{task_title}" failed.'
            if result_summary:
                content += f"\n\nError: {result_summary}"

        await self.send_to_conversation(conversation_id, content, attachments=attachments)

    async def _handle_schedule_event(self, event: Event) -> None:
        """Handle schedule fire failures before a task exists."""

        user_email = event.data.get("created_by")
        agent_id = event.data.get("agent_id")
        if not isinstance(user_email, str) or not isinstance(agent_id, str):
            return

        async with self._session_factory() as db_session:
            account = await get_preferred_channel_account_for_agent(
                db_session,
                user_email=user_email,
                agent_id=agent_id,
            )
            if account is None:
                direct = await get_agent_direct_conversation(db_session, user_email, agent_id)
                conversation_id = direct.conversation_id if direct is not None else None
            else:
                conversation_id = None
                if account.default_conversation_id:
                    route = await get_conversation_channel_route(
                        db_session,
                        account.default_conversation_id,
                    )
                    if route is not None and route[1] == account.account_id:
                        conversation_id = account.default_conversation_id
                if conversation_id is None:
                    latest = await get_latest_active_conversation_for_channel_account(
                        db_session,
                        user_email=user_email,
                        agent_id=agent_id,
                        account_id=account.account_id,
                        prefer_unthreaded=True,
                    )
                    conversation_id = latest.conversation_id if latest is not None else None

        if conversation_id is None:
            return

        schedule_name = str(event.data.get("schedule_name") or "Scheduled task")
        error = str(event.data.get("error") or "Unknown error")
        if event.type == EventType.SCHEDULE_DISABLED:
            reason = str(event.data.get("reason") or "repeated failures")
            content = f'Schedule "{schedule_name}" was disabled after {reason}.'
        else:
            content = f'Schedule "{schedule_name}" failed to start.'
        if error:
            content += f"\n\nError: {error}"

        await self.send_to_conversation(conversation_id, content)

    async def _handle_turn_completed_event(self, event: Event) -> None:
        grace_delivery_id = event.data.get("delivery_id")
        if not isinstance(grace_delivery_id, str) or not event.data.get("channel_deliverable"):
            return

        final_content = event.data.get("final_content")
        if not isinstance(final_content, str):
            final_content = ""
        attachments = event.data.get("attachments")
        if not isinstance(attachments, list):
            attachments = None
        deliverable_id = event.data.get("final_deliverable_id")
        if not isinstance(deliverable_id, str):
            deliverable_id = None

        turn_id = event.data.get("turn_id")
        if not isinstance(turn_id, str):
            await self._deliver_outbox(
                delivery_id=grace_delivery_id,
                final_content=final_content.strip() or None,
                fallback_text=(
                    event.data.get("delivery_fallback_text")
                    if isinstance(event.data.get("delivery_fallback_text"), str)
                    else None
                ),
                attachments=attachments,
                deliverable_id=deliverable_id,
                ignore_next_attempt=True,
            )
            return

        from cognis.store.queries import get_channel_delivery_outbox

        async with self._session_factory() as session:
            grace_row = await get_channel_delivery_outbox(session, grace_delivery_id)
        if grace_row is not None and grace_row.source_type not in {
            "follow_up",
            "task_result_follow_up",
        }:
            await self._deliver_outbox(
                delivery_id=grace_delivery_id,
                final_content=final_content.strip() or None,
                fallback_text=(
                    event.data.get("delivery_fallback_text")
                    if isinstance(event.data.get("delivery_fallback_text"), str)
                    else None
                ),
                attachments=attachments,
                deliverable_id=deliverable_id,
                ignore_next_attempt=True,
            )
            return
        result_delivery_id = await self._ensure_follow_up_result_delivery(
            event=event,
            grace_delivery_id=grace_delivery_id,
            final_content=final_content.strip() or None,
            attachments=attachments,
            deliverable_id=deliverable_id,
        )
        if result_delivery_id is None:
            return
        await self._deliver_outbox(
            delivery_id=result_delivery_id,
            final_content=None,
            fallback_text=None,
            attachments=None,
            deliverable_id=None,
            ignore_next_attempt=True,
        )

    async def _ensure_follow_up_result_delivery(
        self,
        *,
        event: Event,
        grace_delivery_id: str,
        final_content: str | None,
        attachments: list[dict[str, Any]] | None,
        deliverable_id: str | None,
    ) -> str | None:
        """Persist a terminal follow-up result independently from its grace notice."""

        from cognis.store.queries import ensure_follow_up_result_delivery

        conversation_id = event.data.get("conversation_id")
        turn_id = event.data.get("turn_id")
        if not isinstance(conversation_id, str) or not isinstance(turn_id, str):
            logger.warning(
                "channel delivery: terminal follow-up result lacks correlation",
                extra={"extra_data": {"delivery_id": grace_delivery_id}},
            )
            return None
        try:
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                if conversation is None:
                    return None
                authorized_attachments = await authorize_outbound_artifact_refs_in_session(
                    session,
                    attachments or [],
                    user_email=conversation.user_email,
                    conversation_id=conversation_id,
                )
                row = await ensure_follow_up_result_delivery(
                    session,
                    grace_delivery_id=grace_delivery_id,
                    conversation_id=conversation_id,
                    session_id=(
                        event.data.get("session_id")
                        if isinstance(event.data.get("session_id"), str)
                        else None
                    ),
                    turn_id=turn_id,
                    final_content=final_content,
                    attachments=authorized_attachments,
                    deliverable_id=deliverable_id,
                )
                await session.commit()
        except ValueError:
            logger.warning(
                "channel delivery: rejected stale or conflicting follow-up result",
                extra={
                    "extra_data": {
                        "delivery_id": grace_delivery_id,
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                    }
                },
            )
            return None
        return row.delivery_id if row is not None else None

    async def _handle_turn_error_event(self, event: Event) -> None:
        delivery_id = event.data.get("delivery_id")
        if not isinstance(delivery_id, str) or not event.data.get("channel_deliverable"):
            return

        fallback_text = event.data.get("delivery_fallback_text")
        if not isinstance(fallback_text, str):
            fallback_text = None

        await self._deliver_outbox(
            delivery_id=delivery_id,
            final_content=None,
            fallback_text=fallback_text
            or "I could not deliver the detailed follow-up reply. Please open the conversation for details.",
            ignore_next_attempt=True,
        )

    async def _handle_escalation_event(self, event: Event) -> None:
        """Handle escalation creation events."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            return

        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            return

        tool_name = event.data.get("tool_name", "unknown")
        content = (
            f'Escalation: The agent wants to use tool "{tool_name}" '
            "but needs your approval. Reply /approve or /deny."
        )
        await self.send_to_conversation(conversation_id, content)

    async def _handle_notification_event(self, event: Event) -> None:
        """Handle generic notification events."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            return

        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            return

        notification_type = event.data.get("notification_type", "notification")
        payload = event.data.get("payload", {})

        # Flush any buffered observer text before sending the notification
        # so the assistant's preceding message arrives before the question.
        if (
            notification_type in {"step_question", "auth_challenge", "credential_request"}
            and self._turn_scheduler is not None
        ):
            await self._flush_observer_buffers(conversation_id)

        if notification_type == "escalation" and isinstance(payload, dict):
            content = self._render_escalation_notification(payload)
        elif notification_type == "step_question" and isinstance(payload, dict):
            content = self._render_step_question_notification(payload)
        elif notification_type == "auth_challenge" and isinstance(payload, dict):
            content = self._render_auth_challenge_notification(payload)
        elif notification_type == "credential_request" and isinstance(payload, dict):
            content = self._render_credential_request_notification(
                payload,
                notification_id=str(event.data.get("notification_id") or ""),
            )
        elif notification_type == "gate" and isinstance(payload, dict):
            content = self._render_gate_notification(payload)
        else:
            message = event.data.get("message", "You have a new notification.")
            content = f"[{notification_type}] {message}"

        # For non-escalation types, prepend managed-origin context when present.
        # Escalation already embeds it via _render_escalation_notification.
        if notification_type != "escalation" and isinstance(payload, dict):
            managed_title = payload.get("managed_conversation_title")
            managed_agent = payload.get("managed_target_agent_id")
            if managed_title or managed_agent:
                origin = managed_title or managed_agent
                content = f"_From managed conversation: {origin}_\n\n{content}"

        await self.send_to_conversation(conversation_id, content)

    def _render_step_question_notification(self, payload: dict[str, Any]) -> str:
        """Render a question set prompt for plain-text channel integrations."""
        lines: list[str] = []
        context = payload.get("context")
        if isinstance(context, str) and context.strip():
            lines.append(context.strip())
        elif isinstance(context, dict):
            note = context.get("note") or context.get("context")
            if isinstance(note, str) and note.strip():
                lines.append(note.strip())
        questions = payload.get("questions")
        if not isinstance(questions, list) or not questions:
            lines.append("The assistant needs more input to continue.")
        else:
            for q_index, question in enumerate(questions, start=1):
                if not isinstance(question, dict):
                    continue
                q_lines: list[str] = []
                header = question.get("header")
                if isinstance(header, str) and header.strip():
                    q_lines.append(header.strip())
                text = str(question.get("question") or "").strip()
                q_lines.append(f"{q_index}. {text}" if text else f"{q_index}. Question")
                options = question.get("options")
                if isinstance(options, list) and options:
                    for opt_index, option in enumerate(options, start=1):
                        if not isinstance(option, dict):
                            continue
                        label = str(option.get("label") or "").strip()
                        if not label:
                            continue
                        desc = str(option.get("description") or "").strip()
                        q_lines.append(f"   {opt_index}) {label}" + (f" — {desc}" if desc else ""))
                    if question.get("multiple"):
                        q_lines.append("   You may choose multiple options.")
                if question.get("allow_custom", True):
                    q_lines.append("   A custom answer is OK.")
                lines.append("\n".join(q_lines))
        lines.append("Reply in free text. Your full reply will be forwarded to the assistant.")
        return "\n\n".join(lines)

    def _render_auth_challenge_notification(self, payload: dict[str, Any]) -> str:
        """Render an auth challenge prompt for channel integrations."""
        label = str(payload.get("label") or "Authentication required")
        message = str(payload.get("message") or "Reply with the requested authentication code.")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if payload.get("kind") == "oauth_authorization" and isinstance(metadata, dict):
            authorization_url = str(metadata.get("authorization_url") or "")
            flow = str(metadata.get("flow") or "authorization_code")
            lines = [f"*[auth]* {label}", message]
            if flow == "device_code":
                verification_uri = str(metadata.get("verification_uri") or authorization_url)
                verification_uri_complete = str(metadata.get("verification_uri_complete") or "")
                user_code = str(metadata.get("user_code") or "")
                if verification_uri_complete:
                    lines.append(f"Open verification link: {verification_uri_complete}")
                elif verification_uri:
                    lines.append(f"Open verification page: {verification_uri}")
                if user_code:
                    lines.append(f"Enter code: {user_code}")
                lines.append("Cognis will complete automatically after provider authorization.")
            elif authorization_url:
                lines.append(f"Authorize here: {authorization_url}")
            lines.append("Replies will not complete OAuth; use the authorization link.")
            return "\n\n".join(lines)
        lines = [f"*[auth]* {label}", message]
        required = payload.get("required_fields")
        if isinstance(required, list) and "code" in required:
            lines.append("_Reply with the code only._")
        else:
            lines.append("_Reply with the requested response when complete._")
        return "\n\n".join(lines)

    def _render_credential_request_notification(
        self, payload: dict[str, Any], *, notification_id: str
    ) -> str:
        """Render a credential request with a Cognis form link for channels."""

        label = str(payload.get("label") or "Credential required")
        message = str(
            payload.get("message")
            or payload.get("description")
            or "A task needs a credential before it can continue."
        )
        link = (
            f"{self._public_base_url}/notifications/{notification_id}"
            if self._public_base_url and notification_id
            else "Open Cognis to provide the credential."
        )
        lines = [f"*[credential]* {label}", message]
        required = payload.get("required_fields")
        if isinstance(required, list) and required:
            fields = ", ".join(str(field) for field in required if isinstance(field, str))
            if fields:
                lines.append(f"Required fields: {fields}")
        lines.append(f"Provide or cancel it in Cognis: {link}")
        lines.append("Do not send credential values in this chat.")
        return "\n\n".join(lines)

    def _render_gate_notification(self, payload: dict[str, Any]) -> str:
        """Render a workflow gate prompt for channel integrations."""
        message = str(
            payload.get("message")
            or payload.get("question")
            or "A workflow step needs your decision."
        )
        lines = [f"*[gate]* {message}"]
        options = payload.get("options")
        if isinstance(options, list) and options:
            option_lines: list[str] = []
            idx = 1
            for option in options:
                label: str | None = None
                if isinstance(option, str):
                    label = option
                elif isinstance(option, dict) and isinstance(option.get("label"), str):
                    label = option["label"]
                if label:
                    option_lines.append(f"{idx}. {label}")
                    idx += 1
            if option_lines:
                lines.append("\n".join(option_lines))
        lines.append(
            "_Use the matching /retry, /continue, or /cancel command for one of the "
            "workflow options above, or use the task board. "
            "/approve and /deny only apply to tool escalations._"
        )
        return "\n\n".join(lines)

    def _render_escalation_notification(self, payload: dict[str, Any]) -> str:
        """Render a rich escalation prompt for channel integrations."""
        tool_name = str(payload.get("tool_name") or "tool call")
        risk = payload.get("risk")
        reasoning = payload.get("reasoning")
        managed_title = payload.get("managed_conversation_title")
        managed_agent = payload.get("managed_target_agent_id")

        lines = [f"*[escalation]* Approval required for tool `{tool_name}`."]
        if managed_title or managed_agent:
            origin = managed_title or managed_agent
            lines.append(f"_Requested by managed conversation: {origin}_")
        if risk:
            lines.append(f"**Risk:** {risk}")
        if reasoning:
            lines.append(f"**Reason:** {reasoning}")
        lines.append("_Reply /approve to allow it or /deny to block it._")
        lines.append("_You can optionally add a note, for example: /approve safe to continue_")
        return "\n\n".join(lines)

    async def _flush_observer_buffers(self, conversation_id: str) -> None:
        """Flush accumulated text from channel observers before a notification."""
        if self._turn_scheduler is None:
            return
        observers = list(self._turn_scheduler._observers.get(conversation_id, []))
        for observer in observers:
            flush = getattr(observer, "flush_buffered_text", None)
            if callable(flush):
                with contextlib.suppress(Exception):
                    await flush()

    async def recover_pending_deliveries(self) -> None:
        """Best-effort startup recovery for pending channel deliveries."""

        from cognis.store.queries import (
            get_task,
            list_channel_delivery_outbox_due,
            list_channel_delivery_outbox_stale_sending,
            recover_stale_channel_delivery,
        )

        now = datetime.now(UTC)
        if self._recipient_resolution_service is not None:
            await self._recipient_resolution_service.recover_pending()
        if self._managed_channel_service is not None:
            await self._managed_channel_service.reconcile_pending_deliveries()
        async with self._session_factory() as session:
            stale = await list_channel_delivery_outbox_stale_sending(session, now=now)
            for row in stale:
                lease_token = row.lease_token
                lease_expires_at = row.lease_expires_at
                if not lease_token or lease_expires_at is None:
                    continue
                recovered_status = await recover_stale_channel_delivery(
                    session,
                    delivery_id=row.delivery_id,
                    observed_lease_token=lease_token,
                    observed_lease_expires_at=lease_expires_at,
                    observed_inflight_chunk_index=row.inflight_chunk_index,
                    observed_inflight_idempotent=row.inflight_idempotent,
                    now=now,
                )
                if recovered_status is not None:
                    logger.warning(
                        "channel delivery: reset stale sending record for retry",
                        extra={
                            "extra_data": {
                                "delivery_id": row.delivery_id,
                                "conversation_id": row.conversation_id,
                                "status": recovered_status,
                            }
                        },
                    )
                else:
                    logger.warning(
                        "channel delivery: could not reset stale sending record",
                        extra={
                            "extra_data": {
                                "delivery_id": row.delivery_id,
                                "conversation_id": row.conversation_id,
                            }
                        },
                    )
            await session.commit()

        if self._managed_channel_service is not None:
            await self._managed_channel_service.reconcile_pending_deliveries()
        async with self._session_factory() as session:
            due = await list_channel_delivery_outbox_due(session, now=now)

        for row in due:
            if (
                row.source_type == "managed_channel_final"
                and self._managed_channel_service is not None
                and _as_utc(row.updated_at) <= now - _MANAGED_DELIVERY_MAX_PENDING_AGE
            ):
                abandoned = await self._managed_channel_service.abandon_pending_delivery(
                    delivery_id=row.delivery_id,
                    lease_token=None,
                    reason="channel_delivery_timeout",
                    outbox_status="suppressed",
                    expected_outbox_statuses={"pending", "failed"},
                )
                if abandoned:
                    continue
            final_content: str | None = None
            attachments = (
                [item for item in row.attachments_json if isinstance(item, dict)]
                if isinstance(row.attachments_json, list)
                else None
            )
            deliverable_id = row.deliverable_id if isinstance(row.deliverable_id, str) else None
            if (
                row.source_type == _TASK_FINAL_SOURCE_TYPE
                and isinstance(row.source_id, str)
                and row.source_id
            ):
                async with self._session_factory() as session:
                    task_row = await get_task(session, row.source_id)
                result_data = (
                    task_row.result_data
                    if task_row is not None and isinstance(task_row.result_data, dict)
                    else {}
                )
                raw_deliverable_id = result_data.get("final_deliverable_id")
                if (
                    deliverable_id is None
                    and isinstance(raw_deliverable_id, str)
                    and raw_deliverable_id
                ):
                    deliverable_id = raw_deliverable_id
                raw_final_content = None
                if deliverable_id is None:
                    raw_final_content = result_data.get("final_channel_content")
                if not raw_final_content:
                    raw_final_content = result_data.get("final_content")
                if isinstance(raw_final_content, str) and raw_final_content.strip():
                    final_content = raw_final_content.strip()
                raw_attachments = result_data.get("attachments")
                if attachments is None and isinstance(raw_attachments, list):
                    attachments = [item for item in raw_attachments if isinstance(item, dict)]
            await self._deliver_outbox(
                delivery_id=row.delivery_id,
                final_content=final_content,
                fallback_text=row.fallback_text,
                attachments=attachments,
                deliverable_id=deliverable_id,
            )

    async def _retry_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.recover_pending_deliveries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("channel delivery: retry loop iteration failed", exc_info=True)

    async def _has_active_follow_up_delivery(self, delivery_id: str) -> bool:
        from cognis.store.queries import get_channel_delivery_outbox

        async with self._session_factory() as session:
            row = await get_channel_delivery_outbox(session, delivery_id)
            if row is None:
                return False
            return row.status in {"pending", "sending", "failed", "uncertain", "sent"}

    async def _deliver_outbox(
        self,
        *,
        delivery_id: str,
        final_content: str | None,
        fallback_text: str | None,
        attachments: list[dict[str, Any]] | None = None,
        deliverable_id: str | None = None,
        ignore_next_attempt: bool = False,
    ) -> None:
        from cognis.store.queries import (
            claim_channel_delivery_outbox,
            mark_channel_delivery_chunk_inflight,
            mark_channel_delivery_chunk_sent,
            mark_channel_delivery_failed,
            mark_channel_delivery_permanent_failure,
            mark_channel_delivery_sent,
            mark_channel_delivery_uncertain,
            origin_session_is_in_active_scope,
            renew_channel_delivery_lease,
            set_channel_delivery_attachments,
            suppress_channel_delivery_outbox,
            update_deliverable_status,
        )

        lease_token = f"lease_{uuid.uuid4().hex[:12]}"
        lease_expires_at = datetime.now(UTC) + _DELIVERY_LEASE_DURATION
        async with self._session_factory() as session:
            pending_row = await session.get(ChannelDeliveryOutboxRow, delivery_id)
            if pending_row is not None:
                manager = self._channel_manager_ref()
                owns_account = getattr(manager, "owns_account", None)
                if callable(owns_account) and not owns_account(pending_row.account_id):
                    return
            if pending_row is not None and pending_row.source_type == "managed_channel_final":
                from cognis.channels.managed import managed_delivery_fence_valid

                if not await managed_delivery_fence_valid(session, pending_row):
                    await suppress_channel_delivery_outbox(
                        session,
                        delivery_ids=[delivery_id],
                        reason=None,
                    )
                    await session.commit()
                    return
            if pending_row is not None:
                session.expunge(pending_row)
            if attachments:
                persisted = await set_channel_delivery_attachments(
                    session,
                    delivery_id=delivery_id,
                    attachments=attachments,
                )
                if not persisted:
                    await session.rollback()
                    return
            row = await claim_channel_delivery_outbox(
                session,
                delivery_id=delivery_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                ignore_next_attempt=ignore_next_attempt,
            )
            stale_origin = False
            if row is not None and row.source_type in {
                "delegation",
                "task_gate_follow_up",
                "task_result_follow_up",
            }:
                stale_origin = not await origin_session_is_in_active_scope(
                    session,
                    conversation_id=row.conversation_id,
                    origin_session_id=row.session_id,
                )
                if stale_origin:
                    await suppress_channel_delivery_outbox(
                        session,
                        delivery_ids=[delivery_id],
                        reason=None,
                    )
            await session.commit()

        if row is None or stale_origin:
            return
        if getattr(row, "source_type", None) == "managed_channel_final":
            from cognis.channels.managed import managed_delivery_fence_valid

            async with self._session_factory() as session:
                valid = await managed_delivery_fence_valid(session, row)
                if not valid:
                    await suppress_channel_delivery_outbox(
                        session,
                        delivery_ids=[delivery_id],
                        reason=None,
                    )
                    await session.commit()
                    return
        stored_attachments = getattr(row, "attachments_json", None)
        if not attachments and isinstance(stored_attachments, list):
            attachments = [item for item in stored_attachments if isinstance(item, dict)]
        stored_deliverable_id = getattr(row, "deliverable_id", None)
        if deliverable_id is None and isinstance(stored_deliverable_id, str):
            deliverable_id = stored_deliverable_id

        content = final_content or fallback_text or getattr(row, "fallback_text", None) or ""
        if not content and not attachments and deliverable_id is None:
            async with self._session_factory() as session:
                await mark_channel_delivery_sent(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                )
                await session.commit()
            return

        delivery_status = ChannelDeliveryStatus.FAILED
        lease_lost = asyncio.Event()

        async def save_chunk_start(
            chunk_index: int,
            projected_chunk_count: int,
            projection_digest: str,
            idempotent: bool,
        ) -> bool:
            if lease_lost.is_set():
                return False
            if row.source_type in {
                "delegation",
                "task_gate_follow_up",
                "task_result_follow_up",
            }:
                async with self._session_factory() as session:
                    if not await origin_session_is_in_active_scope(
                        session,
                        conversation_id=row.conversation_id,
                        origin_session_id=row.session_id,
                    ):
                        await suppress_channel_delivery_outbox(
                            session,
                            delivery_ids=[delivery_id],
                            reason=None,
                        )
                        await session.commit()
                        return False
            if getattr(row, "source_type", None) == "managed_channel_final":
                from cognis.channels.managed import managed_delivery_fence_valid

                async with self._session_factory() as session:
                    if not await managed_delivery_fence_valid(session, row):
                        await suppress_channel_delivery_outbox(
                            session,
                            delivery_ids=[delivery_id],
                            reason=None,
                        )
                        await session.commit()
                        return False
            async with self._session_factory() as session:
                saved = await mark_channel_delivery_chunk_inflight(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    chunk_index=chunk_index,
                    projected_chunk_count=projected_chunk_count,
                    projection_digest=projection_digest,
                    idempotent=idempotent,
                    lease_expires_at=datetime.now(UTC) + _DELIVERY_LEASE_DURATION,
                )
                if saved:
                    await session.commit()
                else:
                    await session.rollback()
                return saved

        async def save_chunk_progress(
            completed_chunk_count: int,
            projected_chunk_count: int,
            projection_digest: str,
            receipt: dict[str, Any] | None = None,
        ) -> bool:
            async with self._session_factory() as session:
                receipt = receipt or {
                    "chunk_index": completed_chunk_count - 1,
                    "content": row.fallback_text or "",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "external_message_id": None,
                    "attachments_delivered": completed_chunk_count == 1
                    and bool(row.attachments_json),
                }
                if receipt.get("attachments_delivered") is True:
                    receipt = {
                        **receipt,
                        "attachments": safe_attachment_metadata(row.attachments_json),
                    }
                saved = await mark_channel_delivery_chunk_sent(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    completed_chunk_count=completed_chunk_count,
                    projected_chunk_count=projected_chunk_count,
                    projection_digest=projection_digest,
                    lease_expires_at=datetime.now(UTC) + _DELIVERY_LEASE_DURATION,
                    receipt=receipt,
                )
                if saved:
                    from cognis.store.queries import promote_channel_recipient_target_on_receipt

                    await promote_channel_recipient_target_on_receipt(
                        session, delivery_id=delivery_id
                    )
                    await session.commit()
                else:
                    await session.rollback()
                return saved

        managed_binding_id: str | None = None
        managed_send_lease_token: str | None = None

        async def renew_lease() -> None:
            while True:
                await asyncio.sleep(_DELIVERY_LEASE_HEARTBEAT_SECONDS)
                try:
                    async with self._session_factory() as session:
                        renewed = await renew_channel_delivery_lease(
                            session,
                            delivery_id=delivery_id,
                            lease_token=lease_token,
                            lease_expires_at=datetime.now(UTC) + _DELIVERY_LEASE_DURATION,
                        )
                        if renewed and managed_binding_id and managed_send_lease_token:
                            from cognis.channels.managed import renew_managed_delivery_lease

                            renewed = await renew_managed_delivery_lease(
                                session,
                                binding_id=managed_binding_id,
                                lease_token=managed_send_lease_token,
                                expires_at=datetime.now(UTC) + _DELIVERY_LEASE_DURATION,
                            )
                        if not renewed:
                            await session.rollback()
                            lease_lost.set()
                            return
                        await session.commit()
                except Exception:
                    lease_lost.set()
                    logger.exception(
                        "channel delivery: lease heartbeat failed",
                        extra={"extra_data": {"delivery_id": delivery_id}},
                    )
                    return

        heartbeat_task = asyncio.create_task(renew_lease())
        try:
            if getattr(row, "source_type", None) == "managed_channel_final":
                from cognis.channels.managed import acquire_managed_delivery_lease

                managed_binding_id = row.managed_binding_id
                managed_send_lease_token = f"mcsend_{uuid.uuid4().hex}"
                async with self._session_factory() as session:
                    acquired = await acquire_managed_delivery_lease(
                        session,
                        row,
                        lease_token=managed_send_lease_token,
                        expires_at=lease_expires_at,
                    )
                    if not acquired:
                        await session.rollback()
                        return
                    await session.commit()
            delivery_status = await self._send_to_route(
                channel_type=row.channel_type,
                account_id=row.account_id,
                chat_id=row.chat_id,
                thread_id=row.thread_id,
                reply_to_id=getattr(row, "reply_to_id", None),
                content=content,
                media=attachments,
                deliverable_id=deliverable_id,
                start_chunk_index=int(getattr(row, "completed_chunk_count", 0) or 0),
                expected_projection_digest=getattr(row, "projection_digest", None),
                expected_projected_chunk_count=getattr(row, "projected_chunk_count", None),
                on_chunk_start=save_chunk_start,
                on_chunk_sent=save_chunk_progress,
                delivery_idempotency_key=delivery_id,
                delivery_owner_email=getattr(row, "user_email", None),
                delivery_conversation_id=row.conversation_id,
                workflow_task_id=(
                    row.source_id
                    if getattr(row, "source_type", None) == _TASK_FINAL_SOURCE_TYPE
                    and isinstance(getattr(row, "source_id", None), str)
                    else None
                ),
                reject_active_managed_binding=(
                    getattr(row, "source_type", None) == "channel_recipient"
                ),
            )
        except Exception:
            logger.warning(
                "channel delivery: follow-up send failed",
                extra={
                    "extra_data": {
                        "delivery_id": delivery_id,
                        "conversation_id": row.conversation_id,
                    }
                },
                exc_info=True,
            )
            delivery_status = ChannelDeliveryStatus.FAILED
        finally:
            if managed_binding_id and managed_send_lease_token:
                from cognis.channels.managed import release_managed_delivery_lease

                async with self._session_factory() as session:
                    await release_managed_delivery_lease(
                        session,
                        binding_id=managed_binding_id,
                        lease_token=managed_send_lease_token,
                    )
                    await session.commit()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        if (
            getattr(row, "source_type", None) == "managed_channel_final"
            and self._managed_channel_service is not None
            and delivery_status == ChannelDeliveryStatus.SENT
        ):
            await self._managed_channel_service.complete_pending_delivery(
                delivery_id=delivery_id,
                lease_token=lease_token,
            )
            return
        if (
            getattr(row, "source_type", None) == "managed_channel_final"
            and self._managed_channel_service is not None
            and delivery_status
            in {
                ChannelDeliveryStatus.UNCERTAIN,
                ChannelDeliveryStatus.INCOMPLETE,
                ChannelDeliveryStatus.PERMANENT,
            }
        ):
            await self._managed_channel_service.abandon_pending_delivery(
                delivery_id=delivery_id,
                lease_token=lease_token,
                reason=(
                    "external_send_outcome_uncertain"
                    if delivery_status == ChannelDeliveryStatus.UNCERTAIN
                    else "nonretryable_channel_failure"
                    if delivery_status == ChannelDeliveryStatus.PERMANENT
                    else "attachment_materialization_incomplete"
                ),
                outbox_status=(
                    "uncertain"
                    if delivery_status == ChannelDeliveryStatus.UNCERTAIN
                    else "suppressed"
                ),
            )
            return

        if (
            getattr(row, "source_type", None) == "managed_channel_final"
            and self._managed_channel_service is not None
            and delivery_status in {ChannelDeliveryStatus.UNAVAILABLE, ChannelDeliveryStatus.FAILED}
            and int(getattr(row, "attempt_count", 0) or 0) + 1 >= _MANAGED_DELIVERY_MAX_ATTEMPTS
        ):
            await self._managed_channel_service.abandon_pending_delivery(
                delivery_id=delivery_id,
                lease_token=lease_token,
                reason=(
                    "channel_adapter_unavailable"
                    if delivery_status == ChannelDeliveryStatus.UNAVAILABLE
                    else "channel_send_failed"
                ),
                outbox_status="suppressed",
            )
            return

        async with self._session_factory() as session:
            if delivery_status == ChannelDeliveryStatus.SENT:
                ok = await mark_channel_delivery_sent(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    require_complete_chunks=True,
                )
                if not ok:
                    await session.rollback()
                    return
                if deliverable_id is not None:
                    await update_deliverable_status(
                        session,
                        deliverable_id,
                        status="delivered",
                    )
            elif delivery_status == ChannelDeliveryStatus.UNCERTAIN:
                await mark_channel_delivery_uncertain(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    last_error="external_send_outcome_uncertain",
                )
            elif delivery_status == ChannelDeliveryStatus.PERMANENT:
                await mark_channel_delivery_permanent_failure(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    last_error="nonretryable_channel_failure",
                )
            else:
                await mark_channel_delivery_failed(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    last_error=(
                        "attachment_materialization_incomplete"
                        if delivery_status == ChannelDeliveryStatus.INCOMPLETE
                        else "channel_adapter_unavailable"
                        if delivery_status == ChannelDeliveryStatus.UNAVAILABLE
                        else "channel_send_failed"
                    ),
                    next_attempt_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            await session.commit()

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    async def _resolve_channel(
        self,
        conversation_id: str,
    ) -> tuple[str, str, str, str | None] | None:
        """Resolve a conversation to its channel routing info.

        Returns (channel_type, account_id, chat_id) or None if the
        conversation is not channel-originated.
        """
        from cognis.store.queries import get_conversation

        async with self._session_factory() as session:
            row = await get_conversation(session, conversation_id)

        if row is None:
            return None

        # Check if this is a channel conversation
        if row.context_type in {"web", "api"}:
            return None

        # Extract channel routing from platform_data
        platform_data = row.context_data or {}
        channel_type = platform_data.get("channel_type")
        account_id = platform_data.get("account_id")
        chat_id = platform_data.get("chat_id")
        thread_id = platform_data.get("thread_id")

        if not all([channel_type, account_id, chat_id]):
            # Try parsing from context_ref (format: "channel_type:account_id:chat_id")
            if row.context_ref and ":" in row.context_ref:
                parts = row.context_ref.split(":", 3)
                if len(parts) >= 3:
                    return parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else None
            return None

        return (
            str(channel_type),
            str(account_id),
            str(chat_id),
            str(thread_id) if thread_id else None,
        )

    async def _deliverable_channel_projection(
        self,
        *,
        deliverable_id: str,
        fallback_text: str,
        capabilities: Any,
        artifact_store: Any | None,
        owner_email: str | None = None,
        conversation_id: str | None = None,
        workflow_task_id: str | None = None,
        materialize_media: bool = True,
    ) -> ChannelProjection:
        """Build canonical Markdown deliverable chunks with a full-view link."""

        from cognis.store.deliverable_storage import hydrate_deliverable_payload
        from cognis.store.queries import get_deliverable

        row: Any | None = None
        try:
            async with self._session_factory() as session:
                row = await get_deliverable(session, deliverable_id)
            if row is not None and artifact_store is not None:
                try:
                    await hydrate_deliverable_payload(row, artifact_store)
                except Exception:
                    logger.warning(
                        "channel delivery: failed to hydrate deliverable for channel projection",
                        extra={"extra_data": {"deliverable_id": deliverable_id}},
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "channel delivery: failed to load deliverable for channel projection",
                extra={"extra_data": {"deliverable_id": deliverable_id}},
                exc_info=True,
            )
            row = None

        link = self._deliverable_view_link(artifact_store, deliverable_id)
        title = str(getattr(row, "title", "") or "Deliverable ready").strip()
        format_name = str(getattr(row, "format", "") or "").strip()
        content = str(getattr(row, "content", "") or "").strip() or fallback_text
        rich_payload = getattr(row, "rich_payload", None)
        projected_media: tuple[MediaAttachment, ...] = ()
        media_complete = True
        is_rich = format_name == "rich" and isinstance(rich_payload, dict)
        if is_rich:
            media_manifest = rich_media_manifest(rich_payload)
            if materialize_media and media_manifest and capabilities.supports_inline_media:
                prepared: list[MediaAttachment] = []
                canonical = [
                    item for item in media_manifest if isinstance(item.get("media_key"), str)
                ]
                persisted_assets = [
                    item for item in media_manifest if not isinstance(item.get("media_key"), str)
                ]
                if canonical:
                    workflow_media_authorized = False
                    if (
                        row is not None
                        and getattr(row, "step_run_id", None) is not None
                        and workflow_task_id
                        and owner_email
                    ):
                        from cognis.store.queries import get_step_run, get_task

                        async with self._session_factory() as session:
                            step_run = await get_step_run(session, row.step_run_id)
                            task = await get_task(session, workflow_task_id)
                        workflow_media_authorized = bool(
                            step_run is not None
                            and task is not None
                            and step_run.task_id == workflow_task_id
                            and task.created_by == owner_email
                        )
                    if (
                        artifact_store is None
                        or row is None
                        or not conversation_id
                        or (
                            getattr(row, "conversation_id", None) != conversation_id
                            and not workflow_media_authorized
                        )
                    ):
                        media_complete = False
                    else:
                        async with self._session_factory() as session:
                            for item in canonical:
                                media_key = str(item["media_key"])
                                resolved = await resolve_deliverable_media(
                                    session,
                                    artifact_store,
                                    row,
                                    media_key,
                                )
                                if resolved is None:
                                    media_complete = False
                                    continue
                                content_bytes, manifest_item, _artifact = resolved
                                prepared.append(
                                    MediaAttachment(
                                        mime_type=str(manifest_item["mime_type"]),
                                        filename=str(manifest_item.get("filename") or media_key),
                                        size_bytes=len(content_bytes),
                                        content_b64=base64.b64encode(content_bytes).decode("ascii"),
                                        disposition="inline",
                                        media_ref=media_key,
                                    )
                                )
                if persisted_assets:
                    for item in persisted_assets:
                        (
                            persisted_prepared,
                            _fallback_lines,
                            had_failures,
                        ) = await self._prepare_media_attachments(
                            [item],
                            owner_email=owner_email,
                            conversation_id=conversation_id,
                        )
                        prepared.extend(
                            attachment.model_copy(
                                update={
                                    "disposition": "inline",
                                    "media_ref": str(item.get("media_ref") or ""),
                                }
                            )
                            for attachment in persisted_prepared
                        )
                        media_complete = media_complete and not had_failures
                projected_media = tuple(prepared)
            markdown = render_rich_markdown(
                rich_payload,
                title=title,
                full_view_link=link,
                deliverable_id=deliverable_id,
                fallback_text=content,
            )
            if not capabilities.supports_inline_media:
                markdown = render_rich_media_fallbacks(markdown)
        else:
            markdown = render_text_markdown(
                content,
                title=title,
                format_name=format_name,
                full_view_link=link,
                deliverable_id=deliverable_id,
            )
        chunks = split_message(markdown, capabilities.max_message_length)
        identity = ":".join(
            [
                "deliverable",
                deliverable_id,
                str(getattr(row, "version", 1) or 1),
                str(getattr(row, "content_hash", "") or ""),
                str(getattr(row, "rich_hash", "") or ""),
            ]
        )
        return ChannelProjection(
            chunks=chunks,
            identity=identity,
            media=projected_media,
            media_complete=media_complete,
            rich=is_rich,
        )

    def _deliverable_view_link(
        self,
        artifact_store: Any | None,
        deliverable_id: str,
    ) -> DeliverableViewLink | None:
        """Prefer a bounded signed public view link; fall back to private view URL."""

        if artifact_store is not None:
            try:
                return signed_deliverable_view_link(
                    artifact_store,
                    deliverable_id,
                    base_url=self._public_base_url,
                )
            except DeliverableShareUnavailable:
                logger.info(
                    "channel delivery: signed deliverable link unavailable",
                    extra={"extra_data": {"deliverable_id": deliverable_id}},
                )
        base_url = self._public_base_url
        if not base_url and artifact_store is not None:
            config = getattr(artifact_store, "_config", None)
            base_url = str(getattr(config, "base_url", "") or "")
        url = private_deliverable_view_url(deliverable_id, base_url=base_url)
        if url is None:
            return None
        return DeliverableViewLink(
            url=url,
            expires_at=None,
            public=False,
            stable_url=url,
        )
