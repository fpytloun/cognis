"""Conversation search proxy routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user
from cognis.core.conversation_search import join_flat_matches, join_session_matches
from cognis.models.search import (
    ConversationFlatSearchResponse,
    ConversationSearchRequest,
    ConversationSearchResponse,
    SearchHealth,
    SearchRequest,
    SearchRequestFilters,
    SearchSessionsRequest,
)
from cognis.store.queries import (
    get_conversation,
    list_conversation_intaris_session_ids,
)

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_HEALTH_TTL_SECONDS = 30.0
_health_cache: dict[str, tuple[float, SearchHealth]] = {}


def _cache_key(user_email: str) -> str:
    return user_email


def _intaris_filters(filters: Any) -> SearchRequestFilters:
    return SearchRequestFilters(
        agent_id=filters.agent_id,
        session_id=filters.session_id,
        session_ids=filters.session_ids,
        from_ts=filters.from_ts,
        to_ts=filters.to_ts,
    )


@router.get("/health", response_model=SearchHealth)
async def search_health(request: Request) -> SearchHealth:
    user = require_current_user(request)
    key = _cache_key(user.email)
    now = time.monotonic()
    cached = _health_cache.get(key)
    if cached is not None and now - cached[0] < _HEALTH_TTL_SECONDS:
        return cached[1]
    health = await request.app.state.providers.guardrails.search_health(user_email=user.email)
    _health_cache[key] = (now, health)
    return health


@router.post("/conversations", response_model=ConversationSearchResponse)
async def search_conversations(
    request: Request,
    payload: ConversationSearchRequest,
) -> ConversationSearchResponse:
    user = require_current_user(request)
    if not payload.q.strip():
        raise api_exception(400, "validation_error", "Search query cannot be empty")

    intaris_request = SearchSessionsRequest(
        q=payload.q,
        filters=_intaris_filters(payload.filters),
        kinds=payload.kinds,
        mode=payload.mode,
        limit=payload.limit,
        cursor=payload.cursor,
    )
    result = await request.app.state.providers.guardrails.search_sessions(
        intaris_request,
        user_email=user.email,
    )
    async with request.app.state.session_factory() as session:
        matches = await join_session_matches(
            session,
            user_email=user.email,
            matches=result.sessions,
            project_id=payload.filters.project_id,
            status=payload.filters.status,
        )
    return ConversationSearchResponse(
        matches=matches,
        next_cursor=result.next_cursor,
        total_estimated=result.total_estimated,
        backend=result.backend,
    )


@router.post("/conversation/{conversation_id}", response_model=ConversationFlatSearchResponse)
async def search_conversation(
    request: Request,
    conversation_id: str,
    payload: SearchRequest,
) -> ConversationFlatSearchResponse:
    user = require_current_user(request)
    if not payload.q.strip():
        raise api_exception(400, "validation_error", "Search query cannot be empty")

    async with request.app.state.session_factory() as session:
        conversation = await get_conversation(session, conversation_id)
        if conversation is None or conversation.status == "deleted":
            raise api_exception(404, "not_found", "Conversation not found")
        if conversation.user_email != user.email:
            raise api_exception(404, "not_found", "Conversation not found")
        session_ids = await list_conversation_intaris_session_ids(session, conversation_id)

    if not session_ids:
        return ConversationFlatSearchResponse()

    filters = _intaris_filters(payload.filters)
    filters.session_id = None
    filters.session_ids = session_ids
    filters.agent_id = filters.agent_id or conversation.agent_id
    result = await request.app.state.providers.guardrails.search(
        SearchRequest(
            q=payload.q,
            filters=filters,
            kinds=payload.kinds,
            mode=payload.mode,
            limit=payload.limit,
            cursor=payload.cursor,
        ),
        user_email=user.email,
    )
    async with request.app.state.session_factory() as session:
        matches = await join_flat_matches(
            session,
            user_email=user.email,
            conversation_id=conversation_id,
            matches=result.matches,
        )
    return ConversationFlatSearchResponse(
        matches=matches,
        next_cursor=result.next_cursor,
        total_estimated=result.total_estimated,
        backend=result.backend,
    )
