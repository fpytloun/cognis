from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.channels.inbound import (
    ChannelTurnObserver,
    InboundPipeline,
    _fallback_attachment_content,
    _filter_turn_attachments_for_voice_input,
    _prepare_audio_for_stt,
    _signal_image_preview_payload,
    _stt_passthrough_target,
)
from cognis.core.commands import CommandResult
from cognis.core.turn_scheduler import TurnResult
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import ChannelAccountConfig, InboundMessage, MediaAttachment


class _FakeAdapter:
    def __init__(self) -> None:
        self.send_message = AsyncMock(return_value="msg-1")
        self.send_typing = AsyncMock()
        self.capabilities = MagicMock()
        self.capabilities.max_message_length = 4096
        self.capabilities.supports_markdown = False


class _FakeManager:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._adapter = adapter
        self._artifact_store = MagicMock()
        self._artifact_store.async_load = AsyncMock(return_value=(b"audio-bytes", "audio/ogg"))

    def get_adapter(self, account_id: str) -> _FakeAdapter:
        return self._adapter


@pytest.mark.asyncio
async def test_channel_inbound_dispatches_approve_before_submit_turn() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock()
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

    turn_scheduler.submit_turn.assert_awaited_once()
    turn_observers = turn_scheduler.submit_turn.await_args.kwargs["turn_observers"]
    assert len(turn_observers) == 1
    assert isinstance(turn_observers[0], ChannelTurnObserver)


@pytest.mark.asyncio
async def test_channel_inbound_transcribes_voice_input_before_submit() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock(return_value=None)
    llm_provider = MagicMock()
    llm_provider.transcribe = AsyncMock(
        return_value=SimpleNamespace(text="transcribed voice message")
    )

    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=turn_scheduler,
        llm_provider=llm_provider,
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
    )

    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            AttachmentRef(
                artifact_id="att-1",
                kind=ArtifactKind.AUDIO,
                mime_type="audio/ogg",
                filename="voice.ogg",
                size_bytes=128,
            )
        ]
    )
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"voice_input": True},
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

    llm_provider.transcribe.assert_awaited_once()
    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.args[1] == "transcribed voice message"


@pytest.mark.asyncio
async def test_channel_inbound_reports_voice_transcription_errors() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock(return_value=None)
    llm_provider = MagicMock()
    llm_provider.transcribe = AsyncMock(side_effect=RuntimeError("provider unavailable"))

    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=turn_scheduler,
        llm_provider=llm_provider,
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
    )

    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            AttachmentRef(
                artifact_id="att-1",
                kind=ArtifactKind.AUDIO,
                mime_type="audio/ogg",
                filename="voice.ogg",
                size_bytes=128,
            )
        ]
    )
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"voice_input": True},
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

    turn_scheduler.submit_turn.assert_not_awaited()
    adapter.send_message.assert_awaited_once()
    assert "couldn't transcribe" in adapter.send_message.await_args.args[0].content.lower()


def test_fallback_attachment_content_uses_raw_media_when_normalization_fails() -> None:
    assert (
        _fallback_attachment_content(
            "",
            [],
            [MediaAttachment(mime_type="audio/ogg", filename="voice.ogg")],
        )
        == "User attached an audio file."
    )


def test_filter_turn_attachments_for_voice_input_removes_audio() -> None:
    attachments = [
        AttachmentRef(
            artifact_id="att-audio",
            kind=ArtifactKind.AUDIO,
            mime_type="audio/ogg",
            filename="voice.ogg",
            size_bytes=10,
        ),
        AttachmentRef(
            artifact_id="att-image",
            kind=ArtifactKind.IMAGE,
            mime_type="image/png",
            filename="image.png",
            size_bytes=10,
        ),
    ]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"voice_input": True},
    )

    filtered = _filter_turn_attachments_for_voice_input(message, attachments)

    assert [attachment.artifact_id for attachment in filtered] == ["att-image"]


def test_stt_passthrough_target_normalizes_filename_extension() -> None:
    assert _stt_passthrough_target("audio/mpeg", "voice-note") == ("audio/mpeg", "voice-note.mp3")
    assert _stt_passthrough_target("audio/ogg", "voice.ogg") == ("audio/ogg", "voice.ogg")


@pytest.mark.asyncio
async def test_prepare_audio_for_stt_requires_ffmpeg_for_aac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cognis.channels.inbound.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        await _prepare_audio_for_stt(
            b"audio-bytes",
            mime_type="audio/aac",
            filename="voice.aac",
        )


@pytest.mark.asyncio
async def test_prepare_audio_for_stt_passthroughs_supported_audio() -> None:
    content, mime_type, filename = await _prepare_audio_for_stt(
        b"audio-bytes",
        mime_type="audio/ogg",
        filename="voice.ogg",
    )

    assert content == b"audio-bytes"
    assert mime_type == "audio/ogg"
    assert filename == "voice.ogg"


@pytest.mark.asyncio
async def test_channel_turn_observer_immediate_mode_does_not_flush_on_tokens() -> None:
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

    adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_turn_observer_immediate_mode_flushes_full_buffer_only_when_requested() -> (
    None
):
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

    await observer.on_token("conv-1", "sess-1", "msg-2", "First paragraph.\n\nSecond paragraph.")
    await observer.flush_buffered_text()

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "First paragraph.\n\nSecond paragraph."


@pytest.mark.asyncio
async def test_channel_turn_observer_immediate_mode_flushes_on_tool_call() -> None:
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

    await observer.on_token("conv-1", "sess-1", "msg-2", "Let me check that.")
    await observer.on_tool_call("conv-1", "sess-1", "call-1", "bash", {"command": "ls"})

    # The accumulated text should have been flushed to the channel
    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "Let me check that."

    # Buffer should be empty now
    assert observer._accumulated_text == ""


@pytest.mark.asyncio
async def test_channel_turn_observer_final_mode_does_not_flush_on_tool_call() -> None:
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
        assistant_delivery_mode="final",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Let me check that.")
    await observer.on_tool_call("conv-1", "sess-1", "call-1", "bash", {"command": "ls"})

    # In final mode, text should NOT be flushed on tool_call — only typing indicator
    adapter.send_message.assert_not_awaited()
    adapter.send_typing.assert_awaited()
    assert observer._accumulated_text == "Let me check that."


@pytest.mark.asyncio
async def test_channel_turn_observer_final_mode_never_flushes_mid_turn() -> None:
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
        assistant_delivery_mode="final",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Some buffered content")
    await observer.flush_buffered_text()

    adapter.send_message.assert_not_awaited()

    await observer.on_turn_complete(None)
    assert turn_scheduler.remove_observer.called


def test_channel_turn_observer_absorbs_latest_reply_anchor() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()

    active = ChannelTurnObserver(
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        conversation_id="conv-1",
        turn_scheduler=turn_scheduler,
        reply_to_id="msg-1",
        channel_manager_ref=lambda: manager,
        assistant_delivery_mode="final",
    )
    queued = ChannelTurnObserver(
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        conversation_id="conv-1",
        turn_scheduler=turn_scheduler,
        reply_to_id="msg-3",
        channel_manager_ref=lambda: manager,
        assistant_delivery_mode="final",
    )

    assert active.absorb_queued_observer(queued) is True
    assert active._reply_to_id == "msg-3"


@pytest.mark.asyncio
async def test_channel_turn_observer_sends_text_and_media_together() -> None:
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
        assistant_delivery_mode="final",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Here is the banner")
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            attachments=[
                {
                    "url": "https://example.com/banner.png",
                    "mime_type": "image/png",
                    "filename": "banner.png",
                    "size_bytes": 123,
                }
            ],
        )
    )

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "Here is the banner"
    assert len(outbound.media) == 1
    assert outbound.media[0].url == "https://example.com/banner.png"


@pytest.mark.asyncio
async def test_channel_turn_observer_sends_attachment_only_turn() -> None:
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
        assistant_delivery_mode="final",
    )

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            attachments=[
                {
                    "url": "https://example.com/banner.png",
                    "mime_type": "image/png",
                    "filename": "banner.png",
                    "size_bytes": 123,
                }
            ],
        )
    )

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == ""
    assert len(outbound.media) == 1
    assert outbound.media[0].url == "https://example.com/banner.png"


@pytest.mark.asyncio
async def test_channel_turn_observer_logs_delivery_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _FakeAdapter()
    adapter.send_message.side_effect = RuntimeError("boom")
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
        assistant_delivery_mode="final",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Here is the banner")

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            attachments=[
                {
                    "url": "https://example.com/banner.png",
                    "mime_type": "image/png",
                    "filename": "banner.png",
                    "size_bytes": 123,
                }
            ],
        )
    )

    adapter.send_message.assert_awaited_once()
    assert "final delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_channel_turn_observer_uses_signal_image_preview_fallback_on_missing_message_id() -> (
    None
):
    adapter = _FakeAdapter()
    adapter.send_message = AsyncMock(side_effect=[None, "fallback-1"])
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
        assistant_delivery_mode="final",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Here is the banner")
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            attachments=[
                {
                    "url": "https://example.com/banner.png",
                    "mime_type": "image/png",
                    "filename": "banner.png",
                    "size_bytes": 123,
                }
            ],
        )
    )

    assert adapter.send_message.await_count == 2
    fallback = adapter.send_message.await_args_list[1].args[0]
    assert fallback.content == "Here is the banner\n\nhttps://example.com/banner.png"
    assert fallback.platform_data["signal_preview"] == {
        "url": "https://example.com/banner.png",
        "image": "https://example.com/banner.png",
        "title": "banner.png",
    }


def test_signal_image_preview_payload_prefers_first_image_with_url() -> None:
    preview = _signal_image_preview_payload(
        [
            {
                "url": "https://example.com/file.pdf",
                "mime_type": "application/pdf",
                "filename": "file.pdf",
            },
            {
                "url": "https://example.com/banner.png",
                "mime_type": "image/png",
                "filename": "banner.png",
            },
        ]
    )

    assert preview == {
        "url": "https://example.com/banner.png",
        "image": "https://example.com/banner.png",
        "title": "banner.png",
    }


@pytest.mark.asyncio
async def test_channel_turn_observer_does_not_fallback_for_non_signal_missing_message_id() -> None:
    adapter = _FakeAdapter()
    adapter.send_message = AsyncMock(return_value=None)
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()

    observer = ChannelTurnObserver(
        channel_type="telegram",
        account_id="acct-1",
        chat_id="chat-1",
        conversation_id="conv-1",
        turn_scheduler=turn_scheduler,
        reply_to_id="msg-1",
        channel_manager_ref=lambda: manager,
        assistant_delivery_mode="final",
    )

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            attachments=[
                {
                    "url": "https://example.com/banner.png",
                    "mime_type": "image/png",
                    "filename": "banner.png",
                    "size_bytes": 123,
                }
            ],
        )
    )

    adapter.send_message.assert_awaited_once()
