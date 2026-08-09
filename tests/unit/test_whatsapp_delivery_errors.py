from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.channels.adapters.whatsapp import WhatsAppAdapter
from cognis.channels.delivery import ChannelDeliveryService, ChannelDeliveryStatus
from cognis.core.events import EventBus


class _Manager:
    def __init__(self, adapter: WhatsAppAdapter) -> None:
        self.adapter = adapter

    def find_adapter_for_channel(
        self, channel_type: str, account_id: str
    ) -> tuple[WhatsAppAdapter, object]:
        assert channel_type == "whatsapp"
        assert account_id == "whatsapp-account"
        return self.adapter, object()


def _adapter(status_code: int, provider_code: int) -> WhatsAppAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "code": provider_code,
                    "message": "provider failure for +420123456789",
                }
            },
        )

    adapter = WhatsAppAdapter()
    adapter._phone_number_id = "phone-number"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com"
    )
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "provider_code", "expected_status"),
    [
        (500, 999999, ChannelDeliveryStatus.UNCERTAIN),
        (400, 132001, ChannelDeliveryStatus.PERMANENT),
        (400, 190, ChannelDeliveryStatus.PERMANENT),
        (400, 999999, ChannelDeliveryStatus.UNCERTAIN),
    ],
)
async def test_whatsapp_provider_failures_reach_delivery_with_correct_retryability(
    status_code: int,
    provider_code: int,
    expected_status: ChannelDeliveryStatus,
) -> None:
    adapter = _adapter(status_code, provider_code)
    service = ChannelDeliveryService(
        session_factory=AsyncMock(),
        event_bus=EventBus(),
        channel_manager_ref=lambda: _Manager(adapter),
        public_base_url="https://cognis.example.com",
    )
    try:
        status = await service._send_to_route(  # noqa: SLF001
            channel_type="whatsapp",
            account_id="whatsapp-account",
            chat_id="420123456789",
            thread_id=None,
            content="hello",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert status == expected_status
