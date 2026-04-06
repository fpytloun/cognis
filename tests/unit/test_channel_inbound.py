from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.channels.inbound import (
    ChannelTurnObserver,
    InboundPipeline,
    _extract_buffered_delivery_chunk,
)
from cognis.core.commands import CommandResult
from cognis.models.channel import ChannelAccountConfig, InboundMessage


class _FakeAdapter:
    def __init__(self) -> None:
        self.send_message = AsyncMock()
        self.send_typing = AsyncMock()
        self.capabilities = MagicMock()
        self.capabilities.max_message_length = 4096
        self.capabilities.supports_markdown = False


class _FakeManager:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._adapter = adapter

    def get_adapter(self, account_id: str) -> _FakeAdapter:
        return self._adapter


@pytest.mark.asyncio
async def test_channel_inbound_dispatches_approve_before_submit_turn() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock()
    turn_scheduler.add_observer = MagicMock()
    turn_scheduler.has_active_turn = MagicMock(return_value=False)

    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=turn_scheduler,
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
    )

    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(type="system_message", text="User approved tool call")
    )

    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="/approve",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.submit_turn.assert_not_called()
    turn_scheduler.add_observer.assert_not_called()
    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "User approved tool call"
    assert outbound.reply_to_id == "msg-1"


@pytest.mark.asyncio
async def test_channel_inbound_submits_normal_messages() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock(return_value=None)
    turn_scheduler.add_observer = MagicMock()
    turn_scheduler.has_active_turn = MagicMock(return_value=False)

    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=turn_scheduler,
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
    )

    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(return_value=[])  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.add_observer.assert_called_once()
    turn_scheduler.submit_turn.assert_awaited_once()


def test_extract_buffered_delivery_chunk_prefers_paragraphs() -> None:
    chunk, remainder = _extract_buffered_delivery_chunk("Hello world.\n\nNext part")
    assert chunk == "Hello world."
    assert remainder == "Next part"


def test_extract_buffered_delivery_chunk_keeps_short_text_buffered() -> None:
    chunk, remainder = _extract_buffered_delivery_chunk("Short sentence.")
    assert chunk == ""
    assert remainder == "Short sentence."


@pytest.mark.asyncio
async def test_channel_turn_observer_immediate_mode_flushes_buffered_text() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()

    observer = ChannelTurnObserver(
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        conversation_id="conv-1",
        turn_scheduler=turn_scheduler,
        reply_to_id="msg-1",
        channel_manager_ref=lambda: manager,
        assistant_delivery_mode="immediate",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "First sentence. Second sentence. " * 8)

    assert adapter.send_message.await_count >= 1

    await observer.on_turn_complete(None)
    assert turn_scheduler.remove_observer.called
