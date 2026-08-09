from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from cognis.channels.inbound import (
    ChannelTurnObserver,
    InboundPipeline,
    _fallback_attachment_content,
    _filter_turn_attachments_for_voice_input,
    _is_unmentioned_matrix_thread_followup,
    _normalize_assistant_delivery_mode,
    _prepare_audio_for_stt,
    _signal_image_preview_payload,
    _stt_passthrough_target,
    _stt_supported_audio_mime_types,
)
from cognis.channels.managed import GroupContextReservation, ManagedInboundAdmission
from cognis.channels.remote import RemoteChannelAdapterProxy
from cognis.core.commands import CommandResult
from cognis.core.message_envelope import render_user_message
from cognis.core.turn_scheduler import TurnResult
from cognis.executor.channel_handler import ChannelHandler
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelDeliveryDescriptor,
    InboundMessage,
    MediaAttachment,
)


class _FakeAdapter:
    def __init__(self) -> None:
        self.send_message = AsyncMock(return_value="msg-1")
        self.send_typing = AsyncMock()
        self.mark_read = AsyncMock()
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


def _group_context_pipeline() -> tuple[
    InboundPipeline,
    MagicMock,
    MagicMock,
]:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    scheduler = MagicMock()
    scheduler.submit_turn = AsyncMock(return_value=None)
    scheduler.has_active_turn = MagicMock(return_value=False)
    context_service = MagicMock()
    context_service.admit_inbound = AsyncMock(return_value=None)
    context_service.capture_group_message = AsyncMock(
        return_value=SimpleNamespace(inbound_id="inbound-trigger")
    )
    context_service.reserve_group_context = AsyncMock(
        return_value=GroupContextReservation(
            token="reservation-1",
            contextual_messages=[
                {
                    "content": "preceding chatter",
                    "message_metadata": {
                        "ts": "2026-08-02T10:00:00Z",
                        "channel": "signal",
                        "sender": "Alice",
                        "untrusted": True,
                    },
                    "intention_eligible": False,
                }
            ],
        )
    )
    context_service.settle_group_context = AsyncMock()
    context_service.settle_group_context_in_session = AsyncMock()
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=scheduler,
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
        managed_channel_service=context_service,
    )
    pipeline._resolve_user = AsyncMock(return_value="owner@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return pipeline, scheduler, context_service


def _event(seq: int, data: dict) -> SimpleNamespace:
    return SimpleNamespace(seq=seq, data=data)


def test_normalize_assistant_delivery_mode_preserves_legacy_final_semantics() -> None:
    assert _normalize_assistant_delivery_mode("final") == "concatenated"
    assert _normalize_assistant_delivery_mode("final_only") == "final_only"
    assert _normalize_assistant_delivery_mode("concatenated") == "concatenated"
    assert _normalize_assistant_delivery_mode("immediate") == "immediate"
    assert _normalize_assistant_delivery_mode("unknown") == "concatenated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "chat_id"),
    [("direct", "signal-direct"), ("group", "signal-group")],
)
async def test_authenticated_signal_inbound_records_target_before_managed_consumption(
    chat_type: str,
    chat_id: str,
) -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    managed = MagicMock()
    managed.admit_inbound = AsyncMock(return_value=True)
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: MagicMock(),
        observed_target_recorder=recorder,
        managed_channel_service=managed,
    )
    pipeline._resolve_user = AsyncMock(return_value="owner@example.com")  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="signal-account",
        message_id=f"message-{chat_type}",
        sender_id="allowed-sender",
        sender_name="Allowed sender",
        chat_id=chat_id,
        chat_type=chat_type,
        chat_name="Allowed chat",
        content="Hello",
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="signal-account",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-owner",
        user_email="owner@example.com",
        dm_policy="open",
        group_policy="open",
    )

    await pipeline.process(message, config)

    recorder.record.assert_awaited_once_with(message, config)
    managed.admit_inbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_context_silently_captures_authorized_unmentioned_signal_message() -> None:
    pipeline, scheduler, context_service = _group_context_pipeline()
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="chatter-1",
        sender_id="sender-1",
        chat_id="group-1",
        chat_type="group",
        content="preceding chatter",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
        platform_data={
            "_cognis_ordering_key": "00000000000000000001",
            "_cognis_ordering_source": "provider",
        },
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="owner@example.com",
        group_policy="mention",
        settings={"group_context_enabled": True},
    )

    await pipeline.process(message, config)

    context_service.admit_inbound.assert_not_awaited()
    context_service.capture_group_message.assert_awaited_once()
    scheduler.submit_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_context_normalizes_media_before_silent_capture() -> None:
    pipeline, scheduler, context_service = _group_context_pipeline()
    attachment = AttachmentRef(
        artifact_id="img-silent-group",
        kind=ArtifactKind.IMAGE,
        mime_type="image/png",
        filename="group.png",
        size_bytes=19,
    )
    pipeline._normalize_media_attachments = AsyncMock(return_value=([attachment], 0))  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="media-chatter",
        sender_id="sender-1",
        chat_id="group-1",
        chat_type="group",
        content="",
        media=[
            MediaAttachment(
                content_b64=base64.b64encode(b"image").decode(),
                mime_type="image/png",
                filename="group.png",
            )
        ],
        was_mentioned=False,
        timestamp=datetime.now(UTC),
        platform_data={
            "_cognis_ordering_key": "00000000000000000001",
            "_cognis_ordering_source": "provider",
        },
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="owner@example.com",
        group_policy="mention",
        settings={"group_context_enabled": True},
    )

    await pipeline.process(message, config)

    pipeline._normalize_media_attachments.assert_awaited_once_with(  # type: ignore[attr-defined]
        message=message,
        conversation_id=None,
        user_email="owner@example.com",
    )
    context_service.capture_group_message.assert_awaited_once_with(
        message,
        user_email="owner@example.com",
        policy=ANY,
        attachments=[attachment],
    )
    scheduler.submit_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_managed_image_only_inbound_is_normalized_before_admission() -> None:
    pipeline, scheduler, context_service = _group_context_pipeline()
    attachment = AttachmentRef(
        artifact_id="img-managed-inbound",
        kind=ArtifactKind.IMAGE,
        mime_type="image/png",
        filename="managed.png",
        size_bytes=19,
    )
    pipeline._normalize_media_attachments = AsyncMock(return_value=([attachment], 0))  # type: ignore[method-assign]
    context_service.admit_inbound.return_value = ManagedInboundAdmission(
        binding_id="binding-managed",
        conversation_id="conv-managed-child",
        user_email="owner@example.com",
        content="",
        message_id="managed-image-only",
        version=4,
        owner_epoch=2,
        contextual_messages=[],
        attachments=[
            {
                "artifact_id": "img-managed-inbound",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "managed.png",
                "size_bytes": 19,
            }
        ],
    )
    context_service.observer.return_value = MagicMock()
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="managed-image-only",
        sender_id="owner",
        chat_id="managed-direct",
        chat_type="direct",
        content="",
        media=[
            MediaAttachment(
                content_b64=base64.b64encode(b"image").decode(),
                mime_type="image/png",
                filename="managed.png",
            )
        ],
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="owner@example.com",
        settings={},
    )

    await pipeline.process(message, config)

    pipeline._normalize_media_attachments.assert_awaited_once_with(  # type: ignore[attr-defined]
        message=message,
        conversation_id=None,
        user_email="owner@example.com",
        executor_connection_owner=None,
    )
    context_service.admit_inbound.assert_awaited_once_with(
        message,
        user_email="owner@example.com",
        attachments=[attachment],
    )
    scheduler.submit_turn.assert_awaited_once()
    kwargs = scheduler.submit_turn.await_args.kwargs
    assert kwargs["attachments"] == context_service.admit_inbound.return_value.attachments
    assert kwargs["user_message_metadata"]["untrusted"] is True
    assert scheduler.submit_turn.await_args.args[:2] == ("conv-managed-child", "")


@pytest.mark.asyncio
async def test_group_context_first_mention_attaches_context_and_primary_last_contract() -> None:
    pipeline, scheduler, context_service = _group_context_pipeline()
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="trigger-1",
        sender_id="sender-1",
        sender_name="Alice",
        chat_id="group-1",
        chat_type="group",
        content="@agent summarize",
        was_mentioned=True,
        timestamp=datetime.now(UTC),
        platform_data={
            "_cognis_ordering_key": "00000000000000000002",
            "_cognis_ordering_source": "provider",
        },
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="owner@example.com",
        group_policy="mention",
        settings={"group_context_enabled": True},
    )

    await pipeline.process(message, config)

    scheduler.submit_turn.assert_awaited_once()
    call = scheduler.submit_turn.await_args
    assert call.args[:2] == ("conv-1", "@agent summarize")
    assert call.kwargs["contextual_messages"][0]["content"] == "preceding chatter"
    assert call.kwargs["contextual_messages"][0]["intention_eligible"] is False
    assert call.kwargs["user_message_metadata"]["untrusted"] is True
    assert call.kwargs["intention_eligible"] is False
    rendered = render_user_message(
        call.args[1],
        call.kwargs["user_message_metadata"],
        call.kwargs["contextual_messages"],
    ).splitlines()
    assert rendered[0].endswith('sender="Alice" untrusted="true">preceding chatter</message>')
    assert rendered[-1].endswith('sender="Alice" untrusted="true">@agent summarize</message>')
    assert isinstance(call.kwargs["turn_id"], str)
    assert callable(call.kwargs["admission_transaction_participant"])
    context_service.settle_group_context.assert_awaited_once_with(
        "reservation-1",
        turn_id=call.kwargs["turn_id"],
        succeeded=True,
    )


@pytest.mark.asyncio
async def test_group_context_disabled_does_not_retain_unmentioned_body() -> None:
    pipeline, scheduler, context_service = _group_context_pipeline()
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="private-1",
        sender_id="sender-1",
        chat_id="group-1",
        chat_type="group",
        content="must not persist",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="owner@example.com",
        group_policy="mention",
        settings={},
    )

    await pipeline.process(message, config)

    context_service.capture_group_message.assert_not_awaited()
    scheduler.submit_turn.assert_not_awaited()


def test_channel_conversation_context_carries_assistant_delivery_mode() -> None:
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="Hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    context = InboundPipeline._conversation_context(
        message,
        context_ref="signal:acct-1:chat-1",
        assistant_delivery_mode="final_only",
    )

    assert context.type == "signal"
    assert context.platform_data["assistant_delivery_mode"] == "final_only"


@pytest.mark.asyncio
async def test_stale_executor_inbound_is_rejected_before_any_mutation() -> None:
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock()
    session_manager = MagicMock()
    pairing_service = MagicMock()
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=turn_scheduler,
        session_manager=session_manager,
        pairing_service=pairing_service,
        channel_manager_ref=MagicMock(),
    )
    pipeline._admit_executor_inbound = AsyncMock(return_value=False)  # type: ignore[method-assign]
    pipeline._resolve_user = AsyncMock()  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-stale",
        sender_id="sender-1",
        chat_id="chat-1",
        content="stale",
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(
        message,
        config,
        executor_connection_owner=object(),
    )

    pipeline._resolve_user.assert_not_awaited()
    turn_scheduler.submit_turn.assert_not_awaited()
    assert session_manager.mock_calls == []
    assert pairing_service.mock_calls == []


def test_channel_delivery_mode_refresh_matches_only_same_channel_context_type() -> None:
    assert InboundPipeline._conversation_matches_channel_type(
        SimpleNamespace(context_type="signal"),
        "signal",
    )
    assert not InboundPipeline._conversation_matches_channel_type(
        SimpleNamespace(context_type="web"),
        "signal",
    )
    assert not InboundPipeline._conversation_matches_channel_type(
        SimpleNamespace(context_type=""),
        "signal",
    )


@pytest.mark.asyncio
async def test_channel_inbound_refreshes_existing_channel_delivery_mode() -> None:
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._latest_conversation_for_context = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(conversation_id="conv-existing", context_type="signal")
    )
    pipeline._refresh_channel_context_delivery_mode = AsyncMock()  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="Hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"assistant_delivery_mode": "final_only"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result == "conv-existing"
    pipeline._refresh_channel_context_delivery_mode.assert_awaited_once_with(
        conversation_id="conv-existing",
        channel_type="signal",
        assistant_delivery_mode="final_only",
        executor_connection_owner=None,
    )


@pytest.mark.asyncio
async def test_channel_inbound_refreshes_default_conversation_delivery_mode() -> None:
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._refresh_channel_context_delivery_mode = AsyncMock()  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="Hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="user@example.com",
        default_conversation_id="conv-default",
        settings={"assistant_delivery_mode": "concatenated"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result == "conv-default"
    pipeline._refresh_channel_context_delivery_mode.assert_awaited_once_with(
        conversation_id="conv-default",
        channel_type="signal",
        assistant_delivery_mode="concatenated",
        executor_connection_owner=None,
    )


@pytest.mark.asyncio
async def test_channel_inbound_creates_conversation_with_delivery_mode_context() -> None:
    session_manager = MagicMock()
    session_manager.create_conversation_with_root_session = AsyncMock(
        return_value=(SimpleNamespace(conversation_id="conv-new"), SimpleNamespace())
    )
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._latest_conversation_for_context = AsyncMock(return_value=None)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="Hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="signal",
        display_name="Signal",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"assistant_delivery_mode": "final_only"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result == "conv-new"
    context = session_manager.create_conversation_with_root_session.await_args.kwargs["context"]
    assert context.platform_data["assistant_delivery_mode"] == "final_only"


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
        default_agent_profile_id="chat",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.submit_turn.assert_not_called()
    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "*`User approved tool call`*"
    assert outbound.reply_to_id == "msg-1"


@pytest.mark.asyncio
async def test_channel_inbound_formats_matrix_system_message_as_preformatted_text() -> None:
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
        return_value=CommandResult(type="system_message", text="Mode: build\nExecutor: maitrea")
    )

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        content="/build",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.submit_turn.assert_not_called()
    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "```text\nMode: build\nExecutor: maitrea\n```"
    assert outbound.reply_to_id == "msg-1"


@pytest.mark.asyncio
async def test_channel_inbound_formats_signal_system_message_as_italic_monospace() -> None:
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
        return_value=CommandResult(type="system_message", text="Mode: default\nExecutor: maitrea")
    )

    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="/default",
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
    assert outbound.content == "*`Mode: default`\n`Executor: maitrea`*"
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
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
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
        default_agent_profile_id="chat",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.submit_turn.assert_awaited_once()
    turn_observers = turn_scheduler.submit_turn.await_args.kwargs["turn_observers"]
    assert len(turn_observers) == 1
    assert isinstance(turn_observers[0], ChannelTurnObserver)
    assert turn_scheduler.submit_turn.await_args.kwargs["client_message_id"] == "msg-1"
    assert (
        turn_scheduler.submit_turn.await_args.kwargs["channel_default_agent_profile_id"] == "chat"
    )
    assert (
        pipeline._try_command_dispatch.await_args.kwargs["channel_default_agent_profile_id"]
        == "chat"
    )
    assert turn_scheduler.submit_turn.await_args.kwargs["channel_account_id"] == "acct-1"
    adapter.mark_read.assert_awaited_once_with("chat-1", "msg-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_account_id", "message_channel_type"),
    [("other-account", "signal"), ("acct-1", "matrix")],
)
async def test_channel_inbound_rejects_message_config_binding_mismatch(
    message_account_id: str,
    message_channel_type: str,
) -> None:
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type=message_channel_type,
        account_id=message_account_id,
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
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    pipeline._resolve_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_inbound_threads_mode_uses_message_as_thread_root() -> None:
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
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$root",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="hello",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"group_conversation_mode": "threads"},
    )

    await pipeline.process(message, config)

    resolved_message = pipeline._resolve_conversation.await_args.args[0]
    assert resolved_message.thread_id == "$root"
    observer = turn_scheduler.submit_turn.await_args.kwargs["turn_observers"][0]
    assert observer._thread_id == "$root"


def test_channel_inbound_allows_unmentioned_matrix_thread_candidate_for_mention_policy() -> None:
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        group_policy="mention",
    )
    candidate = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="follow up",
        thread_id="$root",
        was_mentioned=False,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"unmentioned_thread_followup_candidate": True},
    )
    unrelated = candidate.model_copy(update={"thread_id": None, "platform_data": {}})

    assert _is_unmentioned_matrix_thread_followup(candidate) is True
    assert pipeline._check_access(candidate, config) is True
    assert pipeline._check_access(unrelated, config) is False


@pytest.mark.asyncio
async def test_channel_inbound_pairing_policy_does_not_challenge_unmentioned_thread_candidate() -> (
    None
):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session_factory = MagicMock(return_value=session)
    pairing_service = MagicMock()
    pairing_service.ensure_verified_sender = AsyncMock(return_value="user@example.com")
    pipeline = InboundPipeline(
        session_factory=session_factory,
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=pairing_service,
        channel_manager_ref=MagicMock(),
    )
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="follow up",
        thread_id="$root",
        was_mentioned=False,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"unmentioned_thread_followup_candidate": True},
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        group_policy="pairing",
    )

    result = await pipeline._resolve_user(message, config)

    assert result is None
    pairing_service.ensure_verified_sender.assert_not_called()


@pytest.mark.asyncio
async def test_channel_inbound_resolves_unmentioned_matrix_thread_candidate_only_if_existing() -> (
    None
):
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._latest_conversation_for_context = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(conversation_id="conv-thread")
    )
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="follow up",
        thread_id="$root",
        was_mentioned=False,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"unmentioned_thread_followup_candidate": True},
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"group_conversation_mode": "threads"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result == "conv-thread"
    pipeline._latest_conversation_for_context.assert_awaited_once_with(
        user_email="user@example.com",
        agent_id="agent-1",
        context_ref="matrix:acct-1:!room:fpy.cz:$root",
    )
    pipeline._session_manager.create_conversation_with_root_session.assert_not_called()


@pytest.mark.asyncio
async def test_channel_inbound_drops_unknown_unmentioned_matrix_thread_candidate() -> None:
    session_manager = MagicMock()
    session_manager.create_conversation_with_root_session = AsyncMock()
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._latest_conversation_for_context = AsyncMock(return_value=None)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="follow up",
        thread_id="$unknown",
        was_mentioned=False,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={"unmentioned_thread_followup_candidate": True},
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        default_conversation_id="conv-default",
        settings={"group_conversation_mode": "threads"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result is None
    session_manager.create_conversation_with_root_session.assert_not_called()


@pytest.mark.asyncio
async def test_channel_inbound_creates_conversation_for_mentioned_matrix_thread() -> None:
    session_manager = MagicMock()
    session_manager.create_conversation_with_root_session = AsyncMock(
        return_value=(SimpleNamespace(conversation_id="conv-new"), SimpleNamespace())
    )
    pipeline = InboundPipeline(
        session_factory=MagicMock(),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._latest_conversation_for_context = AsyncMock(return_value=None)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="hello @bot:fpy.cz",
        thread_id="$user-thread",
        was_mentioned=True,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"group_conversation_mode": "threads"},
    )

    result = await pipeline._resolve_conversation(message, config, "user@example.com")

    assert result == "conv-new"
    session_manager.create_conversation_with_root_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_inbound_matrix_thread_source_ref_forks_original_backing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_session = SimpleNamespace(session_id="sess-old")
    active_session = SimpleNamespace(session_id="sess-active")
    monkeypatch.setattr(
        "cognis.store.queries.get_root_session_chain",
        AsyncMock(return_value=([old_session, active_session], False)),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_manager = MagicMock()
    session_manager._read_history_events = AsyncMock(
        side_effect=[
            [
                _event(
                    3,
                    {
                        "client_message_id": "$root",
                        "source_session_id": "sess-old",
                        "source_seq": 10,
                        "turn_id": "turn-root",
                    },
                )
            ],
            [
                _event(10, {"client_message_id": "$root", "turn_id": "turn-root"}),
                _event(11, {"message_id": "assistant-msg", "turn_id": "turn-root"}),
            ],
        ]
    )
    pipeline = InboundPipeline(
        session_factory=MagicMock(return_value=session),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._session_row_to_model = MagicMock(side_effect=lambda row: row)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@user:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="direct",
        content="start from this thread",
        thread_id="$root",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"dm_conversation_mode": "default", "thread_start_mode": "fork"},
    )
    room_conversation = SimpleNamespace(
        conversation_id="conv-room",
        active_session_id="sess-active",
        agent_id="agent-1",
    )

    result = await pipeline._find_thread_fork_source(
        room_conversation=room_conversation,
        message=message,
        config=config,
    )

    assert result == (old_session, 11, "$root")
    assert message.platform_data["thread_fork_anchor_lookup"] == "source_ref"
    assert session_manager._read_history_events.await_args_list[0].kwargs == {
        "last_n": 5000,
        "allow_missing_stream": True,
    }
    assert session_manager._read_history_events.await_args_list[1].args == (old_session,)
    assert session_manager._read_history_events.await_args_list[1].kwargs == {
        "after_seq": 9,
        "limit": 1000,
        "allow_missing_stream": True,
    }


@pytest.mark.asyncio
async def test_channel_inbound_matrix_thread_unverified_source_ref_uses_tail_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_session = SimpleNamespace(session_id="sess-old")
    active_session = SimpleNamespace(session_id="sess-active")
    monkeypatch.setattr(
        "cognis.store.queries.get_root_session_chain",
        AsyncMock(return_value=([old_session, active_session], False)),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_manager = MagicMock()
    session_manager._read_history_events = AsyncMock(
        side_effect=[
            [
                _event(
                    3,
                    {
                        "client_message_id": "$root",
                        "source_session_id": "sess-old",
                        "source_seq": 10,
                        "turn_id": "turn-root",
                    },
                )
            ],
            [],
            [],
        ]
    )
    pipeline = InboundPipeline(
        session_factory=MagicMock(return_value=session),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._session_row_to_model = MagicMock(side_effect=lambda row: row)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@user:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="direct",
        content="start from this thread",
        thread_id="$root",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={
            "dm_conversation_mode": "default",
            "thread_start_mode": "fork",
            "thread_fork_lookup_max_sessions": 2,
        },
    )
    room_conversation = SimpleNamespace(
        conversation_id="conv-room",
        active_session_id="sess-active",
        agent_id="agent-1",
    )

    result = await pipeline._find_thread_fork_source(
        room_conversation=room_conversation,
        message=message,
        config=config,
    )

    assert result == (active_session, 3, "$root")
    assert message.platform_data["thread_fork_anchor_lookup"] == "bounded_scan"


@pytest.mark.asyncio
async def test_channel_inbound_matrix_thread_walks_back_to_older_backing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_session = SimpleNamespace(session_id="sess-old")
    active_session = SimpleNamespace(session_id="sess-active")
    monkeypatch.setattr(
        "cognis.store.queries.get_root_session_chain",
        AsyncMock(return_value=([old_session, active_session], False)),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_manager = MagicMock()
    session_manager._read_history_events = AsyncMock(
        side_effect=[
            [],
            [
                _event(10, {"client_message_id": "$root", "turn_id": "turn-root"}),
                _event(12, {"message_id": "assistant-msg", "turn_id": "turn-root"}),
            ],
        ]
    )
    pipeline = InboundPipeline(
        session_factory=MagicMock(return_value=session),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._session_row_to_model = MagicMock(side_effect=lambda row: row)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@user:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="direct",
        content="start from this thread",
        thread_id="$root",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={
            "dm_conversation_mode": "default",
            "thread_start_mode": "fork",
            "thread_fork_lookup_max_sessions": 2,
        },
    )
    room_conversation = SimpleNamespace(
        conversation_id="conv-room",
        active_session_id="sess-active",
        agent_id="agent-1",
    )

    result = await pipeline._find_thread_fork_source(
        room_conversation=room_conversation,
        message=message,
        config=config,
    )

    assert result == (old_session, 12, "$root")
    assert message.platform_data["thread_fork_anchor_lookup"] == "bounded_scan"
    assert [call.args[0] for call in session_manager._read_history_events.await_args_list] == [
        active_session,
        old_session,
    ]


@pytest.mark.asyncio
async def test_channel_inbound_matrix_thread_lookup_budget_exhaustion_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_session = SimpleNamespace(session_id="sess-old")
    active_session = SimpleNamespace(session_id="sess-active")
    monkeypatch.setattr(
        "cognis.store.queries.get_root_session_chain",
        AsyncMock(return_value=([old_session, active_session], False)),
    )

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_manager = MagicMock()
    session_manager._read_history_events = AsyncMock(return_value=[])
    pipeline = InboundPipeline(
        session_factory=MagicMock(return_value=session),
        turn_scheduler=MagicMock(),
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    pipeline._session_row_to_model = MagicMock(side_effect=lambda row: row)  # type: ignore[method-assign]
    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@user:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="direct",
        content="start from this thread",
        thread_id="$root",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={
            "dm_conversation_mode": "default",
            "thread_start_mode": "fork",
            "thread_fork_lookup_max_sessions": 1,
        },
    )
    room_conversation = SimpleNamespace(
        conversation_id="conv-room",
        active_session_id="sess-active",
        agent_id="agent-1",
    )

    result = await pipeline._find_thread_fork_source(
        room_conversation=room_conversation,
        message=message,
        config=config,
    )

    assert result is None
    assert message.platform_data["thread_fork_anchor_lookup"] == "exhausted"
    session_manager._read_history_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_inbound_matrix_thread_missing_source_stream_submits_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock(return_value=None)
    turn_scheduler.has_active_turn = MagicMock(return_value=False)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=session)

    source_session = SimpleNamespace(session_id="sess-room")
    monkeypatch.setattr(
        "cognis.store.queries.get_root_session_chain",
        AsyncMock(return_value=([source_session], False)),
    )

    room_conversation = SimpleNamespace(
        conversation_id="conv-room",
        active_session_id="sess-room",
        agent_id="agent-1",
    )
    session_manager = MagicMock()
    session_manager._read_history_events = AsyncMock(return_value=[])
    session_manager.create_conversation_with_root_session = AsyncMock(
        return_value=(SimpleNamespace(conversation_id="conv-thread"), SimpleNamespace())
    )
    pipeline = InboundPipeline(
        session_factory=session_factory,
        turn_scheduler=turn_scheduler,
        session_manager=session_manager,
        pairing_service=MagicMock(),
        channel_manager_ref=lambda: manager,
        command_dispatcher=MagicMock(),
    )
    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._latest_conversation_for_context = AsyncMock(  # type: ignore[method-assign]
        side_effect=[None, room_conversation]
    )
    pipeline._session_row_to_model = MagicMock(side_effect=lambda row: row)  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@user:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="direct",
        content="start from this thread",
        thread_id="$root",
        was_mentioned=False,
        timestamp=datetime.now(UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        agent_id="agent-1",
        user_email="user@example.com",
        settings={"dm_conversation_mode": "default", "thread_start_mode": "fork"},
    )

    await pipeline.process(message, config)

    session_manager._read_history_events.assert_awaited_once_with(
        source_session,
        last_n=5000,
        allow_missing_stream=True,
    )
    session_manager.create_conversation_with_root_session.assert_awaited_once()
    assert message.platform_data["fresh_thread_context"] is True
    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.args[0] == "conv-thread"


@pytest.mark.asyncio
async def test_channel_inbound_fresh_thread_context_includes_root_excerpt() -> None:
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
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="$reply",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        chat_type="group",
        content="continuation",
        thread_id="$root",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
        platform_data={
            "_sender_verified_owner": True,
            "fresh_thread_context": True,
            "thread_root": {
                "sender": "@filip:fpy.cz",
                "body": "original message",
                "timestamp": "2026-08-01T10:00:00Z",
            },
        },
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    submitted_content = turn_scheduler.submit_turn.await_args.args[1]
    assert submitted_content == "continuation"
    assert turn_scheduler.submit_turn.await_args.kwargs["intention_eligible"] is True
    contextual_messages = turn_scheduler.submit_turn.await_args.kwargs["contextual_messages"]
    assert contextual_messages[0]["content"] == "original message"
    assert contextual_messages[0]["intention_eligible"] is False
    assert contextual_messages[0]["message_metadata"]["sender"] == "@filip:fpy.cz"


@pytest.mark.asyncio
async def test_channel_inbound_mark_read_failure_is_non_fatal() -> None:
    adapter = _FakeAdapter()
    adapter.mark_read.side_effect = RuntimeError("read receipt failed")
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
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
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

    adapter.mark_read.assert_awaited_once_with("chat-1", "msg-1")
    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.kwargs["intention_eligible"] is False


@pytest.mark.asyncio
async def test_channel_inbound_submits_matrix_image_as_attachment() -> None:
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

    image_ref = AttachmentRef(
        artifact_id="att-1",
        kind=ArtifactKind.IMAGE,
        mime_type="image/png",
        filename="obrazek.png",
        size_bytes=128,
    )
    pipeline._resolve_user = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]
    pipeline._resolve_conversation = AsyncMock(return_value="conv-1")  # type: ignore[method-assign]
    pipeline._normalize_media_attachments = AsyncMock(return_value=([image_ref], 0))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        content="",
        media=[
            MediaAttachment(url="mxc://fpy.cz/media", mime_type="image/png", filename="obrazek.png")
        ],
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.args[1] == ""
    assert turn_scheduler.submit_turn.await_args.kwargs["attachments"] == [
        image_ref.model_dump(mode="json")
    ]


@pytest.mark.asyncio
async def test_channel_inbound_uses_placeholder_when_matrix_image_normalization_fails() -> None:
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
    # Simulate: normalization returned no refs but 1 failure
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 1))  # type: ignore[method-assign]
    pipeline._try_command_dispatch = AsyncMock(return_value=None)  # type: ignore[method-assign]

    message = InboundMessage(
        channel_type="matrix",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="@filip:fpy.cz",
        chat_id="!room:fpy.cz",
        content="",
        media=[
            MediaAttachment(url="mxc://fpy.cz/media", mime_type="image/png", filename="obrazek.png")
        ],
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    config = ChannelAccountConfig(
        account_id="acct-1",
        channel_type="matrix",
        display_name="Matrix",
        credential_refs={},
        agent_id="agent-1",
        user_email="user@example.com",
    )

    await pipeline.process(message, config)

    # A system notice is sent to the channel about the failed download
    adapter.send_message.assert_awaited_once()
    notice_outbound = adapter.send_message.await_args.args[0]
    assert "couldn't download" in notice_outbound.content
    assert "1 attachment" in notice_outbound.content
    # The turn still proceeds with the placeholder text
    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.args[1] == "User attached an image file."
    assert turn_scheduler.submit_turn.await_args.kwargs["attachments"] == []


@pytest.mark.asyncio
async def test_channel_inbound_attachment_failure_notice_is_not_sent_for_voice_input() -> None:
    """Voice-input attachment failures raise rather than sending a notice."""
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    turn_scheduler = MagicMock()
    turn_scheduler.submit_turn = AsyncMock(return_value=None)
    llm_provider = MagicMock()
    llm_provider.transcribe = AsyncMock(return_value=SimpleNamespace(text="ok"))

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
    # Voice input: 0 refs, 0 failures (failures would raise for voice)
    pipeline._normalize_media_attachments = AsyncMock(return_value=([], 0))  # type: ignore[method-assign]
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

    # No attachment-failure notice for voice input (failures raise instead)
    for call in adapter.send_message.await_args_list:
        outbound = call.args[0]
        assert "couldn't download" not in outbound.content


@pytest.mark.asyncio
async def test_channel_inbound_routes_voice_audio_to_central_turn_transcription() -> None:
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
        return_value=(
            [
                AttachmentRef(
                    artifact_id="att-1",
                    kind=ArtifactKind.AUDIO,
                    mime_type="audio/ogg",
                    filename="voice.ogg",
                    size_bytes=128,
                )
            ],
            0,
        )
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

    turn_scheduler.submit_turn.assert_awaited_once()
    assert turn_scheduler.submit_turn.await_args.args[1] == ""
    assert turn_scheduler.submit_turn.await_args.kwargs["attachments"][0]["artifact_id"] == "att-1"
    assert "prepared_attachment_notice" not in turn_scheduler.submit_turn.await_args.kwargs
    assert "prepared_attachment_context" not in turn_scheduler.submit_turn.await_args.kwargs
    llm_provider.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_inbound_does_not_call_stt_before_turn_admission() -> None:
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
        return_value=(
            [
                AttachmentRef(
                    artifact_id="att-1",
                    kind=ArtifactKind.AUDIO,
                    mime_type="audio/ogg",
                    filename="voice.ogg",
                    size_bytes=128,
                )
            ],
            0,
        )
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

    turn_scheduler.submit_turn.assert_awaited_once()
    llm_provider.transcribe.assert_not_awaited()
    adapter.send_message.assert_not_awaited()


def test_fallback_attachment_content_uses_raw_media_when_normalization_fails() -> None:
    assert (
        _fallback_attachment_content(
            "",
            [],
            [MediaAttachment(mime_type="audio/ogg", filename="voice.ogg")],
        )
        == "User attached an audio file."
    )
    assert (
        _fallback_attachment_content(
            "",
            [],
            [MediaAttachment(mime_type="image/png", filename="photo.png")],
        )
        == "User attached an image file."
    )


def test_filter_turn_attachments_for_voice_input_preserves_audio() -> None:
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

    assert [attachment.artifact_id for attachment in filtered] == ["att-audio", "att-image"]


def test_stt_passthrough_target_normalizes_filename_extension() -> None:
    assert _stt_passthrough_target("audio/mpeg", "voice-note") == ("audio/mpeg", "voice-note.mp3")
    assert _stt_passthrough_target("audio/ogg", "voice.ogg") == ("audio/ogg", "voice.ogg")


def test_stt_passthrough_target_uses_configured_model_formats() -> None:
    assert _stt_passthrough_target(
        "audio/aac",
        "voice",
        supported_mime_types=["audio/aac"],
    ) == ("audio/aac", "voice.aac")


def test_stt_passthrough_target_accepts_alias_of_configured_model_format() -> None:
    assert _stt_passthrough_target(
        "audio/x-m4a",
        "voice.m4a",
        supported_mime_types=["audio/mp4"],
    ) == ("audio/mp4", "voice.m4a")


def test_stt_supported_audio_mime_types_uses_model_metadata() -> None:
    model_info = SimpleNamespace(supported_audio_mime_types=["audio/aac", " audio/flac "])

    assert _stt_supported_audio_mime_types(model="custom-stt", model_info=model_info) == [
        "audio/aac",
        "audio/flac",
    ]


@pytest.mark.asyncio
async def test_prepare_audio_for_stt_requires_ffmpeg_for_aac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cognis.audio.preprocessing.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        await _prepare_audio_for_stt(
            b"audio-bytes",
            mime_type="audio/aac",
            filename="voice.aac",
        )


@pytest.mark.asyncio
async def test_prepare_audio_for_stt_transcodes_when_policy_excludes_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_transcode(content: bytes, *, mime_type: str, filename: str):
        assert content == b"audio-bytes"
        assert mime_type == "audio/aac"
        assert filename == "voice.aac"
        return b"wav-bytes", "audio/wav", "voice-input.wav"

    monkeypatch.setattr("cognis.audio.preprocessing.transcode_audio_for_stt", fake_transcode)

    content, mime_type, filename = await _prepare_audio_for_stt(
        b"audio-bytes",
        mime_type="audio/aac",
        filename="voice.aac",
        supported_mime_types=["audio/wav"],
    )

    assert content == b"wav-bytes"
    assert mime_type == "audio/wav"
    assert filename == "voice-input.wav"


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
async def test_executor_channel_fetch_media_normalizes_voice_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        async def download_attachment(self, _message: object, _attachment: object):
            return b"aac-bytes", "audio/aac", "voice.aac"

    async def fake_prepare(content: bytes, **kwargs: object):
        assert content == b"aac-bytes"
        assert kwargs["supported_mime_types"] == ["audio/wav"]
        return b"wav-bytes", "audio/wav", "voice-input.wav"

    monkeypatch.setattr("cognis.channels.inbound._prepare_audio_for_stt", fake_prepare)
    handler = ChannelHandler()
    handler._adapters["acct-1"] = _Adapter()  # noqa: SLF001

    result = await handler.fetch_media(
        "acct-1",
        {
            "channel_type": "signal",
            "account_id": "acct-1",
            "message_id": "msg-1",
            "sender_id": "sender-1",
            "chat_id": "chat-1",
            "content": "",
            "timestamp": __import__("datetime").datetime.now(__import__("datetime").UTC),
        },
        {"mime_type": "audio/aac", "filename": "voice.aac"},
        stt_supported_mime_types=["audio/wav"],
    )

    assert result["content_b64"] == base64.b64encode(b"wav-bytes").decode("ascii")
    assert result["content_type"] == "audio/wav"
    assert result["filename"] == "voice-input.wav"


@pytest.mark.asyncio
async def test_remote_channel_download_attachment_for_stt_passes_policy() -> None:
    class _Connection:
        executor_id = "exec-1"

        def __init__(self) -> None:
            self.params: dict[str, object] | None = None

        async def rpc_call(self, _method: str, params: dict[str, object], timeout: float):
            self.params = params
            assert timeout == 60.0
            return {
                "content_b64": base64.b64encode(b"wav-bytes").decode("ascii"),
                "content_type": "audio/wav",
                "filename": "voice-input.wav",
            }

    conn = _Connection()
    proxy = RemoteChannelAdapterProxy(
        connection=conn,
        channel_type="signal",
        capabilities=MagicMock(),
        account_id="acct-1",
    )
    message = InboundMessage(
        channel_type="signal",
        account_id="acct-1",
        message_id="msg-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    attachment = MediaAttachment(mime_type="audio/aac", filename="voice.aac")

    fetched = await proxy.download_attachment_for_stt(
        message,
        attachment,
        supported_mime_types=["audio/wav"],
    )

    assert fetched == (b"wav-bytes", "audio/wav", "voice-input.wav")
    assert conn.params is not None
    assert conn.params["stt_supported_mime_types"] == ["audio/wav"]


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
async def test_channel_turn_observer_concatenated_mode_does_not_flush_on_tool_call() -> None:
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
        assistant_delivery_mode="concatenated",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Let me check that.")
    await observer.on_tool_call("conv-1", "sess-1", "call-1", "bash", {"command": "ls"})

    # In concatenated mode, text should NOT be flushed on tool_call — only typing indicator
    adapter.send_message.assert_not_awaited()
    adapter.send_typing.assert_awaited()
    assert observer._accumulated_text == "Let me check that."


@pytest.mark.asyncio
async def test_channel_turn_observer_concatenated_mode_never_flushes_mid_turn() -> None:
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
        assistant_delivery_mode="concatenated",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Some buffered content")
    await observer.flush_buffered_text()

    adapter.send_message.assert_not_awaited()

    await observer.on_turn_complete(None)
    assert turn_scheduler.remove_observer.called


@pytest.mark.asyncio
async def test_channel_turn_observer_legacy_final_mode_is_concatenated() -> None:
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

    await observer.on_token("conv-1", "sess-1", "msg-2", "Progress update.")
    await observer.on_tool_call("conv-1", "sess-1", "call-1", "bash", {"command": "ls"})
    await observer.on_token("conv-1", "sess-1", "msg-2", "Final answer.")
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            final_content="Final answer.",
        )
    )

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "Progress update.Final answer."


@pytest.mark.asyncio
async def test_channel_turn_observer_records_outbound_delivery_mapping() -> None:
    adapter = _FakeAdapter()
    adapter.send_message.return_value = "$assistant"
    manager = _FakeManager(adapter)
    guardrails = SimpleNamespace(
        record_events=AsyncMock(return_value=SimpleNamespace(first_seq=10, last_seq=10))
    )
    turn_scheduler = MagicMock()
    turn_scheduler._providers = SimpleNamespace(guardrails=guardrails)

    observer = ChannelTurnObserver(
        channel_type="matrix",
        account_id="acct-1",
        chat_id="!room:fpy.cz",
        thread_id="$root",
        conversation_id="conv-1",
        turn_scheduler=turn_scheduler,
        reply_to_id="$user",
        channel_manager_ref=lambda: manager,
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", delta="reply")
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="turn-1",
            turn_id="turn-1",
            final_content="reply",
        )
    )

    guardrails.record_events.assert_awaited_once()
    event = guardrails.record_events.await_args.kwargs["events"][0]
    # Recorded as a valid Intaris event type — "channel_delivery" is not in
    # the Intaris VALID_EVENT_TYPES set and was rejected with a 400.
    assert event.type == "lifecycle"
    assert event.data["event"] == "channel_delivery"
    assert event.data["platform_message_id"] == "$assistant"
    assert event.data["turn_id"] == "turn-1"
    assert event.data["thread_id"] == "$root"


@pytest.mark.asyncio
async def test_channel_turn_observer_final_only_mode_sends_final_content() -> None:
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
        assistant_delivery_mode="final_only",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Let me check that.")
    await observer.on_tool_call("conv-1", "sess-1", "call-1", "bash", {"command": "ls"})
    await observer.on_token("conv-1", "sess-1", "msg-2", "Done.")
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-2",
            final_content="Done.",
        )
    )

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "Done."


@pytest.mark.asyncio
async def test_channel_turn_observer_final_only_mode_falls_back_to_buffer_without_final_content() -> (
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
        assistant_delivery_mode="final_only",
    )

    await observer.on_token("conv-1", "sess-1", "msg-2", "Only buffered text.")
    await observer.on_turn_complete(None)

    adapter.send_message.assert_awaited_once()
    outbound = adapter.send_message.await_args.args[0]
    assert outbound.content == "Only buffered text."


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
async def test_durable_channel_observer_never_sends_terminal_error_directly() -> None:
    adapter = _FakeAdapter()
    manager = _FakeManager(adapter)
    observer = ChannelTurnObserver(
        channel_type="matrix",
        account_id="acct-1",
        chat_id="!room:example.com",
        thread_id="$thread",
        conversation_id="conv-1",
        turn_scheduler=MagicMock(),
        reply_to_id="$inbound",
        channel_manager_ref=lambda: manager,
        assistant_delivery_mode="final",
        channel_delivery=ChannelDeliveryDescriptor(
            channel_type="matrix",
            account_id="acct-1",
            chat_id="!room:example.com",
            thread_id="$thread",
            reply_to_id="$inbound",
        ),
    )
    observer._turn_active = True

    await observer.on_turn_error(
        "conv-1",
        SimpleNamespace(message="The turn could not be completed."),
    )

    adapter.send_message.assert_not_awaited()


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
