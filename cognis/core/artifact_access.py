"""Shared conversation-scoped artifact authorization."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def artifact_authorized_for_conversation(
    session: AsyncSession,
    *,
    artifact: Any | None,
    owner_email: str | None,
    conversation_id: str | None,
    agent_id: str | None = None,
) -> bool:
    """Authorize an artifact without loading its bytes.

    Same-conversation and owner-global artifacts are directly accessible. Artifacts
    attached to another conversation are accessible across a managed ancestor path
    in either direction; siblings are intentionally denied.
    """

    if artifact is None or not owner_email or not conversation_id:
        return False
    if artifact.owner_email not in {None, owner_email}:
        return False
    source_conversation_id = getattr(artifact, "conversation_id", None)
    if source_conversation_id is None or source_conversation_id == conversation_id:
        return True

    from cognis.store.queries import (
        get_conversation,
        get_managed_conversation_ancestry,
        get_managed_conversation_link_for_target,
    )

    accessor = await get_conversation(session, conversation_id)
    source = await get_conversation(session, source_conversation_id)
    if (
        accessor is None
        or source is None
        or accessor.user_email != owner_email
        or source.user_email != owner_email
        or (agent_id is not None and accessor.agent_id != agent_id)
    ):
        return False

    async def _is_managed_ancestor(*, ancestor: Any, descendant: Any) -> bool:
        link = await get_managed_conversation_link_for_target(
            session, descendant.conversation_id, user_email=owner_email
        )
        if link is None or link.depth > 2 or link.target_agent_id != descendant.agent_id:
            return False
        try:
            ancestry = await get_managed_conversation_ancestry(
                session, link, user_email=owner_email
            )
        except ValueError:
            return False
        return any(
            item.controller_conversation_id == ancestor.conversation_id
            and item.controller_agent_id == ancestor.agent_id
            for item in ancestry
        )

    return await _is_managed_ancestor(
        ancestor=accessor,
        descendant=source,
    ) or await _is_managed_ancestor(
        ancestor=source,
        descendant=accessor,
    )
