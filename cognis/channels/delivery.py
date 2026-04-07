"""Outbound delivery service — EventBus → channel.

Subscribes to EventBus events (TASK_COMPLETED, ESCALATION_CREATED, etc.)
and delivers notifications to the originating channel by looking up the
conversation's channel context in ``ConversationContext.platform_data``.
"""

from __future__ import annotations

import asyncio
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
from cognis.models.channel import OutboundMessage

logger = get_logger(__name__)


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
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._channel_manager_ref = channel_manager_ref
        self._turn_scheduler = turn_scheduler
        self._retry_task: asyncio.Task[None] | None = None

        # Subscribe to relevant events
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_task_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_task_event)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._handle_task_event)
        event_bus.subscribe(EventType.ESCALATION_CREATED, self._handle_escalation_event)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._handle_notification_event)
        event_bus.subscribe(EventType.TURN_COMPLETED, self._handle_turn_completed_event)
        event_bus.subscribe(EventType.TURN_ERROR, self._handle_turn_error_event)

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
    ) -> bool:
        """Send a message to a conversation's channel.

        Looks up the conversation's channel context and delivers via
        the appropriate adapter.  Returns True if delivered.
        """
        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            return False

        channel_type, account_id, chat_id, thread_id = channel_info
        status = await self._send_to_route(
            channel_type=channel_type,
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            content=content,
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

        # Only deliver to channel conversations (not web)
        channel_info = await self._resolve_channel(conversation_id)
        if channel_info is None:
            return

        task_title = event.data.get("task_title", "Background task")
        event.data.get("status", "completed")
        result_summary = event.data.get("result_summary", "")

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

        await self._deliver_outbox(
            delivery_id=delivery_id,
            final_content=final_content.strip() or None,
            fallback_text=fallback_text,
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
        if notification_type == "step_question" and self._turn_scheduler is not None:
            await self._flush_observer_buffers(conversation_id)

        if notification_type == "escalation" and isinstance(payload, dict):
            content = self._render_escalation_notification(payload)
        elif notification_type == "step_question" and isinstance(payload, dict):
            content = self._render_step_question_notification(payload)
        elif notification_type == "gate" and isinstance(payload, dict):
            content = self._render_gate_notification(payload)
        else:
            message = event.data.get("message", "You have a new notification.")
            content = f"[{notification_type}] {message}"
        await self.send_to_conversation(conversation_id, content)

    def _render_step_question_notification(self, payload: dict[str, Any]) -> str:
        """Render a step question prompt for channel integrations."""
        question = str(payload.get("question") or "The assistant needs more input to continue.")
        lines: list[str] = []
        # Context first (provides rationale before the question)
        context = payload.get("context")
        if isinstance(context, str) and context.strip():
            lines.append(context.strip())
        elif isinstance(context, dict) and isinstance(context.get("context"), str):
            lines.append(context["context"].strip())
        lines.append(question)
        # Numbered option list
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
        lines.append("_Reply /approve or /deny to continue._")
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
            await self._deliver_outbox(
                delivery_id=row.delivery_id,
                final_content=None,
                fallback_text=row.fallback_text,
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
        ignore_next_attempt: bool = False,
    ) -> None:
        from cognis.store.queries import (
            claim_channel_delivery_outbox,
            mark_channel_delivery_failed,
            mark_channel_delivery_sent,
            mark_channel_delivery_uncertain,
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

        content = final_content or fallback_text
        if not content:
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
