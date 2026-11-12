"""Authoritative classification of legacy hook-created conversations."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from cognis.store.models import AgentHookRunRow, Conversation


def internal_hook_conversation_clause() -> ColumnElement[bool]:
    """Classify by persisted owner/agent/run linkage, never by title or ID prefix."""
    return exists(
        select(AgentHookRunRow.run_id).where(
            # Let the child index exclude inline history before JSON filtering.
            AgentHookRunRow.child_conversation_id.is_not(None),
            AgentHookRunRow.child_conversation_id == Conversation.conversation_id,
            AgentHookRunRow.conversation_id != Conversation.conversation_id,
            AgentHookRunRow.owner_email == Conversation.user_email,
            AgentHookRunRow.agent_id == Conversation.agent_id,
            AgentHookRunRow.configuration["action"].as_string() == "prompt",
            AgentHookRunRow.configuration["mode"].as_string().in_(["fork", "fresh"]),
        )
    )


async def is_internal_hook_conversation(db: AsyncSession, conversation_id: str) -> bool:
    """Check legacy execution provenance for delivery and access adapters."""
    return bool(
        await db.scalar(
            select(Conversation.conversation_id).where(
                Conversation.conversation_id == conversation_id,
                internal_hook_conversation_clause(),
            )
        )
    )
