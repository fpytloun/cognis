from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cognis.executor.channel_handler import ChannelHandler
from cognis.models.channel import InboundMessage, OutboundMessage


class FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, data: str) -> None:
        self.messages.append(json.loads(data))


class FakeAdapter:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.sent: list[OutboundMessage] = []
        self.on_message = None

    async def start(self, config, credentials, on_message) -> None:
        self.started = True
        self.on_message = on_message

    async def stop(self) -> None:
        self.stopped = True

    async def send_message(self, message: OutboundMessage) -> str:
        self.sent.append(message)
        return "platform-123"


@pytest.mark.asyncio
async def test_start_creates_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())

    result = await handler.start(
        "acct-1",
        "signal",
        {"display_name": "Signal", "agent_id": "a", "user_email": "u@example.com"},
        {"api_url": "http://localhost"},
    )

    assert result == {"status": "started", "account_id": "acct-1"}
    assert handler.active_count == 1
    assert adapter.started is True


@pytest.mark.asyncio
async def test_start_replaces_existing_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeAdapter()
    second = FakeAdapter()
    adapters = iter([first, second])
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: next(adapters))
    handler = ChannelHandler()
    handler.set_ws(FakeWS())

    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    assert first.stopped is True
    assert second.started is True
    assert handler.active_count == 1


@pytest.mark.asyncio
async def test_stop_and_stop_nonexistent(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    stopped = await handler.stop("acct-1")
    missing = await handler.stop("missing")

    assert stopped["status"] == "stopped"
    assert adapter.stopped is True
    assert missing["status"] == "not_found"


@pytest.mark.asyncio
async def test_send_delegates_to_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    result = await handler.send(
        "acct-1",
        {"channel_type": "signal", "chat_id": "+420", "content": "hello", "reply_to_id": "1"},
    )

    assert result == {"status": "sent", "platform_message_id": "platform-123"}
    assert len(adapter.sent) == 1
    assert adapter.sent[0].content == "hello"


@pytest.mark.asyncio
async def test_send_unknown_account_returns_error() -> None:
    handler = ChannelHandler()
    result = await handler.send("missing", {"content": "hello"})
    assert "error" in result


@pytest.mark.asyncio
async def test_inbound_message_sends_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    ws = FakeWS()
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: adapter)
    handler = ChannelHandler()
    handler.set_ws(ws)
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    assert adapter.on_message is not None
    await adapter.on_message(
        InboundMessage(
            channel_type="signal",
            account_id="acct-1",
            message_id="m1",
            sender_id="sender",
            chat_id="chat",
            content="hello",
            timestamp=datetime.now(UTC),
        )
    )

    assert ws.messages[0]["method"] == "channel.message"
    assert ws.messages[0]["params"]["account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_stop_all(monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = [FakeAdapter(), FakeAdapter()]
    iterator = iter(adapters)
    monkeypatch.setattr("cognis.executor.channel_handler._create_adapter", lambda _: next(iterator))
    handler = ChannelHandler()
    handler.set_ws(FakeWS())
    await handler.start("acct-1", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})
    await handler.start("acct-2", "signal", {"agent_id": "a", "user_email": "u@example.com"}, {})

    await handler.stop_all()

    assert all(adapter.stopped for adapter in adapters)
    assert handler.active_count == 0
