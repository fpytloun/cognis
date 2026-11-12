"""Validated managed-work provenance, independent of content or usage authority."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import aliased

from cognis.store.models import Conversation, ManagedConversationLink


def managed_lineage(*, owner_email: Any, name: str) -> Any:
    """Return canonical owner-bound root/parent links, including closed work.

    This is provenance only. Callers must apply their own current access policy.
    Keep the depth and binding checks shared with content grant registration.
    """
    m = ManagedConversationLink
    controller, target = aliased(Conversation), aliased(Conversation)
    valid = (
        sa.select(
            m.link_id,
            m.parent_link_id,
            m.root_link_id,
            m.depth,
            m.kind,
            m.controller_conversation_id,
            m.controller_agent_id,
            m.controller_session_id,
            m.target_conversation_id,
            m.target_agent_id,
        )
        .join(controller, controller.conversation_id == m.controller_conversation_id)
        .join(target, target.conversation_id == m.target_conversation_id)
        .where(
            m.user_email == owner_email,
            controller.user_email == owner_email,
            target.user_email == owner_email,
            controller.agent_id == m.controller_agent_id,
            target.agent_id == m.target_agent_id,
        )
    )
    result = valid.where(m.parent_link_id.is_(None), m.root_link_id == m.link_id, m.depth == 1).cte(
        name, recursive=True
    )
    return result.union_all(
        valid.join(result, m.parent_link_id == result.c.link_id).where(
            m.root_link_id == result.c.root_link_id,
            m.depth == result.c.depth + 1,
            m.controller_conversation_id == result.c.target_conversation_id,
            m.controller_agent_id == result.c.target_agent_id,
            m.depth <= 2,
        )
    )
