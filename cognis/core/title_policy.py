"""Canonical conversation title eligibility and publication policy."""

from __future__ import annotations

from typing import Any

from cognis.core.agent_direct import is_agent_direct_context
from cognis.core.events import Event, EventBus, EventType
from cognis.models.session import ConversationModel
from cognis.store.queries import update_conversation_context_data

INTARIS_TITLE_KEY = "intaris_latest_title"
INTARIS_TITLE_UPDATED_AT_KEY = "intaris_latest_title_at"


def _context_platform_data(conversation: ConversationModel) -> dict[str, object]:
    data = conversation.context.platform_data if conversation.context else {}
    return dict(data or {})


def can_generate_title(conversation: ConversationModel) -> bool:
    """Return whether Cognis may initialize an automatic visible title."""

    if is_agent_direct_context(
        conversation.context.type if conversation.context else None,
        conversation.context.platform_data if conversation.context else None,
    ):
        return False

    return conversation.title_source == "unset" and not (conversation.title or "").strip()


def latest_intaris_title(conversation: ConversationModel) -> str | None:
    """Return the latest stored Intaris title suggestion for a conversation."""

    return latest_intaris_title_from_platform_data(_context_platform_data(conversation))


def latest_intaris_title_from_platform_data(platform_data: dict[str, object] | None) -> str | None:
    """Return the latest stored Intaris title from serialized platform data."""

    value = (platform_data or {}).get(INTARIS_TITLE_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


async def sync_intaris_title(
    db_session: Any,
    conversation: ConversationModel,
    title: str | None,
    *,
    updated_at: str | None = None,
) -> bool:
    """Store an optional suggestion; the result never indicates a visible change."""

    normalized = (title or "").strip()[:200]
    if not normalized:
        return False

    platform_data = _context_platform_data(conversation)
    platform_data[INTARIS_TITLE_KEY] = normalized
    if updated_at:
        platform_data[INTARIS_TITLE_UPDATED_AT_KEY] = updated_at

    conversation.context.platform_data = platform_data
    changed = await update_conversation_context_data(
        db_session,
        conversation.conversation_id,
        context_data=platform_data,
    )

    return changed


async def publish_conversation_title_updated(
    event_bus: EventBus | None,
    *,
    conversation_id: str,
    title: str,
) -> None:
    """Notify live clients that a conversation title is now visible."""

    if event_bus is None:
        return
    await event_bus.publish(
        Event(
            type=EventType.CONVERSATION_UPDATED,
            data={"conversation_id": conversation_id, "title": title},
        )
    )
