"""Payload-independent authorization for individual and grouped document reads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.artifact_access import readable_deliverable_ids
from cognis.core.content_refs import authorize_deliverable_access
from cognis.store.document_people import person_may_read_document
from cognis.store.document_visibility import unmanaged_deliverable_clause
from cognis.store.models import Conversation, DeliverableRow
from cognis.store.queries import get_conversation


@dataclass(frozen=True)
class AuthorizedDeliverableRead:
    """Authorized metadata and the existing missing-payload visibility policy."""

    row: DeliverableRow
    kind: Literal["owned", "reference", "project"]


async def readable_deliverables(
    session: AsyncSession,
    deliverable_ids: Sequence[str],
    caller_email: str,
    *,
    accessor_conversation_id: str | None = None,
    allow_unscoped_ref: bool = False,
    allow_project_reader: bool = True,
) -> dict[str, AuthorizedDeliverableRead]:
    """Apply the detail-read policy without loading bytes or retaining a DB lease.

    Timeline membership is not authority. Ordinary owner reads are resolved in
    bulk; task, managed, and project references use the existing access policy.
    The caller must finish the transaction before loading returned payloads.
    """
    ids = list(dict.fromkeys(deliverable_ids))
    if not ids:
        return {}
    rows = {
        row.deliverable_id: row
        for row in await session.scalars(
            select(DeliverableRow).where(DeliverableRow.deliverable_id.in_(ids))
        )
    }
    if not rows:
        return {}
    unmanaged = set(
        await session.scalars(
            select(DeliverableRow.deliverable_id).where(
                DeliverableRow.deliverable_id.in_(rows),
                unmanaged_deliverable_clause(DeliverableRow),
            )
        )
    )
    directly_owned = set(
        await session.scalars(
            select(DeliverableRow.deliverable_id)
            .join(Conversation, Conversation.conversation_id == DeliverableRow.conversation_id)
            .where(
                DeliverableRow.deliverable_id.in_(rows),
                DeliverableRow.step_run_id.is_(None),
                Conversation.user_email == caller_email,
            )
        )
    )
    accessor_agent_id = None
    if accessor_conversation_id:
        accessor = await get_conversation(session, accessor_conversation_id)
        if accessor is not None and accessor.user_email == caller_email:
            accessor_agent_id = accessor.agent_id
    scoped_owned = {
        id
        for id, row in rows.items()
        if row.owner_email == caller_email and not (id in directly_owned and id in unmanaged)
    }
    scoped_readable = (
        await readable_deliverable_ids(
            session,
            sorted(scoped_owned),
            owner_email=caller_email,
            agent_id=accessor_agent_id,
            conversation_id=accessor_conversation_id,
        )
        if accessor_conversation_id and scoped_owned
        else set()
    )
    readable: dict[str, AuthorizedDeliverableRead] = {}
    for id in ids:
        row = rows.get(id)
        if row is None:
            continue
        if id in directly_owned and id in unmanaged:
            readable[id] = AuthorizedDeliverableRead(row, "owned")
            continue
        if accessor_conversation_id or allow_unscoped_ref:
            if accessor_conversation_id and row.owner_email == caller_email:
                if id in scoped_readable:
                    readable[id] = AuthorizedDeliverableRead(row, "reference")
                continue
            access = await authorize_deliverable_access(
                session,
                row,
                caller_email,
                accessor_conversation_id=accessor_conversation_id,
                accessor_agent_id=accessor_agent_id,
            )
            if access is not None:
                if accessor_conversation_id or id in unmanaged:
                    # The detail route previously fell through to explicit
                    # sharing after a missing reference payload.
                    project_readable = (
                        allow_project_reader
                        and row.owner_email != caller_email
                        and id in unmanaged
                        and await person_may_read_document(session, row, caller_email)
                    )
                    readable[id] = AuthorizedDeliverableRead(
                        row, "project" if project_readable else "reference"
                    )
                continue
        if (
            allow_project_reader
            and row.owner_email != caller_email
            and id in unmanaged
            and await person_may_read_document(session, row, caller_email)
        ):
            readable[id] = AuthorizedDeliverableRead(row, "project")
    return readable
