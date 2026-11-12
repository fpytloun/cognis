"""Shared, permission-aware conversation list presentation."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.attention_actions import (
    list_attention_rows_by_conversation,
    project_attention_summary,
)
from cognis.api.conversation_identity import enrich_conversation_identity
from cognis.api.models import ConversationResponse
from cognis.api.serializers import conversation_to_response
from cognis.core.conversation_state import snapshots_for_conversations
from cognis.store.models import Conversation
from cognis.store.queries import (
    list_conversation_todos_by_conversation,
    list_managed_conversation_links_for_targets,
    list_pending_notification_types_by_conversation,
    list_sessions_by_ids,
)


async def conversation_list_summaries(
    session: AsyncSession,
    rows: list[Conversation],
    *,
    user_email: str,
    can_mutate: bool,
    active_turn_states: dict[str, Any],
    include_attention_actions: bool,
) -> list[ConversationResponse]:
    """Present authorized rows with the same attention state on every list surface."""
    own_rows = [row for row in rows if row.user_email == user_email]
    own_ids = [row.conversation_id for row in own_rows]
    active_sessions = await list_sessions_by_ids(
        session, [row.active_session_id for row in own_rows if row.active_session_id]
    )
    pending_notifications = await list_pending_notification_types_by_conversation(
        session, user_email, own_ids
    )
    attention_rows = (
        await list_attention_rows_by_conversation(
            session, owner_email=user_email, conversation_ids=own_ids
        )
        if include_attention_actions
        else {}
    )
    active_rows = [
        row for row in own_rows if active_turn_states.get(row.conversation_id) is not None
    ]
    todo_snapshots = await list_conversation_todos_by_conversation(
        session, [row.conversation_id for row in active_rows]
    )
    conversation_states = await snapshots_for_conversations(
        session,
        user_email=user_email,
        conversations=active_rows,
        running_turn_states=active_turn_states,
        active_sessions=active_sessions,
        conversation_todos=todo_snapshots,
    )
    managed_links = await list_managed_conversation_links_for_targets(
        session,
        [
            row.conversation_id
            for row in rows
            if (row.context_data or {}).get("kind") in {"agent_work", "managed_agent_conversation"}
        ],
        user_email=user_email,
    )
    items = [
        conversation_to_response(
            row,
            viewer_email=user_email,
            has_active_turn=active_turn_states.get(row.conversation_id) is not None,
            active_session=(
                active_sessions.get(row.active_session_id) if row.active_session_id else None
            ),
            active_turn_state=active_turn_states.get(row.conversation_id),
            pending_notification_types=pending_notifications.get(row.conversation_id, []),
            attention_actions=[
                project_attention_summary(action_row, can_mutate=can_mutate)
                for action_row in attention_rows.get(row.conversation_id, [])
            ],
            conversation_state=conversation_states.get(row.conversation_id),
            managed_link=managed_links.get(row.conversation_id),
        )
        for row in rows
    ]
    return await enrich_conversation_identity(
        session, items, user_email=user_email, can_mutate=can_mutate
    )
