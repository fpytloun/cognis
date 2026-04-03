from __future__ import annotations

import pytest

from cognis.channels.remote import RemoteChannelAdapterProxy
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelCapabilities,
    ChannelStatus,
    OutboundMessage,
)


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
