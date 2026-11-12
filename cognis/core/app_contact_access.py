"""Recipient-facing projections of sender-owned managed app channels."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import Agent, AppChannelDeliveryRow, Conversation


def app_contact_binding_id(conversation: Conversation) -> str | None:
    """Return the app-client route marker without granting any access."""
    value = (conversation.context_data or {}).get("app_contact_binding_id")
    return value if isinstance(value, str) and value else None


async def can_read_app_contact(
    session: AsyncSession, conversation: Conversation, user_email: str
) -> bool:
    """Require a published transport entitlement, not forgeable client metadata."""
    binding_id = app_contact_binding_id(conversation)
    if conversation.user_email != user_email or binding_id is None:
        return False
    return (
        await session.scalar(
            select(AppChannelDeliveryRow.delivery_id)
            .where(
                AppChannelDeliveryRow.recipient_email == user_email,
                AppChannelDeliveryRow.destination_conversation_id == conversation.conversation_id,
                AppChannelDeliveryRow.binding_id == binding_id,
                AppChannelDeliveryRow.published_at.is_not(None),
            )
            .limit(1)
        )
    ) is not None


async def list_app_contact_agents(
    session: AsyncSession, user_email: str
) -> list[tuple[Agent, Conversation]]:
    """Return the newest published contact per agent, without granting agent use."""
    rows = (
        await session.execute(
            select(Agent, Conversation)
            .join(Conversation, Conversation.agent_id == Agent.agent_id)
            .where(
                Conversation.user_email == user_email,
                Conversation.status == "active",
                select(AppChannelDeliveryRow.delivery_id)
                .where(
                    AppChannelDeliveryRow.recipient_email == user_email,
                    AppChannelDeliveryRow.destination_conversation_id
                    == Conversation.conversation_id,
                    AppChannelDeliveryRow.binding_id.is_not(None),
                    AppChannelDeliveryRow.published_at.is_not(None),
                )
                .exists(),
            )
            .order_by(Conversation.last_message_at.desc(), Conversation.conversation_id.desc())
        )
    ).all()
    seen: set[str] = set()
    result: list[tuple[Agent, Conversation]] = []
    for agent, conversation in rows:
        if agent.agent_id not in seen:
            seen.add(agent.agent_id)
            result.append((agent, conversation))
    return result
