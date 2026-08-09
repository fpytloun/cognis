from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from cognis.channels.adapters.irc import IRCAdapter
from cognis.channels.adapters.signal import SignalAdapter
from cognis.channels.adapters.telegram import TelegramAdapter
from cognis.channels.adapters.whatsapp import WhatsAppAdapter
from cognis.channels.protocol import NonRetryableChannelError
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelRecipient,
    OutboundMessage,
)


def _config(channel_type: str) -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id=f"{channel_type}-account",
        channel_type=channel_type,
        display_name=channel_type,
        agent_id="agent",
        user_email="owner@example.com",
    )


@pytest.mark.parametrize(
    ("adapter", "kinds", "chat_kinds", "supports_resolution"),
    [
        (
            SignalAdapter(),
            {"signal_e164", "signal_uuid", "signal_group_id"},
            ["direct", "group"],
            False,
        ),
        (WhatsAppAdapter(), {"whatsapp_e164"}, ["direct"], False),
        (
            TelegramAdapter(),
            {"telegram_chat_id", "telegram_public_username"},
            ["direct", "group"],
            True,
        ),
        (IRCAdapter(), {"irc_nick", "irc_channel"}, ["direct", "group"], False),
    ],
)
def test_adapters_declare_actual_recipient_capabilities(
    adapter, kinds, chat_kinds, supports_resolution
) -> None:
    capabilities = adapter.capabilities.recipient_capabilities
    assert set(capabilities.address_kinds) == kinds
    assert capabilities.chat_kinds == chat_kinds
    assert capabilities.supports_resolution is supports_resolution
    assert capabilities.supports_creation is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "address_kind", "chat_kind"),
    [
        ("+420123456789", "signal_e164", "direct"),
        ("123e4567-e89b-42d3-a456-426614174000", "signal_uuid", "direct"),
        ("group-id/with+safe=chars", "signal_group_id", "group"),
    ],
)
async def test_signal_resolution_returns_each_canonical_address(
    address: str, address_kind: str, chat_kind: str
) -> None:
    adapter = SignalAdapter()
    adapter._config = _config("signal")  # noqa: SLF001
    recipient = ChannelRecipient(
        channel_type="signal",
        address=address,
        address_kind=address_kind,
        chat_kind=chat_kind,
    )

    target = await adapter.resolve_recipient(recipient, resolution_key="intent")

    assert target.chat_id == address
    assert target.chat_kind == chat_kind
    assert target.account_id == "signal-account"


@pytest.mark.asyncio
async def test_signal_infers_simple_e164_and_rejects_mismatch_or_resolution() -> None:
    adapter = SignalAdapter()
    adapter._config = _config("signal")  # noqa: SLF001

    target = await adapter.resolve_recipient(
        ChannelRecipient(channel_type="signal", address="+420123456789"),
        resolution_key="intent",
    )
    assert target.chat_id == "+420123456789"

    for recipient, code in [
        (
            ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                address_kind="signal_e164",
                chat_kind="group",
            ),
            "chat_kind_mismatch",
        ),
        (
            ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                address_kind="signal_e164",
                allow_resolution=True,
            ),
            "resolution_unsupported",
        ),
        (
            ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                address_kind="signal_e164",
                allow_creation=True,
            ),
            "creation_unsupported",
        ),
    ]:
        with pytest.raises(Exception) as caught:
            await adapter.resolve_recipient(recipient, resolution_key="intent")
        assert caught.value.code == code
        assert recipient.address not in str(caught.value)


@pytest.mark.asyncio
async def test_whatsapp_resolution_converges_to_digits_only_provider_id() -> None:
    adapter = WhatsAppAdapter()
    adapter._config = _config("whatsapp")  # noqa: SLF001

    target = await adapter.resolve_recipient(
        ChannelRecipient(
            channel_type="whatsapp",
            address="+420123456789",
            address_kind="whatsapp_e164",
            chat_kind="direct",
        ),
        resolution_key="intent",
    )

    assert target.chat_id == "420123456789"
    assert target.chat_kind == "direct"


@pytest.mark.asyncio
async def test_whatsapp_resolution_gates_are_structured_and_pii_safe() -> None:
    adapter = WhatsAppAdapter()
    adapter._config = _config("whatsapp")  # noqa: SLF001
    address = "+420123456789"

    for flags, code in [
        ({"allow_resolution": True}, "resolution_unsupported"),
        ({"allow_creation": True}, "creation_unsupported"),
    ]:
        with pytest.raises(Exception) as caught:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="whatsapp",
                    address=address,
                    address_kind="whatsapp_e164",
                    **flags,
                ),
                resolution_key="intent",
            )
        assert caught.value.code == code
        assert address not in str(caught.value)


@pytest.mark.asyncio
async def test_telegram_chat_ids_are_canonical_without_lookup() -> None:
    adapter = TelegramAdapter()
    adapter._config = _config("telegram")  # noqa: SLF001
    adapter._client = None  # noqa: SLF001

    direct = await adapter.resolve_recipient(
        ChannelRecipient(
            channel_type="telegram",
            address="123456789",
            address_kind="telegram_chat_id",
            chat_kind="direct",
            allow_resolution=True,
        ),
        resolution_key="intent",
    )
    group = await adapter.resolve_recipient(
        ChannelRecipient(
            channel_type="telegram",
            address="-100123456789",
            address_kind="telegram_chat_id",
            chat_kind="group",
        ),
        resolution_key="intent",
    )

    assert direct.chat_id == "123456789"
    assert direct.chat_kind == "direct"
    assert group.chat_id == "-100123456789"
    assert group.chat_kind == "group"


@pytest.mark.asyncio
async def test_telegram_username_uses_get_chat_and_returns_numeric_group_id() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url.params["chat_id"]))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": -100987654321, "type": "supergroup", "title": "Public group"},
            },
        )

    adapter = TelegramAdapter()
    adapter._config = _config("telegram")  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    try:
        target = await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="telegram",
                address="@public_group",
                address_kind="telegram_public_username",
                chat_kind="group",
                allow_resolution=True,
            ),
            resolution_key="intent",
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert requested == ["@public_group"]
    assert target.chat_id == "-100987654321"
    assert target.chat_kind == "group"
    assert target.display_name == "Public group"


@pytest.mark.asyncio
async def test_telegram_resolution_gates_and_provider_errors_are_safe() -> None:
    address = "@private_user"

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
        )

    adapter = TelegramAdapter()
    adapter._config = _config("telegram")  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(forbidden), base_url="https://api.telegram.org"
    )
    try:
        with pytest.raises(Exception) as caught:
            await adapter.resolve_recipient(
                ChannelRecipient(
                    channel_type="telegram",
                    address=address,
                    address_kind="telegram_public_username",
                    chat_kind="group",
                    allow_resolution=True,
                ),
                resolution_key="intent",
            )
        assert caught.value.code == "telegram_forbidden"
        assert address not in str(caught.value)
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    private = TelegramAdapter()
    private._config = _config("telegram")  # noqa: SLF001
    with pytest.raises(Exception) as caught:
        await private.resolve_recipient(
            ChannelRecipient(
                channel_type="telegram",
                address=address,
                address_kind="telegram_public_username",
                chat_kind="direct",
                allow_resolution=True,
            ),
            resolution_key="intent",
        )
    assert caught.value.code == "private_user_first_contact"
    assert address not in str(caught.value)

    creation = TelegramAdapter()
    creation._config = _config("telegram")  # noqa: SLF001
    with pytest.raises(Exception) as caught:
        await creation.resolve_recipient(
            ChannelRecipient(
                channel_type="telegram",
                address="@public_group",
                address_kind="telegram_public_username",
                chat_kind="group",
                allow_resolution=True,
                allow_creation=True,
            ),
            resolution_key="intent",
        )
    assert caught.value.code == "creation_unsupported"
    assert "@public_group" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "address_kind", "chat_kind"),
    [
        ("alice", "irc_nick", "direct"),
        ("#cognis", "irc_channel", "group"),
    ],
)
async def test_irc_resolution_returns_validated_canonical_target(
    address: str, address_kind: str, chat_kind: str
) -> None:
    adapter = IRCAdapter()
    adapter._config = _config("irc")  # noqa: SLF001

    target = await adapter.resolve_recipient(
        ChannelRecipient(
            channel_type="irc",
            address=address,
            address_kind=address_kind,
            chat_kind=chat_kind,
        ),
        resolution_key="intent",
    )

    assert target.chat_id == address
    assert target.chat_kind == chat_kind


@pytest.mark.asyncio
async def test_irc_rejects_control_characters_and_has_no_directory_operations() -> None:
    adapter = IRCAdapter()
    adapter._config = _config("irc")  # noqa: SLF001
    address = "alice\r\nPRIVMSG #other"

    with pytest.raises(Exception) as caught:
        await adapter.resolve_recipient(
            ChannelRecipient(
                channel_type="irc",
                address=address,
                address_kind="irc_nick",
                chat_kind="direct",
            ),
            resolution_key="intent",
        )

    assert caught.value.code == "invalid_address"
    assert address not in str(caught.value)


@pytest.mark.asyncio
async def test_whatsapp_graph_policy_failure_is_explicit_and_nonretryable() -> None:
    recipient = "+420123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 132001,
                    "type": "OAuthException",
                    "message": f"Template rejected for {recipient}",
                }
            },
        )

    adapter = WhatsAppAdapter()
    adapter._config = _config("whatsapp")  # noqa: SLF001
    adapter._phone_number_id = "phone-number"
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com"
    )
    try:
        with pytest.raises(NonRetryableChannelError) as caught:
            await adapter.send_message(
                OutboundMessage(
                    channel_type="whatsapp",
                    account_id="whatsapp-account",
                    chat_id=recipient[1:],
                    content="hello",
                )
            )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == "provider_policy"
    assert recipient not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "provider_code", "expected_code", "permanent"),
    [
        (500, None, "provider_error", False),
        (400, 132001, "provider_policy", True),
        (400, 190, "provider_auth", True),
        (400, 999999, "provider_unknown", False),
    ],
)
async def test_whatsapp_graph_failures_are_classified_without_recipient_data(
    status_code: int,
    provider_code: int | None,
    expected_code: str,
    permanent: bool,
) -> None:
    recipient = "+420123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        error: dict[str, object] = {
            "code": provider_code if provider_code is not None else 999999,
            "message": f"Failure for {recipient}",
        }
        return httpx.Response(status_code, json={"error": error})

    adapter = WhatsAppAdapter()
    adapter._phone_number_id = "phone-number"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com"
    )
    try:
        with pytest.raises(Exception) as caught:
            await adapter.send_message(
                OutboundMessage(
                    channel_type="whatsapp",
                    account_id="whatsapp-account",
                    chat_id=recipient[1:],
                    content="hello",
                )
            )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert caught.value.code == expected_code
    assert isinstance(caught.value, NonRetryableChannelError) is permanent
    assert recipient not in str(caught.value)


@pytest.mark.asyncio
async def test_whatsapp_network_failure_remains_a_normal_retryable_exception() -> None:
    recipient = "+420123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    adapter = WhatsAppAdapter()
    adapter._phone_number_id = "phone-number"  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com"
    )
    try:
        with pytest.raises(httpx.ConnectError) as caught:
            await adapter.send_message(
                OutboundMessage(
                    channel_type="whatsapp",
                    account_id="whatsapp-account",
                    chat_id=recipient[1:],
                    content="hello",
                )
            )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert not isinstance(caught.value, NonRetryableChannelError)
    assert recipient not in str(caught.value)


@pytest.mark.asyncio
async def test_whatsapp_successful_send_still_returns_provider_message_id() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.success"}]})

    adapter = WhatsAppAdapter()
    adapter._config = _config("whatsapp")  # noqa: SLF001
    adapter._phone_number_id = "phone-number"
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com"
    )
    try:
        result = await adapter.send_message(
            OutboundMessage(
                channel_type="whatsapp",
                account_id="whatsapp-account",
                chat_id="420123456789",
                content="hello",
            )
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert result == "wamid.success"
    assert payloads[0]["to"] == "420123456789"


@pytest.mark.asyncio
async def test_telegram_successful_send_still_returns_provider_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    adapter = TelegramAdapter()
    adapter._config = _config("telegram")  # noqa: SLF001
    adapter._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    try:
        result = await adapter.send_message(
            OutboundMessage(
                channel_type="telegram",
                account_id="telegram-account",
                chat_id="-100123456789",
                content="hello",
            )
        )
    finally:
        await adapter._client.aclose()  # noqa: SLF001

    assert result == "123"


@pytest.mark.asyncio
async def test_irc_send_still_returns_transport_level_acknowledgment_limitation() -> None:
    writes: list[bytes] = []
    adapter = IRCAdapter()
    adapter._writer = SimpleNamespace(write=writes.append)  # noqa: SLF001

    result = await adapter.send_message(
        OutboundMessage(
            channel_type="irc",
            account_id="irc-account",
            chat_id="#cognis",
            content="hello",
        )
    )

    assert result is None
    assert writes == [b"PRIVMSG #cognis :hello\r\n"]
