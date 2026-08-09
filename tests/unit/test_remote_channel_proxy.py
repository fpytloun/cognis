from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from cognis.channels.remote import RemoteChannelAdapterProxy
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelCapabilities,
    ChannelRecipient,
    ChannelStatus,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
    ResolvedChannelTarget,
)
from cognis.providers.executor.websocket import ExecutorDisconnectedError, ExecutorRPCError


class FakeConnection:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.executor_id = "exec-1"
        self.calls: list[tuple[str, dict, float | None]] = []
        self._result = result or {"status": "ok", "platform_message_id": "pm-1"}
        self._error = error

    async def rpc_call(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self.calls.append((method, params, timeout))
        if self._error is not None:
            raise self._error
        return self._result


def _config() -> ChannelAccountConfig:
    return ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )


@pytest.mark.asyncio
async def test_start_sends_channel_start_rpc() -> None:
    conn = FakeConnection({"status": "started"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    await proxy.start(_config(), {"token": "secret"}, None)
    method, params, timeout = conn.calls[0]
    assert method == "channel.start"
    assert params["account_id"] == "acct-1"
    assert params["credentials"]["token"] == "secret"
    assert timeout == 30.0


@pytest.mark.asyncio
async def test_start_narrows_runtime_capabilities_and_omitted_metadata_is_compatible() -> None:
    capabilities = ChannelCapabilities(
        supports_media=True,
        supports_typing=True,
        recipient_capabilities={
            "address_kinds": ["signal_e164", "signal_uuid"],
            "supports_resolution": True,
        },
    )
    advertised = capabilities.model_dump(mode="json")
    advertised["supports_media"] = False
    advertised["supports_typing"] = True
    advertised["recipient_capabilities"] = {
        "address_kinds": ["signal_e164"],
        "chat_kinds": ["direct"],
        "supports_resolution": True,
        "supports_creation": False,
    }
    conn = FakeConnection({"status": "started", "capabilities": advertised})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=capabilities,
        account_id="acct-1",
    )
    await proxy.start(_config(), {}, None)
    assert proxy.capabilities.supports_media is False
    assert proxy.capabilities.supports_typing is True
    assert proxy.capabilities.recipient_capabilities.address_kinds == ["signal_e164"]

    legacy = FakeConnection({"status": "started"})
    legacy_proxy = RemoteChannelAdapterProxy(
        connection=legacy,
        channel_type="signal",
        capabilities=capabilities,
        account_id="acct-1",
    )
    await legacy_proxy.start(_config(), {}, None)
    assert legacy_proxy.capabilities == capabilities


@pytest.mark.asyncio
async def test_resolve_recipient_wire_shape_and_result() -> None:
    target = ResolvedChannelTarget(
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        chat_kind="direct",
        display_name="Contact",
    )
    conn = FakeConnection(target.model_dump(mode="json"))
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    recipient = ChannelRecipient(
        channel_type="signal",
        address="+420111222333",
        address_kind="signal_e164",
        chat_kind="direct",
        allow_resolution=True,
    )
    result = await proxy.resolve_recipient(recipient, resolution_key="opaque-resolution-key")
    assert result == target
    method, params, timeout = conn.calls[0]
    assert method == "channel.resolve_recipient"
    assert params == {
        "account_id": "acct-1",
        "recipient": recipient.model_dump(mode="json"),
        "resolution_key": "opaque-resolution-key",
    }
    assert timeout == 30.0


@pytest.mark.asyncio
async def test_resolve_recipient_structured_error_is_safe_and_transport_errors_raise() -> None:
    raw_address = "+420999888777"
    conn = FakeConnection(
        {
            "error": {
                "code": "provider_rejected",
                "message": "Recipient resolution failed",
                "retryable": False,
                "side_effect_certainty": "none",
            }
        }
    )
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    with pytest.raises(RuntimeError) as exc_info:
        await proxy.resolve_recipient(
            ChannelRecipient(channel_type="signal", address=raw_address),
            resolution_key="key",
        )
    assert exc_info.value.code == "provider_rejected"
    assert raw_address not in str(exc_info.value)

    disconnected = RemoteChannelAdapterProxy(
        connection=FakeConnection(error=ExecutorDisconnectedError("connection lost")),
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    with pytest.raises(ExecutorDisconnectedError):
        await disconnected.resolve_recipient(
            ChannelRecipient(channel_type="signal", address=raw_address),
            resolution_key="key",
        )

    timed_out = RemoteChannelAdapterProxy(
        connection=FakeConnection(error=TimeoutError()),
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    with pytest.raises(TimeoutError):
        await timed_out.resolve_recipient(
            ChannelRecipient(channel_type="signal", address=raw_address),
            resolution_key="key",
        )


@pytest.mark.asyncio
async def test_resolve_recipient_protocol_error_data_matches_result_envelope() -> None:
    raw_address = "+420999888777"
    data = {
        "code": "provider_rejected",
        "message": f"leaked {raw_address}",
        "retryable": True,
        "side_effect_certainty": "uncertain",
    }
    recipient = ChannelRecipient(channel_type="signal", address=raw_address)

    protocol_error = RemoteChannelAdapterProxy(
        connection=FakeConnection(
            error=ExecutorRPCError(-32000, f"leaked {raw_address}", data=data)
        ),
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    envelope_error = RemoteChannelAdapterProxy(
        connection=FakeConnection({"error": data}),
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )

    with pytest.raises(RuntimeError) as protocol_exc:
        await protocol_error.resolve_recipient(recipient, resolution_key="key")
    with pytest.raises(RuntimeError) as envelope_exc:
        await envelope_error.resolve_recipient(recipient, resolution_key="key")

    assert type(protocol_exc.value) is type(envelope_exc.value)
    assert protocol_exc.value.code == envelope_exc.value.code == "provider_rejected"
    assert protocol_exc.value.retryable is envelope_exc.value.retryable is True
    assert (
        protocol_exc.value.side_effect_certainty
        == envelope_exc.value.side_effect_certainty
        == "uncertain"
    )
    assert raw_address not in str(protocol_exc.value)
    assert raw_address not in str(envelope_exc.value)


@pytest.mark.asyncio
async def test_resolve_recipient_invalid_protocol_error_data_is_safe() -> None:
    raw_address = "+420999888777"
    proxies = [
        RemoteChannelAdapterProxy(
            connection=FakeConnection(
                error=ExecutorRPCError(-32000, raw_address, data={"message": raw_address})
            ),
            channel_type="signal",
            capabilities=ChannelCapabilities(),
            account_id="acct-1",
        ),
        RemoteChannelAdapterProxy(
            connection=FakeConnection({"error": {"message": raw_address}}),
            channel_type="signal",
            capabilities=ChannelCapabilities(),
            account_id="acct-1",
        ),
    ]
    for proxy in proxies:
        with pytest.raises(RuntimeError) as exc_info:
            await proxy.resolve_recipient(
                ChannelRecipient(channel_type="signal", address=raw_address),
                resolution_key="key",
            )
        assert raw_address not in str(exc_info.value)


@pytest.mark.asyncio
async def test_stop_sends_channel_stop_rpc() -> None:
    conn = FakeConnection({"status": "stopped"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    await proxy.stop()
    assert conn.calls[0][0] == "channel.stop"


@pytest.mark.asyncio
async def test_send_message_sends_channel_send_rpc() -> None:
    conn = FakeConnection({"status": "sent", "platform_message_id": "discord-1"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="discord",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    result = await proxy.send_message(
        OutboundMessage(channel_type="discord", account_id="acct-1", chat_id="c1", content="hello")
    )
    assert result == "discord-1"
    assert conn.calls[0][0] == "channel.send"


@pytest.mark.asyncio
async def test_start_failure_sets_error_status() -> None:
    conn = FakeConnection(error=RuntimeError("boom"))
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    with pytest.raises(RuntimeError, match="boom"):
        await proxy.start(_config(), {}, None)
    status = await proxy.get_status()
    assert status.status == ChannelStatus.ERROR
    assert status.last_error == "boom"


@pytest.mark.asyncio
async def test_update_status() -> None:
    conn = FakeConnection()
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    proxy.update_status({"status": "connected", "last_error": "warn"})
    status = await proxy.get_status()
    assert status.status == ChannelStatus.CONNECTED
    assert status.last_error == "warn"


@pytest.mark.asyncio
async def test_send_typing_sends_rpc() -> None:
    conn = FakeConnection({"status": "ok"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    await proxy.send_typing("+420111222333")
    assert conn.calls[0][0] == "channel.typing"
    assert conn.calls[0][1]["chat_id"] == "+420111222333"


@pytest.mark.asyncio
async def test_mark_read_sends_rpc() -> None:
    conn = FakeConnection({"status": "ok"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    await proxy.mark_read("+420111222333", "12345")
    assert conn.calls[0][0] == "channel.mark_read"
    assert conn.calls[0][1]["message_id"] == "12345"


@pytest.mark.asyncio
async def test_sync_profile_sends_rpc() -> None:
    from cognis.models.channel import AgentProfile

    conn = FakeConnection({"status": "ok"})
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    await proxy.sync_profile(AgentProfile(name="TestBot"))
    assert conn.calls[0][0] == "channel.sync_profile"
    assert conn.calls[0][1]["name"] == "TestBot"


@pytest.mark.asyncio
async def test_send_typing_failure_does_not_raise() -> None:
    conn = FakeConnection(error=RuntimeError("boom"))
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    # Should not raise
    await proxy.send_typing("+420111222333")


@pytest.mark.asyncio
async def test_get_status_returns_current_state() -> None:
    conn = FakeConnection()
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
    )
    status = await proxy.get_status()
    assert status.account_id == "acct-1"
    assert status.channel_type == "signal"
    assert status.status == ChannelStatus.DISCONNECTED


@pytest.mark.asyncio
async def test_fetch_media_retries_once_on_reconnected_executor() -> None:
    disconnected = FakeConnection(error=ExecutorDisconnectedError("disconnected"))
    replacement = FakeConnection(
        {
            "content_b64": base64.b64encode(b"audio").decode("ascii"),
            "content_type": "audio/mp4",
            "filename": "voice.m4a",
        }
    )
    reconnect_calls = 0

    async def reconnect() -> FakeConnection:
        nonlocal reconnect_calls
        reconnect_calls += 1
        return replacement

    proxy = RemoteChannelAdapterProxy(
        connection=disconnected,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
        reconnect_connection=reconnect,
    )
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=datetime.now(UTC),
    )

    result = await proxy.download_attachment_for_stt(
        message,
        MediaAttachment(mime_type="audio/mp4", filename="voice.m4a"),
    )

    assert result == (b"audio", "audio/mp4", "voice.m4a")
    assert reconnect_calls == 1
    assert len(disconnected.calls) == 1
    assert len(replacement.calls) == 1


@pytest.mark.asyncio
async def test_fetch_media_does_not_retry_non_disconnect_failure() -> None:
    connection = FakeConnection(error=RuntimeError("platform failed"))
    reconnect_calls = 0

    async def reconnect() -> FakeConnection:
        nonlocal reconnect_calls
        reconnect_calls += 1
        return connection

    proxy = RemoteChannelAdapterProxy(
        connection=connection,
        channel_type="signal",
        capabilities=ChannelCapabilities(),
        account_id="acct-1",
        reconnect_connection=reconnect,
    )
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=datetime.now(UTC),
    )

    result = await proxy.download_attachment(
        message,
        MediaAttachment(mime_type="audio/mp4", filename="voice.m4a"),
    )

    assert result is None
    assert reconnect_calls == 0
    assert len(connection.calls) == 1
