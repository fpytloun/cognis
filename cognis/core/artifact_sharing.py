"""Explicit content grants. Read access never implies permission to reshare."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import ArtifactGrantRow, ArtifactRecordRow, DeliverableRow


async def grant_referenced_inputs(
    session: AsyncSession,
    *,
    owner_email: str,
    source_agent_id: str,
    target_session_id: str,
    text: str,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
) -> None:
    """Grant only exact authorized refs explicitly handed to a worker."""
    import re

    from cognis.core.artifact_access import content_read_predicate
    from cognis.core.artifact_entitlements import add_grant
    from cognis.store.models import Session

    refs = set(re.findall(r"\b(?:art|att|doc|img|dlv)_[0-9a-f]{32}\b", text))
    if not refs:
        return
    target = await session.get(Session, target_session_id)
    if target is None or target.user_email != owner_email:
        raise ValueError("Artifact input recipient is unavailable")
    recipient = (
        f"scope:{target.artifact_scope_id or target.session_id}"
        if target.agent_id.startswith("system:")
        else target.agent_id
    )
    for model, ref_col in (
        (ArtifactRecordRow, ArtifactRecordRow.artifact_id),
        (DeliverableRow, DeliverableRow.deliverable_id),
    ):
        predicate = await content_read_predicate(
            session,
            owner_email=owner_email,
            agent_id=source_agent_id,
            conversation_id=source_conversation_id,
            session_id=source_session_id,
            model=model,
        )
        for ref in await session.scalars(sa.select(ref_col).where(ref_col.in_(refs), predicate)):
            await session.run_sync(add_grant, owner_email, ref, recipient, "input")


async def grant_artifact_inputs(
    session: AsyncSession,
    *,
    owner_email: str,
    agent_id: str,
    content_refs: list[str],
) -> None:
    """Persist only already-authorized input refs at the trusted admission boundary."""
    for content_ref in set(content_refs):
        key = dict(
            owner_email=owner_email, content_ref=content_ref, agent_id=agent_id, source="input"
        )
        if await session.get(ArtifactGrantRow, key) is None:
            session.add(ArtifactGrantRow(**key))


async def set_artifact_share(
    session: AsyncSession,
    *,
    owner_email: str,
    owner_agent_id: str | None,
    content_ref: str,
    grantee_agent_id: str,
    revoke: bool = False,
    permission: str = "read",
) -> None:
    """Grant/revoke read access as the owning agent or authenticated human owner."""
    await manage_document_shares(
        session,
        owner_email=owner_email,
        owner_agent_id=owner_agent_id,
        content_ref=content_ref,
        action="revoke" if revoke else "grant",
        agent_id=grantee_agent_id,
        permission=permission,
    )


async def manage_document_shares(
    session: AsyncSession,
    *,
    owner_email: str,
    owner_agent_id: str | None,
    content_ref: str,
    action: str,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
    write_agent_ids: list[str] | None = None,
    permission: str = "read",
) -> dict[str, Any]:
    """Use Library lineage sharing; the caller transaction owns commit/cancellation.

    Lineage locks serialize grant replacement with revisions. No ownership or
    document ownership changes, external I/O, or automatic retries are involved.
    """
    from cognis.core.document_sharing import (
        AgentSharesRequest,
        document_access,
        owned_document,
        replace_explicit_shares,
        validate_recipients,
    )

    if action not in {"get", "set", "grant", "revoke"}:
        raise ValueError("Unknown sharing action.")
    if permission not in {"read", "write"}:
        raise ValueError("permission must be read or write")
    kind = "deliverable" if content_ref.startswith("dlv_") else "artifact"
    try:
        row = await owned_document(session, kind, content_ref, owner_email, lock=action != "get")
    except LookupError as exc:
        raise ValueError("Content reference or recipient is unavailable.") from exc
    if owner_agent_id is not None and row.owner_agent_id != owner_agent_id:
        raise ValueError("Content reference or recipient is unavailable.")
    access = await document_access(session, row, owner_email)
    if action == "get":
        return access.model_dump(mode="json")
    if action == "set":
        if agent_ids is None:
            raise ValueError("agent_ids is required")
        selected = AgentSharesRequest(agent_ids=agent_ids).agent_ids
        writers = write_agent_ids or []
    else:
        if not agent_id:
            raise ValueError("agent_id is required")
        await validate_recipients(session, owner_email, [agent_id])
        selected = [agent.agent_id for agent in access.agents if agent.explicit]
        writers = [
            agent.agent_id for agent in access.agents if agent.explicit_permission == "write"
        ]
        writers = [recipient for recipient in writers if recipient != agent_id]
        if action == "grant":
            selected = list(dict.fromkeys([*selected, agent_id]))
            if permission == "write":
                writers.append(agent_id)
        else:
            selected = [recipient for recipient in selected if recipient != agent_id]
    return (await replace_explicit_shares(session, row, owner_email, selected, writers)).model_dump(
        mode="json"
    )
