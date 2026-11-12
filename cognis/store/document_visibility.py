"""Managed outputs require their explicit context, not global human grants."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from cognis.store.models import ManagedConversationLink


def unmanaged_deliverable_clause(model: Any) -> ColumnElement[bool]:
    """Apply the same managed-target fence to an ORM class or persisted row."""
    if model.conversation_id is None:
        return sa.true()
    return ~sa.exists().where(
        ManagedConversationLink.target_conversation_id == model.conversation_id
    )
