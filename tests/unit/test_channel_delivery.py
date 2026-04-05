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

    assert 'Approval required for tool "bash".' in content
    assert "Risk: high" in content
    assert "Reason: Needs network access and may change files." in content
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
    assert 'Approval required for tool "bash".' in content
    assert "Reply /approve to allow it or /deny to block it." in content
