"""Conversation routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.models import (
    ConversationCreateRequest,
    ConversationResponse,
    ConversationUpdateRequest,
    CursorPage,
    MessageHistoryResponse,
    SessionResponse,
)
from cognis.api.serializers import conversation_to_response, event_to_response, session_to_response
from cognis.models.session import ConversationContext
from cognis.store.queries import (
    get_agent,
    get_conversation,
    get_session_row,
    list_conversation_sessions,
    list_conversations,
)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=CursorPage[ConversationResponse])
async def conversation_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> CursorPage[ConversationResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_conversations(session, user.email)
    items = [conversation_to_response(row) for row in rows]
    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.conversation_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


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
    (
        conversation,
        _root_session,
    ) = await request.app.state.session_manager.create_conversation_with_root_session(
        user_email=user.email,
        agent_id=payload.agent_id,
        context=ConversationContext(
            type=payload.context.type,
            ref=payload.context.ref,
            platform_data=payload.context.platform_data,
            memory_labels=payload.context.memory_labels,
        ),
        title=payload.title,
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
        await session.commit()
        await session.refresh(row)
        return conversation_to_response(row)


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
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)
    ok = await request.app.state.session_manager.purge_conversation(conversation_id)
    return {
        "ok": ok,
        "intaris_cascade": False,
        "warning": "Intaris purge is not supported by the current provider contract.",
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
        if row.root_session_id is None:
            return MessageHistoryResponse(items=[], last_seq=0, has_more=False)
        session_row = await get_session_row(session, row.root_session_id)
    if session_row is None:
        return MessageHistoryResponse(items=[], last_seq=0, has_more=False)
    event_result = await request.app.state.providers.guardrails.read_events(
        session_id=session_row.intaris_session_id or session_row.session_id,
        after_seq=after_seq,
        limit=limit,
    )
    return MessageHistoryResponse(
        items=[event_to_response(item) for item in event_result.events],
        last_seq=event_result.last_seq,
        has_more=event_result.has_more,
    )


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
