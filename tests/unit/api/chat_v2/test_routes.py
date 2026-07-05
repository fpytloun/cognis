"""Route contract tests for Chat v2."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from cognis.api.chat_v2 import routes as chat_v2_routes
from cognis.api.chat_v2.routes import (
    _cursor_secret,
    chat_v2_cancel_turn,
    chat_v2_delete_queued_message,
    chat_v2_send_message,
    chat_v2_update_queued_message,
    router,
)
from cognis.api.chat_v2.schemas import (
    ControlMutationV2Request,
    QueueUpdateV2Request,
    SendMessageV2Request,
)
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
        "PUT",
        "/api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}",
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


def _request(scheduler: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(turn_scheduler=scheduler)))


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
