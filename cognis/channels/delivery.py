"""Outbound delivery service — EventBus → channel.

Subscribes to EventBus events (TASK_COMPLETED, ESCALATION_CREATED, etc.)
and delivers notifications to the originating channel by looking up the
conversation's channel context in ``ConversationContext.platform_data``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.formatting import format_for_channel
from cognis.channels.protocol import (
    CHANNEL_DELIVERY_ERRORS,
    CHANNEL_OUTBOUND_TOTAL,
)
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.channel import MediaAttachment, OutboundMessage
from cognis.store.queries import (
    get_agent_direct_conversation,
    get_conversation,
    get_conversation_channel_route,
    get_latest_active_conversation_for_channel_account,
    get_preferred_channel_account_for_agent,
)

logger = get_logger(__name__)


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
    url = raw.get("url") if isinstance(raw.get("url"), str) else None
    filename = raw.get("filename") if isinstance(raw.get("filename"), str) else None
    mime_type = raw.get("mime_type") if isinstance(raw.get("mime_type"), str) else None
    if url and mime_type and mime_type.startswith("image/"):
        return url
    if url and filename:
        return f"{filename}\n{url}"
    return url or filename


async def prepare_media_attachments(
    media: list[dict[str, Any]],
    *,
    session_factory: async_sessionmaker[Any] | None,
    artifact_store: Any | None,
) -> tuple[list[MediaAttachment], list[str], bool]:
    prepared: list[MediaAttachment] = []
    fallback_lines: list[str] = []
    had_failures = False
    for item in media:
        attachment, fallback_line, materialized = await _materialize_media_attachment(
            item,
            session_factory=session_factory,
            artifact_store=artifact_store,
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
) -> tuple[MediaAttachment | None, str | None, bool]:
    content_b64 = raw.get("content_b64") if isinstance(raw.get("content_b64"), str) else None
    artifact_id = raw.get("artifact_id")
    if content_b64 is None and isinstance(artifact_id, str) and artifact_id:
        from cognis.store.queries import get_artifact_record

        try:
            if session_factory is not None and artifact_store is not None:
                async with session_factory() as session:
                    row = await get_artifact_record(session, artifact_id)
                if row is not None and row.status != "deleted":
                    content, _ct = await artifact_store.async_load(
                        row.namespace,
                        row.object_id,
                        row.filename,
                    )
                    return (
                        MediaAttachment(
                            url=raw.get("url") if isinstance(raw.get("url"), str) else None,
                            mime_type=(
                                raw.get("mime_type")
                                if isinstance(raw.get("mime_type"), str)
                                else None
                            )
                            or getattr(row, "mime_type", None),
                            filename=(
                                raw.get("filename")
                                if isinstance(raw.get("filename"), str)
                                else None
                            )
                            or row.filename,
                            size_bytes=(
                                raw.get("size_bytes")
                                if isinstance(raw.get("size_bytes"), int)
                                else None
                            )
                            or len(content),
                            content_b64=base64.b64encode(content).decode("ascii"),
                        ),
                        None,
                        True,
                    )
        except Exception:
            logger.warning("channel delivery: failed to materialize attachment", exc_info=True)
            if isinstance(raw.get("url"), str):
                return (
                    MediaAttachment(
                        url=raw["url"],
                        mime_type=raw.get("mime_type")
                        if isinstance(raw.get("mime_type"), str)
                        else None,
                        filename=raw.get("filename")
                        if isinstance(raw.get("filename"), str)
                        else None,
                        size_bytes=raw.get("size_bytes")
                        if isinstance(raw.get("size_bytes"), int)
                        else None,
                    ),
                    None,
                    True,
                )
            return None, _attachment_fallback_text(raw), False
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
        status = await self._send_to_route(
            channel_type=channel_type,
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            content=content,
            media=attachments,
        )
        return status == "sent"

    async def _send_to_route(
        self,
        *,
        channel_type: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None,
        content: str,
        media: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send content to a resolved channel route.

        Returns ``sent`` when all chunks were delivered, ``failed`` when
        nothing was sent, and ``partial`` when some chunks were sent
        before a later chunk failed.
        """

        manager = self._channel_manager_ref()
        if manager is None:
            return "failed"

        result = manager.find_adapter_for_channel(channel_type, account_id)
        if result is None:
            return "failed"

        adapter, config = result

        (
            outbound_media,
            attachment_fallback_lines,
            had_attachment_failures,
        ) = await self._prepare_media_attachments(media or [])
        content = _append_attachment_fallback(content, attachment_fallback_lines)
        if not content and not outbound_media:
            return "failed"

        if channel_type == "signal":
            try:
                message_id = await adapter.send_message(
                    OutboundMessage(
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        content=content,
                        thread_id=thread_id,
                        media=outbound_media,
                    )
                )
                if (not isinstance(message_id, str) or not message_id.strip()) and (
                    content or outbound_media
                ):
                    CHANNEL_DELIVERY_ERRORS.labels(
                        channel_type=channel_type,
                        account_id=account_id,
                    ).inc()
                    return "failed"
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return "partial" if had_attachment_failures else "sent"
            except Exception:
                CHANNEL_DELIVERY_ERRORS.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return "failed"

        if outbound_media:
            try:
                await adapter.send_message(
                    OutboundMessage(
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        content=content,
                        thread_id=thread_id,
                        media=outbound_media,
                    )
                )
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return "partial" if had_attachment_failures else "sent"
            except Exception:
                CHANNEL_DELIVERY_ERRORS.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return "failed"

        # Format for channel
        chunks = format_for_channel(content, adapter.capabilities)

        delivered = False
        for chunk in chunks:
            try:
                await adapter.send_message(
                    OutboundMessage(
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        content=chunk,
                        thread_id=thread_id,
                    )
                )
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                delivered = True
            except Exception:
                CHANNEL_DELIVERY_ERRORS.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                return "partial" if delivered else "failed"

        if not delivered:
            CHANNEL_DELIVERY_ERRORS.labels(
                channel_type=channel_type,
                account_id=account_id,
            ).inc()

        return "sent" if delivered else "failed"

    async def _prepare_media_attachments(
        self, media: list[dict[str, Any]]
    ) -> tuple[list[MediaAttachment], list[str], bool]:
        manager = self._channel_manager_ref()
        return await prepare_media_attachments(
            media,
            session_factory=self._session_factory,
            artifact_store=getattr(manager, "_artifact_store", None)
            if manager is not None
            else None,
        )

    async def _materialize_media_attachment(
        self, raw: dict[str, Any]
    ) -> tuple[MediaAttachment | None, str | None, bool]:
        manager = self._channel_manager_ref()
        return await _materialize_media_attachment(
            raw,
            session_factory=self._session_factory,
            artifact_store=getattr(manager, "_artifact_store", None)
            if manager is not None
            else None,
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
        delivery_id = event.data.get("delivery_id")
        if not isinstance(delivery_id, str) or not event.data.get("channel_deliverable"):
            return

        final_content = event.data.get("final_content")
        if not isinstance(final_content, str):
            final_content = ""
        fallback_text = event.data.get("delivery_fallback_text")
        if not isinstance(fallback_text, str):
            fallback_text = None
        attachments = event.data.get("attachments")
        if not isinstance(attachments, list):
            attachments = None
        deliverable_id = event.data.get("final_deliverable_id")
        if not isinstance(deliverable_id, str):
            deliverable_id = None

        await self._deliver_outbox(
            delivery_id=delivery_id,
            final_content=final_content.strip() or None,
            fallback_text=fallback_text,
            attachments=attachments,
            deliverable_id=deliverable_id,
            ignore_next_attempt=True,
        )

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

        lines = [f"*[escalation]* Approval required for tool `{tool_name}`."]
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
            mark_channel_delivery_failed,
        )

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            stale = await list_channel_delivery_outbox_stale_sending(session, now=now)
            for row in stale:
                reset = await mark_channel_delivery_failed(
                    session,
                    delivery_id=row.delivery_id,
                    lease_token=row.lease_token or "",
                    last_error="stale_sending_recovered",
                    next_attempt_at=now,
                )
                if reset:
                    logger.warning(
                        "channel delivery: reset stale sending record for retry",
                        extra={
                            "extra_data": {
                                "delivery_id": row.delivery_id,
                                "conversation_id": row.conversation_id,
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

        async with self._session_factory() as session:
            due = await list_channel_delivery_outbox_due(session, now=now)

        for row in due:
            final_content: str | None = None
            attachments: list[dict[str, Any]] | None = None
            deliverable_id: str | None = None
            if row.source_type == "task" and isinstance(row.source_id, str) and row.source_id:
                async with self._session_factory() as session:
                    task_row = await get_task(session, row.source_id)
                result_data = (
                    task_row.result_data
                    if task_row is not None and isinstance(task_row.result_data, dict)
                    else {}
                )
                raw_final_content = result_data.get("final_channel_content") or result_data.get(
                    "final_content"
                )
                if isinstance(raw_final_content, str) and raw_final_content.strip():
                    final_content = raw_final_content.strip()
                raw_attachments = result_data.get("attachments")
                if isinstance(raw_attachments, list):
                    attachments = [item for item in raw_attachments if isinstance(item, dict)]
                raw_deliverable_id = result_data.get("final_deliverable_id")
                if isinstance(raw_deliverable_id, str) and raw_deliverable_id:
                    deliverable_id = raw_deliverable_id
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
            return row.status in {"pending", "sending", "failed", "sent"}

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
            mark_channel_delivery_failed,
            mark_channel_delivery_sent,
            mark_channel_delivery_uncertain,
            update_deliverable_status,
        )

        lease_token = f"lease_{uuid.uuid4().hex[:12]}"
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        async with self._session_factory() as session:
            row = await claim_channel_delivery_outbox(
                session,
                delivery_id=delivery_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                ignore_next_attempt=ignore_next_attempt,
            )
            await session.commit()

        if row is None:
            return

        content = final_content or fallback_text or ""
        if not content and not attachments:
            async with self._session_factory() as session:
                await mark_channel_delivery_sent(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                )
                await session.commit()
            return

        delivery_status = "failed"
        try:
            delivery_status = await self._send_to_route(
                channel_type=row.channel_type,
                account_id=row.account_id,
                chat_id=row.chat_id,
                thread_id=row.thread_id,
                content=content,
                media=attachments,
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
            delivery_status = "failed"

        async with self._session_factory() as session:
            if delivery_status == "sent":
                ok = await mark_channel_delivery_sent(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
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
            elif delivery_status == "partial":
                await mark_channel_delivery_uncertain(
                    session,
                    delivery_id=delivery_id,
                )
            else:
                await mark_channel_delivery_failed(
                    session,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                    last_error="channel_send_failed",
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
