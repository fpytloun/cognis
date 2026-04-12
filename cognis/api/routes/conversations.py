"""Conversation routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.models import (
    ConversationCreateRequest,
    ConversationResolveRequest,
    ConversationResponse,
    ConversationUpdateRequest,
    CursorPage,
    MessageHistoryResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionEventsResponse,
    SessionResponse,
)
from cognis.api.serializers import (
    conversation_to_response,
    serialize_event_rows,
    session_to_response,
)
from cognis.core.turn_scheduler import TurnError
from cognis.logging import get_logger
from cognis.models.session import ConversationContext
from cognis.store.queries import (
    get_agent,
    get_artifact_record,
    get_conversation,
    get_latest_active_conversation_for_agent,
    get_root_session_chain,
    get_session_row,
    list_conversation_sessions,
    list_conversations,
    mark_artifacts_attached,
    mark_conversation_read,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=CursorPage[ConversationResponse])
async def conversation_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    context_type: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
) -> CursorPage[ConversationResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_conversations(session, user.email, context_type=context_type)
    if agent_id is not None:
        rows = [row for row in rows if row.agent_id == agent_id]
    items = [conversation_to_response(row) for row in rows]
    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.conversation_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


@router.post("/resolve", response_model=ConversationResponse)
async def resolve_conversation(
    request: Request,
    payload: ConversationResolveRequest,
) -> ConversationResponse:
    """Find an existing conversation for the given agent and context type, or create one.

    This is the "persistent channel" endpoint: the web UI calls it to ensure
    there is always a default conversation for a given agent.
    """
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, agent.owner_email)
        existing = await get_latest_active_conversation_for_agent(
            session,
            user.email,
            payload.agent_id,
            context_type=payload.context_type,
        )
    if existing is not None:
        return conversation_to_response(existing)
    context_ref = f"{payload.context_type}:user:{user.email}:default"
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        context=ConversationContext(
            type=payload.context_type,
            ref=context_ref,
            platform_data={},
            memory_labels={},
        ),
        title=None,
    )
    return conversation_to_response(conversation)


@router.post("", response_model=ConversationResponse)
async def create_conversation(
    request: Request,
    payload: ConversationCreateRequest,
) -> ConversationResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, agent.owner_email)
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        context=ConversationContext(
            type=payload.context.type,
            ref=payload.context.ref,
            platform_data=payload.context.platform_data,
            memory_labels=payload.context.memory_labels,
        ),
        title=payload.title,
        title_source="manual" if payload.title else "unset",
    )
    return conversation_to_response(conversation)


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def conversation_detail(request: Request, conversation_id: str) -> ConversationResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)
    return conversation_to_response(row)


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    request: Request,
    conversation_id: str,
    payload: ConversationUpdateRequest,
) -> ConversationResponse:
    forbid_mutation_for_viewer(request)
    manager = request.app.state.session_manager
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_owner_or_admin(request, row.user_email)
        if payload.archived is True:
            await manager.archive_conversation(conversation_id)
        elif payload.archived is False and row.status == "archived":
            row.status = "active"
        if payload.title is not None:
            row.title = payload.title
            row.title_source = "manual" if payload.title.strip() else "unset"
        await session.commit()
        await session.refresh(row)
        return conversation_to_response(row)


@router.post("/{conversation_id}/read", response_model=dict)
async def mark_read(request: Request, conversation_id: str) -> dict[str, bool]:
    """Mark a conversation as read (sets last_read_at to now)."""
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_owner_or_admin(request, row.user_email)
        await mark_conversation_read(session, conversation_id)
        await session.commit()
    return {"ok": True}


@router.delete("/{conversation_id}", response_model=dict)
async def delete_conversation(request: Request, conversation_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)
    ok = await request.app.state.session_manager.soft_delete_conversation(conversation_id)
    return {"ok": ok}


@router.delete("/{conversation_id}/purge", response_model=dict)
async def purge_conversation(request: Request, conversation_id: str) -> dict[str, object]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        sessions = (
            await list_conversation_sessions(session, conversation_id) if row is not None else []
        )
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)
    ok = await request.app.state.session_manager.purge_conversation(conversation_id)
    delete_session = getattr(request.app.state.providers.guardrails, "delete_session", None)
    if not callable(delete_session):
        return {
            "ok": ok,
            "intaris_cascade": False,
            "warning": "Intaris purge is not supported by the current provider contract.",
        }

    cascade_ok = True
    for session_row in sessions:
        intaris_session_id = session_row.intaris_session_id or session_row.session_id
        try:
            await delete_session(intaris_session_id)
        except Exception:
            cascade_ok = False
            break
    return {
        "ok": ok,
        "intaris_cascade": cascade_ok,
        "warning": None if cascade_ok else "Intaris session purge failed for one or more sessions.",
    }


@router.get("/{conversation_id}/messages", response_model=MessageHistoryResponse)
async def conversation_messages(
    request: Request,
    conversation_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> MessageHistoryResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_owner_or_admin(request, row.user_email)
        if row.active_session_id is None:
            return MessageHistoryResponse(items=[], last_seq=0, has_more=False)

        # Incremental fetch (after_seq > 0): read only the active session.
        # Full load (after_seq == 0): walk the root-session lineage and
        # merge events from all root sessions (oldest first).
        lineage_truncated = False
        if after_seq > 0:
            session_row = await get_session_row(session, row.active_session_id)
            session_rows = [session_row] if session_row is not None else []
        else:
            session_rows, lineage_truncated = await get_root_session_chain(
                session, conversation_id, row.active_session_id
            )

    if not session_rows:
        return MessageHistoryResponse(items=[], last_seq=0, has_more=False)

    guardrails = request.app.state.providers.guardrails

    # Read events from each session in the chain (parallel for full load)
    all_events: list[dict[str, Any]] = []
    last_seq_value = 0
    has_more = False
    active_session_id = session_rows[-1].session_id if session_rows else None
    active_session_last_seq = 0
    history_truncated = False
    truncation_reason: str | None = None

    if after_seq > 0:
        # Incremental: single session read
        sr = session_rows[0]
        event_result = await guardrails.read_events(
            session_id=sr.intaris_session_id or sr.session_id,
            after_seq=after_seq,
            limit=limit,
            allow_missing_stream=True,
        )
        if event_result.missing_stream_fallback_used:
            logger.warning(
                "Conversation history missing in Intaris; returning empty history",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": sr.session_id,
                        "intaris_session_id": sr.intaris_session_id or sr.session_id,
                    }
                },
            )
        all_events = list(event_result.events)
        last_seq_value = event_result.last_seq
        has_more = event_result.has_more
        active_session_last_seq = event_result.last_seq
    else:
        # Full load: read all sessions in parallel
        import asyncio as _asyncio

        async def _read_session(sr: Any) -> tuple[Any, list[dict[str, Any]], int]:
            try:
                result = await guardrails.read_events(
                    session_id=sr.intaris_session_id or sr.session_id,
                    after_seq=0,
                    limit=0,
                    allow_missing_stream=True,
                )
                if result.missing_stream_fallback_used:
                    logger.warning(
                        "Session stream missing in Intaris during lineage read",
                        extra={
                            "extra_data": {
                                "conversation_id": conversation_id,
                                "session_id": sr.session_id,
                            }
                        },
                    )
                    return (
                        sr,
                        [
                            {
                                "type": "history_gap",
                                "data": {
                                    "reason": "stream_missing",
                                    "session_id": sr.session_id,
                                },
                                "seq": 0,
                                "ts": None,
                            }
                        ],
                        result.last_seq,
                    )
                return sr, list(result.events), result.last_seq
            except Exception:
                logger.warning(
                    "Failed to read session events during lineage walk",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": sr.session_id,
                        }
                    },
                    exc_info=True,
                )
                return (
                    sr,
                    [
                        {
                            "type": "history_gap",
                            "data": {
                                "reason": "read_failed",
                                "session_id": sr.session_id,
                            },
                            "seq": 0,
                            "ts": None,
                        }
                    ],
                    0,
                )

        results = await _asyncio.gather(*[_read_session(sr) for sr in session_rows])

        if lineage_truncated:
            history_truncated = True
            truncation_reason = "lineage_truncated"
            all_events.append(
                {
                    "type": "history_gap",
                    "data": {"reason": "lineage_truncated"},
                    "seq": 0,
                    "ts": None,
                }
            )

        for sr, events, session_last_seq in results:
            # Tag each event with session_id so the UI can build
            # lineage-safe timeline item IDs (seq is session-local).
            sid = sr.session_id
            if sid == active_session_id:
                active_session_last_seq = session_last_seq
            for event in events:
                if isinstance(event, dict):
                    data = event.get("data")
                    if isinstance(data, dict) and "session_id" not in data:
                        data["session_id"] = sid
            all_events.extend(events)

        # For full loads, return the full lineage history and let the
        # client switch to incremental mode afterward using the active
        # session's seq space. Avoid single-session cursor semantics here.
        has_more = False

    # Enrich attachment URLs
    artifact_store = request.app.state.artifact_store
    async with request.app.state.session_factory() as artifact_session:
        for event in all_events:
            data = event.get("data") if isinstance(event, dict) else None
            attachments = data.get("attachments") if isinstance(data, dict) else None
            if not isinstance(attachments, list):
                continue
            refreshed: list[dict[str, Any]] = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                artifact_id = attachment.get("artifact_id")
                if not isinstance(artifact_id, str):
                    continue
                artifact_row = await get_artifact_record(artifact_session, artifact_id)
                if artifact_row is None or artifact_row.status == "deleted":
                    continue
                refreshed.append(
                    {
                        **attachment,
                        "filename": artifact_row.filename,
                        "mime_type": artifact_row.mime_type,
                        "size_bytes": artifact_row.size_bytes,
                        "url": await artifact_store.async_get_public_url(
                            artifact_row.namespace,
                            artifact_row.object_id,
                            artifact_row.filename,
                        ),
                    }
                )
            data["attachments"] = refreshed

    return MessageHistoryResponse(
        items=serialize_event_rows(
            all_events,
            log_label="conversation_messages",
            log_context={
                "conversation_id": conversation_id,
                "session_id": session_rows[-1].session_id if session_rows else "",
            },
        ),
        last_seq=last_seq_value,
        has_more=has_more,
        active_session_id=active_session_id,
        active_session_last_seq=active_session_last_seq,
        history_truncated=history_truncated,
        truncation_reason=truncation_reason,
    )


@router.post("/{conversation_id}/messages")
async def send_message(
    request: Request,
    conversation_id: str,
    payload: SendMessageRequest,
) -> Response:
    """Send a chat message to a conversation.

    Supports two delivery modes via the ``Accept`` header:

    - ``Accept: text/event-stream`` — SSE streaming response with real-time
      token deltas, tool calls, and turn completion events.
    - ``Accept: application/json`` (default) — fire-and-forget 202 Accepted.
      Poll ``GET /conversations/{id}/messages`` for the response.

    Slash commands (``/compact``, ``/new``, ``/model``, etc.) are dispatched
    through the ``CommandDispatcher`` and return their result directly.
    """
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)

    # --- Slash command dispatch ---
    command_result = await _try_command_dispatch(request, conversation_id, payload.content, user)
    if command_result is not None:
        return JSONResponse(
            status_code=200,
            content={"status": "command_executed", "result": command_result},
        )

    # --- Turn submission ---
    accept = request.headers.get("accept", "application/json")
    wants_sse = "text/event-stream" in accept
    turn_scheduler = request.app.state.turn_scheduler

    if wants_sse:
        from cognis.api.sse import SSETurnObserver

        observer = SSETurnObserver(conversation_id)
        error = await turn_scheduler.submit_turn(
            conversation_id,
            payload.content,
            user_email=user.email,
            attachments=[item.model_dump(mode="json") for item in payload.attachments],
            turn_observers=[observer],
        )
        if error is not None:
            raise _turn_error_to_http(error)
        try:
            async with request.app.state.session_factory() as session:
                latest_row = await get_conversation(session, conversation_id)
                await mark_artifacts_attached(
                    session,
                    [item.artifact_id for item in payload.attachments],
                    owner_email=user.email,
                    conversation_id=conversation_id,
                    session_id=latest_row.active_session_id if latest_row else None,
                )
                await session.commit()
        except Exception:
            logger.warning(
                "Failed to persist post-submit attachment association",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )

        async def _cleanup_generator():  # type: ignore[return]
            try:
                async for event in observer.event_generator():
                    yield event
            finally:
                turn_scheduler.remove_observer(conversation_id, observer)

        return StreamingResponse(
            _cleanup_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        error = await turn_scheduler.submit_turn(
            conversation_id,
            payload.content,
            user_email=user.email,
            attachments=[item.model_dump(mode="json") for item in payload.attachments],
        )
        if error is not None:
            raise _turn_error_to_http(error)
        try:
            async with request.app.state.session_factory() as session:
                latest_row = await get_conversation(session, conversation_id)
                await mark_artifacts_attached(
                    session,
                    [item.artifact_id for item in payload.attachments],
                    owner_email=user.email,
                    conversation_id=conversation_id,
                    session_id=latest_row.active_session_id if latest_row else None,
                )
                await session.commit()
        except Exception:
            logger.warning(
                "Failed to persist post-submit attachment association",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
        return JSONResponse(
            status_code=202,
            content=SendMessageResponse(status="accepted").model_dump(),
        )


async def _try_command_dispatch(
    request: Request,
    conversation_id: str,
    content: str,
    user: Any,
) -> dict[str, Any] | None:
    """Try to dispatch a slash command. Returns result dict or None."""
    command_dispatcher = getattr(request.app.state, "command_dispatcher", None)
    if command_dispatcher is None:
        return None
    if not content.strip().startswith("/"):
        return None

    from cognis.api.serializers import agent_to_response
    from cognis.core.session import _to_conversation_model, _to_session_model
    from cognis.models.agent import AgentDefinition

    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        if conversation_row is None:
            return None
        agent_row = await get_agent(session, conversation_row.agent_id)
        if agent_row is None:
            return None
        agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation_model = _to_conversation_model(conversation_row)
        session_row = (
            await get_session_row(session, conversation_row.active_session_id)
            if conversation_row.active_session_id
            else None
        )

    if session_row is None:
        return None
    session_model = _to_session_model(session_row)

    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    has_active = turn_scheduler.has_active_turn(conversation_id) if turn_scheduler else False

    cmd_result = await command_dispatcher.dispatch(
        content,
        conversation=conversation_model,
        session=session_model,
        agent=agent_model,
        user_email=user.email,
        has_active_turn=has_active,
    )
    if cmd_result is None:
        return None

    return {
        "type": cmd_result.type,
        "text": cmd_result.text,
        "data": cmd_result.data,
    }


def _turn_error_to_http(error: TurnError) -> Exception:
    """Map a TurnError to an HTTP exception."""
    status_map: dict[str, int] = {
        "not_found": 404,
        "forbidden": 403,
        "session_ended": 409,
        "session_suspended": 409,
        "conflict": 409,
        "rate_limited": 429,
        "queue_full": 429,
    }
    status = status_map.get(error.code, 500)
    return api_exception(status, error.code, error.message)


@router.get("/{conversation_id}/sessions", response_model=list[SessionResponse])
async def conversation_sessions(request: Request, conversation_id: str) -> list[SessionResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_owner_or_admin(request, row.user_email)
        sessions = await list_conversation_sessions(session, conversation_id)
    return [session_to_response(item) for item in sessions]


@router.get("/{conversation_id}/delegations", response_model=list[SessionResponse])
async def active_delegations(request: Request, conversation_id: str) -> list[SessionResponse]:
    sessions = await conversation_sessions(request, conversation_id)
    return [
        item for item in sessions if item.parent_session_id is not None and item.status == "active"
    ]


@router.get(
    "/{conversation_id}/sessions/{session_id}/events",
    response_model=SessionEventsResponse,
)
async def session_events(
    request: Request,
    conversation_id: str,
    session_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> SessionEventsResponse:
    """Read events for a specific session within a conversation.

    Used by the sub-session panel to fetch a child session's event stream.
    """
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_owner_or_admin(request, row.user_email)
        session_row = await get_session_row(session, session_id)
    if session_row is None or session_row.conversation_id != conversation_id:
        raise api_exception(404, "not_found", "Session not found in this conversation")
    event_result = await request.app.state.providers.guardrails.read_events(
        session_id=session_row.intaris_session_id or session_row.session_id,
        after_seq=after_seq,
        limit=limit,
        allow_missing_stream=True,
    )
    if event_result.missing_stream_fallback_used:
        logger.warning(
            "Conversation session history missing in Intaris; returning empty history",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "session_id": session_row.session_id,
                    "intaris_session_id": session_row.intaris_session_id or session_row.session_id,
                }
            },
        )
    return SessionEventsResponse(
        session_id=session_id,
        items=serialize_event_rows(
            event_result.events,
            log_label="conversation_session_events",
            log_context={
                "conversation_id": conversation_id,
                "session_id": session_row.session_id,
            },
        ),
        last_seq=event_result.last_seq,
        has_more=event_result.has_more,
    )
