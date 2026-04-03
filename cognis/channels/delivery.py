"""Outbound delivery service — EventBus → channel.

Subscribes to EventBus events (TASK_COMPLETED, ESCALATION_CREATED, etc.)
and delivers notifications to the originating channel by looking up the
conversation's channel context in ``ConversationContext.platform_data``.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.formatting import format_for_channel
from cognis.channels.protocol import (
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
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._channel_manager_ref = channel_manager_ref

        # Subscribe to relevant events
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_task_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_task_event)
        event_bus.subscribe(EventType.ESCALATION_CREATED, self._handle_escalation_event)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._handle_notification_event)

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

        channel_type, account_id, chat_id = channel_info
        manager = self._channel_manager_ref()
        if manager is None:
            return False

        result = manager.find_adapter_for_channel(channel_type, account_id)
        if result is None:
            return False

        adapter, config = result

        # Format for channel
        chunks = format_for_channel(content, adapter.capabilities)

        delivered = False
        for chunk in chunks:
            with contextlib.suppress(Exception):
                await adapter.send_message(
                    OutboundMessage(
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        content=chunk,
                    )
                )
                CHANNEL_OUTBOUND_TOTAL.labels(
                    channel_type=channel_type,
                    account_id=account_id,
                ).inc()
                delivered = True

        return delivered

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _handle_task_event(self, event: Event) -> None:
        """Handle task completion/failure events."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
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
        else:
            content = f'Task "{task_title}" failed.'
            if result_summary:
                content += f"\n\nError: {result_summary}"

        await self.send_to_conversation(conversation_id, content)

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
        message = event.data.get("message", "You have a new notification.")
        content = f"[{notification_type}] {message}"
        await self.send_to_conversation(conversation_id, content)

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    async def _resolve_channel(
        self,
        conversation_id: str,
    ) -> tuple[str, str, str] | None:
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

        if not all([channel_type, account_id, chat_id]):
            # Try parsing from context_ref (format: "channel_type:account_id:chat_id")
            if row.context_ref and ":" in row.context_ref:
                parts = row.context_ref.split(":", 2)
                if len(parts) == 3:
                    return parts[0], parts[1], parts[2]
            return None

        return channel_type, account_id, chat_id
