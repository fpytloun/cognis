"""Idempotent schema creation shared by bootstrap and Alembic."""

from __future__ import annotations

from typing import cast

from sqlalchemy import Connection, Table, select, update

from cognis.store.models import (
    AppChannelDeliveryRow,
    AppContactBlockRow,
    Conversation,
    ManagedChannelBinding,
    ManagedConversationLink,
)


def ensure_app_contact_schema(connection: Connection) -> None:
    """Create recipient metadata without copying session content."""
    cast(Table, AppChannelDeliveryRow.__table__).create(connection, checkfirst=True)
    cast(Table, AppContactBlockRow.__table__).create(connection, checkfirst=True)


def repair_app_contact_titles(connection: Connection) -> None:
    """Replace v177 objective-derived titles, preserving recipient renames."""
    contacts = cast(Table, Conversation.__table__)
    sources = contacts.alias("contact_source")
    bindings = cast(Table, ManagedChannelBinding.__table__)
    links = cast(Table, ManagedConversationLink.__table__)
    statement = (
        select(
            contacts.c.conversation_id,
            contacts.c.title,
            contacts.c.context_data,
            bindings.c.binding_id,
            bindings.c.objective,
            sources.c.title.label("source_title"),
            links.c.title.label("link_title"),
        )
        .select_from(
            contacts.join(bindings, contacts.c.context_ref == bindings.c.binding_id)
            .join(links, bindings.c.link_id == links.c.link_id)
            .join(sources, links.c.target_conversation_id == sources.c.conversation_id)
        )
        .where(contacts.c.context_type == "web", contacts.c.title_source == "user")
    )
    # Retained only for upgrades from v177's persisted generated-title format.
    for row in connection.execute(statement).mappings().all():
        context = row["context_data"]
        if (
            not isinstance(context, dict)
            or context.get("app_contact_binding_id") != row["binding_id"]
        ):
            continue
        legacy_title = (row["objective"] or f"Contact from {context.get('agent_name', '')}")[:200]
        if row["title"] != legacy_title:
            continue
        title = (
            row["source_title"]
            or row["link_title"]
            or f"Chat with {context.get('agent_name') or 'agent'}"
        )[:200]
        connection.execute(
            update(contacts)
            .where(contacts.c.conversation_id == row["conversation_id"])
            .values(title=title, title_source="app_contact")
        )
