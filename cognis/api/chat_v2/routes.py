"""REST routes for the Chat v2 snapshot/sync contract."""

from __future__ import annotations

import asyncio
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
    CommandV2Request,
    CommandV2Response,
    ControlMutationV2Request,
    QueueMutationResponse,
    QueueUpdateV2Request,
    RetryTurnV2Response,
    SendMessageV2Request,
    SendMessageV2Response,
    TimelineBackfillResponse,
    TimelineScope,
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
    RuntimeOverlayInput,
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
from cognis.api.models import ToolOutputChunkResponse, ToolOutputPageResponse
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.chat_modes import parse_chat_mode_directive
from cognis.core.commands import is_system_slash_command_message
from cognis.core.conversation_state import snapshot_for_conversation
from cognis.core.turn_scheduler import TurnError
from cognis.models.agent import AgentDefinition
from cognis.providers.circuit_breaker import CircuitBreakerError
from cognis.store.models import ChatClientTransactionRow
from cognis.store.queries import (
    claim_chat_client_transaction,
    complete_chat_client_transaction,
    get_agent,
    get_conversation,
    get_root_session_chain,
    get_session_row,
    get_step_run,
    get_task,
    list_conversation_sessions,
    mark_artifacts_attached,
)

router = APIRouter(prefix="/api/v1/chat/v2", tags=["chat-v2"])
logger = logging.getLogger(__name__)
_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}


async def _scoped_tool_output_page(
    request: Request,
    *,
    context: dict[str, Any],
    call_id: str,
    offset: int,
    limit: int,
    latest: bool,
) -> ToolOutputPageResponse:
    scope: TimelineScope = context["scope"]
    session_refs: list[ConversationSessionRef] = context["session_refs"]
    conversation_id = scope.conversation_id
    if not conversation_id or not session_refs:
        raise api_exception(404, "not_found", "Tool output not found")

    event_data: dict[str, Any] | None = None
    storage_call_id = call_id
    resolved_session_id: str | None = None
    event_store: IntarisSessionEventStore = context["event_store"]
    for ref in session_refs:
        before_seq: int | None = None
        visited_before_seq: set[int] = set()
        while True:
            is_disconnected = getattr(request, "is_disconnected", None)
            if callable(is_disconnected) and await is_disconnected():
                raise api_exception(499, "client_disconnected", "Client disconnected")
            page = await event_store.read_session_events(
                session_id=ref.event_store_session_id,
                before_seq=before_seq,
                limit=500,
                direction="backward",
            )
            for event in page.events:
                event_call_id = event.data.get("call_id")
                recovery_call_id = event.data.get("recovery_call_id")
                if call_id not in {event_call_id, recovery_call_id}:
                    continue
                storage_call_id = (
                    recovery_call_id
                    if isinstance(recovery_call_id, str) and recovery_call_id
                    else str(event_call_id)
                )
                if event.type == "tool_result":
                    event_data = event.data
                    resolved_session_id = ref.session_id
                    break
                if event_data is None and event.type in {
                    "tool_call",
                    "tool_result_chunk",
                    "tool_output_chunk",
                }:
                    event_data = event.data
                    resolved_session_id = ref.session_id
            if event_data is not None and event_data.get("result") is not None:
                break
            if not page.has_more_before or page.first_seq is None:
                break
            if page.first_seq in visited_before_seq:
                raise api_exception(
                    502,
                    "event_store_pagination_stalled",
                    "Session event pagination did not advance",
                )
            visited_before_seq.add(page.first_seq)
            before_seq = page.first_seq
            await asyncio.sleep(0)
        if event_data is not None:
            break

    allowed_session_ids = {ref.session_id for ref in session_refs}
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    if event_data is None and turn_scheduler is not None:
        snapshots = await turn_scheduler.active_tool_output_snapshots(conversation_id)
        for snapshot in snapshots:
            snapshot_session_id = snapshot.get("session_id")
            if (
                call_id in {snapshot.get("call_id"), snapshot.get("recovery_call_id")}
                and isinstance(snapshot_session_id, str)
                and snapshot_session_id in allowed_session_ids
            ):
                event_data = snapshot
                resolved_session_id = snapshot_session_id
                snapshot_recovery_id = snapshot.get("recovery_call_id")
                storage_call_id = (
                    snapshot_recovery_id
                    if isinstance(snapshot_recovery_id, str) and snapshot_recovery_id
                    else str(snapshot.get("call_id"))
                )
                break
    if event_data is None or resolved_session_id not in allowed_session_ids:
        raise api_exception(404, "not_found", "Tool output not found")

    if turn_scheduler is not None:
        live_page = turn_scheduler.read_live_tool_output_page(
            conversation_id=conversation_id,
            session_id=resolved_session_id,
            call_id=storage_call_id,
            offset=offset,
            limit=limit,
            latest=latest,
        )
        if live_page is not None and live_page.status == "running":
            return ToolOutputPageResponse(
                conversation_id=conversation_id,
                session_id=resolved_session_id,
                call_id=storage_call_id,
                status=live_page.status,
                source="live_spool",
                content=live_page.content,
                chunks=[
                    ToolOutputChunkResponse(
                        index=chunk.index,
                        offset=chunk.offset,
                        stream=chunk.stream,
                        text=chunk.text,
                    )
                    for chunk in live_page.chunks
                ],
                offset=live_page.offset,
                limit=live_page.limit,
                next_offset=live_page.next_offset,
                prev_offset=live_page.prev_offset,
                has_more_before=live_page.has_more_before,
                has_more_after=live_page.has_more_after,
                output_size=live_page.output_size,
                recoverable=True,
                truncated=live_page.truncated,
                spool_truncated=live_page.truncated,
            )

    tool_output_store = getattr(request.app.state, "tool_output_store", None)
    if tool_output_store is not None and bool(event_data.get("has_full_output")):
        stored = await tool_output_store.read(
            storage_call_id, offset=max(1, offset or 1), limit=limit
        )
        if stored is not None:
            return ToolOutputPageResponse(
                conversation_id=conversation_id,
                session_id=resolved_session_id,
                call_id=storage_call_id,
                status="completed",
                source="stored_output",
                content=stored.content,
                offset=stored.offset,
                limit=stored.limit,
                next_offset=stored.offset + stored.limit if stored.has_more else None,
                prev_offset=max(1, stored.offset - stored.limit) if stored.offset > 1 else None,
                has_more_before=stored.offset > 1,
                has_more_after=stored.has_more,
                output_size=int(event_data.get("output_size") or len(stored.content)),
                total_lines=stored.total_lines,
                recoverable=True,
                truncated=bool(
                    event_data.get("truncated") or event_data.get("agent_visible_truncated")
                ),
            )

    preview = str(event_data.get("result") or "")
    return ToolOutputPageResponse(
        conversation_id=conversation_id,
        session_id=resolved_session_id,
        call_id=call_id,
        status=str(event_data.get("status") or "completed"),
        source="event_preview",
        content=preview,
        offset=0,
        limit=limit,
        output_size=len(preview),
        recoverable=False,
    )


@router.get("/conversations/{conversation_id}/snapshot", response_model=ChatSnapshot)
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


@router.get(
    "/conversations/{conversation_id}/tool-outputs/{call_id}",
    response_model=ToolOutputPageResponse,
)
async def chat_v2_conversation_tool_output(
    request: Request,
    conversation_id: str,
    call_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = Query(default=False),
) -> ToolOutputPageResponse:
    return await _scoped_tool_output_page(
        request,
        context=await _load_read_context(request, conversation_id),
        call_id=call_id,
        offset=offset,
        limit=limit,
        latest=latest,
    )


@router.get(
    "/sessions/{session_id}/tool-outputs/{call_id}",
    response_model=ToolOutputPageResponse,
)
async def chat_v2_session_tool_output(
    request: Request,
    session_id: str,
    call_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = Query(default=False),
) -> ToolOutputPageResponse:
    return await _scoped_tool_output_page(
        request,
        context=await _load_session_context(request, session_id),
        call_id=call_id,
        offset=offset,
        limit=limit,
        latest=latest,
    )


@router.get(
    "/task-steps/{step_run_id}/tool-outputs/{call_id}",
    response_model=ToolOutputPageResponse,
)
async def chat_v2_task_step_tool_output(
    request: Request,
    step_run_id: str,
    call_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = Query(default=False),
) -> ToolOutputPageResponse:
    return await _scoped_tool_output_page(
        request,
        context=await _load_task_step_context(request, step_run_id),
        call_id=call_id,
        offset=offset,
        limit=limit,
        latest=latest,
    )


@router.get("/conversations/{conversation_id}/sync", response_model=ChatSyncResponse)
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


@router.get("/conversations/{conversation_id}/timeline", response_model=TimelineBackfillResponse)
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
            scope=context["scope"],
            before=before,
            limit=limit,
            session_refs=context["session_refs"],
            event_store=context["event_store"],
            cursor_secret=context["cursor_secret"],
            event_post_processor=context["event_post_processor"],
            session_cache=context["session_cache"],
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
    "/conversations/{conversation_id}/messages/{client_txn_id}",
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


@router.put(
    "/conversations/{conversation_id}/commands/{client_txn_id}",
    response_model=CommandV2Response,
)
async def chat_v2_execute_command(
    request: Request,
    conversation_id: str,
    client_txn_id: str,
    payload: CommandV2Request,
) -> CommandV2Response:
    """Execute a slash command through the durable Chat v2 write path."""

    user, conversation_row = await _require_mutable_conversation(request, conversation_id)
    content = payload.content.strip()
    if not is_system_slash_command_message(content):
        raise api_exception(422, "invalid_command", "A recognized slash command is required")

    payload_hash = _payload_hash("execute_command", {"content": content})
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=client_txn_id,
        operation="execute_command",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _command_response_from_transaction(tx_row, duplicate=True)

    from cognis.api.serializers import agent_to_response
    from cognis.core.session import _to_conversation_model, _to_session_model

    async with request.app.state.session_factory() as session:
        agent_row = await get_agent(session, conversation_row.agent_id)
        session_row = (
            await get_session_row(session, conversation_row.active_session_id)
            if conversation_row.active_session_id
            else None
        )
    if agent_row is None:
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={"code": "not_found", "message": "Agent not found", "http_status": 404},
        )
        raise api_exception(404, "not_found", "Agent not found")

    conversation = _to_conversation_model(conversation_row)
    active_session = _to_session_model(session_row) if session_row else None
    try:
        if active_session is None:
            active_session = await request.app.state.session_manager.ensure_root_session(
                conversation_id=conversation_id,
                user_email=user.email,
                agent_id=conversation.agent_id,
                intention=content,
            )
            conversation = conversation.model_copy(
                update={"active_session_id": active_session.session_id}
            )

        agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        scheduler = request.app.state.turn_scheduler
        command_result = await request.app.state.command_dispatcher.dispatch(
            content,
            conversation=conversation,
            session=active_session,
            agent=agent,
            user_email=user.email,
            has_active_turn=scheduler.has_running_turn(conversation_id),
            has_busy_turn=scheduler.has_active_turn(conversation_id),
        )
    except Exception as exc:
        logger.exception(
            "Chat v2 command execution failed",
            extra={"extra_data": {"conversation_id": conversation_id}},
        )
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={
                "code": "command_execution_failed",
                "message": str(exc) or "Command execution failed",
                "http_status": 500,
            },
        )
        raise
    if command_result is None:
        await _complete_transaction(
            request,
            tx_row.transaction_id,
            status="failed",
            error={
                "code": "invalid_command",
                "message": "Slash command was not handled",
                "http_status": 422,
            },
        )
        raise api_exception(422, "invalid_command", "Slash command was not handled")

    result = {
        "conversation_id": conversation_id,
        "client_txn_id": client_txn_id,
        "status": "completed",
        "result_type": command_result.type,
        "text": command_result.text or "",
        "data": command_result.data,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request,
        tx_row.transaction_id,
        status="completed",
        result=result,
    )
    return _command_response_from_transaction(tx_row, duplicate=False)


@router.post("/conversations/{conversation_id}/cancel", response_model=CancelTurnV2Response)
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


@router.post(
    "/conversations/{conversation_id}/turns/{turn_id}/retry",
    response_model=RetryTurnV2Response,
)
async def chat_v2_retry_turn(
    request: Request,
    conversation_id: str,
    turn_id: str,
    payload: ControlMutationV2Request,
) -> RetryTurnV2Response:
    """Retry a failed Chat v2 turn without recording a new user message."""

    user, row = await _require_mutable_conversation(request, conversation_id)
    payload_hash = _payload_hash("retry_turn", {"turn_id": turn_id})
    tx_row, created = await _claim_transaction(
        request,
        conversation_id=conversation_id,
        principal_id=user.email,
        client_txn_id=payload.client_txn_id,
        operation="retry_turn",
        payload_hash=payload_hash,
    )
    if not created:
        _ensure_replayable_transaction(tx_row, payload_hash)
        return _retry_response_from_transaction(tx_row, duplicate=True)

    turn_scheduler = request.app.state.turn_scheduler
    async with turn_scheduler.retry_admission_lock(conversation_id):
        if turn_scheduler.has_active_turn(conversation_id):
            await _complete_transaction(
                request,
                tx_row.transaction_id,
                status="failed",
                error={
                    "code": "active_turn_in_progress",
                    "message": "A turn is already active for this conversation.",
                    "http_status": 409,
                },
            )
            raise api_exception(
                409,
                "active_turn_in_progress",
                "A turn is already active for this conversation.",
            )

        retry_source, failed_turn_found = await _retry_source_from_failed_turn(
            request, row, turn_id
        )
        if retry_source is None:
            error_code, error_message = _retry_unavailable_error(failed_turn_found)
            await _complete_transaction(
                request,
                tx_row.transaction_id,
                status="failed",
                error={
                    "code": error_code,
                    "message": error_message,
                    "http_status": 409,
                },
            )
            raise api_exception(
                409,
                error_code,
                error_message,
            )

        error = await turn_scheduler.submit_turn(
            conversation_id,
            retry_source["content"],
            user_email=user.email,
            attachments=retry_source["attachments"],
            client_message_id=retry_source["client_message_id"],
            is_retry=True,
            retry_source_turn_id=turn_id,
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

    result = {
        "conversation_id": conversation_id,
        "client_txn_id": payload.client_txn_id,
        "turn_id": turn_id,
        "status": "accepted",
        "runtime": None,
        "server_time": _server_time(),
    }
    tx_row = await _complete_transaction(
        request,
        tx_row.transaction_id,
        status="completed",
        result=result,
    )
    return _retry_response_from_transaction(tx_row, duplicate=False)


@router.delete(
    "/conversations/{conversation_id}/queue/{queue_id}",
    response_model=QueueMutationResponse,
)
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


@router.patch(
    "/conversations/{conversation_id}/queue/{queue_id}",
    response_model=QueueMutationResponse,
)
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
    scope = TimelineScope(
        key=f"conversation:{conversation_id}",
        kind="conversation",
        conversation_id=conversation_id,
        session_id=conversation.active_session_id,
        label=conversation.title,
        status=conversation.status,
    )
    return {
        "scope": scope,
        "conversation": conversation,
        "session_refs": session_refs,
        "event_store": IntarisSessionEventStore(request.app.state.providers.guardrails),
        "cursor_secret": _cursor_secret(request),
        "queue": queue_state_from_messages(queued_messages),
        "state": state_view_from_snapshot(state_snapshot),
        "runtime_input": runtime_input_from_scheduler(
            conversation_id=conversation_id,
            scope_key=scope.key,
            active_session_id=conversation.active_session_id,
            turn_scheduler=turn_scheduler,
            session_cache=getattr(request.app.state, "session_cache", None),
        ),
        "session_cache": getattr(request.app.state, "session_cache", None),
        "event_post_processor_cache_key": f"attachments:{conversation_id}:{user.email}",
        "event_post_processor": _event_attachment_hydrator(
            request,
            owner_email=user.email,
            conversation_id=conversation_id,
        ),
    }


async def _build_scoped_snapshot(request: Request, context: dict[str, Any]) -> ChatSnapshot:
    try:
        return await build_chat_snapshot(**context)
    except CircuitBreakerError as exc:
        raise api_exception(
            503, "event_store_unavailable", "Session event store is temporarily unavailable"
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


async def _build_scoped_sync(
    request: Request,
    context: dict[str, Any],
    *,
    cursor: str,
    limit: int,
) -> ChatSyncResponse:
    del request
    try:
        return await build_chat_sync_response(cursor=cursor, limit=limit, **context)
    except ChatCursorError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc
    except CircuitBreakerError as exc:
        raise api_exception(
            503, "event_store_unavailable", "Session event store is temporarily unavailable"
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


async def _build_scoped_backfill(
    request: Request,
    context: dict[str, Any],
    *,
    before: str | None,
    limit: int,
) -> TimelineBackfillResponse:
    del request
    backfill_context = {
        key: context[key]
        for key in (
            "scope",
            "session_refs",
            "event_store",
            "cursor_secret",
            "event_post_processor",
            "session_cache",
        )
    }
    try:
        return await build_timeline_backfill_response(
            before=before, limit=limit, **backfill_context
        )
    except CircuitBreakerError as exc:
        raise api_exception(
            503, "event_store_unavailable", "Session event store is temporarily unavailable"
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


async def _load_session_context(request: Request, session_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        session_row = await get_session_row(session, session_id)
        if session_row is None:
            raise api_exception(404, "not_found", "Session not found")
        require_resource_owner(request, session_row.user_email)
        conversation_row = await get_conversation(session, session_row.conversation_id)
        if conversation_row is None or getattr(conversation_row, "status", None) == "deleted":
            raise api_exception(404, "not_found", "Owning conversation not found")
        require_resource_owner(request, conversation_row.user_email)

    scope = TimelineScope(
        key=f"session:{session_id}",
        kind="session",
        conversation_id=session_row.conversation_id,
        session_id=session_id,
        parent_session_id=session_row.parent_session_id,
        label=session_row.delegation_task or session_row.agent_id,
        status=session_row.status,
    )
    return _single_session_context(
        request,
        user_email=user.email,
        conversation_row=conversation_row,
        session_row=session_row,
        scope=scope,
    )


async def _load_task_step_context(request: Request, step_run_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        step_run = await get_step_run(session, step_run_id)
        if step_run is None:
            raise api_exception(404, "not_found", "Task step run not found")
        task = await get_task(session, step_run.task_id)
        if task is None:
            raise api_exception(404, "not_found", "Task not found")
        require_resource_owner(request, task.created_by)
        session_row = (
            await get_session_row(session, step_run.session_id) if step_run.session_id else None
        )
        if session_row is not None:
            require_resource_owner(request, session_row.user_email)
            if (
                step_run.conversation_id is not None
                and session_row.conversation_id != step_run.conversation_id
            ):
                raise api_exception(404, "not_found", "Task step session linkage is invalid")
        effective_conversation_id = step_run.conversation_id or (
            session_row.conversation_id if session_row is not None else None
        )
        conversation_row = (
            await get_conversation(session, effective_conversation_id)
            if effective_conversation_id
            else None
        )
        if effective_conversation_id and conversation_row is None:
            raise api_exception(404, "not_found", "Owning conversation not found")
        if conversation_row is not None:
            require_resource_owner(request, conversation_row.user_email)
        elif session_row is not None:
            conversation_row = await get_conversation(session, session_row.conversation_id)
            if conversation_row is None:
                raise api_exception(404, "not_found", "Owning conversation not found")
            require_resource_owner(request, conversation_row.user_email)

    scope = TimelineScope(
        key=f"task_step:{step_run_id}",
        kind="task_step",
        conversation_id=effective_conversation_id,
        session_id=step_run.session_id,
        task_id=step_run.task_id,
        step_run_id=step_run_id,
        parent_session_id=session_row.parent_session_id if session_row is not None else None,
        label=f"{step_run.step_name} (attempt {step_run.attempt_number})",
        status=step_run.status,
        missing_stream=session_row is None,
    )
    if session_row is None:
        now = datetime.now(UTC)
        return {
            "scope": scope,
            "conversation": (
                conversation_summary_from_row(conversation_row)
                if conversation_row is not None
                else None
            ),
            "session_refs": [],
            "event_store": IntarisSessionEventStore(request.app.state.providers.guardrails),
            "cursor_secret": _cursor_secret(request),
            "queue": None,
            "state": state_view_from_snapshot(None, now=now),
            "runtime_input": RuntimeOverlayInput(
                runtime_epoch=f"{scope.key}:missing",
                runtime_revision=0,
                active_turn=None,
            ),
            "session_cache": getattr(request.app.state, "session_cache", None),
            "event_post_processor_cache_key": None,
            "event_post_processor": None,
        }
    return _single_session_context(
        request,
        user_email=user.email,
        conversation_row=conversation_row,
        session_row=session_row,
        scope=scope,
    )


def _single_session_context(
    request: Request,
    *,
    user_email: str,
    conversation_row: Any | None,
    session_row: Any,
    scope: TimelineScope,
) -> dict[str, Any]:
    conversation_id = str(session_row.conversation_id)
    runtime_input = runtime_input_from_scheduler(
        conversation_id=conversation_id,
        scope_key=scope.key,
        active_session_id=session_row.session_id,
        turn_scheduler=getattr(request.app.state, "turn_scheduler", None),
        session_cache=getattr(request.app.state, "session_cache", None),
    )
    if (
        runtime_input.active_turn is not None
        and runtime_input.active_turn.get("session_id") != session_row.session_id
    ):
        runtime_input = runtime_input.model_copy(
            update={"runtime_revision": 0, "active_turn": None}
        )
    return {
        "scope": scope,
        "conversation": (
            conversation_summary_from_row(conversation_row)
            if conversation_row is not None
            else None
        ),
        "session_refs": [
            ConversationSessionRef(
                session_id=session_row.session_id,
                event_store_session_id=session_row.intaris_session_id or session_row.session_id,
                store="intaris",
                role="session",
                ordinal=0,
                status=session_row.status,
                completion_reason=session_row.completion_reason,
            )
        ],
        "event_store": IntarisSessionEventStore(request.app.state.providers.guardrails),
        "cursor_secret": _cursor_secret(request),
        "queue": None,
        "state": state_view_from_snapshot(None),
        "runtime_input": runtime_input,
        "session_cache": getattr(request.app.state, "session_cache", None),
        "event_post_processor_cache_key": f"attachments:{scope.key}:{user_email}",
        "event_post_processor": _event_attachment_hydrator(
            request,
            owner_email=user_email,
            conversation_id=conversation_id,
        ),
    }


@router.get("/sessions/{session_id}/snapshot", response_model=ChatSnapshot)
async def chat_v2_session_snapshot(request: Request, session_id: str) -> ChatSnapshot:
    """Return an authoritative snapshot for one verified session stream."""

    return await _build_scoped_snapshot(request, await _load_session_context(request, session_id))


@router.get("/sessions/{session_id}/sync", response_model=ChatSyncResponse)
async def chat_v2_session_sync(
    request: Request,
    session_id: str,
    cursor: str,
    limit: int = Query(default=SYNC_DEFAULT_LIMIT, ge=SYNC_MIN_LIMIT, le=SYNC_MAX_LIMIT),
) -> ChatSyncResponse:
    context = await _load_session_context(request, session_id)
    return await _build_scoped_sync(request, context, cursor=cursor, limit=limit)


@router.get("/sessions/{session_id}/timeline", response_model=TimelineBackfillResponse)
async def chat_v2_session_timeline(
    request: Request,
    session_id: str,
    before: str | None = None,
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=BACKFILL_MAX_LIMIT
    ),
) -> TimelineBackfillResponse:
    context = await _load_session_context(request, session_id)
    return await _build_scoped_backfill(request, context, before=before, limit=limit)


@router.get("/task-steps/{step_run_id}/snapshot", response_model=ChatSnapshot)
async def chat_v2_task_step_snapshot(request: Request, step_run_id: str) -> ChatSnapshot:
    """Return one task-step attempt timeline without merging other attempts."""

    return await _build_scoped_snapshot(
        request, await _load_task_step_context(request, step_run_id)
    )


@router.get("/task-steps/{step_run_id}/sync", response_model=ChatSyncResponse)
async def chat_v2_task_step_sync(
    request: Request,
    step_run_id: str,
    cursor: str,
    limit: int = Query(default=SYNC_DEFAULT_LIMIT, ge=SYNC_MIN_LIMIT, le=SYNC_MAX_LIMIT),
) -> ChatSyncResponse:
    context = await _load_task_step_context(request, step_run_id)
    return await _build_scoped_sync(request, context, cursor=cursor, limit=limit)


@router.get("/task-steps/{step_run_id}/timeline", response_model=TimelineBackfillResponse)
async def chat_v2_task_step_timeline(
    request: Request,
    step_run_id: str,
    before: str | None = None,
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=BACKFILL_MAX_LIMIT
    ),
) -> TimelineBackfillResponse:
    context = await _load_task_step_context(request, step_run_id)
    return await _build_scoped_backfill(request, context, before=before, limit=limit)


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
        "active_turn_in_progress": 409,
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


def _command_response_from_transaction(
    row: Any,
    *,
    duplicate: bool,
) -> CommandV2Response:
    result = dict(row.result or {})
    result.setdefault("conversation_id", row.conversation_id)
    result.setdefault("client_txn_id", row.client_txn_id)
    result.setdefault("result_type", "system_message")
    result.setdefault("text", "")
    result.setdefault("data", {})
    result["status"] = "duplicate" if duplicate else result.get("status", "completed")
    result["server_time"] = _server_time()
    return CommandV2Response.model_validate(result)


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


def _retry_response_from_transaction(
    row: Any,
    *,
    duplicate: bool,
) -> RetryTurnV2Response:
    result = dict(row.result or {})
    result.setdefault("conversation_id", row.conversation_id)
    result.setdefault("client_txn_id", row.client_txn_id)
    result.setdefault("turn_id", "")
    result.setdefault("runtime", None)
    result["status"] = "duplicate" if duplicate else result.get("status", "accepted")
    result["server_time"] = _server_time()
    return RetryTurnV2Response.model_validate(result)


def _turn_error_to_http(error: TurnError) -> Exception:
    return api_exception(_turn_error_status(error.code), error.code, error.message)


async def _retry_source_from_failed_turn(
    request: Request,
    conversation_row: Any,
    turn_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    async with request.app.state.session_factory() as session:
        session_refs = await _session_refs(
            session,
            conversation_row.conversation_id,
            conversation_row.active_session_id,
        )
    event_store = IntarisSessionEventStore(request.app.state.providers.guardrails)
    failed_turn_found = False
    completed_turn_found = False
    user_event: RawSessionEvent | None = None
    for ref in reversed(session_refs):
        before_seq: int | None = None
        for _ in range(20):
            page = await event_store.read_session_events(
                session_id=ref.event_store_session_id,
                before_seq=before_seq,
                limit=500,
                direction="backward",
            )
            if not page.events:
                break
            for event in page.events:
                event_turn_id = _event_turn_id(event)
                retry_source_turn_id = _str_or_none(event.data.get("retry_source_turn_id"))
                if event.data.get("event") == "retry_source_consumed" and (
                    retry_source_turn_id == turn_id
                ):
                    completed_turn_found = True
                if event_turn_id != turn_id:
                    continue
                if event.type == "user_message" and user_event is None:
                    user_event = event
                if _is_completed_turn_marker(event):
                    completed_turn_found = True
                if _is_failed_turn_marker(event):
                    failed_turn_found = True
            if not page.has_more_before or page.first_seq is None:
                break
            before_seq = page.first_seq
    if completed_turn_found:
        return None, False
    if failed_turn_found and user_event is not None:
        return (
            {
                "content": str(user_event.data.get("content") or ""),
                "attachments": user_event.data.get("attachments")
                if isinstance(user_event.data.get("attachments"), list)
                else [],
                "client_message_id": _str_or_none(
                    user_event.data.get("client_message_id") or user_event.data.get("message_id")
                ),
            },
            True,
        )
    return None, failed_turn_found


def _retry_unavailable_error(failed_turn_found: bool) -> tuple[str, str]:
    if failed_turn_found:
        return (
            "retry_source_not_persisted",
            "This failed legacy turn has no persisted source message and cannot be retried.",
        )
    return "retry_turn_not_available", "Only failed, inactive turns can be retried."


def _event_turn_id(event: RawSessionEvent) -> str | None:
    value = event.data.get("turn_id")
    return value if isinstance(value, str) and value else None


def _is_failed_turn_marker(event: RawSessionEvent) -> bool:
    data = event.data
    lifecycle_event = str(data.get("event") or data.get("type") or event.type)
    status = str(data.get("status") or "").lower()
    if lifecycle_event in {"turn_error", "turn_failed", "message_error"}:
        return True
    if status in {"failed", "error"} and lifecycle_event in {
        "turn",
        "turn_status",
        "turn_state",
        "turn_error",
        "message_error",
    }:
        return True
    text = str(data.get("content") or data.get("message") or data.get("text") or "")
    return text.startswith("Turn failed:") or "model error occurred" in text.lower()


def _is_completed_turn_marker(event: RawSessionEvent) -> bool:
    data = event.data
    lifecycle_event = str(data.get("event") or data.get("type") or event.type)
    return lifecycle_event in {
        "turn_completed",
        "message_complete",
        "retry_source_consumed",
    }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
