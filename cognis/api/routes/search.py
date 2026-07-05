"""Conversation search proxy routes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user
from cognis.core.conversation_search import (
    attach_extra_matches,
    join_flat_matches,
    join_session_matches,
)
from cognis.logging import get_logger
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
    get_setting_value,
    list_conversation_intaris_session_ids,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_HEALTH_TTL_SECONDS = 30.0
# Bounded fan-out for the supplemental flat search used to populate
# `extra_matches` on aggregated session results.
_EXTRA_MATCHES_PER_SESSION = 4
_EXTRA_MATCHES_TOTAL_LIMIT = 50
_SESSION_SEARCH_OVERFETCH_FACTOR = 3
_SESSION_SEARCH_MAX_LIMIT = 100
_MAX_MULTI_AGENT_SEARCH_FILTERS = 25
_health_cache: dict[str, tuple[float, SearchHealth]] = {}


def _cache_key(user_email: str) -> str:
    return user_email


def _agent_filter_values(filters: Any) -> list[str]:
    return sorted(
        {
            value
            for value in [
                getattr(filters, "agent_id", None),
                *(getattr(filters, "agent_ids", None) or []),
            ]
            if value
        }
    )


def _intaris_filters(filters: Any, *, agent_id_override: str | None = None) -> SearchRequestFilters:
    agent_ids = _agent_filter_values(filters)
    agent_id = agent_id_override
    if agent_id is None and len(agent_ids) == 1:
        agent_id = agent_ids[0]
    return SearchRequestFilters(
        agent_id=agent_id,
        session_id=filters.session_id,
        session_ids=filters.session_ids,
        from_ts=filters.from_ts,
        to_ts=filters.to_ts,
    )


async def _display_min_score(request: Request) -> float:
    async with request.app.state.session_factory() as session:
        raw = await get_setting_value(session, "search.display_min_score", 0.2)
    return float(raw)


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
    agent_filter = _agent_filter_values(payload.filters)
    if len(agent_filter) > _MAX_MULTI_AGENT_SEARCH_FILTERS:
        raise api_exception(
            400,
            "too_many_agent_filters",
            f"Conversation search supports at most {_MAX_MULTI_AGENT_SEARCH_FILTERS} agent filters",
        )
    display_min_score = await _display_min_score(request)

    intaris_limit = min(
        _SESSION_SEARCH_MAX_LIMIT,
        payload.limit * _SESSION_SEARCH_OVERFETCH_FACTOR,
    )
    if len(agent_filter) > 1:
        results = await asyncio.gather(
            *(
                request.app.state.providers.guardrails.search_sessions(
                    SearchSessionsRequest(
                        q=payload.q,
                        filters=_intaris_filters(payload.filters, agent_id_override=agent_id),
                        kinds=payload.kinds,
                        mode=payload.mode,
                        limit=intaris_limit,
                        cursor=None,
                    ),
                    user_email=user.email,
                )
                for agent_id in agent_filter
            )
        )
        result = results[0]
        session_matches = [match for search_result in results for match in search_result.sessions]
        total_estimates = [search_result.total_estimated for search_result in results]
        total_estimated = (
            sum(total_estimates) if all(total is not None for total in total_estimates) else None
        )
        next_cursor = None
    else:
        result = await request.app.state.providers.guardrails.search_sessions(
            SearchSessionsRequest(
                q=payload.q,
                filters=_intaris_filters(payload.filters),
                kinds=payload.kinds,
                mode=payload.mode,
                limit=intaris_limit,
                cursor=payload.cursor,
            ),
            user_email=user.email,
        )
        session_matches = result.sessions
        total_estimated = result.total_estimated
        next_cursor = result.next_cursor
    async with request.app.state.session_factory() as session:
        matches = await join_session_matches(
            session,
            user_email=user.email,
            matches=session_matches,
            agent_ids=agent_filter,
            project_id=payload.filters.project_id,
            status=payload.filters.status,
            context_type=payload.filters.context_type,
            context_types=payload.filters.context_types,
            min_score=display_min_score,
            query=payload.q,
        )
        truncated_after_join = len(matches) > payload.limit
        matches = matches[: payload.limit]

    # Supplemental flat search: only run for conversations whose Intaris
    # session reported multiple matches, so the UI can expand to see them.
    multi_match_session_ids = [row.intaris_session_id for row in matches if row.match_count > 1]
    if multi_match_session_ids:
        flat_filters = _intaris_filters(payload.filters)
        flat_filters.session_id = None
        flat_filters.session_ids = multi_match_session_ids
        flat_limit = min(
            _EXTRA_MATCHES_TOTAL_LIMIT,
            len(multi_match_session_ids) * (_EXTRA_MATCHES_PER_SESSION + 1),
        )
        try:
            flat = await request.app.state.providers.guardrails.search(
                SearchRequest(
                    q=payload.q,
                    filters=flat_filters,
                    kinds=payload.kinds,
                    mode=payload.mode,
                    limit=flat_limit,
                ),
                user_email=user.email,
            )
            attach_extra_matches(
                matches,
                flat.matches,
                per_session_limit=_EXTRA_MATCHES_PER_SESSION,
                min_score=display_min_score,
                query=payload.q,
            )
        except Exception:
            logger.warning("search: extra_matches fetch failed", exc_info=True)

    return ConversationSearchResponse(
        matches=matches,
        next_cursor=None if truncated_after_join else next_cursor,
        total_estimated=total_estimated,
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
    display_min_score = await _display_min_score(request)

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
            min_score=display_min_score,
            query=payload.q,
        )
    return ConversationFlatSearchResponse(
        matches=matches,
        next_cursor=result.next_cursor,
        total_estimated=result.total_estimated,
        backend=result.backend,
    )
