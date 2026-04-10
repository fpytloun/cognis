from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cognis.channels.delivery import ChannelDeliveryService
from cognis.core.events import Event, EventBus, EventType


def _make_service() -> ChannelDeliveryService:
    return ChannelDeliveryService(
        session_factory=AsyncMock(),
        event_bus=EventBus(),
        channel_manager_ref=lambda: None,
    )


def test_render_escalation_notification_includes_required_details() -> None:
    service = _make_service()

    content = service._render_escalation_notification(
        {
            "tool_name": "bash",
            "risk": "high",
            "reasoning": "Needs network access and may change files.",
        }
    )

    assert "Approval required for tool `bash`." in content
    assert "**Risk:** high" in content
    assert "**Reason:** Needs network access and may change files." in content
    assert "/approve" in content
    assert "/deny" in content
    assert "optionally add a note" in content


@pytest.mark.asyncio
async def test_notification_event_delivers_rich_escalation_text() -> None:
    service = _make_service()
    service._resolve_channel = AsyncMock(return_value=("signal", "acct-1", "+420111222333"))  # type: ignore[method-assign]
    service.send_to_conversation = AsyncMock(return_value=True)  # type: ignore[method-assign]

    event = Event(
        type=EventType.NOTIFICATION_CREATED,
        data={
            "conversation_id": "conv-1",
            "notification_type": "escalation",
            "payload": {
                "tool_name": "bash",
                "risk": "high",
                "reasoning": "Needs approval before running.",
            },
        },
    )

    await service._handle_notification_event(event)

    service.send_to_conversation.assert_awaited_once()
    content = service.send_to_conversation.await_args.args[1]
    assert "Approval required for tool `bash`." in content
    assert "Reply /approve to allow it or /deny to block it." in content


@pytest.mark.asyncio
async def test_turn_completed_event_uses_delivery_outbox() -> None:
    service = _make_service()
    service._deliver_outbox = AsyncMock()  # type: ignore[method-assign]

    event = Event(
        type=EventType.TURN_COMPLETED,
        data={
            "delivery_id": "cdel_1",
            "channel_deliverable": True,
            "final_content": "Done.",
            "delivery_fallback_text": "fallback",
        },
    )

    await service._handle_turn_completed_event(event)

    service._deliver_outbox.assert_awaited_once_with(
        delivery_id="cdel_1",
        final_content="Done.",
        fallback_text="fallback",
        attachments=None,
        ignore_next_attempt=True,
    )


@pytest.mark.asyncio
async def test_turn_error_event_uses_fallback_outbox_delivery() -> None:
    service = _make_service()
    service._deliver_outbox = AsyncMock()  # type: ignore[method-assign]

    event = Event(
        type=EventType.TURN_ERROR,
        data={
            "delivery_id": "cdel_2",
            "channel_deliverable": True,
            "delivery_fallback_text": "fallback",
        },
    )

    await service._handle_turn_error_event(event)

    service._deliver_outbox.assert_awaited_once_with(
        delivery_id="cdel_2",
        final_content=None,
        fallback_text="fallback",
        ignore_next_attempt=True,
    )


@pytest.mark.asyncio
async def test_task_event_is_suppressed_when_follow_up_delivery_exists() -> None:
    service = _make_service()
    service._has_active_follow_up_delivery = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service.send_to_conversation = AsyncMock(return_value=True)  # type: ignore[method-assign]

    event = Event(
        type=EventType.TASK_COMPLETED,
        data={
            "conversation_id": "conv-1",
            "task_id": "task-1",
            "channel_follow_up_delivery_id": "cdel_1",
            "task_title": "Background task",
            "result_summary": "Done",
        },
    )

    await service._handle_task_event(event)

    service.send_to_conversation.assert_not_awaited()
