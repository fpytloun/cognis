"""REST routes for the Chat v2 snapshot/sync contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

from fastapi import APIRouter, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cognis.api.chat_v2.cursors import ChatCursorError
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventStore
from cognis.api.chat_v2.request_metrics import (
    CHAT_V2_REQUEST_METRICS,
    CacheOnlyOutcome,
    SyncOutcome,
)
from cognis.api.chat_v2.schemas import (
    ActivityOverviewDetail,
    ActivityOverviewResponse,
    CancelTurnV2Response,
    ChatSnapshot,
    ChatSyncResponse,
    ClientPerformanceRequest,
    CommandV2Request,
    CommandV2Response,
    ControlMutationV2Request,
    ForkAssistantMessageV2Request,
    ForkAssistantMessageV2Response,
    QueueMutationResponse,
    QueueUpdateV2Request,
    RetryTurnV2Response,
    SendMessageV2Request,
    SendMessageV2Response,
    TimelineBackfillResponse,
    TimelineScope,
    WorkCategory,
    WorkDeliverable,
    WorkProjectionResponse,
)
from cognis.api.chat_v2.shared_snapshot_cache import SnapshotRequestTrace
from cognis.api.chat_v2.snapshot_coordinator import (
    ConversationSnapshotContext,
    build_chat_snapshot_coordinated,
    get_cached_chat_snapshot_coordinated,
    load_conversation_snapshot_context,
)
from cognis.api.chat_v2.snapshot_metrics import (
    SNAPSHOT_CACHE_METRICS,
    SnapshotRequestOutcome,
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
from cognis.api.chat_v2.work_graph import (
    WORK_GRAPH_MAX_SECONDS,
    AuthorizedWorkGraph,
    resolve_authorized_work_graph,
)
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.api.chat_v2.work_repository import (
    WorkCursorError,
    read_activity_overview,
    read_work_page,
)
from cognis.api.chat_v2.work_revisions import WorkRevisionSnapshot
from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_current_user,
    require_resource_owner,
    require_session_user,
)
from cognis.api.models import ToolOutputChunkResponse, ToolOutputPageResponse
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.chat_modes import parse_chat_mode_directive
from cognis.core.command_notices import persist_command_system_notice
from cognis.core.commands import is_system_slash_command_message
from cognis.core.turn_scheduler import TurnError
from cognis.core.user_message_overflow import TextArtifact, normalize_user_message_content
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.retry import RetryReason
from cognis.providers.circuit_breaker import CircuitBreakerError
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import ChatClientTransactionRow, DeliverableRow, WorkRecordRow
from cognis.store.queries import (
    claim_chat_client_transaction,
    complete_chat_client_transaction,
    create_artifact_record,
    get_agent,
    get_artifact_record,
    get_child_session_continuation_chain,
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
WORK_PAGE_MAX_LIMIT = 100
WORK_REQUEST_MAX_SECONDS = 20.0
_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}
_CLIENT_PERFORMANCE_BODY_MAX_BYTES = 256


async def _scoped_tool_output_page(
    request: Request,
    *,
    context: dict[str, Any] | ConversationSnapshotContext,
    call_id: str,
    offset: int,
    limit: int,
    latest: bool,
) -> ToolOutputPageResponse:
    if isinstance(context, ConversationSnapshotContext):
        scope = context.scope
        session_refs = context.session_refs
        event_store = context.event_store
    else:
        scope = context["scope"]
        session_refs = context["session_refs"]
        event_store = context.get("event_store")
    conversation_id = scope.conversation_id
    if not conversation_id or not session_refs:
        raise api_exception(404, "not_found", "Tool output not found")

    event_data: dict[str, Any] | None = None
    storage_call_id = call_id
    resolved_session_id: str | None = None
    for ref in session_refs:
        before_seq: int | None = None
        visited_before_seq: set[int] = set()
        while True:
            is_disconnected = getattr(request, "is_disconnected", None)
            if callable(is_disconnected) and await is_disconnected():
                raise api_exception(499, "client_disconnected", "Client disconnected")
            reader = _required_ref_reader(ref, event_store)
            page = await reader.read_session_events(
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

    started = monotonic()
    trace = SnapshotRequestTrace()
    outcome: SnapshotRequestOutcome = "error"
    try:
        context = await _load_read_context(request, conversation_id)
        snapshot = await build_chat_snapshot_coordinated(
            request.app,
            context,
            request_trace=trace,
        )
        outcome = "success"
        return snapshot
    except CircuitBreakerError as exc:
        raise api_exception(
            503,
            "event_store_unavailable",
            "Session event store is temporarily unavailable",
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc
    finally:
        SNAPSHOT_CACHE_METRICS.request(trace.tier, outcome, monotonic() - started)


@router.get(
    "/conversations/{conversation_id}/snapshot/cache-only",
    response_model=ChatSnapshot,
    responses={204: {"description": "No warmed snapshot is available"}},
)
async def chat_v2_snapshot_cache_only(
    request: Request,
    conversation_id: str,
    response: Response,
) -> ChatSnapshot | Response:
    """Return only an authorized, already-warmed Chat v2 snapshot."""

    started = monotonic()
    outcome: CacheOnlyOutcome = "error"
    response.headers["Cache-Control"] = "private, no-store"
    try:
        context = await _load_read_context(request, conversation_id)
        snapshot, outcome = await get_cached_chat_snapshot_coordinated(request.app, context)
        if snapshot is None:
            return Response(
                status_code=204,
                headers={"Cache-Control": "private, no-store"},
            )
        return snapshot
    finally:
        CHAT_V2_REQUEST_METRICS.cache_only(outcome, monotonic() - started)


@router.post("/client-performance", status_code=204)
async def chat_v2_client_performance(request: Request) -> None:
    """Record one untrusted, best-effort, whitelisted browser UX timing."""

    require_session_user(request)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _CLIENT_PERFORMANCE_BODY_MAX_BYTES:
                raise api_exception(
                    413, "content_too_large", "Client performance body is too large"
                )
        except ValueError as exc:
            raise api_exception(400, "invalid_content_length", "Invalid Content-Length") from exc
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _CLIENT_PERFORMANCE_BODY_MAX_BYTES:
            raise api_exception(413, "content_too_large", "Client performance body is too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        payload = ClientPerformanceRequest.model_validate_json(body)
    except ValidationError as exc:
        raise api_exception(422, "validation_error", "Invalid client performance payload") from exc
    SNAPSHOT_CACHE_METRICS.client_performance(payload.metric, payload.duration_ms)


@router.get(
    "/conversations/{conversation_id}/work",
    response_model=WorkProjectionResponse,
)
async def chat_v2_conversation_work(
    request: Request,
    conversation_id: str,
    before: str | None = None,
    category: WorkCategory | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    filter_session_id: str | None = Query(default=None, alias="session_id"),
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=WORK_PAGE_MAX_LIMIT
    ),
) -> WorkProjectionResponse:
    """Project persisted mutation evidence for the conversation's current window."""

    context = await _load_read_context(request, conversation_id)
    return await _build_work_graph_projection(
        request,
        context,
        before=before,
        limit=limit,
        category=category if isinstance(category, str) else None,
        from_time=from_time if isinstance(from_time, datetime) else None,
        to_time=to_time if isinstance(to_time, datetime) else None,
        exact_session_id=filter_session_id if isinstance(filter_session_id, str) else None,
    )


@router.get(
    "/conversations/{conversation_id}/activity-overview",
    response_model=ActivityOverviewResponse,
)
async def chat_v2_conversation_activity_overview(
    request: Request,
    conversation_id: str,
    detail: ActivityOverviewDetail = Query(default="lightweight"),
) -> ActivityOverviewResponse:
    return await _build_activity_overview(
        request, await _load_read_context(request, conversation_id), detail=detail
    )


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

    started = monotonic()
    outcome: SyncOutcome = "error"
    context = await _load_read_context(request, conversation_id)
    try:
        result = await build_chat_sync_response(
            cursor=cursor,
            limit=limit,
            scope=context.scope,
            conversation=context.conversation,
            session_refs=context.session_refs,
            event_store=cast(SessionEventStore, context.event_store),
            cursor_secret=context.cursor_secret,
            queue=context.queue,
            state=context.state,
            runtime_input=context.runtime_input,
            event_post_processor=context.event_post_processor,
            session_cache=context.session_cache,
        )
        outcome = "reset" if getattr(result, "reset_required", False) else "success"
        return result
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
    finally:
        CHAT_V2_REQUEST_METRICS.sync(outcome, monotonic() - started)


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
            scope=context.scope,
            before=before,
            limit=limit,
            session_refs=context.session_refs,
            event_store=cast(SessionEventStore, context.event_store),
            cursor_secret=context.cursor_secret,
            event_post_processor=context.event_post_processor,
            session_cache=context.session_cache,
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
        if tx_row.status == "pending":
            if tx_row.payload_hash != payload_hash:
                _ensure_replayable_transaction(tx_row, payload_hash)
        else:
            _ensure_replayable_transaction(tx_row, payload_hash)
            return _send_response_from_transaction(tx_row, duplicate=True)

    normalized = normalize_user_message_content(payload.content)
    generated_attachments = await _persist_generated_text_artifacts(
        request,
        user_email=user.email,
        transaction_id=tx_row.transaction_id,
        artifacts=normalized.artifacts,
    )
    attachments = [*payload.attachments, *generated_attachments]
    turn_scheduler = request.app.state.turn_scheduler
    error = await turn_scheduler.submit_turn(
        conversation_id,
        normalized.content,
        user_email=user.email,
        attachments=[item.model_dump(mode="json") for item in attachments],
        client_message_id=payload.client_message_id,
        one_shot_chat_mode=payload.chat_mode,
        idempotency_scope=f"chat-v2:{conversation_id}:{user.email}",
        idempotency_key=client_txn_id,
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

    await _mark_attachments_attached(request, row, user.email, attachments)
    queued_message = _queued_message_for_client(
        await request.app.state.turn_scheduler.get_queued_messages(conversation_id),
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
        durable_running = getattr(scheduler, "durable_running_turn_state", None)
        runtime_turn = (
            await durable_running(conversation_id)
            if callable(durable_running)
            else scheduler.running_turn_state(conversation_id)
        )
        command_result = await request.app.state.command_dispatcher.dispatch(
            content,
            conversation=conversation,
            session=active_session,
            agent=agent,
            user_email=user.email,
            has_active_turn=runtime_turn is not None,
            has_busy_turn=runtime_turn is not None or scheduler.has_active_turn(conversation_id),
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

    if command_result.type == "system_message":
        command = command_result.data.get("command")
        if isinstance(command, str) and command in {"/profile", "/model", "/thinking", "/fast"}:
            command_result.data["notice_id"] = (
                f"command:{command.lstrip('/')}:{tx_row.transaction_id}"
            )
        notice_persisted = await persist_command_system_notice(
            conversation_id=conversation_id,
            result=command_result,
            providers=request.app.state.providers,
            session_cache=getattr(request.app.state, "session_cache", None),
            session=active_session,
            agent=agent,
            user_email=user.email,
        )
        if not notice_persisted:
            await _complete_transaction(
                request,
                tx_row.transaction_id,
                status="failed",
                error={
                    "code": "command_notice_persistence_failed",
                    "message": "Could not persist command feedback",
                    "http_status": 503,
                },
            )
            raise api_exception(
                503, "command_notice_persistence_failed", "Could not persist command feedback"
            )

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


@router.post(
    "/conversations/{conversation_id}/assistant-messages/fork",
    response_model=ForkAssistantMessageV2Response,
)
async def chat_v2_fork_assistant_message(
    request: Request,
    conversation_id: str,
    payload: ForkAssistantMessageV2Request,
) -> ForkAssistantMessageV2Response:
    """Fork a conversation retaining history through one assistant message."""

    user, conversation_row = await _require_mutable_conversation(request, conversation_id)

    from cognis.api.serializers import agent_to_response
    from cognis.core.session import _to_conversation_model, _to_session_model

    async with request.app.state.session_factory() as session:
        source_session_row = await get_session_row(session, payload.source_session_id)
        if (
            source_session_row is None
            or source_session_row.conversation_id != conversation_id
            or source_session_row.user_email != user.email
        ):
            raise api_exception(404, "not_found", "Assistant message not found")
        agent_row = await get_agent(session, conversation_row.agent_id)
        source_ref = await _session_read_ref(
            request,
            source_session_row,
            user_email=user.email,
            role="root",
            ordinal=0,
        )

    if agent_row is None:
        raise api_exception(404, "not_found", "Agent not found")

    source_session = _to_session_model(source_session_row)
    page = await _required_ref_reader(source_ref).read_session_events(
        session_id=source_ref.event_store_session_id,
        after_seq=payload.source_seq - 1 if payload.source_seq > 0 else None,
        limit=1,
    )
    event = page.events[0] if page.events and page.events[0].seq == payload.source_seq else None
    if event is None or not _is_assistant_message_event(event):
        raise api_exception(404, "not_found", "Assistant message not found")

    agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
    source_conversation = _to_conversation_model(conversation_row)
    (
        new_conversation,
        new_session,
        copied,
    ) = await request.app.state.session_manager.fork_into_new_conversation(
        source_session=source_session,
        source_conversation=source_conversation,
        agent=agent,
        user_email=user.email,
        title=(
            f"Fork: {source_conversation.title}" if source_conversation.title else "Forked chat"
        ),
        intention=f"Forked from assistant message in {agent.name}",
        max_source_seq=payload.source_seq,
        snapshot_extras={
            "trigger": "assistant_message_action",
            "forked_from_message_seq": payload.source_seq,
        },
    )
    if not copied:
        raise api_exception(500, "fork_failed", "Could not copy conversation history")

    return ForkAssistantMessageV2Response(
        conversation_id=new_conversation.conversation_id,
        session_id=new_session.session_id,
        source_session_id=payload.source_session_id,
        source_seq=payload.source_seq,
        copied=copied,
        server_time=_server_time(),
    )


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
        durable_running = getattr(turn_scheduler, "durable_running_turn_state", None)
        active_turn = (
            await durable_running(conversation_id)
            if callable(durable_running)
            else turn_scheduler.running_turn_state(conversation_id)
        )
        if active_turn is not None or turn_scheduler.has_active_turn(conversation_id):
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
            retry_reason=RetryReason.MANUAL_RETRY,
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
        await request.app.state.turn_scheduler.get_queued_messages(conversation_id)
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
        await request.app.state.turn_scheduler.get_queued_messages(conversation_id)
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


async def _load_read_context(request: Request, conversation_id: str) -> ConversationSnapshotContext:
    user = require_current_user(request)
    return await load_conversation_snapshot_context(
        request.app,
        user_email=user.email,
        conversation_id=conversation_id,
    )


async def _build_scoped_snapshot(
    request: Request,
    context: dict[str, Any] | ConversationSnapshotContext,
) -> ChatSnapshot:
    try:
        if isinstance(context, ConversationSnapshotContext):
            return await build_chat_snapshot_coordinated(request.app, context)
        snapshot = await build_chat_snapshot(**context)
        overview = await _build_activity_overview(request, context)
        return snapshot.model_copy(update={"activity_overview": overview})
    except CircuitBreakerError as exc:
        raise api_exception(
            503, "event_store_unavailable", "Session event store is temporarily unavailable"
        ) from exc
    except ChatV2SyncError as exc:
        raise api_exception(400, exc.code, str(exc)) from exc


async def _build_scoped_sync(
    request: Request,
    context: dict[str, Any] | ConversationSnapshotContext,
    *,
    cursor: str,
    limit: int,
) -> ChatSyncResponse:
    try:
        if isinstance(context, ConversationSnapshotContext):
            return await build_chat_sync_response(
                cursor=cursor,
                limit=limit,
                scope=context.scope,
                conversation=context.conversation,
                session_refs=context.session_refs,
                event_store=cast(SessionEventStore, context.event_store),
                cursor_secret=context.cursor_secret,
                queue=context.queue,
                state=context.state,
                runtime_input=context.runtime_input,
                event_post_processor=context.event_post_processor,
                session_cache=context.session_cache,
            )
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
    context: dict[str, Any] | ConversationSnapshotContext,
    *,
    before: str | None,
    limit: int,
) -> TimelineBackfillResponse:
    if isinstance(context, ConversationSnapshotContext):
        backfill_context = {
            "scope": context.scope,
            "session_refs": context.session_refs,
            "event_store": cast(SessionEventStore, context.event_store),
            "cursor_secret": context.cursor_secret,
            "event_post_processor": context.event_post_processor,
            "session_cache": context.session_cache,
        }
    else:
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
        session_rows, _truncated = await get_child_session_continuation_chain(session, session_id)

    current_session_row = session_rows[-1] if session_rows else session_row
    scope = TimelineScope(
        key=f"session:{session_id}",
        kind="session",
        conversation_id=session_row.conversation_id,
        session_id=session_id,
        parent_session_id=session_row.parent_session_id,
        label=session_row.delegation_task or session_row.agent_id,
        status=current_session_row.status,
    )
    return await _single_session_context(
        request,
        user_email=user.email,
        conversation_row=conversation_row,
        session_row=session_row,
        session_rows=session_rows,
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
            "event_store": None,
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
    return await _single_session_context(
        request,
        user_email=user.email,
        conversation_row=conversation_row,
        session_row=session_row,
        scope=scope,
    )


async def _single_session_context(
    request: Request,
    *,
    user_email: str,
    conversation_row: Any | None,
    session_row: Any,
    scope: TimelineScope,
    session_rows: list[Any] | None = None,
) -> dict[str, Any]:
    session_rows = session_rows or [session_row]
    current_session_row = session_rows[-1]
    conversation_id = str(session_row.conversation_id)
    runtime_input = await runtime_input_from_scheduler(
        conversation_id=conversation_id,
        scope_key=scope.key,
        active_session_id=current_session_row.session_id,
        turn_scheduler=getattr(request.app.state, "turn_scheduler", None),
        session_cache=getattr(request.app.state, "session_cache", None),
    )
    if (
        runtime_input.active_turn is not None
        and runtime_input.active_turn.get("session_id") != current_session_row.session_id
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
            await _session_read_ref(
                request,
                row,
                user_email=user_email,
                role="session",
                ordinal=ordinal,
            )
            for ordinal, row in enumerate(session_rows)
        ],
        "event_store": None,
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


@router.get("/sessions/{session_id}/work", response_model=WorkProjectionResponse)
async def chat_v2_session_work(
    request: Request,
    session_id: str,
    before: str | None = None,
    category: WorkCategory | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    filter_session_id: str | None = Query(default=None, alias="session_id"),
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=WORK_PAGE_MAX_LIMIT
    ),
) -> WorkProjectionResponse:
    context = await _load_session_context(request, session_id)
    return await _build_work_graph_projection(
        request,
        context,
        before=before,
        limit=limit,
        category=category if isinstance(category, str) else None,
        from_time=from_time if isinstance(from_time, datetime) else None,
        to_time=to_time if isinstance(to_time, datetime) else None,
        exact_session_id=filter_session_id if isinstance(filter_session_id, str) else None,
    )


@router.get(
    "/sessions/{session_id}/activity-overview",
    response_model=ActivityOverviewResponse,
)
async def chat_v2_session_activity_overview(
    request: Request,
    session_id: str,
    detail: ActivityOverviewDetail = Query(default="lightweight"),
) -> ActivityOverviewResponse:
    return await _build_activity_overview(
        request, await _load_session_context(request, session_id), detail=detail
    )


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


@router.get("/task-steps/{step_run_id}/work", response_model=WorkProjectionResponse)
async def chat_v2_task_step_work(
    request: Request,
    step_run_id: str,
    before: str | None = None,
    category: WorkCategory | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    filter_session_id: str | None = Query(default=None, alias="session_id"),
    limit: int = Query(
        default=BACKFILL_DEFAULT_LIMIT, ge=BACKFILL_MIN_LIMIT, le=WORK_PAGE_MAX_LIMIT
    ),
) -> WorkProjectionResponse:
    context = await _load_task_step_context(request, step_run_id)
    return await _build_work_graph_projection(
        request,
        context,
        before=before,
        limit=limit,
        category=category if isinstance(category, str) else None,
        from_time=from_time if isinstance(from_time, datetime) else None,
        to_time=to_time if isinstance(to_time, datetime) else None,
        exact_session_id=filter_session_id if isinstance(filter_session_id, str) else None,
    )


@router.get(
    "/task-steps/{step_run_id}/activity-overview",
    response_model=ActivityOverviewResponse,
)
async def chat_v2_task_step_activity_overview(
    request: Request,
    step_run_id: str,
    detail: ActivityOverviewDetail = Query(default="lightweight"),
) -> ActivityOverviewResponse:
    return await _build_activity_overview(
        request, await _load_task_step_context(request, step_run_id), detail=detail
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
        for event in events:
            attachments = event.data.get("attachments")
            if not isinstance(attachments, list):
                hydrated_events.append(event)
                continue
            session_id = event.data.get("cognis_session_id")
            hydrated = await hydrate_attachment_refs(
                request.app.state.session_factory,
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
    attachments: list[AttachmentRef],
) -> None:
    try:
        async with request.app.state.session_factory() as session:
            latest_row = await get_conversation(session, conversation_row.conversation_id)
            await mark_artifacts_attached(
                session,
                [item.artifact_id for item in attachments],
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


async def _persist_generated_text_artifacts(
    request: Request,
    *,
    user_email: str,
    transaction_id: str,
    artifacts: tuple[TextArtifact, ...],
) -> list[AttachmentRef]:
    """Persist server-generated text as temporary chat attachments."""

    if not artifacts:
        return []

    artifact_store = request.app.state.artifact_store
    persisted: list[AttachmentRef] = []
    for index, artifact in enumerate(artifacts):
        content = artifact.content.encode("utf-8")
        if len(content) > artifact_store._config.max_size_bytes:  # noqa: SLF001
            raise api_exception(400, "validation_error", "Generated message artifact is too large")
        digest = hashlib.sha256(f"{transaction_id}:{index}".encode()).hexdigest()
        artifact_id = f"att_message_{digest[:24]}"
        await artifact_store.async_save(
            "attachments",
            artifact_id,
            artifact.filename,
            content,
            "text/plain; charset=utf-8",
            owner_email=user_email,
        )
        async with request.app.state.session_factory() as session:
            if await get_artifact_record(session, artifact_id) is None:
                try:
                    await create_artifact_record(
                        session,
                        artifact_id=artifact_id,
                        namespace="attachments",
                        object_id=artifact_id,
                        filename=artifact.filename,
                        owner_email=user_email,
                        purpose="chat_input",
                        kind=ArtifactKind.FILE.value,
                        mime_type="text/plain; charset=utf-8",
                        size_bytes=len(content),
                        expires_at=datetime.now(UTC) + timedelta(hours=24),
                        content_hash=hashlib.sha256(content).hexdigest(),
                    )
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
            else:
                await session.commit()
        url = await artifact_store.async_get_public_url(
            "attachments",
            artifact_id,
            artifact.filename,
        )
        persisted.append(
            AttachmentRef(
                artifact_id=artifact_id,
                kind=ArtifactKind.FILE,
                filename=artifact.filename,
                mime_type="text/plain; charset=utf-8",
                size_bytes=len(content),
                url=url,
            )
        )
    return persisted


def _payload_hash(operation: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _server_time() -> str:
    return datetime.now(UTC).isoformat()


def _is_assistant_message_event(event: RawSessionEvent) -> bool:
    if event.type == "assistant_message":
        return True
    return event.type == "message" and event.data.get("role") == "assistant"


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
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        session_refs = await _session_refs(
            request,
            session,
            conversation_row.conversation_id,
            conversation_row.active_session_id,
            user_email=user.email,
        )
    failed_turn_found = False
    completed_turn_found = False
    user_event: RawSessionEvent | None = None
    for ref in reversed(session_refs):
        before_seq: int | None = None
        for _ in range(20):
            page = await _required_ref_reader(ref).read_session_events(
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
    request: Request,
    session: Any,
    conversation_id: str,
    active_session_id: str | None,
    *,
    user_email: str,
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
        await _session_read_ref(
            request,
            row,
            user_email=user_email,
            role="root",
            ordinal=index,
        )
        for index, row in enumerate(chain)
    ]


async def _session_read_ref(
    request: Request,
    session_row: Any,
    *,
    user_email: str,
    role: str,
    ordinal: int,
) -> ConversationSessionRef:
    if session_row.user_email != user_email:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session event-store authority does not match the authorized user",
        )
    agent = await request.app.state.agent_registry.get(
        session_row.agent_id,
        owner_email=user_email,
        include_disabled=True,
    )
    agent_owner_email = agent.owner_email if agent is not None else None
    if not agent_owner_email:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session agent authority is unavailable",
        )
    authority = EventStoreAuthority(
        user_email=user_email,
        agent_id=session_row.agent_id,
        agent_owner_email=agent_owner_email,
    )
    cached_store = getattr(request.app.state, "cached_event_store", None)
    if cached_store is None or not callable(getattr(cached_store, "bind", None)):
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Cached session event store is not configured",
        )
    reader = cached_store.bind(authority)
    authority_token = getattr(reader, "authority_token", None)
    if not isinstance(authority_token, str) or not authority_token:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Cached session event-store authority token is unavailable",
        )
    return ConversationSessionRef(
        session_id=session_row.session_id,
        event_store_session_id=session_row.intaris_session_id or session_row.session_id,
        store="intaris",
        role=role,
        ordinal=ordinal,
        status=session_row.status,
        completion_reason=session_row.completion_reason,
        reader=reader,
        authority_token=authority_token,
    )


def _required_ref_reader(
    ref: ConversationSessionRef,
    fallback: SessionEventStore | None = None,
) -> SessionEventStore:
    reader = getattr(ref, "reader", None) or fallback
    if reader is None:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session event-store authority is unavailable",
        )
    return reader


async def _build_work_graph_projection_with_stages(
    request: Request,
    context: dict[str, Any] | ConversationSnapshotContext,
    *,
    before: str | None,
    limit: int,
    category: WorkCategory | None,
    from_time: datetime | None,
    to_time: datetime | None,
    exact_session_id: str | None,
) -> WorkProjectionResponse:
    from_time, to_time = _normalized_work_range(from_time, to_time)
    user = require_current_user(request)
    scope = context.scope if isinstance(context, ConversationSnapshotContext) else context["scope"]
    definitions = _work_tool_definitions(request)
    revision = WorkRevisionSnapshot(scope_key=scope.key, work_revision=0, graph_revision=0)
    async with request.app.state.session_factory() as db:
        try:
            graph = await resolve_authorized_work_graph(
                db,
                user_email=user.email,
                scope=scope,
                deadline=monotonic() + WORK_GRAPH_MAX_SECONDS,
            )
        except ChatV2SyncError as exc:
            status = 503 if exc.code == "work_graph_timeout" else 400
            raise api_exception(status, exc.code, str(exc)) from exc
        try:
            page = await read_work_page(
                db,
                owner_email=user.email,
                scope=scope,
                session_rows=graph.session_rows,
                graph_fingerprint=graph.fingerprint,
                cursor_secret=(
                    context.cursor_secret
                    if isinstance(context, ConversationSnapshotContext)
                    else context["cursor_secret"]
                ),
                before=before,
                limit=limit,
                category=category,
                from_time=from_time,
                to_time=to_time,
                tool_definitions=definitions,
                exact_session_id=exact_session_id,
            )
        except WorkCursorError as exc:
            raise api_exception(
                400,
                "work_cursor_invalid",
                str(exc),
            ) from exc
        root_node = next(
            (node for node in graph.nodes if node.key == node.root_key),
            None,
        )
        session_by_id = {row.session_id: row for row in graph.session_rows}
        root_rotation_ids: set[str] = set()
        cursor_session_id = root_node.session_id if root_node else None
        while cursor_session_id and cursor_session_id not in root_rotation_ids:
            row = session_by_id.get(cursor_session_id)
            if row is None or row.user_email != user.email:
                break
            root_rotation_ids.add(cursor_session_id)
            cursor_session_id = row.previous_session_id
        root_nodes = [node for node in graph.nodes if node.session_id in root_rotation_ids]
        root_session_ids = {node.session_id for node in root_nodes}
        root_step_ids = {node.step_run_id for node in root_nodes if node.step_run_id}
        candidates: list[DeliverableRow] = []
        if root_session_ids:
            candidates.extend(
                (
                    await db.scalars(
                        select(DeliverableRow)
                        .where(
                            DeliverableRow.session_id.in_(root_session_ids),
                            DeliverableRow.status.in_(["buffered", "approved", "delivered"]),
                        )
                        .order_by(
                            DeliverableRow.created_at.desc(),
                            DeliverableRow.version.desc(),
                        )
                        .limit(1)
                    )
                ).all()
            )
        if root_step_ids:
            candidates.extend(
                (
                    await db.scalars(
                        select(DeliverableRow)
                        .where(
                            DeliverableRow.step_run_id.in_(root_step_ids),
                            DeliverableRow.status.in_(["buffered", "approved", "delivered"]),
                        )
                        .order_by(
                            DeliverableRow.created_at.desc(),
                            DeliverableRow.version.desc(),
                        )
                        .limit(1)
                    )
                ).all()
            )
        primary_row = max(
            candidates,
            key=lambda row: (row.created_at, row.version),
            default=None,
        )
        if primary_row is not None:
            await hydrate_deliverable_payload(
                primary_row,
                request.app.state.artifact_store,
            )
        primary_materialized = bool(
            primary_row is not None
            and await db.scalar(
                select(WorkRecordRow.work_record_id)
                .where(
                    WorkRecordRow.owner_email == user.email,
                    WorkRecordRow.session_id.in_(
                        [row.session_id for row in graph.session_rows] or [""]
                    ),
                    WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkRecordRow.is_evidence.is_(True),
                    WorkRecordRow.category == "deliverables",
                    WorkRecordRow.entity_id == primary_row.deliverable_id,
                    *((WorkRecordRow.occurred_at >= from_time,) if from_time is not None else ()),
                    *((WorkRecordRow.occurred_at < to_time,) if to_time is not None else ()),
                )
                .limit(1)
            )
        )
        await db.commit()
    materializer = getattr(request.app.state, "work_materializer", None)
    if materializer is not None and page.materialization.state != "caught_up":
        await materializer.prioritize_sessions(graph.session_rows)
    projection = _work_from_page(
        request,
        page,
        graph=graph,
        revision=revision,
        definitions=definitions,
    )
    primary_created_at = primary_row.created_at if primary_row is not None else None
    if primary_created_at is not None and primary_created_at.tzinfo is None:
        primary_created_at = primary_created_at.replace(tzinfo=UTC)
    primary_in_range = bool(
        primary_row is not None
        and primary_created_at is not None
        and (from_time is None or primary_created_at >= from_time)
        and (to_time is None or primary_created_at < to_time)
    )
    primary_missing = bool(primary_row is not None and not primary_materialized)
    if primary_row is not None and (
        category is None
        or (category == "deliverables" and before is None and primary_in_range and primary_missing)
    ):
        source = next(
            (
                node
                for node in graph.nodes
                if node.session_id == primary_row.session_id
                or node.step_run_id == primary_row.step_run_id
            ),
            None,
        )
        primary = WorkDeliverable(
            deliverable_id=primary_row.deliverable_id,
            sort_key="",
            format=primary_row.format,
            title=primary_row.title,
            content=primary_row.content,
            content_preview_truncated=False,
            recoverable=True,
            render_metadata=primary_row.render_metadata,
            export_metadata=primary_row.export_metadata,
            source_workstream=source,
        )
        if primary is not None:
            deliverables = [
                item
                for item in projection.deliverables
                if item.deliverable_id != primary.deliverable_id
            ]
            projection = projection.model_copy(
                update={
                    "final_deliverable": primary,
                    "deliverables": [primary, *deliverables],
                    "summary": (
                        projection.summary.model_copy(
                            update={"deliverables": projection.summary.deliverables + 1}
                        )
                        if category is not None
                        else projection.summary.model_copy(
                            update={"deliverables": len(deliverables) + 1}
                        )
                    ),
                }
            )
    elif (
        category == "deliverables"
        and primary_row is not None
        and primary_in_range
        and primary_missing
    ):
        projection = projection.model_copy(
            update={
                "summary": projection.summary.model_copy(
                    update={"deliverables": projection.summary.deliverables + 1}
                )
            }
        )
    return projection


async def _build_work_graph_projection(
    request: Request,
    context: dict[str, Any] | ConversationSnapshotContext,
    *,
    before: str | None,
    limit: int,
    category: WorkCategory | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    exact_session_id: str | None = None,
) -> WorkProjectionResponse:
    """Bound the complete Work request while each I/O stage keeps its own budget."""

    try:
        async with asyncio.timeout(WORK_REQUEST_MAX_SECONDS):
            return await _build_work_graph_projection_with_stages(
                request,
                context,
                before=before,
                limit=limit,
                category=category,
                from_time=from_time,
                to_time=to_time,
                exact_session_id=exact_session_id,
            )
    except TimeoutError as exc:
        raise api_exception(
            503,
            "work_request_timeout",
            "Work projection exceeded the total request deadline",
        ) from exc


async def _build_activity_overview(
    request: Request,
    context: dict[str, Any] | ConversationSnapshotContext,
    *,
    detail: ActivityOverviewDetail = "lightweight",
) -> ActivityOverviewResponse:
    user = require_current_user(request)
    scope = context.scope if isinstance(context, ConversationSnapshotContext) else context["scope"]
    async with request.app.state.session_factory() as db:
        graph = await resolve_authorized_work_graph(
            db,
            user_email=user.email,
            scope=scope,
            deadline=monotonic() + WORK_GRAPH_MAX_SECONDS,
        )
        return await read_activity_overview(
            db,
            owner_email=user.email,
            scope=scope,
            session_rows=list(graph.session_rows),
            workstreams=list(graph.nodes),
            graph_fingerprint=graph.fingerprint,
            graph_truncated=graph.truncated,
            tool_definitions=_work_tool_definitions(request),
            detail=detail,
        )


def _work_from_page(
    request: Request,
    page: Any,
    *,
    graph: AuthorizedWorkGraph | None = None,
    revision: WorkRevisionSnapshot | None = None,
    definitions: dict[str, Any] | None = None,
) -> WorkProjectionResponse:
    definitions = definitions or _work_tool_definitions(request)
    return build_work_projection(
        scope=page.scope,
        projection_version=page.projection_version,
        items=page.items if hasattr(page, "items") else page.timeline.items,
        tool_definitions=definitions,
        has_more_before=(
            page.has_more_before if hasattr(page, "items") else page.timeline.has_more_before
        ),
        before_cursor=(
            page.before_cursor if hasattr(page, "items") else page.timeline.before_cursor
        ),
        server_time=page.server_time,
        workstreams=graph.nodes if graph is not None else (),
        graph_fingerprint=graph.fingerprint if graph is not None else None,
        graph_truncated=graph.truncated if graph is not None else False,
        work_revision=revision.work_revision if revision is not None else 0,
        graph_revision=revision.graph_revision if revision is not None else 0,
        materialization=getattr(page, "materialization", None),
        removed_call_ids=getattr(page, "removed_call_ids", ()),
        summary=getattr(page, "summary", None),
        newest_first=getattr(page, "category", None) is not None,
        complete_files=getattr(page, "category", None) == "files",
    )


def _normalized_work_range(
    from_time: datetime | None,
    to_time: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    for value in (from_time, to_time):
        if value is not None and value.tzinfo is None:
            raise api_exception(
                422,
                "work_time_range_invalid",
                "Work time filters must include a timezone",
            )
    normalized_from = from_time.astimezone(UTC) if from_time else None
    normalized_to = to_time.astimezone(UTC) if to_time else None
    if normalized_from and normalized_to and normalized_from > normalized_to:
        raise api_exception(
            422,
            "work_time_range_invalid",
            "Work time range start must not be after its end",
        )
    return normalized_from, normalized_to


def _work_from_snapshot(request: Request, snapshot: ChatSnapshot) -> WorkProjectionResponse:
    return _work_from_page(request, snapshot)


def _work_tool_definitions(request: Request) -> dict[str, Any]:
    registry = getattr(request.app.state, "tool_registry", None)
    return {
        definition.name: definition
        for definition in (registry.list_tools() if registry is not None else [])
    }


def _cursor_secret(request: Request) -> str:
    secret = getattr(request.app.state, "chat_v2_cursor_secret", None)
    if isinstance(secret, str) and secret:
        return secret
    raise api_exception(
        500,
        "cursor_secret_unavailable",
        "Chat v2 cursor signing secret is not configured",
    )
