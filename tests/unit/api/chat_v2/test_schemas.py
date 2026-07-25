from __future__ import annotations

import pytest
from pydantic import ValidationError

from cognis.api.chat_v2.schemas import (
    ChatSyncResponse,
    CommandV2Request,
    MessageTimelineItem,
    QueueState,
    RuntimeOverlaySnapshot,
    SendMessageV2Request,
    TimelineBackfillResponse,
    TimelineScope,
    TimelineWindow,
    UpsertTimelineItemOp,
)


def test_command_request_is_strict_and_non_empty() -> None:
    assert CommandV2Request(content="/fork new topic").content == "/fork new topic"
    with pytest.raises(ValidationError):
        CommandV2Request(content="")
    with pytest.raises(ValidationError):
        CommandV2Request(content="/fork", unsupported=True)


def test_runtime_overlay_rejects_stable_volatile_items() -> None:
    item = MessageTimelineItem(
        id="runtime-message",
        sort_key="runtime:1",
        stable=True,
        role="assistant",
        content="partial",
        message_id="msg-1",
        partial=True,
    )

    with pytest.raises(ValidationError, match="volatile_items must have stable=false"):
        RuntimeOverlaySnapshot(
            runtime_epoch="epoch-1",
            runtime_revision=1,
            generated_at="2026-06-29T10:00:00Z",
            has_active_turn=True,
            volatile_items=[item],
        )


def test_timeline_scope_uses_canonical_identity_key() -> None:
    scope = TimelineScope(
        key="session:sess-1",
        kind="session",
        conversation_id="conv-1",
        session_id="sess-1",
    )
    assert scope.key == "session:sess-1"

    with pytest.raises(ValidationError, match="does not match"):
        TimelineScope(key="session:other", kind="session", session_id="sess-1")


def test_runtime_overlay_rejects_active_turn_when_inactive() -> None:
    with pytest.raises(ValidationError, match="active_turn must be null"):
        RuntimeOverlaySnapshot.model_validate(
            {
                "runtime_epoch": "epoch-1",
                "runtime_revision": 1,
                "generated_at": "2026-06-29T10:00:00Z",
                "has_active_turn": False,
                "active_turn": {
                    "turn_id": "turn-1",
                    "session_id": "sess-1",
                    "status": "running",
                },
            },
        )


def test_queue_state_requires_count_to_match_messages() -> None:
    with pytest.raises(ValidationError, match="queued_count must match"):
        QueueState(
            queued_count=1,
            messages=[],
        )


def test_send_request_requires_content_or_attachment() -> None:
    with pytest.raises(ValidationError, match="content or attachments are required"):
        SendMessageV2Request(
            client_message_id="client-1",
            content="   ",
        )


def test_sync_response_requires_reset_reason_when_reset_required() -> None:
    with pytest.raises(ValidationError, match="reset_reason is required"):
        ChatSyncResponse(
            projection_version="chat-v2.1",
            scope=TimelineScope(
                key="conversation:conv-1", kind="conversation", conversation_id="conv-1"
            ),
            conversation_id="conv-1",
            cursor_before="a",
            cursor_after="b",
            reset_required=True,
            server_time="2026-06-29T10:00:00Z",
        )


def test_timeline_item_union_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        UpsertTimelineItemOp.model_validate(
            {
                "op": "upsert_item",
                "item": {
                    "id": "x",
                    "kind": "unknown",
                    "sort_key": "1",
                    "stable": True,
                },
            }
        )


def test_timeline_window_rejects_volatile_items() -> None:
    item = MessageTimelineItem(
        id="runtime-message",
        sort_key="runtime:1",
        stable=False,
        role="assistant",
        content="partial",
        message_id="msg-1",
        partial=True,
    )

    with pytest.raises(ValidationError, match="canonical timeline items must have stable=true"):
        TimelineWindow(items=[item])


def test_upsert_op_rejects_volatile_items() -> None:
    item = MessageTimelineItem(
        id="runtime-message",
        sort_key="runtime:1",
        stable=False,
        role="assistant",
        content="partial",
        message_id="msg-1",
        partial=True,
    )

    with pytest.raises(ValidationError, match="canonical timeline items must have stable=true"):
        UpsertTimelineItemOp(item=item)


def test_backfill_response_rejects_volatile_items() -> None:
    item = MessageTimelineItem(
        id="runtime-message",
        sort_key="runtime:1",
        stable=False,
        role="assistant",
        content="partial",
        message_id="msg-1",
        partial=True,
    )

    with pytest.raises(ValidationError, match="canonical timeline items must have stable=true"):
        TimelineBackfillResponse(
            projection_version="chat-v2.1",
            scope=TimelineScope(
                key="conversation:conv-1", kind="conversation", conversation_id="conv-1"
            ),
            conversation_id="conv-1",
            items=[item],
            server_time="2026-06-29T10:00:00Z",
        )


def test_strict_models_reject_type_coercion() -> None:
    with pytest.raises(ValidationError):
        QueueState.model_validate({"queued_count": "0", "messages": []})


def test_message_timeline_item_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MessageTimelineItem(
            id="message:user:1",
            sort_key="0001",
            role="user",
            content="hello",
            message_id="message-1",
            unexpected=True,  # type: ignore[call-arg]
        )
