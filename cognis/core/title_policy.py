"""Conversation title ownership and Intaris adoption rules."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.session import ConversationModel
from cognis.store.queries import update_conversation

PROTECTED_TITLE_SOURCES = frozenset({"manual", "channel_seed"})


def can_adopt_intaris_title(conversation: ConversationModel) -> bool:
    """Return True when Cognis may update the conversation title from Intaris."""

    return conversation.title_source not in PROTECTED_TITLE_SOURCES


async def sync_intaris_title(
    db_session: AsyncSession,
    conversation: ConversationModel,
    title: str | None,
) -> bool:
    """Persist an Intaris-generated title when conversation ownership allows it."""

    normalized = (title or "").strip()[:200]
    if not normalized or not can_adopt_intaris_title(conversation):
        return False
    if conversation.title == normalized and conversation.title_source == "intaris":
        return False
    ok = await update_conversation(
        db_session,
        conversation.conversation_id,
        title=normalized,
        title_source="intaris",
    )
    if ok:
        conversation.title = normalized
        conversation.title_source = "intaris"
    return ok
