from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.channels.delivery import ChannelDeliveryService
from cognis.core.events import Event, EventBus, EventType


def _make_service() -> ChannelDeliveryService:
    return ChannelDeliveryService(
        session_factory=AsyncMock(),
        event_bus=EventBus(),
        channel_manager_ref=lambda: None,
        public_base_url="https://cognis.example.com",
    )


class _ArtifactStore:
    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        return b"pdf-bytes", "application/pdf"


class _Manager:
    def __init__(self) -> None:
        self._artifact_store = _ArtifactStore()

    def find_adapter_for_channel(self, channel_type: str, account_id: str) -> tuple[object, object]:
        return MagicMock(), object()


class _Session:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


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


def test_render_gate_notification_lists_real_options_and_task_board_instruction() -> None:
    service = _make_service()

    content = service._render_gate_notification(
        {
            "message": "Step 'plan' has exhausted its retry limit.",
            "options": [
                {"label": "Retry step", "action": "revise(plan)"},
                {"label": "Continue", "action": "continue"},
            ],
        }
    )

    assert "*[gate]* Step 'plan' has exhausted its retry limit." in content
    assert "1. Retry step" in content
    assert "2. Continue" in content
    assert "task board" in content
    assert "/approve" in content
    assert "/deny" in content
    assert "tool escalations" in content


def test_render_credential_request_notification_includes_form_link() -> None:
    service = _make_service()

    content = service._render_credential_request_notification(
        {
            "label": "Cocky Kontaktni Google login",
            "message": "Need password for Google login.",
            "required_fields": ["username", "password"],
        },
        notification_id="auth_123",
    )

    assert "*[credential]* Cocky Kontaktni Google login" in content
    assert "Need password for Google login." in content
    assert "Required fields: username, password" in content
    assert "https://cognis.example.com/notifications/auth_123" in content
    assert "Do not send credential values in this chat." in content


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
        deliverable_id=None,
        ignore_next_attempt=True,
    )


@pytest.mark.asyncio
async def test_materialize_media_attachment_loads_artifact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    manager = _Manager()
    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: manager,
    )
    monkeypatch.setattr(
        "cognis.store.queries.get_artifact_record",
        AsyncMock(
            return_value=type(
                "ArtifactRow",
                (),
                {
                    "status": "attached",
                    "namespace": "documents",
                    "object_id": "doc_1",
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                },
            )()
        ),
    )

    media, fallback_text, materialized = await service._materialize_media_attachment(  # noqa: SLF001
        {
            "artifact_id": "doc_1",
            "url": "https://cognis.example.com/report.pdf",
            "mime_type": "application/pdf",
            "filename": "report.pdf",
            "size_bytes": 9,
        }
    )

    assert materialized is True
    assert fallback_text is None
    assert media.content_b64 == base64.b64encode(b"pdf-bytes").decode("ascii")


@pytest.mark.asyncio
async def test_materialize_media_attachment_falls_back_to_artifact_link_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    manager = _Manager()
    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: manager,
    )
    monkeypatch.setattr(
        "cognis.store.queries.get_artifact_record",
        AsyncMock(
            return_value=type(
                "ArtifactRow",
                (),
                {
                    "status": "attached",
                    "namespace": "attachments",
                    "object_id": "img_1",
                    "filename": "diagram.png",
                    "mime_type": "image/png",
                },
            )()
        ),
    )

    async def boom(namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        raise RuntimeError("artifact unavailable")

    manager._artifact_store.async_load = boom  # type: ignore[method-assign]

    media, fallback_text, materialized = await service._materialize_media_attachment(  # noqa: SLF001
        {
            "artifact_id": "img_1",
            "url": "https://cognis.example.com/diagram.png",
            "mime_type": "image/png",
            "filename": "diagram.png",
        }
    )

    assert media is not None
    assert materialized is True
    assert fallback_text is None
    assert media.url == "https://cognis.example.com/diagram.png"


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
async def test_deliver_outbox_sends_attachment_only_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _Manager(),
    )
    sent: dict[str, object] = {}

    async def fake_send_to_route(**kwargs: object) -> str:
        sent.update(kwargs)
        return "sent"

    monkeypatch.setattr(service, "_send_to_route", fake_send_to_route)
    monkeypatch.setattr(
        "cognis.store.queries.claim_channel_delivery_outbox",
        AsyncMock(
            return_value=type(
                "OutboxRow",
                (),
                {
                    "channel_type": "signal",
                    "account_id": "acct-1",
                    "chat_id": "chat-1",
                    "thread_id": None,
                    "conversation_id": "conv-1",
                },
            )()
        ),
    )
    monkeypatch.setattr(
        "cognis.store.queries.mark_channel_delivery_sent", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("cognis.store.queries.mark_channel_delivery_failed", AsyncMock())
    monkeypatch.setattr("cognis.store.queries.mark_channel_delivery_uncertain", AsyncMock())

    await service._deliver_outbox(  # noqa: SLF001
        delivery_id="cdel_3",
        final_content=None,
        fallback_text=None,
        attachments=[
            {
                "artifact_id": "img_1",
                "url": "https://cognis.example.com/image.jpg",
                "mime_type": "image/jpeg",
                "filename": "image.jpg",
                "size_bytes": 12,
            }
        ],
        ignore_next_attempt=True,
    )

    assert sent["content"] == ""
    assert sent["media"] is not None


@pytest.mark.asyncio
async def test_send_to_route_marks_signal_attachment_fallback_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        capabilities = type("Caps", (), {"max_message_length": 4000})()

        def __init__(self) -> None:
            self.sent_message: object | None = None

        async def send_message(self, message: object) -> str | None:
            self.sent_message = message
            return "ts-1"

    adapter = _Adapter()

    class _SignalManager(_Manager):
        def find_adapter_for_channel(
            self, channel_type: str, account_id: str
        ) -> tuple[object, object]:
            return adapter, object()

    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _SignalManager(),
    )
    monkeypatch.setattr(
        service,
        "_prepare_media_attachments",
        AsyncMock(return_value=([], ["https://cognis.example.com/image.png"], True)),
    )

    status = await service._send_to_route(  # noqa: SLF001
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        thread_id=None,
        content="Artifact ready.",
        media=[{"artifact_id": "img_1"}],
    )

    assert status == "partial"
    assert adapter.sent_message is not None
    assert adapter.sent_message.content == "Artifact ready.\n\nhttps://cognis.example.com/image.png"


@pytest.mark.asyncio
async def test_send_to_route_treats_missing_signal_message_id_as_failure() -> None:
    class _Adapter:
        capabilities = type("Caps", (), {"max_message_length": 4000})()

        async def send_message(self, message: object) -> str | None:
            return None

    class _SignalManager(_Manager):
        def find_adapter_for_channel(
            self, channel_type: str, account_id: str
        ) -> tuple[object, object]:
            return _Adapter(), object()

    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _SignalManager(),
    )

    status = await service._send_to_route(  # noqa: SLF001
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        thread_id=None,
        content="hello",
        media=None,
    )

    assert status == "failed"


@pytest.mark.asyncio
async def test_send_to_route_treats_blank_signal_message_id_as_failure() -> None:
    class _Adapter:
        capabilities = type("Caps", (), {"max_message_length": 4000})()

        async def send_message(self, message: object) -> str | None:
            return ""

    class _SignalManager(_Manager):
        def find_adapter_for_channel(
            self, channel_type: str, account_id: str
        ) -> tuple[object, object]:
            return _Adapter(), object()

    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _SignalManager(),
    )

    status = await service._send_to_route(  # noqa: SLF001
        channel_type="signal",
        account_id="acct-1",
        chat_id="chat-1",
        thread_id=None,
        content="hello",
        media=None,
    )

    assert status == "failed"


@pytest.mark.asyncio
async def test_send_to_route_allows_non_signal_media_send_without_message_id() -> None:
    class _Adapter:
        capabilities = type("Caps", (), {"max_message_length": 4000})()

        async def send_message(self, message: object) -> str | None:
            return None

    class _MediaManager(_Manager):
        def find_adapter_for_channel(
            self, channel_type: str, account_id: str
        ) -> tuple[object, object]:
            return _Adapter(), object()

    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _MediaManager(),
    )

    status = await service._send_to_route(  # noqa: SLF001
        channel_type="telegram",
        account_id="acct-1",
        chat_id="chat-1",
        thread_id=None,
        content="",
        media=[{"url": "https://cognis.example.com/file.pdf", "filename": "file.pdf"}],
    )

    assert status == "sent"


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


@pytest.mark.asyncio
async def test_task_event_passes_attachments_to_fallback_notification() -> None:
    service = _make_service()
    service._resolve_channel = AsyncMock(return_value=("signal", "acct-1", "chat-1", None))  # type: ignore[method-assign]
    service.send_to_conversation = AsyncMock(return_value=True)  # type: ignore[method-assign]

    event = Event(
        type=EventType.TASK_COMPLETED,
        data={
            "conversation_id": "conv-1",
            "task_title": "Background task",
            "result_summary": "Done",
            "attachments": [
                {
                    "artifact_id": "doc_1",
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                }
            ],
        },
    )

    await service._handle_task_event(event)

    service.send_to_conversation.assert_awaited_once()
    assert (
        service.send_to_conversation.await_args.kwargs["attachments"][0]["artifact_id"] == "doc_1"
    )
