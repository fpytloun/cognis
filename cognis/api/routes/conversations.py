"""Conversation routes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from cognis.api.common import (
    api_exception,
    check_agent_access,
    check_project_access,
    decode_cursor,
    encode_cursor,
    forbid_mutation_for_viewer,
    require_current_user,
    require_resource_owner,
)
from cognis.api.models import (
    AgentDirectChatResponse,
    ConversationCreateRequest,
    ConversationOpenRequest,
    ConversationResolveRequest,
    ConversationResponse,
    ConversationTitleSuggestionResponse,
    ConversationUpdateRequest,
    CursorPage,
    ManagedConversationActionRequest,
    ManagedConversationActionResponse,
    QueuedMessageResponse,
    QueuedMessagesResponse,
    SessionResponse,
    SidebarProjectionResponse,
    SlashCommandSuggestionResponse,
    SlashCommandSuggestionsResponse,
    UpdateQueuedMessageRequest,
)
from cognis.api.serializers import (
    agent_to_response,
    conversation_to_response,
    session_to_response,
)
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.attachment_utils import hydrate_attachment_ref_groups
from cognis.core.chat_modes import ChatMode
from cognis.core.conversation_state import snapshot_for_conversation
from cognis.core.managed_conversations import (
    ManagedConversationAdmissionConflict,
    ManagedConversationRetryMessage,
    ManagedConversationTurnObserver,
    last_managed_conversation_user_message_for_retry,
    new_managed_turn_id,
)
from cognis.core.title_policy import latest_intaris_title_from_platform_data
from cognis.core.turn_scheduler import TurnError
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, SessionEvent, SessionModel
from cognis.store.queries import (
    admit_managed_conversation_turn,
    create_managed_conversation_link,
    get_agent,
    get_conversation,
    get_latest_active_conversation_for_agent,
    get_managed_conversation_link_for_target,
    get_project,
    get_session_row,
    get_user_ui_state_value,
    list_agent_direct_conversations,
    list_conversation_context_types,
    list_conversation_sessions,
    list_conversation_todos_by_conversation,
    list_conversations,
    list_managed_conversation_links_for_targets,
    list_pending_notification_types_by_conversation,
    list_sessions_by_ids,
    list_sidebar_tombstone_conversation_ids,
    list_visible_agents,
    mark_conversation_read,
    update_conversation_context_data,
    update_managed_conversation_link,
    upsert_user_ui_state,
)

logger = get_logger(__name__)

_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}
_CHAT_LAST_OPENED_UI_STATE_PREFIX = "chat.last_opened"
# Agent-agnostic global key: tracks the most recently opened conversation
# across all agents so PWA cold-starts can restore the right conversation
# even when the selected agent doesn't match the last-active one.
_CHAT_LAST_OPENED_GLOBAL_STATE_KEY = "chat.last_opened:global"


def _filter_values(single: str | None, multiple: list[str] | None) -> list[str] | None:
    values = sorted(
        {value.strip() for value in [single, *(multiple or [])] if value and value.strip()}
    )
    return values or None


def _agent_definition_from_row(row: object) -> AgentDefinition:
    return AgentDefinition.model_validate(agent_to_response(row).model_dump())


async def _emit_sidebar_conversation_upsert(request: Request, conversation_id: str) -> None:
    """Fan out a hydrated sidebar row for a REST-created/updated conversation."""

    ws_manager = getattr(request.app.state, "ws_manager", None)
    send_sidebar_update = getattr(ws_manager, "send_sidebar_update_to_owner", None)
    if not callable(send_sidebar_update):
        return
    send_sidebar_update_func = cast(Callable[..., Awaitable[None]], send_sidebar_update)
    try:
        await send_sidebar_update_func(
            conversation_id,
            {
                "type": "sidebar_conversation_upsert",
                "conversation_id": conversation_id,
            },
            include_subscribers=True,
        )
    except Exception:
        logger.warning(
            "Failed to fan out sidebar conversation upsert",
            extra={"extra_data": {"conversation_id": conversation_id}},
            exc_info=True,
        )


async def _emit_sidebar_conversation_removed(
    request: Request, *, user_email: str, conversation_id: str
) -> None:
    """Fan out an owner-wide sidebar row removal for a deleted conversation."""

    ws_manager = getattr(request.app.state, "ws_manager", None)
    send_to_user = getattr(ws_manager, "send_to_user", None)
    if not callable(send_to_user):
        return
    send_to_user_func = cast(Callable[[str, dict[str, Any]], Awaitable[None]], send_to_user)
    try:
        await send_to_user_func(
            user_email,
            {
                "type": "sidebar_conversation_removed",
                "conversation_id": conversation_id,
            },
        )
    except Exception:
        logger.warning(
            "Failed to fan out sidebar conversation removal",
            extra={"extra_data": {"conversation_id": conversation_id, "user_email": user_email}},
            exc_info=True,
        )


def _require_visible_conversation(request: Request, row: Any) -> Any:
    if row is None or getattr(row, "status", None) == "deleted":
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    return row


async def _conversation_attention_context(
    session: Any,
    rows: list[Any],
    user_email: str,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    conversation_ids = [row.conversation_id for row in rows]
    active_session_ids = [
        row.active_session_id for row in rows if getattr(row, "active_session_id", None)
    ]
    active_sessions = await list_sessions_by_ids(session, active_session_ids)
    pending_notifications = await list_pending_notification_types_by_conversation(
        session,
        user_email,
        conversation_ids,
    )
    return active_sessions, pending_notifications


async def _conversation_response(
    request: Request,
    row: Any,
    *,
    has_active_turn: bool | None = None,
    include_state: bool = True,
) -> ConversationResponse:
    active_session = None
    managed_link = None
    pending_notifications: list[str] = []
    active_turn_state = request.app.state.turn_scheduler.running_turn_state(row.conversation_id)
    resolved_has_active_turn = (
        active_turn_state is not None if has_active_turn is None else has_active_turn
    )
    async with request.app.state.session_factory() as session:
        if getattr(row, "active_session_id", None):
            active_session = await get_session_row(session, row.active_session_id)
        row_platform_data = getattr(row, "context_data", None) or {}
        if getattr(row, "context_type", None) in {
            "agent_work",
            "managed_agent_conversation",
        } or row_platform_data.get("kind") in {"agent_work", "managed_agent_conversation"}:
            managed_link = await get_managed_conversation_link_for_target(
                session,
                row.conversation_id,
                user_email=row.user_email,
            )
        pending_notifications = (
            await list_pending_notification_types_by_conversation(
                session,
                row.user_email,
                [row.conversation_id],
            )
        ).get(row.conversation_id, [])
        conversation_state = (
            await snapshot_for_conversation(
                session,
                user_email=row.user_email,
                conversation_id=row.conversation_id,
                turn_scheduler=getattr(request.app.state, "turn_scheduler", None),
            )
            if include_state
            else None
        )
    return conversation_to_response(
        row,
        has_active_turn=resolved_has_active_turn,
        active_turn_state=active_turn_state,
        active_session=active_session,
        pending_notification_types=pending_notifications,
        conversation_state=conversation_state,
        managed_link=managed_link,
    )


async def _require_mutable_conversation(
    request: Request,
    conversation_id: str,
    *,
    allow_managed_conversation: bool = False,
) -> Any:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None or row.status == "deleted":
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    if row.status == "archived":
        raise api_exception(409, "conflict", "Conversation is not active")
    if row.context_type in _MANAGED_CONVERSATION_CONTEXT_TYPES and not allow_managed_conversation:
        raise api_exception(
            409,
            "managed_conversation_read_only",
            "Managed conversations are read-only from the target chat; use managed actions from the controller conversation.",
        )
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, row.agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, agent, required="use")
    return user


def _queued_messages_response(messages: list[dict[str, Any]]) -> QueuedMessagesResponse:
    items = [QueuedMessageResponse.model_validate(item) for item in messages]
    return QueuedMessagesResponse(messages=items, queued_count=len(items))


def _agent_direct_sort_key(item: AgentDirectChatResponse) -> datetime:
    return (
        item.conversation.last_message_at
        or item.conversation.updated_at
        or item.conversation.created_at
        or datetime.min.replace(tzinfo=UTC)
    )


async def _conversation_page_projection(
    request: Request,
    *,
    user_email: str,
    cursor: str | None = None,
    limit: int,
    context_type: str | None = None,
    context_types: list[str] | None = None,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
    project_id: str | None = None,
    status: str = "active",
    include_agent_direct: bool = False,
    changed_since: datetime | None = None,
) -> CursorPage[ConversationResponse]:
    cursor_payload = decode_cursor(cursor)
    cursor_id = str(cursor_payload.get("id", "")) if cursor_payload is not None else None
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    async with request.app.state.session_factory() as session:
        rows = await list_conversations(
            session,
            user_email,
            context_type=context_type,
            context_types=context_types,
            agent_id=agent_id,
            agent_ids=agent_ids,
            project_id=project_id,
            status=status,
            include_agent_direct=include_agent_direct,
            cursor_id=cursor_id,
            changed_since=changed_since,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        active_sessions, pending_notifications = await _conversation_attention_context(
            session,
            page_rows,
            user_email,
        )
        active_turn_states = (
            {
                row.conversation_id: turn_scheduler.running_turn_state(row.conversation_id)
                for row in page_rows
            }
            if turn_scheduler is not None
            else {row.conversation_id: None for row in page_rows}
        )
        active_rows = [
            row for row in page_rows if active_turn_states.get(row.conversation_id) is not None
        ]
        todo_snapshots = await list_conversation_todos_by_conversation(
            session,
            [row.conversation_id for row in active_rows],
        )
        conversation_states = {}
        for row in active_rows:
            snapshot = await snapshot_for_conversation(
                session,
                user_email=user_email,
                conversation_id=row.conversation_id,
                turn_scheduler=turn_scheduler,
                conversation=row,
                conversation_todos=todo_snapshots.get(row.conversation_id, []),
            )
            if snapshot is not None:
                conversation_states[row.conversation_id] = snapshot
        managed_links = await list_managed_conversation_links_for_targets(
            session,
            [
                row.conversation_id
                for row in page_rows
                if (row.context_data or {}).get("kind")
                in {"agent_work", "managed_agent_conversation"}
            ],
            user_email=user_email,
        )
    items: list[ConversationResponse] = []
    for row in page_rows:
        active_turn_state = active_turn_states.get(row.conversation_id)
        items.append(
            conversation_to_response(
                row,
                has_active_turn=active_turn_state is not None,
                active_session=(
                    active_sessions.get(row.active_session_id) if row.active_session_id else None
                ),
                active_turn_state=active_turn_state,
                pending_notification_types=pending_notifications.get(row.conversation_id, []),
                conversation_state=conversation_states.get(row.conversation_id),
                managed_link=managed_links.get(row.conversation_id),
            )
        )
    next_cursor = encode_cursor({"id": items[-1].conversation_id}) if has_more and items else None
    return CursorPage(items=items, cursor=next_cursor, has_more=has_more)


def _is_openable_chat_conversation(
    conversation: Any,
    *,
    user_email: str,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None = None,
) -> bool:
    platform_data = conversation.context_data or {}
    return (
        conversation.user_email == user_email
        and conversation.agent_id == agent_id
        and (agent_profile_id is None or conversation.agent_profile_id == agent_profile_id)
        and conversation.status == "active"
        and conversation.context_type == context_type
        and platform_data.get("kind") != "agent_direct"
    )


def _chat_last_opened_scope_key(
    *,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
) -> str:
    return f"{agent_id}\x1f{agent_profile_id or ''}\x1f{context_type}"


def _chat_last_opened_state_key(
    *,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
) -> str:
    return (
        f"{_CHAT_LAST_OPENED_UI_STATE_PREFIX}:"
        f"{_chat_last_opened_scope_key(agent_id=agent_id, context_type=context_type, agent_profile_id=agent_profile_id)}"
    )


@dataclass(frozen=True)
class _ChatLastOpenedCandidate:
    conversation_id: str
    opened_at: datetime | None
    order: int


def _parse_chat_last_opened_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _chat_last_opened_conversation_id(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    conversation_id = state.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


def _chat_last_opened_candidate_sort_key(
    candidate: _ChatLastOpenedCandidate,
) -> tuple[bool, datetime, int]:
    return (
        candidate.opened_at is not None,
        candidate.opened_at or datetime.min.replace(tzinfo=UTC),
        -candidate.order,
    )


def _chat_last_opened_candidates(
    *,
    persisted_state: dict[str, Any] | None,
    payload: ConversationOpenRequest,
) -> list[_ChatLastOpenedCandidate]:
    by_conversation_id: dict[str, _ChatLastOpenedCandidate] = {}
    order = 0

    def add_candidate(conversation_id: str, opened_at: datetime | None) -> None:
        nonlocal order
        normalized_id = conversation_id.strip()
        if not normalized_id:
            return
        candidate = _ChatLastOpenedCandidate(
            conversation_id=normalized_id,
            opened_at=opened_at,
            order=order,
        )
        order += 1
        existing = by_conversation_id.get(normalized_id)
        if existing is None or _chat_last_opened_candidate_sort_key(
            candidate
        ) > _chat_last_opened_candidate_sort_key(existing):
            by_conversation_id[normalized_id] = candidate

    persisted_conversation_id = _chat_last_opened_conversation_id(persisted_state)
    if persisted_conversation_id:
        add_candidate(
            persisted_conversation_id,
            _parse_chat_last_opened_at((persisted_state or {}).get("opened_at")),
        )

    for candidate in payload.candidate_conversations:
        add_candidate(candidate.conversation_id, _parse_chat_last_opened_at(candidate.opened_at))

    for conversation_id in payload.candidate_conversation_ids:
        add_candidate(conversation_id, None)

    return sorted(
        by_conversation_id.values(),
        key=_chat_last_opened_candidate_sort_key,
        reverse=True,
    )


async def _remember_chat_last_opened(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
    conversation_id: str,
    opened_at: datetime | None = None,
) -> None:
    opened_at = opened_at or datetime.now(UTC)
    state_value = {
        "conversation_id": conversation_id,
        "agent_id": agent_id,
        "agent_profile_id": agent_profile_id,
        "context_type": context_type,
        "opened_at": opened_at.isoformat(),
    }
    scope_keys = [
        _chat_last_opened_state_key(
            agent_id=agent_id,
            context_type=context_type,
            agent_profile_id=agent_profile_id,
        )
    ]
    if agent_profile_id is not None:
        scope_keys.append(
            _chat_last_opened_state_key(
                agent_id=agent_id,
                context_type=context_type,
                agent_profile_id=None,
            )
        )
    for scope_key in scope_keys:
        await upsert_user_ui_state(session, user_email, scope_key, state_value)
    # Also write the agent-agnostic global key so PWA cold-starts can restore
    # the last-opened conversation regardless of which agent is selected at
    # launch. The global record carries agent_id so open_conversation can
    # derive the correct agent from the conversation.
    await upsert_user_ui_state(session, user_email, _CHAT_LAST_OPENED_GLOBAL_STATE_KEY, state_value)


async def _agent_direct_chat_projection(
    request: Request,
    *,
    user_email: str,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
    status: str = "active",
) -> list[AgentDirectChatResponse]:
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    rows: list[tuple[Any, Any]] = []
    agent_filter = set(_filter_values(agent_id, agent_ids) or [])
    async with request.app.state.session_factory() as session:
        visible_agents = await list_visible_agents(session, user_email)
        primary_agents = [
            agent
            for agent, _grant in visible_agents
            if agent.agent_type == "primary"
            and agent.status == "active"
            and (not agent_filter or agent.agent_id in agent_filter)
        ]
        direct_conversations = await list_agent_direct_conversations(
            session,
            user_email,
            [agent.agent_id for agent in primary_agents],
        )
        for agent, _grant in visible_agents:
            if agent.agent_type != "primary" or agent.status != "active":
                continue
            if agent_filter and agent.agent_id not in agent_filter:
                continue
            rows.append((agent, direct_conversations.get(agent.agent_id)))
        active_sessions, pending_notifications = await _conversation_attention_context(
            session,
            [conversation for _agent, conversation in rows if conversation is not None],
            user_email,
        )
        active_turn_states = (
            {
                conversation.conversation_id: turn_scheduler.running_turn_state(
                    conversation.conversation_id
                )
                for _agent, conversation in rows
                if conversation is not None
            }
            if turn_scheduler is not None
            else {
                conversation.conversation_id: None
                for _agent, conversation in rows
                if conversation is not None
            }
        )
        active_conversations = [
            conversation
            for _agent, conversation in rows
            if conversation is not None
            and active_turn_states.get(conversation.conversation_id) is not None
        ]
        todo_snapshots = await list_conversation_todos_by_conversation(
            session,
            [conversation.conversation_id for conversation in active_conversations],
        )
        conversation_states = {}
        for conversation in active_conversations:
            snapshot = await snapshot_for_conversation(
                session,
                user_email=user_email,
                conversation_id=conversation.conversation_id,
                turn_scheduler=turn_scheduler,
                conversation=conversation,
                conversation_todos=todo_snapshots.get(conversation.conversation_id, []),
            )
            if snapshot is not None:
                conversation_states[conversation.conversation_id] = snapshot

    responses: list[AgentDirectChatResponse] = []
    for agent, conversation in rows:
        if conversation is None:
            continue
        if status == "active" and conversation.status != "active":
            continue
        if status == "archived" and conversation.status != "archived":
            continue
        if status == "starred" and not conversation.starred_at:
            continue
        active_turn_state = active_turn_states.get(conversation.conversation_id)
        responses.append(
            AgentDirectChatResponse(
                agent=agent_to_response(agent),
                conversation=conversation_to_response(
                    conversation,
                    has_active_turn=active_turn_state is not None,
                    active_session=(
                        active_sessions.get(conversation.active_session_id)
                        if conversation.active_session_id
                        else None
                    ),
                    active_turn_state=active_turn_state,
                    pending_notification_types=pending_notifications.get(
                        conversation.conversation_id,
                        [],
                    ),
                    conversation_state=conversation_states.get(conversation.conversation_id),
                ),
            )
        )

    responses.sort(key=_agent_direct_sort_key, reverse=True)
    return responses


async def _hydrate_event_attachments(
    request: Request,
    events: list[dict[str, Any]],
    *,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> None:
    artifact_store = request.app.state.artifact_store
    current_user = require_current_user(request)
    attachment_groups: list[list[Any]] = []
    event_data: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data") if isinstance(event, dict) else None
        attachments = data.get("attachments") if isinstance(data, dict) else None
        if not isinstance(attachments, list):
            continue
        attachment_groups.append(attachments)
        event_data.append(cast(dict[str, Any], data))
    if not attachment_groups:
        return
    async with request.app.state.session_factory() as artifact_session:
        hydrated_groups = await hydrate_attachment_ref_groups(
            artifact_session,
            artifact_store,
            attachment_groups,
            owner_email=current_user.email,
            conversation_id=conversation_id,
            session_id=session_id,
        )
    for data, hydrated in zip(event_data, hydrated_groups, strict=True):
        data["attachments"] = hydrated


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=CursorPage[ConversationResponse])
async def conversation_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    context_type: str | None = Query(default=None),
    context_types: list[str] | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    agent_ids: list[str] | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
    include_agent_direct: bool = Query(default=False),
) -> CursorPage[ConversationResponse]:
    user = require_current_user(request)
    context_filter = _filter_values(context_type, context_types)
    agent_filter = _filter_values(agent_id, agent_ids)
    return await _conversation_page_projection(
        request,
        user_email=user.email,
        cursor=cursor,
        limit=limit,
        context_types=context_filter,
        agent_ids=agent_filter,
        project_id=project_id,
        status=status,
        include_agent_direct=include_agent_direct,
    )


@router.get("/context-types", response_model=list[str])
async def conversation_context_types(
    request: Request,
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> list[str]:
    """Return distinct conversation context types for sidebar filters."""

    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        return await list_conversation_context_types(
            session,
            user.email,
            status=status,
            include_agent_direct=False,
        )


@router.get("/agent-direct", response_model=list[AgentDirectChatResponse])
async def agent_direct_chats(
    request: Request,
    agent_id: str | None = Query(default=None),
    agent_ids: list[str] | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> list[AgentDirectChatResponse]:
    """Return sticky web direct chats for visible primary agents."""

    user = require_current_user(request)
    return await _agent_direct_chat_projection(
        request,
        user_email=user.email,
        agent_ids=_filter_values(agent_id, agent_ids),
        status=status,
    )


@router.get("/sidebar", response_model=SidebarProjectionResponse)
async def sidebar_projection(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    changed_since: datetime | None = Query(default=None),
    context_type: str | None = Query(default=None),
    context_types: list[str] | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    agent_ids: list[str] | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> SidebarProjectionResponse:
    """Return the UI-shaped sidebar projection in one request."""

    user = require_current_user(request)
    sync_timestamp = datetime.now(UTC)
    context_filter = _filter_values(context_type, context_types)
    agent_filter = _filter_values(agent_id, agent_ids)
    is_delta = changed_since is not None
    agents = [
        agent_to_response(agent)
        for agent in await request.app.state.agent_registry.list_all(
            owner_email=user.email,
            include_hidden=False,
            include_system=True,
            include_disabled=False,
        )
    ]
    conversations = await _conversation_page_projection(
        request,
        user_email=user.email,
        cursor=cursor,
        limit=limit,
        context_types=context_filter,
        agent_ids=agent_filter,
        project_id=project_id,
        status=status,
        include_agent_direct=False,
        changed_since=changed_since,
    )
    direct_chats = (
        await _agent_direct_chat_projection(
            request,
            user_email=user.email,
            agent_ids=agent_filter,
            status="active",
        )
        if context_filter is None or "web" in context_filter
        else []
    )
    if changed_since is not None:
        direct_chats = [
            item
            for item in direct_chats
            if item.conversation.updated_at is not None
            and item.conversation.updated_at > changed_since
        ]
    async with request.app.state.session_factory() as session:
        context_types = await list_conversation_context_types(
            session,
            user.email,
            status=status,
            include_agent_direct=False,
        )
        removed_conversation_ids = (
            await list_sidebar_tombstone_conversation_ids(
                session,
                user.email,
                changed_since=changed_since,
                context_types=context_filter,
                agent_ids=agent_filter,
                project_id=project_id,
                status=status,
                include_agent_direct=True,
            )
            if changed_since is not None
            else []
        )
    return SidebarProjectionResponse(
        agents=[] if is_delta else agents,
        agent_direct_chats=direct_chats,
        conversations=conversations,
        context_types=[] if is_delta else context_types,
        removed_conversation_ids=removed_conversation_ids,
        full_resync_required=is_delta and conversations.has_more,
        sync_timestamp=sync_timestamp,
    )


@router.post("/open", response_model=ConversationResponse)
async def open_conversation(
    request: Request,
    payload: ConversationOpenRequest,
) -> ConversationResponse:
    """Resolve the best chat conversation to open for the selected agent/channel.

    Browser-local last-opened IDs are treated only as ordered hints. The server
    validates each hint against ownership, selected agent, context, active
    status, and direct-chat exclusion before falling back to latest/create.
    """
    user = require_current_user(request)
    context_type = payload.context_type or "web"
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc

        async def return_opened(conversation: Any) -> ConversationResponse:
            if user.role != "viewer":
                await _remember_chat_last_opened(
                    session,
                    user_email=user.email,
                    agent_id=payload.agent_id,
                    context_type=context_type,
                    agent_profile_id=payload.agent_profile_id,
                    conversation_id=conversation.conversation_id,
                    opened_at=datetime.now(UTC),
                )
                await session.commit()
            return await _conversation_response(
                request,
                conversation,
                include_state=payload.include_state,
            )

        persisted_state_key = _chat_last_opened_state_key(
            agent_id=payload.agent_id,
            context_type=context_type,
            agent_profile_id=payload.agent_profile_id,
        )
        persisted_state = await get_user_ui_state_value(
            session,
            user.email,
            persisted_state_key,
        )
        candidate_conversations = _chat_last_opened_candidates(
            persisted_state=persisted_state,
            payload=payload,
        )

        for candidate_hint in candidate_conversations[:10]:
            candidate = await get_conversation(session, candidate_hint.conversation_id)
            if candidate is None:
                continue
            if _is_openable_chat_conversation(
                candidate,
                user_email=user.email,
                agent_id=payload.agent_id,
                context_type=context_type,
                agent_profile_id=payload.agent_profile_id,
            ):
                return await return_opened(candidate)

        # Global last-opened fallback: if no agent-scoped candidate validated,
        # check the agent-agnostic global key. This handles the common PWA
        # cold-start case where the selected agent at launch differs from the
        # agent of the genuinely last-opened conversation (e.g. the user has
        # multiple agents and the PWA always falls back to the first primary).
        # We validate ownership, active status, and context_type but allow any
        # agent so the conversation-first restore works correctly.
        #
        # Skip when the request targets a specific agent profile — the global
        # key does not track profiles, so restoring a conversation from a
        # different profile would be incorrect and would overwrite the
        # profile-specific last-opened state.
        if payload.agent_profile_id is None:
            global_state = await get_user_ui_state_value(
                session, user.email, _CHAT_LAST_OPENED_GLOBAL_STATE_KEY
            )
            global_conversation_id = _chat_last_opened_conversation_id(global_state)
            if global_conversation_id:
                global_candidate = await get_conversation(session, global_conversation_id)
                if global_candidate is not None and _is_openable_chat_conversation(
                    global_candidate,
                    user_email=user.email,
                    agent_id=global_candidate.agent_id,
                    context_type=context_type,
                    agent_profile_id=None,
                ):
                    # Record under the conversation's actual agent so subsequent
                    # agent-scoped lookups find it correctly.
                    if user.role != "viewer":
                        await _remember_chat_last_opened(
                            session,
                            user_email=user.email,
                            agent_id=global_candidate.agent_id,
                            context_type=context_type,
                            agent_profile_id=None,
                            conversation_id=global_candidate.conversation_id,
                            opened_at=datetime.now(UTC),
                        )
                        await session.commit()
                    return await _conversation_response(
                        request,
                        global_candidate,
                        include_state=payload.include_state,
                    )

        fallback_rows = await list_conversations(
            session,
            user.email,
            context_type=context_type,
            agent_id=payload.agent_id,
            status="active",
            include_agent_direct=False,
            limit=1 if payload.agent_profile_id is None else None,
        )
        for existing in fallback_rows:
            if _is_openable_chat_conversation(
                existing,
                user_email=user.email,
                agent_id=payload.agent_id,
                context_type=context_type,
                agent_profile_id=payload.agent_profile_id,
            ):
                return await return_opened(existing)

    forbid_mutation_for_viewer(request)
    context_ref = f"{context_type}:user:{user.email}:default"
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        agent_profile_id=payload.agent_profile_id,
        context=ConversationContext(
            type=context_type,
            ref=context_ref,
            platform_data={},
            memory_labels={},
        ),
    )
    async with request.app.state.session_factory() as session:
        await _remember_chat_last_opened(
            session,
            user_email=user.email,
            agent_id=payload.agent_id,
            context_type=context_type,
            agent_profile_id=payload.agent_profile_id,
            conversation_id=conversation.conversation_id,
            opened_at=datetime.now(UTC),
        )
        await session.commit()
    await _emit_sidebar_conversation_upsert(request, conversation.conversation_id)
    return await _conversation_response(
        request,
        conversation,
        include_state=payload.include_state,
    )


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
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc
        if payload.scope == "agent_direct":
            if payload.context_type != "web":
                raise api_exception(
                    400,
                    "invalid_request",
                    "Agent direct conversations are only supported for web context.",
                )
            existing = None
        else:
            existing = await get_latest_active_conversation_for_agent(
                session,
                user.email,
                payload.agent_id,
                context_type=payload.context_type,
            )
    if existing is not None:
        return await _conversation_response(request, existing)
    if payload.scope == "agent_direct":
        conversation = (
            await request.app.state.session_manager.get_or_create_agent_direct_conversation(
                user_email=user.email,
                agent_id=payload.agent_id,
                agent_profile_id=payload.agent_profile_id,
                agent_profile_explicit=payload.agent_profile_id is not None,
            )
        )
    else:
        context_ref = f"{payload.context_type}:user:{user.email}:default"
        conversation = await request.app.state.session_manager.create_conversation(
            user_email=user.email,
            agent_id=payload.agent_id,
            agent_profile_id=payload.agent_profile_id,
            context=ConversationContext(
                type=payload.context_type,
                ref=context_ref,
                platform_data={},
                memory_labels={},
            ),
            title=None,
            title_source="unset",
        )
    await _emit_sidebar_conversation_upsert(request, conversation.conversation_id)
    return await _conversation_response(request, conversation)


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
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc
    await _validate_project_access(request, payload.project_id)
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        agent_profile_id=payload.agent_profile_id,
        context=ConversationContext(
            type=payload.context.type,
            ref=payload.context.ref,
            platform_data=payload.context.platform_data,
            memory_labels=payload.context.memory_labels,
        ),
        title=payload.title,
        title_source="manual" if payload.title else "unset",
        project_id=payload.project_id,
    )
    await _emit_sidebar_conversation_upsert(request, conversation.conversation_id)
    return await _conversation_response(request, conversation)


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def conversation_detail(
    request: Request,
    conversation_id: str,
    include_state: bool = Query(
        True,
        description=(
            "Include the legacy conversation_state snapshot. Chat v2 callers should "
            "disable this and use the canonical chat v2 snapshot endpoint instead."
        ),
    ),
) -> ConversationResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    row = _require_visible_conversation(request, row)
    return await _conversation_response(request, row, include_state=include_state)


@router.post("/{conversation_id}/opened", response_model=ConversationResponse)
async def remember_opened_conversation(
    request: Request,
    conversation_id: str,
) -> ConversationResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        if user.role != "viewer" and _is_openable_chat_conversation(
            row,
            user_email=user.email,
            agent_id=row.agent_id,
            context_type=row.context_type,
            agent_profile_id=row.agent_profile_id,
        ):
            await _remember_chat_last_opened(
                session,
                user_email=user.email,
                agent_id=row.agent_id,
                context_type=row.context_type,
                agent_profile_id=row.agent_profile_id,
                conversation_id=row.conversation_id,
                opened_at=datetime.now(UTC),
            )
            await session.commit()
    return await _conversation_response(request, row)


@router.get(
    "/{conversation_id}/title-suggestion", response_model=ConversationTitleSuggestionResponse
)
async def conversation_title_suggestion(
    request: Request,
    conversation_id: str,
) -> ConversationTitleSuggestionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    row = _require_visible_conversation(request, row)
    platform_data = row.context_data or {}
    suggestion = latest_intaris_title_from_platform_data(platform_data)
    if suggestion:
        generated_at = platform_data.get("intaris_latest_title_at")
        return ConversationTitleSuggestionResponse(
            title=suggestion,
            generated_at=generated_at if isinstance(generated_at, str) else None,
            available=True,
        )

    active_session_id = getattr(row, "active_session_id", None)
    if active_session_id:
        async with request.app.state.session_factory() as session:
            session_row = await get_session_row(session, active_session_id)
        if session_row is not None:
            try:
                intaris_sid = session_row.intaris_session_id or session_row.session_id
                intaris_session = await request.app.state.providers.guardrails.get_session(
                    intaris_sid
                )
                title = (intaris_session.title or "").strip()
                if title:
                    return ConversationTitleSuggestionResponse(title=title, available=True)
            except Exception:
                logger.debug(
                    "conversation: failed to fetch latest Intaris title suggestion",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                    exc_info=True,
                )

    return ConversationTitleSuggestionResponse(
        available=False,
        reason="No Intaris title suggestion is available yet.",
    )


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
        require_resource_owner(request, row.user_email)
        if row.status == "deleted":
            raise api_exception(404, "not_found", "Conversation not found")
        if payload.archived is True:
            await manager.archive_conversation(conversation_id)
        elif payload.archived is False and row.status == "archived":
            row.status = "active"
        if payload.title is not None:
            row.title = payload.title
            row.title_source = "manual" if payload.title.strip() else "unset"
        if payload.project_id is not None:
            await _validate_project_access(request, payload.project_id)
            row.project_id = payload.project_id
        if "starred_at" in payload.model_fields_set:
            row.starred_at = payload.starred_at
        await session.commit()
        await session.refresh(row)
        active_session = (
            await get_session_row(session, row.active_session_id) if row.active_session_id else None
        )
        pending_notifications = (
            await list_pending_notification_types_by_conversation(
                session,
                row.user_email,
                [row.conversation_id],
            )
        ).get(row.conversation_id, [])
        active_turn_state = request.app.state.turn_scheduler.running_turn_state(row.conversation_id)
        response = conversation_to_response(
            row,
            has_active_turn=active_turn_state is not None,
            active_turn_state=active_turn_state,
            active_session=active_session,
            pending_notification_types=pending_notifications,
        )
    await _emit_sidebar_conversation_upsert(request, conversation_id)
    return response


@router.post("/{conversation_id}/read", response_model=dict)
async def mark_read(request: Request, conversation_id: str) -> dict[str, bool]:
    """Mark a conversation as read (sets last_read_at to now)."""
    require_current_user(request)
    payload: dict[str, Any] | None = None
    user_email: str | None = None
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        user_email = row.user_email
        was_unread = row.last_message_at is not None and (
            row.last_read_at is None or row.last_message_at > row.last_read_at
        )
        await mark_conversation_read(session, conversation_id)
        if was_unread:
            payload = {
                "type": "conversation_updated",
                "conversation_id": row.conversation_id,
                "has_unread": False,
                "last_read_at": row.last_read_at.isoformat() if row.last_read_at else None,
                "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
            }
        await session.commit()
    if payload is not None:
        ws_manager = getattr(request.app.state, "ws_manager", None)
        send_to_user = getattr(ws_manager, "send_to_user", None)
        send_to_user_func = (
            cast(Callable[[str, dict[str, Any]], Awaitable[None]], send_to_user)
            if callable(send_to_user)
            else None
        )
        if send_to_user_func is not None and user_email is not None:
            try:
                await send_to_user_func(user_email, payload)
            except Exception:
                logger.warning(
                    "Failed to fan out conversation read state",
                    extra={
                        "extra_data": {"conversation_id": conversation_id, "user_email": user_email}
                    },
                    exc_info=True,
                )
    return {"ok": True}


async def _validate_project_access(request: Request, project_id: str | None) -> None:
    if project_id is None:
        return
    async with request.app.state.session_factory() as session:
        project = await get_project(session, project_id)
    if project is None or project.status != "active":
        raise api_exception(404, "not_found", "Project not found")
    await check_project_access(request, project, required="use")


@router.delete("/{conversation_id}", response_model=dict)
async def delete_conversation(request: Request, conversation_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    ok = await request.app.state.session_manager.soft_delete_conversation(conversation_id)
    if ok:
        await _emit_sidebar_conversation_removed(
            request, user_email=row.user_email, conversation_id=conversation_id
        )
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
    require_resource_owner(request, row.user_email)
    ok = await request.app.state.session_manager.purge_conversation(conversation_id)
    if ok:
        await _emit_sidebar_conversation_removed(
            request, user_email=row.user_email, conversation_id=conversation_id
        )
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


@router.get("/{conversation_id}/queue", response_model=QueuedMessagesResponse)
async def get_queued_messages(request: Request, conversation_id: str) -> QueuedMessagesResponse:
    await _require_mutable_conversation(
        request,
        conversation_id,
        allow_managed_conversation=True,
    )
    return _queued_messages_response(
        request.app.state.turn_scheduler.queued_messages(conversation_id)
    )


@router.patch("/{conversation_id}/queue/{queue_id}", response_model=QueuedMessageResponse)
async def update_queued_message(
    request: Request,
    conversation_id: str,
    queue_id: str,
    payload: UpdateQueuedMessageRequest,
) -> QueuedMessageResponse:
    await _require_mutable_conversation(request, conversation_id)
    updated = await request.app.state.turn_scheduler.update_queued_message(
        conversation_id,
        queue_id,
        content=payload.content.strip(),
    )
    if updated is None:
        raise api_exception(404, "not_found", "Queued message not found")
    return QueuedMessageResponse.model_validate(updated)


@router.delete("/{conversation_id}/queue/{queue_id}", status_code=204)
async def delete_queued_message(request: Request, conversation_id: str, queue_id: str) -> Response:
    await _require_mutable_conversation(request, conversation_id)
    cancelled = await request.app.state.turn_scheduler.cancel_queued_message(
        conversation_id, queue_id
    )
    if not cancelled:
        raise api_exception(404, "not_found", "Queued message not found")
    return Response(status_code=204)


@router.get(
    "/{conversation_id}/slash-command-suggestions",
    response_model=SlashCommandSuggestionsResponse,
)
async def slash_command_suggestions(
    request: Request,
    conversation_id: str,
    input: str = Query(default="", max_length=500),
    limit: int = Query(default=12, ge=1, le=50),
) -> SlashCommandSuggestionsResponse:
    """Return read-only slash command and parameter suggestions for a conversation."""

    require_current_user(request)
    command_dispatcher = getattr(request.app.state, "command_dispatcher", None)
    if command_dispatcher is None:
        return SlashCommandSuggestionsResponse()

    from cognis.core.session import _to_conversation_model, _to_session_model

    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        conversation_row = _require_visible_conversation(request, conversation_row)
        agent_row = await get_agent(session, conversation_row.agent_id)
        if agent_row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent_row, required="use")
        session_row = (
            await get_session_row(session, conversation_row.active_session_id)
            if conversation_row.active_session_id
            else None
        )
        agent_model = _agent_definition_from_row(agent_row)
        conversation_model = _to_conversation_model(conversation_row)
        session_model = _to_session_model(session_row) if session_row is not None else None

    suggestions = await command_dispatcher.suggest(
        input,
        conversation=conversation_model,
        session=session_model,
        agent=agent_model,
        user_email=conversation_row.user_email,
        limit=limit,
    )
    return SlashCommandSuggestionsResponse(
        items=[
            SlashCommandSuggestionResponse(
                kind=item.kind,
                command=item.command,
                value=item.value,
                label=item.label,
                description=item.description,
                insert_text=item.insert_text,
                suffix=item.suffix,
                badges=item.badges,
            )
            for item in suggestions
        ]
    )


async def _require_managed_conversation(
    request: Request,
    conversation_id: str,
) -> tuple[Any, Any]:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        if row.context_type not in {"agent_work", "managed_agent_conversation"}:
            raise api_exception(409, "not_managed_conversation", "Conversation is not managed")
        link = await get_managed_conversation_link_for_target(
            session,
            conversation_id,
            user_email=user.email,
        )
        if link is None:
            raise api_exception(404, "not_found", "Agent work link not found")
    return user, link


async def _managed_action_response(
    request: Request,
    conversation_id: str,
    status: str,
    result: dict[str, Any] | None = None,
) -> ManagedConversationActionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        response = await _conversation_response(request, row)
    return ManagedConversationActionResponse(
        status=status,
        conversation_id=conversation_id,
        managed_agent=response.managed_agent,
        result=result,
    )


def _managed_control_metadata(link: Any, **updates: Any) -> dict[str, Any]:
    """Return link control metadata with updates merged in."""

    metadata = getattr(link, "control_metadata", None)
    base = dict(metadata) if isinstance(metadata, dict) else {}
    for key, value in updates.items():
        if value is not None:
            base[key] = value
    return base


def _managed_control_metadata_for_new_turn(link: Any) -> dict[str, Any]:
    """Return control metadata with transient manual-cancel flags removed."""

    metadata = _managed_control_metadata(link)
    for key in ("cancelled_by_user", "cancel_source", "cancelled_at"):
        metadata.pop(key, None)
    return metadata


def _managed_conversation_has_active_work(
    request: Request, link: Any, conversation_id: str
) -> bool:
    """Return whether a managed target has queued or running work."""

    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    if turn_scheduler is not None and turn_scheduler.has_active_turn(conversation_id):
        return True
    if getattr(link, "active_turn_id", None):
        return True
    return getattr(link, "turn_state", None) in {"queued", "running"}


def _require_inactive_managed_conversation(
    request: Request, link: Any, conversation_id: str
) -> None:
    if _managed_conversation_has_active_work(request, link, conversation_id):
        raise api_exception(
            409,
            "active_turn_running",
            "Stop the active managed turn before using this action.",
        )


async def _last_agent_work_user_message(
    request: Request, link: Any
) -> ManagedConversationRetryMessage | None:
    return await last_managed_conversation_user_message_for_retry(
        session_cache=request.app.state.session_cache,
        guardrails=request.app.state.providers.guardrails,
        session_factory=request.app.state.session_factory,
        link=link,
    )


async def _record_agent_work_context(
    request: Request,
    *,
    session_model: SessionModel,
    controller_agent_id: str,
    controller_conversation_id: str,
    controller_session_id: str,
    target_agent_id: str,
) -> None:
    content = "\n".join(
        [
            "Agent work context:",
            f"- This session is managed by Cognis agent `{controller_agent_id}` on behalf of the user.",
            "- Treat user messages in this session as instructions from that authenticated internal agent.",
            "- Do not mention this management context unless it is operationally relevant.",
            f"- Controller conversation: {controller_conversation_id}",
            f"- Controller session: {controller_session_id}",
        ]
    )
    event = SessionEvent(
        type="developer_message",
        data={
            "role": "developer",
            "content": content,
            "content_type": "text",
            "source": "agent_work_context",
            "target_agent_id": target_agent_id,
        },
    )
    append_result = await request.app.state.providers.guardrails.record_events(
        session_model.session_id,
        [event],
        source="cognis_agent_work",
        user_email=session_model.user_email,
        agent_id=session_model.agent_id,
    )
    await request.app.state.session_cache.append_recorded_events(
        session_model,
        [event],
        append_result,
    )


async def _record_managed_takeover_notice(
    request: Request,
    *,
    session_model: SessionModel,
    link: Any,
    follow_up_conversation_id: str,
    follow_up_session_id: str,
) -> None:
    """Record a durable UI-visible notice on the closed managed conversation."""

    event = SessionEvent(
        type="system_message",
        data={
            "role": "system",
            "content": "User took control in a follow-up conversation.",
            "content_type": "text",
            "kind": "managed_takeover",
            "notice_id": f"managed_takeover:{link.link_id}",
            "follow_up_conversation_id": follow_up_conversation_id,
            "follow_up_session_id": follow_up_session_id,
        },
    )
    append_result = await request.app.state.providers.guardrails.record_events(
        session_model.session_id,
        [event],
        source="cognis_agent_work",
        user_email=session_model.user_email,
        agent_id=session_model.agent_id,
    )
    await request.app.state.session_cache.append_recorded_events(
        session_model,
        [event],
        append_result,
    )


async def _send_managed_conversation_message(
    request: Request,
    *,
    conversation_id: str,
    link: Any,
    user_email: str,
    message: str,
    wait: bool,
    one_shot_chat_mode: ChatMode | None = None,
) -> ManagedConversationActionResponse:
    turn_id = new_managed_turn_id()
    turn_completion: asyncio.Future[Any] | None = (
        asyncio.get_running_loop().create_future() if wait else None
    )

    class _ManagedApiTurnObserver(ManagedConversationTurnObserver):
        async def on_turn_complete(self, result: Any) -> None:
            if turn_completion is not None and not turn_completion.done():
                turn_completion.set_result(result)

        async def on_turn_error(self, conversation_id: str, error: Any) -> None:
            if turn_completion is not None and not turn_completion.done():
                turn_completion.set_result(error)

    async def _on_admitted(admitted_turn_id: str, queued: bool) -> None:
        async with request.app.state.session_factory() as session:
            admitted = await admit_managed_conversation_turn(
                session,
                link.link_id,
                turn_id=admitted_turn_id,
                turn_state="queued" if queued else "running",
                notify_on_completion=not wait,
                control_metadata=_managed_control_metadata_for_new_turn(link),
            )
            await session.commit()
        if admitted is None:
            raise ManagedConversationAdmissionConflict(
                "Managed conversation already has a queued admission"
            )

    error = await request.app.state.turn_scheduler.submit_turn(
        conversation_id,
        message,
        user_email=user_email,
        one_shot_chat_mode=one_shot_chat_mode,
        turn_id=turn_id,
        turn_observers=(_ManagedApiTurnObserver(),),
        admission_observer=_on_admitted,
    )
    if error is not None:
        raise _turn_error_to_http(error)
    result = None
    if wait:
        if turn_completion is None:
            raise RuntimeError("Managed wait observer was not initialized")
        waited = await turn_completion
        result = {
            "kind": waited.__class__.__name__,
            "message": getattr(waited, "message", None),
            "result_summary": getattr(waited, "result_summary", None),
            "expected_turn_id": turn_id,
            "settled_turn_id": getattr(waited, "turn_id", None),
        }
    return await _managed_action_response(request, conversation_id, "sent", result)


@router.post("/{conversation_id}/managed/send", response_model=ManagedConversationActionResponse)
async def managed_conversation_send(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    if link.conversation_state == "closed":
        raise api_exception(409, "closed", "Agent work is closed")
    _require_inactive_managed_conversation(request, link, conversation_id)
    message = (payload.message or "").strip()
    if not message:
        raise api_exception(400, "invalid_request", "Message is required")
    return await _send_managed_conversation_message(
        request,
        conversation_id=conversation_id,
        link=link,
        user_email=user.email,
        message=message,
        wait=payload.wait,
    )


@router.post("/{conversation_id}/managed/wait", response_model=ManagedConversationActionResponse)
async def managed_conversation_wait(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    await _require_managed_conversation(request, conversation_id)
    waited = await request.app.state.turn_scheduler.wait_for_turn(
        conversation_id,
        timeout_seconds=30 if payload.wait else 0,
    )
    result = {
        "kind": waited.__class__.__name__ if waited is not None else "idle",
        "message": getattr(waited, "message", None),
        "result_summary": getattr(waited, "result_summary", None),
    }
    return await _managed_action_response(request, conversation_id, "waited", result)


@router.post(
    "/{conversation_id}/managed/interrupt", response_model=ManagedConversationActionResponse
)
async def managed_conversation_interrupt(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    cancelled = await request.app.state.turn_scheduler.cancel_turn(conversation_id)
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            turn_state="interrupted" if cancelled else "idle",
            clear_active_turn_id=True,
            last_error=payload.reason or "Interrupted from web UI",
        )
        await session.commit()
    return await _managed_action_response(
        request,
        conversation_id,
        "interrupted" if cancelled else "idle",
    )


@router.post("/{conversation_id}/managed/stop", response_model=ManagedConversationActionResponse)
async def managed_conversation_stop(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    if link.conversation_state == "closed":
        raise api_exception(409, "closed", "Agent work is closed")

    now = datetime.now(UTC).isoformat()
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            notify_on_completion=True,
            last_error=payload.reason or "Stopped by user from managed conversation UI",
            control_metadata=_managed_control_metadata(
                link,
                cancelled_by_user=True,
                cancel_source="managed_ui",
                cancelled_at=now,
            ),
        )
        await session.commit()

    stopped = False
    command_dispatcher = getattr(request.app.state, "command_dispatcher", None)
    if command_dispatcher is not None and hasattr(command_dispatcher, "stop_conversation"):
        stopped = await command_dispatcher.stop_conversation(
            conversation_id,
            user_email=user.email,
        )
    else:
        stopped = await request.app.state.turn_scheduler.cancel_turn(conversation_id)

    if not stopped:
        async with request.app.state.session_factory() as session:
            await update_managed_conversation_link(
                session,
                link.link_id,
                turn_state="idle",
                clear_active_turn_id=True,
                notify_on_completion=False,
            )
            await session.commit()

    return await _managed_action_response(
        request,
        conversation_id,
        "stopped" if stopped else "idle",
    )


@router.post("/{conversation_id}/managed/retry", response_model=ManagedConversationActionResponse)
async def managed_conversation_retry(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    if link.turn_state not in {"failed", "interrupted"}:
        raise api_exception(
            409,
            "not_retryable",
            "Agent work retry is only available after a failed or interrupted turn.",
        )
    if link.conversation_state == "closed":
        raise api_exception(409, "closed", "Agent work is closed")
    _require_inactive_managed_conversation(request, link, conversation_id)
    retry_message = await _last_agent_work_user_message(request, link)
    if retry_message is None:
        raise api_exception(409, "not_retryable", "No previous user message is available to retry")
    return await _send_managed_conversation_message(
        request,
        conversation_id=conversation_id,
        link=link,
        user_email=link.user_email,
        message=retry_message.content,
        wait=payload.wait,
        one_shot_chat_mode=retry_message.one_shot_chat_mode,
    )


@router.post("/{conversation_id}/managed/close", response_model=ManagedConversationActionResponse)
async def managed_conversation_close(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    await request.app.state.turn_scheduler.cancel_turn(conversation_id)
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            conversation_state="closed",
            turn_state="interrupted",
            clear_active_turn_id=True,
            last_error=payload.reason or "Closed from web UI",
            closed=True,
        )
        await session.commit()
    return await _managed_action_response(request, conversation_id, "closed")


@router.post("/{conversation_id}/managed/fork", response_model=ManagedConversationActionResponse)
async def managed_conversation_fork(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    active_turn_id = request.app.state.turn_scheduler.active_turn_id(conversation_id)
    from cognis.core.session import _to_conversation_model, _to_session_model
    from cognis.models.agent import AgentDefinition

    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        session_row = (
            await get_session_row(session, link.target_session_id)
            if link.target_session_id
            else None
        )
        agent_row = await get_agent(session, link.target_agent_id)
    if conversation_row is None or session_row is None or agent_row is None:
        raise api_exception(404, "not_found", "Agent work runtime not found")

    target_agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
    managed_context_data = {
        "kind": "agent_work",
        "controller_agent_id": link.controller_agent_id,
        "controller_conversation_id": link.controller_conversation_id,
        "controller_session_id": link.controller_session_id,
        "target_agent_id": link.target_agent_id,
        "forked_from_conversation_id": link.target_conversation_id,
        "forked_from_session_id": link.target_session_id,
        "provenance_in_prefix": True,
    }
    managed_context = ConversationContext(
        type="agent_work",
        platform_data=managed_context_data,
    )
    fork_title = f"{link.title or 'Agent work'} (fork)"
    fork_intention = payload.message or f"Forked agent work with {target_agent.name}"
    if active_turn_id:
        (
            new_conversation,
            new_session,
            copied,
        ) = await request.app.state.session_manager.fork_active_turn_checkpoint_into_new_conversation(
            source_session=_to_session_model(session_row),
            source_conversation=_to_conversation_model(conversation_row),
            agent=target_agent,
            user_email=user.email,
            active_turn_id=active_turn_id,
            title=fork_title,
            intention=fork_intention,
            context=managed_context,
            snapshot_extras={"trigger": "managed_conversation_fork"},
        )
    else:
        (
            new_conversation,
            new_session,
            copied,
        ) = await request.app.state.session_manager.fork_into_new_conversation(
            source_session=_to_session_model(session_row),
            source_conversation=_to_conversation_model(conversation_row),
            agent=target_agent,
            user_email=user.email,
            title=fork_title,
            intention=fork_intention,
            context=managed_context,
            snapshot_extras={"trigger": "managed_conversation_fork"},
        )
    if not copied:
        raise api_exception(500, "fork_failed", "Agent work fork did not copy context")
    async with request.app.state.session_factory() as session:
        new_link = await create_managed_conversation_link(
            session,
            user_email=user.email,
            controller_agent_id=link.controller_agent_id,
            controller_conversation_id=link.controller_conversation_id,
            controller_session_id=link.controller_session_id,
            target_agent_id=link.target_agent_id,
            target_conversation_id=new_conversation.conversation_id,
            target_session_id=new_session.session_id,
            title=fork_title,
        )
        await update_conversation_context_data(
            session,
            new_conversation.conversation_id,
            context_data={**managed_context_data, "link_id": new_link.link_id},
        )
        await session.commit()
    await _record_agent_work_context(
        request,
        session_model=new_session,
        controller_agent_id=link.controller_agent_id,
        controller_conversation_id=link.controller_conversation_id,
        controller_session_id=link.controller_session_id,
        target_agent_id=link.target_agent_id,
    )
    await _emit_sidebar_conversation_upsert(request, new_conversation.conversation_id)
    if payload.message:
        error = await request.app.state.turn_scheduler.submit_turn(
            new_conversation.conversation_id,
            payload.message,
            user_email=user.email,
        )
        if error is not None:
            raise _turn_error_to_http(error)
    return await _managed_action_response(request, new_conversation.conversation_id, "forked")


@router.post(
    "/{conversation_id}/managed/take-control",
    response_model=ManagedConversationActionResponse,
)
async def managed_conversation_take_control(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    if link.conversation_state == "closed":
        raise api_exception(409, "closed", "Agent work is closed")
    _require_inactive_managed_conversation(request, link, conversation_id)

    from cognis.core.session import _to_conversation_model, _to_session_model

    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        target_session_id = link.target_session_id or getattr(
            conversation_row, "active_session_id", None
        )
        session_row = (
            await get_session_row(session, target_session_id)
            if isinstance(target_session_id, str)
            else None
        )
        agent_row = await get_agent(session, link.target_agent_id)
    if conversation_row is None or session_row is None or agent_row is None:
        raise api_exception(404, "not_found", "Agent work runtime not found")

    target_agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
    fork_title = f"Follow-up: {link.title or conversation_row.title or 'Agent work'}"
    fork_intention = (
        payload.message or f"User took control of managed work with {target_agent.name}"
    )
    (
        new_conversation,
        new_session,
        copied,
    ) = await request.app.state.session_manager.fork_into_new_conversation(
        source_session=_to_session_model(session_row),
        source_conversation=_to_conversation_model(conversation_row),
        agent=target_agent,
        user_email=user.email,
        title=fork_title,
        intention=fork_intention,
        snapshot_extras={"trigger": "managed_conversation_take_control"},
    )
    if not copied:
        raise api_exception(
            500, "fork_failed", "Managed conversation takeover fork did not copy context"
        )

    now = datetime.now(UTC).isoformat()
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            conversation_state="closed",
            turn_state="idle",
            clear_active_turn_id=True,
            notify_on_completion=False,
            last_result_summary="User took control in a follow-up conversation.",
            last_error="Taken over by user",
            control_metadata=_managed_control_metadata(
                link,
                follow_up_conversation_id=new_conversation.conversation_id,
                follow_up_session_id=new_session.session_id,
                taken_over_by_user_at=now,
                takeover_source="managed_ui",
                closed_reason="taken_over_by_user",
            ),
            closed=True,
        )
        await session.commit()

    try:
        await _record_managed_takeover_notice(
            request,
            session_model=_to_session_model(session_row),
            link=link,
            follow_up_conversation_id=new_conversation.conversation_id,
            follow_up_session_id=new_session.session_id,
        )
    except Exception:
        logger.warning(
            "managed conversation takeover notice recording failed",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "follow_up_conversation_id": new_conversation.conversation_id,
                }
            },
            exc_info=True,
        )

    return await _managed_action_response(
        request,
        conversation_id,
        "taken_over",
        {
            "conversation_id": new_conversation.conversation_id,
            "session_id": new_session.session_id,
        },
    )


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
async def conversation_sessions(
    request: Request,
    conversation_id: str,
    root_only: bool = Query(default=False),
    active_only: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=500),
    order: Literal["asc", "desc"] = Query(default="asc"),
) -> list[SessionResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        sessions = await list_conversation_sessions(
            session,
            conversation_id,
            root_only=root_only,
            statuses=["active"] if active_only else None,
            order=order,
            limit=limit,
        )
    return [session_to_response(item) for item in sessions]


@router.get("/{conversation_id}/delegations", response_model=list[SessionResponse])
async def active_delegations(request: Request, conversation_id: str) -> list[SessionResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        sessions = await list_conversation_sessions(
            session,
            conversation_id,
            parent_only=True,
            statuses=["active"],
            order="asc",
            limit=200,
        )
    return [session_to_response(item) for item in sessions]


@router.get(
    "/{conversation_id}/subsessions/{session_id}",
    response_model=SessionResponse,
)
async def conversation_subsession(
    request: Request,
    conversation_id: str,
    session_id: str,
) -> SessionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        del row
        session_row = await get_session_row(session, session_id)
    if session_row is None or session_row.conversation_id != conversation_id:
        raise api_exception(404, "not_found", "Session not found in this conversation")
    if session_row.parent_session_id is None:
        raise api_exception(404, "not_found", "Session is not a sub-session")
    return session_to_response(session_row, include_result_content=True)
