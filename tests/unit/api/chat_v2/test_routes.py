"""Route contract tests for Chat v2."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from cognis.api.chat_v2 import routes as chat_v2_routes
from cognis.api.chat_v2.cursors import (
    CursorLineageEntry,
    CursorSessionWatermark,
    InternalChatCursorPayload,
    encode_cursor,
)
from cognis.api.chat_v2.routes import (
    _cursor_secret,
    _load_session_context,
    _load_task_step_context,
    _scoped_tool_output_page,
    chat_v2_cancel_turn,
    chat_v2_delete_queued_message,
    chat_v2_send_message,
    chat_v2_session_snapshot,
    chat_v2_session_sync,
    chat_v2_session_timeline,
    chat_v2_task_step_snapshot,
    chat_v2_task_step_sync,
    chat_v2_task_step_timeline,
    chat_v2_update_queued_message,
    router,
)
from cognis.api.chat_v2.schemas import (
    ControlMutationV2Request,
    QueueUpdateV2Request,
    SendMessageV2Request,
)
from cognis.api.chat_v2.sync import PROJECTION_VERSION
from cognis.api.common import AuthenticatedUser
from cognis.core.turn_scheduler import TurnError


def test_chat_v2_read_routes_are_registered() -> None:
    routes = {
        (next(iter(route.methods)), route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
    }

    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/snapshot") in routes
    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/sync") in routes
    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/timeline") in routes
    assert (
        "GET",
        "/api/v1/chat/v2/conversations/{conversation_id}/tool-outputs/{call_id}",
    ) in routes
    assert ("GET", "/api/v1/chat/v2/sessions/{session_id}/tool-outputs/{call_id}") in routes
    assert ("GET", "/api/v1/chat/v2/task-steps/{step_run_id}/tool-outputs/{call_id}") in routes
    assert (
        "PUT",
        "/api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}",
    ) in routes
    assert (
        "PUT",
        "/api/v1/chat/v2/conversations/{conversation_id}/commands/{client_txn_id}",
    ) in routes
    assert ("POST", "/api/v1/chat/v2/conversations/{conversation_id}/cancel") in routes
    assert (
        "DELETE",
        "/api/v1/chat/v2/conversations/{conversation_id}/queue/{queue_id}",
    ) in routes
    assert (
        "PATCH",
        "/api/v1/chat/v2/conversations/{conversation_id}/queue/{queue_id}",
    ) in routes
    assert not any("/e2e/" in route.path for route in router.routes)
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path.endswith("/sync"):
            assert {parameter.name for parameter in route.dependant.query_params} == {
                "cursor",
                "limit",
            }


def test_cursor_secret_uses_app_state_secret() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(chat_v2_cursor_secret="s")))

    assert _cursor_secret(cast(Any, request)) == "s"


def test_cursor_secret_fails_closed_when_missing() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc_info:
        _cursor_secret(cast(Any, request))

    assert exc_info.value.status_code == 500
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "cursor_secret_unavailable"


def test_retry_completion_marker_ignores_unrelated_completed_events() -> None:
    tool_event = SimpleNamespace(
        type="tool_result",
        data={"turn_id": "turn-1", "status": "completed"},
    )
    turn_event = SimpleNamespace(
        type="lifecycle",
        data={"turn_id": "turn-1", "event": "turn_completed"},
    )

    assert chat_v2_routes._is_completed_turn_marker(cast(Any, tool_event)) is False
    assert chat_v2_routes._is_completed_turn_marker(cast(Any, turn_event)) is True


@pytest.mark.asyncio
async def test_send_message_claims_transaction_and_submits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        cast(Any, request),
        "conv-1",
        "txn-1",
        SendMessageV2Request(client_message_id="client-1", content="hello"),
    )

    assert response.status == "accepted"
    assert scheduler.submitted == [
        {
            "conversation_id": "conv-1",
            "content": "hello",
            "client_message_id": "client-1",
            "chat_mode": None,
        }
    ]


@pytest.mark.asyncio
async def test_send_message_rejects_known_system_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(
            request,
            "conv-1",
            "txn-1",
            SendMessageV2Request(
                client_message_id="msg-1",
                content="/plan investigate this",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "slash_command_not_supported"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_accepts_unknown_slash_prefixed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        request,
        "conv-1",
        "txn-1",
        SendMessageV2Request(
            client_message_id="msg-1",
            content="/not-a-command should be plain text",
        ),
    )

    assert response.status == "accepted"
    assert scheduler.submitted == [
        {
            "conversation_id": "conv-1",
            "content": "/not-a-command should be plain text",
            "client_message_id": "msg-1",
            "chat_mode": None,
        }
    ]


@pytest.mark.asyncio
async def test_send_message_duplicate_replays_without_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    payload_hash = chat_v2_routes._payload_hash(
        "send_message",
        {
            "content": payload.content,
            "attachments": [],
            "client_message_id": payload.client_message_id,
            "chat_mode": payload.chat_mode,
        },
    )
    tx = _tx(
        "txn-1",
        payload_hash=payload_hash,
        result={
            "status": "accepted",
            "client_txn_id": "txn-1",
            "client_message_id": "client-1",
            "conversation_id": "conv-1",
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_send_message(cast(Any, request), "conv-1", "txn-1", payload)

    assert response.status == "duplicate"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_duplicate_pending_is_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    tx = _tx(
        "txn-1",
        payload_hash=chat_v2_routes._payload_hash(
            "send_message",
            {
                "content": payload.content,
                "attachments": [],
                "client_message_id": payload.client_message_id,
                "chat_mode": payload.chat_mode,
            },
        ),
        status="pending",
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(cast(Any, request), "conv-1", "txn-1", payload)

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "client_txn_pending"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_duplicate_failed_replays_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    tx = _tx(
        "txn-1",
        payload_hash=chat_v2_routes._payload_hash(
            "send_message",
            {
                "content": payload.content,
                "attachments": [],
                "client_message_id": payload.client_message_id,
                "chat_mode": payload.chat_mode,
            },
        ),
        status="failed",
        error={"code": "queue_full", "message": "Queue is full"},
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(cast(Any, request), "conv-1", "txn-1", payload)

    assert exc_info.value.status_code == 429
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "queue_full"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_failed_retry_preserves_unknown_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(error=TurnError("session_creation_failed", "Session failed", False))
    request = _request(scheduler)
    tx = _tx("txn-1", status="pending")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    with pytest.raises(HTTPException) as first_exc:
        await chat_v2_send_message(
            cast(Any, request),
            "conv-1",
            "txn-1",
            SendMessageV2Request(client_message_id="client-1", content="hello"),
        )

    assert first_exc.value.status_code == 500
    assert tx.status == "failed"
    assert tx.error == {
        "code": "session_creation_failed",
        "message": "Session failed",
        "http_status": 500,
    }

    with pytest.raises(HTTPException) as retry_exc:
        chat_v2_routes._ensure_replayable_transaction(tx, tx.payload_hash)

    assert retry_exc.value.status_code == first_exc.value.status_code
    detail = cast(dict[str, Any], retry_exc.value.detail)
    assert detail["code"] == "session_creation_failed"


@pytest.mark.asyncio
async def test_send_message_records_queued_status(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = _Scheduler(queued=[{"queue_id": "queue-1", "client_message_id": "client-1"}])
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        cast(Any, request),
        "conv-1",
        "txn-1",
        SendMessageV2Request(client_message_id="client-1", content="hello"),
    )

    assert response.status == "queued"
    assert response.queue_id == "queue-1"
    assert tx.status == "queued"


@pytest.mark.asyncio
async def test_send_message_conflicting_retry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1", payload_hash="different")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(
            cast(Any, request),
            "conv-1",
            "txn-1",
            SendMessageV2Request(client_message_id="client-1", content="hello"),
        )

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "client_txn_conflict"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_cancel_turn_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = _Scheduler(cancelled=True)
    request = _request(scheduler)
    tx = _tx("cancel-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    response = await chat_v2_cancel_turn(
        cast(Any, request),
        "conv-1",
        ControlMutationV2Request(client_txn_id="cancel-1"),
    )

    assert response.status == "cancelled"
    assert scheduler.cancel_calls == [("conv-1", False)]


@pytest.mark.asyncio
async def test_delete_queued_message_replays_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload_hash = chat_v2_routes._payload_hash("delete_queued_message", {"queue_id": "queue-1"})
    tx = _tx(
        "delete-1",
        operation="delete_queued_message",
        payload_hash=payload_hash,
        result={
            "conversation_id": "conv-1",
            "client_txn_id": "delete-1",
            "status": "deleted",
            "queue": {"messages": [], "queued_count": 0},
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_delete_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        client_txn_id="delete-1",
    )

    assert response.status == "duplicate"
    assert scheduler.deleted == []


@pytest.mark.asyncio
async def test_update_queued_message_updates_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(
        queued=[
            {
                "queue_id": "queue-1",
                "client_message_id": "client-1",
                "content": "old",
                "attachments": [],
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    request = _request(scheduler)
    tx = _tx("update-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    response = await chat_v2_update_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        QueueUpdateV2Request(client_txn_id="update-1", content=" updated "),
    )

    assert response.status == "updated"
    assert scheduler.updated == [("conv-1", "queue-1", "updated")]
    assert response.queue.queued_count == 1
    assert response.queue.messages[0].content == "updated"


@pytest.mark.asyncio
async def test_update_queued_message_replays_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload_hash = chat_v2_routes._payload_hash(
        "update_queued_message",
        {"queue_id": "queue-1", "content": "updated"},
    )
    tx = _tx(
        "update-1",
        operation="update_queued_message",
        payload_hash=payload_hash,
        result={
            "conversation_id": "conv-1",
            "client_txn_id": "update-1",
            "status": "updated",
            "queue": {
                "messages": [
                    {
                        "queue_id": "queue-1",
                        "content": "updated",
                        "attachments": [],
                        "position": 0,
                    }
                ],
                "queued_count": 1,
            },
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_update_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        QueueUpdateV2Request(client_txn_id="update-1", content="updated"),
    )

    assert response.status == "duplicate"
    assert scheduler.updated == []
    assert response.queue.messages[0].content == "updated"


@pytest.mark.asyncio
async def test_update_queued_message_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(update_result=None)
    request = _request(scheduler)
    tx = _tx("update-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_update_queued_message(
            cast(Any, request),
            "conv-1",
            "missing",
            QueueUpdateV2Request(client_txn_id="update-1", content="updated"),
        )

    assert exc_info.value.status_code == 404
    assert scheduler.updated == [("conv-1", "missing", "updated")]


@pytest.mark.asyncio
async def test_session_snapshot_route_denies_cross_user_and_preserves_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="bob@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="bob@example.com")
    _patch_scope_queries(monkeypatch, session_row=session_row, conversation_row=conversation_row)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_session_snapshot(request, "session-1")

    assert exc_info.value.status_code == 403

    monkeypatch.setattr(chat_v2_routes, "_build_scoped_snapshot", _return_context_scope)
    request = _scoped_request("bob@example.com")
    result = await chat_v2_session_snapshot(request, "session-1")
    assert result.key == "session:session-1"
    assert result.conversation_id == "conv-1"
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_session_context_rejects_session_conversation_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="alice@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="bob@example.com")
    _patch_scope_queries(monkeypatch, session_row=session_row, conversation_row=conversation_row)

    with pytest.raises(HTTPException) as exc_info:
        await _load_session_context(request, "session-1")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_task_step_route_denies_task_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="bob@example.com"),
        session_row=_session("session-1", owner="alice@example.com", conversation_id="conv-1"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_task_step_snapshot(request, "step-1")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_task_step_context_preserves_previous_session_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=2,
        status="completed",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        session_row=_session(
            "session-1",
            owner="alice@example.com",
            conversation_id="conv-1",
            parent_session_id="session-parent",
        ),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")
    assert context["scope"].parent_session_id == "session-parent"
    assert context["scope"].session_id == "session-1"


@pytest.mark.asyncio
async def test_task_step_context_uses_linked_session_conversation_when_step_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id=None,
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        session_row=_session("session-1", owner="alice@example.com", conversation_id="conv-1"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")

    assert context["scope"].conversation_id == "conv-1"
    assert context["scope"].session_id == "session-1"


@pytest.mark.asyncio
async def test_task_step_context_allows_missing_stream_without_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id=None,
        conversation_id=None,
        step_name="build",
        attempt_number=1,
        status="pending",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")

    assert context["scope"].conversation_id is None
    assert context["scope"].missing_stream is True
    assert context["session_refs"] == []


@pytest.mark.asyncio
async def test_task_step_snapshot_explicitly_marks_missing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id=None,
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="pending",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )
    monkeypatch.setattr(chat_v2_routes, "_build_scoped_snapshot", _return_context_scope)

    result = await chat_v2_task_step_snapshot(request, "step-1")
    assert result.missing_stream is True
    assert result.kind == "task_step"


@pytest.mark.asyncio
async def test_task_step_tool_output_prefers_final_result_in_authorized_step_session() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = None

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            assert kwargs["session_id"] == "intaris-historical-step"
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_call",
                        data={
                            "call_id": "call-historical",
                            "result": "stale preview",
                        },
                    ),
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call-historical",
                            "result": "historical full output",
                            "status": "completed",
                        },
                    ),
                ],
                has_more_before=False,
                first_seq=1,
            )

    context = {
        "scope": SimpleNamespace(
            kind="task_step",
            conversation_id="conv-1",
            session_id="session-historical-step",
            step_run_id="step-historical",
        ),
        "session_refs": [
            SimpleNamespace(
                session_id="session-historical-step",
                event_store_session_id="intaris-historical-step",
            )
        ],
        "event_store": _EventStore(),
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call-historical",
        offset=0,
        limit=200,
        latest=False,
    )

    assert result.conversation_id == "conv-1"
    assert result.session_id == "session-historical-step"
    assert result.content == "historical full output"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["session", "task_step"])
async def test_scoped_tool_output_finds_call_beyond_ten_thousand_events(
    scope_kind: str,
) -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = None

    class _LongEventStore:
        pages = 0

        async def read_session_events(self, **kwargs: Any) -> Any:
            self.pages += 1
            found = self.pages == 22
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call-old",
                            "result": "older than 10k events",
                            "status": "completed",
                        },
                    )
                ]
                if found
                else [
                    SimpleNamespace(
                        type="message",
                        data={"index": (self.pages - 1) * 500 + index},
                    )
                    for index in range(500)
                ],
                has_more_before=not found,
                first_seq=max(1, 20_000 - self.pages * 500),
            )

    event_store = _LongEventStore()
    context = {
        "scope": SimpleNamespace(kind=scope_kind, conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-old", event_store_session_id="intaris-old")
        ],
        "event_store": event_store,
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call-old",
        offset=0,
        limit=1000,
        latest=False,
    )
    assert result.content == "older than 10k events"
    assert event_store.pages == 22


@pytest.mark.asyncio
async def test_scoped_tool_output_authorizes_recovery_id_and_reads_saved_key() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = SimpleNamespace(
        read=AsyncMock(
            return_value=SimpleNamespace(
                content="saved output",
                offset=1,
                limit=1000,
                has_more=False,
                total_lines=1,
            )
        )
    )

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call_orig",
                            "recovery_call_id": "call_saved",
                            "result": "preview",
                            "has_full_output": True,
                        },
                    )
                ],
                has_more_before=False,
                first_seq=1,
            )

    context = {
        "scope": SimpleNamespace(conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-1", event_store_session_id="intaris-1")
        ],
        "event_store": _EventStore(),
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call_saved",
        offset=0,
        limit=1000,
        latest=False,
    )
    assert result.call_id == "call_saved"
    assert result.content == "saved output"
    request.app.state.tool_output_store.read.assert_awaited_once_with(
        "call_saved", offset=1, limit=1000
    )


@pytest.mark.asyncio
async def test_scoped_tool_output_denies_call_from_other_session() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = SimpleNamespace(read=AsyncMock())

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            return SimpleNamespace(events=[], has_more_before=False, first_seq=None)

    context = {
        "scope": SimpleNamespace(conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-1", event_store_session_id="intaris-1")
        ],
        "event_store": _EventStore(),
    }
    with pytest.raises(HTTPException) as exc_info:
        await _scoped_tool_output_page(
            request,
            context=context,
            call_id="call-other-session",
            offset=0,
            limit=1000,
            latest=False,
        )
    assert exc_info.value.status_code == 404
    request.app.state.tool_output_store.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "argument"),
    [
        (chat_v2_session_sync, "session-1"),
        (chat_v2_session_timeline, "session-1"),
        (chat_v2_task_step_sync, "step-1"),
        (chat_v2_task_step_timeline, "step-1"),
    ],
)
async def test_scoped_route_rejects_cursor_from_different_scope(
    monkeypatch: pytest.MonkeyPatch,
    route: Any,
    argument: str,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="alice@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        session_row=session_row,
        conversation_row=conversation_row,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
    )

    wrong_cursor = _cursor("conversation:other-conversation")
    if route in (chat_v2_session_sync, chat_v2_task_step_sync):
        with pytest.raises(HTTPException) as exc_info:
            await route(request, argument, wrong_cursor, limit=1)
    else:
        with pytest.raises(HTTPException) as exc_info:
            await route(request, argument, before=wrong_cursor, limit=1)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "cursor_invalid"


def _request(scheduler: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(turn_scheduler=scheduler)))


def _scoped_request(email: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email=email, role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_v2_cursor_secret="route-test-secret",
                providers=SimpleNamespace(guardrails=SimpleNamespace()),
                session_factory=lambda: _SessionContext(),
                turn_scheduler=None,
                session_cache=None,
            )
        ),
    )


def _session(
    session_id: str,
    *,
    owner: str,
    conversation_id: str,
    parent_session_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        user_email=owner,
        conversation_id=conversation_id,
        parent_session_id=parent_session_id,
        intaris_session_id=session_id,
        delegation_task="delegation",
        agent_id="agent",
        status="active",
        completion_reason=None,
    )


def _conversation(conversation_id: str, *, owner: str) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=conversation_id,
        user_email=owner,
        agent_id="agent",
        title="Conversation",
        status="active",
        active_session_id="session-1",
    )


def _patch_scope_queries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_row: Any | None = None,
    conversation_row: Any | None = None,
    step_run: Any | None = None,
    task: Any | None = None,
) -> None:
    async def get_session(_session: Any, session_id: str) -> Any:
        return (
            session_row
            if session_row is not None and session_row.session_id == session_id
            else None
        )

    async def get_conversation(_session: Any, conversation_id: str) -> Any:
        return (
            conversation_row
            if conversation_row is not None and conversation_row.conversation_id == conversation_id
            else None
        )

    async def get_step(_session: Any, _step_run_id: str) -> Any:
        return step_run

    async def get_task(_session: Any, _task_id: str) -> Any:
        return task

    monkeypatch.setattr(chat_v2_routes, "get_session_row", get_session)
    monkeypatch.setattr(chat_v2_routes, "get_conversation", get_conversation)
    monkeypatch.setattr(chat_v2_routes, "get_step_run", get_step)
    monkeypatch.setattr(chat_v2_routes, "get_task", get_task)


async def _return_context_scope(_request: Any, context: dict[str, Any]) -> Any:
    return context["scope"]


def _cursor(scope_key: str) -> str:
    return encode_cursor(
        InternalChatCursorPayload(
            scope_key=scope_key,
            conversation_id=None,
            projection_version=PROJECTION_VERSION,
            session_watermarks=[
                CursorSessionWatermark(store="intaris", session_id="session-1", last_seq=0)
            ],
            lineage=[
                CursorLineageEntry(
                    store="intaris", session_id="session-1", role="session", ordinal=0
                )
            ],
            view_revision=0,
            issued_at="2026-01-01T00:00:00Z",
        ),
        "route-test-secret",
    )


class _SessionContext:
    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _tx(
    client_txn_id: str,
    *,
    operation: str = "send_message",
    payload_hash: str = "hash",
    status: str = "accepted",
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=f"chat_txn_{client_txn_id}",
        conversation_id="conv-1",
        principal_id="user@test.com",
        client_txn_id=client_txn_id,
        operation=operation,
        payload_hash=payload_hash,
        status=status,
        result=result,
        error=error,
    )


class _Scheduler:
    def __init__(
        self,
        *,
        cancelled: bool = False,
        queued: list[dict[str, Any]] | None = None,
        error: TurnError | None = None,
        update_result: dict[str, Any] | None | bool = True,
    ) -> None:
        self.cancelled = cancelled
        self.queued = queued or []
        self.error = error
        self.update_result = update_result
        self.submitted: list[dict[str, Any]] = []
        self.cancel_calls: list[tuple[str, bool]] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str, str]] = []

    async def submit_turn(
        self,
        conversation_id: str,
        content: str,
        **kwargs: Any,
    ) -> TurnError | None:
        self.submitted.append(
            {
                "conversation_id": conversation_id,
                "content": content,
                "client_message_id": kwargs.get("client_message_id"),
                "chat_mode": kwargs.get("one_shot_chat_mode"),
            }
        )
        return self.error

    async def cancel_turn(self, conversation_id: str, *, clear_queue: bool) -> bool:
        self.cancel_calls.append((conversation_id, clear_queue))
        return self.cancelled

    async def cancel_queued_message(self, conversation_id: str, queue_id: str) -> bool:
        self.deleted.append((conversation_id, queue_id))
        return True

    async def update_queued_message(
        self,
        conversation_id: str,
        queue_id: str,
        *,
        content: str,
    ) -> dict[str, Any] | None:
        self.updated.append((conversation_id, queue_id, content))
        if self.update_result is None:
            return None
        for item in self.queued:
            if item.get("queue_id") == queue_id:
                item["content"] = content
                return item
        return {"queue_id": queue_id, "content": content}

    def queued_messages(self, _conversation_id: str) -> list[Any]:
        return self.queued
