"""REST routes for the Chat v2 snapshot/sync contract."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from cognis.api.chat_v2.cursors import ChatCursorError
from cognis.api.chat_v2.event_store import IntarisSessionEventStore, RawSessionEvent
from cognis.api.chat_v2.schemas import (
    CancelTurnV2Response,
    ChatSnapshot,
    ChatSyncResponse,
    ControlMutationV2Request,
    QueueMutationResponse,
    QueueUpdateV2Request,
    SendMessageV2Request,
    SendMessageV2Response,
    TimelineBackfillResponse,
)
from cognis.api.chat_v2.sync import (
    BACKFILL_DEFAULT_LIMIT,
    BACKFILL_MAX_LIMIT,
    BACKFILL_MIN_LIMIT,
    SYNC_DEFAULT_LIMIT,
    SYNC_MAX_LIMIT,
    SYNC_MIN_LIMIT,
    ChatV2SyncError,
    ConversationSessionRef,
    EventPostProcessor,
    build_chat_snapshot,
    build_chat_sync_response,
    build_timeline_backfill_response,
    conversation_summary_from_row,
    queue_state_from_messages,
    runtime_input_from_scheduler,
    state_view_from_snapshot,
)
from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_current_user,
    require_resource_owner,
)
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.chat_modes import parse_chat_mode_directive
from cognis.core.commands import is_system_slash_command_message
from cognis.core.conversation_state import snapshot_for_conversation
from cognis.core.turn_scheduler import TurnError
from cognis.providers.circuit_breaker import CircuitBreakerError
from cognis.store.models import ChatClientTransactionRow
from cognis.store.queries import (
    claim_chat_client_transaction,
    complete_chat_client_transaction,
    get_agent,
    get_conversation,
    get_root_session_chain,
    list_conversation_sessions,
    mark_artifacts_attached,
)

router = APIRouter(prefix="/api/v1/chat/v2/conversations", tags=["chat-v2-conversations"])
logger = logging.getLogger(__name__)
_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}


@router.get("/{conversation_id}/snapshot", response_model=ChatSnapshot)
async def chat_v2_snapshot(request: Request, conversation_id: str) -> ChatSnapshot:
    """Return an authoritative Chat v2 conversation snapshot."""

    context = await _load_read_context(request, conversation_id)
    try:
        return await build_chat_snapshot(**context)
    except CircuitBreakerError as exc:
        raise api_exception(
            503,
            "event_store_unavailable",
            "Session event store is temporarily unavailable",
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


@router.get("/{conversation_id}/sync", response_model=ChatSyncResponse)
async def chat_v2_sync(
    request: Request,
    conversation_id: str,
    cursor: str,
    limit: int = Query(default=SYNC_DEFAULT_LIMIT, ge=SYNC_MIN_LIMIT, le=SYNC_MAX_LIMIT),
) -> ChatSyncResponse:
    """Return cursor-checked incremental Chat v2 updates."""

    context = await _load_read_context(request, conversation_id)
    try:
        return await build_chat_sync_response(
            conversation_id=conversation_id,
            cursor=cursor,
            limit=limit,
            **context,
        )
    except ChatCursorError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc
    except CircuitBreakerError as exc:
        raise api_exception(
            503,
            "event_store_unavailable",
            "Session event store is temporarily unavailable",
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


@router.get("/{conversation_id}/timeline", response_model=TimelineBackfillResponse)
async def chat_v2_timeline(
    request: Request,
    conversation_id: str,
    before: str | None = None,
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=BACKFILL_MAX_LIMIT
    ),
) -> TimelineBackfillResponse:
    """Return an older canonical timeline page for scrollback."""

    context = await _load_read_context(request, conversation_id)
    try:
        return await build_timeline_backfill_response(
            conversation_id=conversation_id,
            before=before,
            limit=limit,
            session_refs=context["session_refs"],
            event_store=context["event_store"],
            cursor_secret=context["cursor_secret"],
            event_post_processor=context["event_post_processor"],
        )
    except CircuitBreakerError as exc:
        raise api_exception(
            503,
            "event_store_unavailable",
            "Session event store is temporarily unavailable",
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


@router.put(
    "/{conversation_id}/messages/{client_txn_id}",
    response_model=SendMessageV2Response,
    status_code=202,
)
async def chat_v2_send_message(
    request: Request,
    conversation_id: str,
    client_txn_id: str,
    payload: SendMessageV2Request,
) -> SendMessageV2Response:
    """Submit a normal Chat v2 user message with durable idempotency."""

    user, row = await _require_mutable_conversation(request, conversation_id)
    chat_mode_directive = parse_chat_mode_directive(payload.content)
    if is_system_slash_command_message(payload.content) or (
        chat_mode_directive is not None and chat_mode_directive.one_shot
    ):
        raise api_exception(
            409,
            "slash_command_not_supported",
            "Slash commands are not yet available through Chat v2 send.",
        )

    payload_hash = _payload_hash(
        "send_message",
        {
            "content": payload.content,
            "attachments": [item.model_dump(mode="json") for item in payload.attachments],
            "client_message_id": payload.client_message_id,
            "chat_mode": payload.chat_mode,
        },
    )
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=client_txn_id,
        operation="send_message",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _send_response_from_transaction(tx_row, duplicate=True)

    turn_scheduler = request.app.state.turn_scheduler
    error = await turn_scheduler.submit_turn(
        conversation_id,
        payload.content,
        user_email=user.email,
        attachments=[item.model_dump(mode="json") for item in payload.attachments],
        client_message_id=payload.client_message_id,
        one_shot_chat_mode=payload.chat_mode,
    )
    if error is not None:
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={
                "code": error.code,
                "message": error.message,
                "http_status": _turn_error_status(error.code),
            },
        )
        raise _turn_error_to_http(error)

    await _mark_attachments_attached(request, row, user.email, payload)
    queued_message = _queued_message_for_client(
        request.app.state.turn_scheduler.queued_messages(conversation_id),
        payload.client_message_id,
    )
    result_status = "queued" if queued_message is not None else "accepted"
    result = {
        "status": result_status,
        "client_txn_id": client_txn_id,
        "client_message_id": payload.client_message_id,
        "conversation_id": conversation_id,
        "message_id": None,
        "queue_id": queued_message.get("queue_id") if queued_message else None,
        "cursor": None,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request, tx_row.transaction_id, status=result_status, result=result
    )
    return _send_response_from_transaction(tx_row, duplicate=False)


@router.post("/{conversation_id}/cancel", response_model=CancelTurnV2Response)
async def chat_v2_cancel_turn(
    request: Request,
    conversation_id: str,
    payload: ControlMutationV2Request,
) -> CancelTurnV2Response:
    """Cancel the active Chat v2 turn without clearing queued messages."""

    user, _row = await _require_mutable_conversation(request, conversation_id)
    payload_hash = _payload_hash("cancel_turn", {"clear_queue": False})
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=payload.client_txn_id,
        operation="cancel_turn",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _cancel_response_from_transaction(tx_row, duplicate=True)

    cancelled = await request.app.state.turn_scheduler.cancel_turn(
        conversation_id,
        clear_queue=False,
    )
    result = {
        "conversation_id": conversation_id,
        "client_txn_id": payload.client_txn_id,
        "status": "cancelled" if cancelled else "idle",
        "runtime": None,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request,
        tx_row.transaction_id,
        status="completed",
        result=result,
    )
    return _cancel_response_from_transaction(tx_row, duplicate=False)


@router.delete("/{conversation_id}/queue/{queue_id}", response_model=QueueMutationResponse)
async def chat_v2_delete_queued_message(
    request: Request,
    conversation_id: str,
    queue_id: str,
    client_txn_id: str = Query(min_length=1, max_length=128),
) -> QueueMutationResponse:
    """Delete one queued Chat v2 message with durable idempotency."""

    user, _row = await _require_mutable_conversation(request, conversation_id)
    payload_hash = _payload_hash("delete_queued_message", {"queue_id": queue_id})
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=client_txn_id,
        operation="delete_queued_message",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _queue_response_from_transaction(tx_row, duplicate=True)

    deleted = await request.app.state.turn_scheduler.cancel_queued_message(
        conversation_id,
        queue_id,
    )
    if not deleted:
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={"code": "not_found", "message": "Queued message not found"},
        )
        raise api_exception(404, "not_found", "Queued message not found")

    queue = queue_state_from_messages(
        request.app.state.turn_scheduler.queued_messages(conversation_id)
    )
    result = {
        "conversation_id": conversation_id,
        "client_txn_id": client_txn_id,
        "status": "deleted",
        "queue": queue.model_dump(mode="json"),
        "cursor": None,
        "runtime": None,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request,
        tx_row.transaction_id,
        status="completed",
        result=result,
    )
    return _queue_response_from_transaction(tx_row, duplicate=False)


@router.patch("/{conversation_id}/queue/{queue_id}", response_model=QueueMutationResponse)
async def chat_v2_update_queued_message(
    request: Request,
    conversation_id: str,
    queue_id: str,
    payload: QueueUpdateV2Request,
) -> QueueMutationResponse:
    """Update one queued Chat v2 message with durable idempotency."""

    user, _row = await _require_mutable_conversation(request, conversation_id)
    content = payload.content.strip()
    if not content:
        raise api_exception(422, "invalid_request", "Queued message content is required")
    payload_hash = _payload_hash(
        "update_queued_message",
        {"queue_id": queue_id, "content": content},
    )
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=payload.client_txn_id,
        operation="update_queued_message",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _queue_response_from_transaction(tx_row, duplicate=True)

    updated = await request.app.state.turn_scheduler.update_queued_message(
        conversation_id,
        queue_id,
        content=content,
    )
    if updated is None:
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={"code": "not_found", "message": "Queued message not found"},
        )
        raise api_exception(404, "not_found", "Queued message not found")

    queue = queue_state_from_messages(
        request.app.state.turn_scheduler.queued_messages(conversation_id)
    )
    result = {
        "conversation_id": conversation_id,
        "client_txn_id": payload.client_txn_id,
        "status": "updated",
        "queue": queue.model_dump(mode="json"),
        "cursor": None,
        "runtime": None,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request,
        tx_row.transaction_id,
        status="completed",
        result=result,
    )
    return _queue_response_from_transaction(tx_row, duplicate=False)


async def _load_read_context(request: Request, conversation_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None or getattr(row, "status", None) == "deleted":
            raise api_exception(404, "not_found", "Conversation not found")
        require_resource_owner(request, row.user_email)

        session_refs = await _session_refs(session, conversation_id, row.active_session_id)
        state_snapshot = await snapshot_for_conversation(
            session,
            user_email=user.email,
            conversation_id=conversation_id,
            turn_scheduler=getattr(request.app.state, "turn_scheduler", None),
        )

    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    queued_messages = turn_scheduler.queued_messages(conversation_id) if turn_scheduler else []
    conversation = conversation_summary_from_row(row)
    return {
        "conversation": conversation,
        "session_refs": session_refs,
        "event_store": IntarisSessionEventStore(request.app.state.providers.guardrails),
        "cursor_secret": _cursor_secret(request),
        "queue": queue_state_from_messages(queued_messages),
        "state": state_view_from_snapshot(state_snapshot),
        "runtime_input": runtime_input_from_scheduler(
            conversation_id=conversation_id,
            active_session_id=conversation.active_session_id,
            turn_scheduler=turn_scheduler,
            session_cache=getattr(request.app.state, "session_cache", None),
        ),
        "event_post_processor": _event_attachment_hydrator(
            request,
            owner_email=user.email,
            conversation_id=conversation_id,
        ),
    }


def _event_attachment_hydrator(
    request: Request,
    *,
    owner_email: str,
    conversation_id: str,
) -> EventPostProcessor:
    async def _hydrate(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
        if not any(isinstance(event.data.get("attachments"), list) for event in events):
            return list(events)

        artifact_store = request.app.state.artifact_store
        hydrated_events: list[RawSessionEvent] = []
        async with request.app.state.session_factory() as artifact_session:
            for event in events:
                attachments = event.data.get("attachments")
                if not isinstance(attachments, list):
                    hydrated_events.append(event)
                    continue
                session_id = event.data.get("cognis_session_id")
                hydrated = await hydrate_attachment_refs(
                    artifact_session,
                    artifact_store,
                    attachments,
                    owner_email=owner_email,
                    conversation_id=conversation_id,
                    session_id=str(session_id) if session_id is not None else None,
                )
                hydrated_events.append(
                    event.model_copy(update={"data": {**event.data, "attachments": hydrated}})
                )
        return hydrated_events

    return _hydrate


async def _require_mutable_conversation(request: Request, conversation_id: str) -> tuple[Any, Any]:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None or getattr(row, "status", None) == "deleted":
            raise api_exception(404, "not_found", "Conversation not found")
        require_resource_owner(request, row.user_email)
        agent = await get_agent(session, row.agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, agent, required="use")
    if row.status == "archived":
        raise api_exception(409, "conflict", "Conversation is not active")
    if row.context_type in _MANAGED_CONVERSATION_CONTEXT_TYPES:
        raise api_exception(
            409,
            "managed_conversation_read_only",
            "Managed conversations are read-only from the target chat; use managed actions from the controller conversation.",
        )
    return user, row


async def _claim_transaction(
    request: Request,
    *,
    conversation_id: str,
    principal_id: str,
    client_txn_id: str,
    operation: str,
    payload_hash: str,
) -> tuple[Any, bool]:
    async with request.app.state.session_factory() as session:
        row, created = await claim_chat_client_transaction(
            session,
            conversation_id=conversation_id,
            principal_id=principal_id,
            client_txn_id=client_txn_id,
            operation=operation,
            payload_hash=payload_hash,
        )
        await session.commit()
        return row, created


async def _complete_transaction(
    request: Request,
    transaction_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> Any:
    async with request.app.state.session_factory() as session:
        db_result = await session.execute(
            select(ChatClientTransactionRow).where(
                ChatClientTransactionRow.transaction_id == transaction_id
            )
        )
        row = db_result.scalar_one()
        await complete_chat_client_transaction(
            session,
            row,
            status=status,
            result=result,
            error=error,
        )
        await session.commit()
        return row


async def _mark_attachments_attached(
    request: Request,
    conversation_row: Any,
    user_email: str,
    payload: SendMessageV2Request,
) -> None:
    try:
        async with request.app.state.session_factory() as session:
            latest_row = await get_conversation(session, conversation_row.conversation_id)
            await mark_artifacts_attached(
                session,
                [item.artifact_id for item in payload.attachments],
                owner_email=user_email,
                conversation_id=conversation_row.conversation_id,
                session_id=latest_row.active_session_id if latest_row else None,
            )
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to persist Chat v2 post-submit attachment association",
            extra={"extra_data": {"conversation_id": conversation_row.conversation_id}},
            exc_info=True,
        )


def _payload_hash(operation: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _server_time() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_replayable_transaction(row: Any, payload_hash: str) -> None:
    if row.payload_hash != payload_hash:
        raise api_exception(
            409,
            "client_txn_conflict",
            "Client transaction id was already used with a different payload",
        )
    if row.status == "pending":
        raise api_exception(
            409,
            "client_txn_pending",
            "Client transaction is still being processed",
        )
    if row.status == "failed":
        error = row.error or {}
        code = str(error.get("code") or "client_txn_failed")
        message = str(error.get("message") or "Client transaction failed")
        http_status = error.get("http_status")
        status = http_status if isinstance(http_status, int) else _turn_error_status(code)
        raise api_exception(status, code, message)


def _turn_error_status(code: str) -> int:
    return {
        "not_found": 404,
        "forbidden": 403,
        "session_ended": 409,
        "session_suspended": 409,
        "pending_question": 409,
        "pending_input_request": 409,
        "conflict": 409,
        "rate_limited": 429,
        "queue_full": 429,
        "session_creation_failed": 500,
    }.get(code, 500)


def _queued_message_for_client(
    queued_messages: list[dict[str, Any]],
    client_message_id: str,
) -> dict[str, Any] | None:
    for item in queued_messages:
        if item.get("client_message_id") == client_message_id:
            return item
    return None


def _send_response_from_transaction(
    row: Any,
    *,
    duplicate: bool,
) -> SendMessageV2Response:
    result = dict(row.result or {})
    result.setdefault("conversation_id", row.conversation_id)
    result.setdefault("client_txn_id", row.client_txn_id)
    result.setdefault("client_message_id", result.get("client_message_id") or row.client_txn_id)
    result.setdefault("message_id", None)
    result.setdefault("queue_id", None)
    result.setdefault("cursor", None)
    result["status"] = "duplicate" if duplicate else result.get("status", "accepted")
    result["server_time"] = _server_time()
    return SendMessageV2Response.model_validate(result)


def _cancel_response_from_transaction(
    row: Any,
    *,
    duplicate: bool,
) -> CancelTurnV2Response:
    result = dict(row.result or {})
    result.setdefault("conversation_id", row.conversation_id)
    result.setdefault("client_txn_id", row.client_txn_id)
    result.setdefault("runtime", None)
    result["status"] = "duplicate" if duplicate else result.get("status", "idle")
    result["server_time"] = _server_time()
    return CancelTurnV2Response.model_validate(result)


def _queue_response_from_transaction(
    row: Any,
    *,
    duplicate: bool,
) -> QueueMutationResponse:
    result = dict(row.result or {})
    result.setdefault("conversation_id", row.conversation_id)
    result.setdefault("client_txn_id", row.client_txn_id)
    result.setdefault("queue", {"messages": [], "queued_count": 0})
    result.setdefault("cursor", None)
    result.setdefault("runtime", None)
    result["status"] = "duplicate" if duplicate else result.get("status", "deleted")
    result["server_time"] = _server_time()
    return QueueMutationResponse.model_validate(result)


def _turn_error_to_http(error: TurnError) -> Exception:
    return api_exception(_turn_error_status(error.code), error.code, error.message)


async def _session_refs(
    session: Any,
    conversation_id: str,
    active_session_id: str | None,
) -> list[ConversationSessionRef]:
    if active_session_id is None:
        latest_roots = await list_conversation_sessions(
            session,
            conversation_id,
            root_only=True,
            order="desc",
            limit=1,
        )
        active_session_id = latest_roots[0].session_id if latest_roots else None
    if active_session_id is None:
        return []

    chain, _truncated = await get_root_session_chain(
        session,
        conversation_id,
        active_session_id,
    )
    return [
        ConversationSessionRef(
            session_id=row.session_id,
            event_store_session_id=row.intaris_session_id or row.session_id,
            store="intaris",
            role="root",
            ordinal=index,
            status=row.status,
            completion_reason=row.completion_reason,
        )
        for index, row in enumerate(chain)
    ]


def _cursor_secret(request: Request) -> str:
    secret = getattr(request.app.state, "chat_v2_cursor_secret", None)
    if isinstance(secret, str) and secret:
        return secret
    raise api_exception(
        500,
        "cursor_secret_unavailable",
        "Chat v2 cursor signing secret is not configured",
    )
