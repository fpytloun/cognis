"""Minimal agent identity for the native channel client, never agent access."""

from __future__ import annotations

from cognis.api.models import AgentResponse
from cognis.store.models import Agent, Conversation


def contact_agent_response(agent: Agent, conversation: Conversation) -> AgentResponse:
    """Expose only identity already present in delivered channel messages."""
    return AgentResponse(
        agent_id=agent.agent_id,
        owner_email=agent.owner_email,
        name=agent.name,
        display_name=agent.display_name,
        description="Incoming managed channel",
        status="active",
        is_contact_only=True,
        contact_conversation_id=conversation.conversation_id,
    )
