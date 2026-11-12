"""Persist content-specific work entitlements before their provenance can disappear."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session as DBSession

from cognis.core.artifact_access import _work_edges
from cognis.store.models import (
    Agent,
    ArtifactGrantRow,
    ArtifactRecordRow,
    Conversation,
    DeliverableRow,
    Session,
    StepRun,
    Task,
)


def add_grant(
    db: DBSession, owner: str, ref: str, recipient: str, source: str, permission: str = "read"
) -> None:
    key = dict(owner_email=owner, content_ref=ref, agent_id=recipient, source=source)
    if db.get(ArtifactGrantRow, key) is None:
        db.add(ArtifactGrantRow(**key, permission=permission))


def session_ancestors(db: DBSession, producer: Session) -> list[Session]:
    pending, result, seen = [producer], [], set()
    while pending:
        item = pending.pop()
        if item.session_id in seen or item.user_email != producer.user_email:
            continue
        seen.add(item.session_id)
        result.append(item)
        task = db.scalar(
            sa.select(Task)
            .join(StepRun, StepRun.task_id == Task.task_id)
            .where(StepRun.session_id == item.session_id, Task.created_by == producer.user_email)
        )
        for source in (
            item.parent_session_id,
            item.previous_session_id,
            item.source_session_id,
            task.source_session_id if task else None,
        ):
            parent = db.get(Session, source) if source else None
            if parent is not None:
                pending.append(parent)
    return result


def effective_kb_agent(db: DBSession, producer: Session) -> str | None:
    """Resolve execution's primary identity without using it for artifact ownership reads."""
    for source in session_ancestors(db, producer):
        task = db.scalar(
            sa.select(Task)
            .join(StepRun, StepRun.task_id == Task.task_id)
            .where(StepRun.session_id == source.session_id, Task.created_by == producer.user_email)
        )
        if task and task.agent_id and not task.agent_id.startswith("system:"):
            return task.agent_id
        if not source.agent_id.startswith("system:"):
            return source.agent_id
        conversation = db.get(Conversation, source.conversation_id)
        if (
            conversation
            and conversation.user_email == producer.user_email
            and not conversation.agent_id.startswith("system:")
        ):
            return conversation.agent_id
    return None


def content_recipients(db: DBSession, row: ArtifactRecordRow | DeliverableRow) -> list[str]:
    """Reverse the canonical graph once, then match primary-agent seed nodes."""
    owner = row.owner_email
    if not owner:
        return []
    seeds = []
    if row.session_id is not None:
        seeds.append(sa.select(sa.literal("s:" + row.session_id).label("node")))
    elif row.conversation_id is not None:
        seeds.append(sa.select(sa.literal("c:" + row.conversation_id).label("node")))
    if isinstance(row, DeliverableRow) and row.step_run_id is not None:
        seeds.append(
            sa.select((sa.literal("t:") + StepRun.task_id).label("node")).where(
                StepRun.step_run_id == row.step_run_id
            )
        )
    if not seeds:
        return []
    seed_rows = sa.union(*seeds).subquery()
    ancestors = sa.select(seed_rows.c.node).cte("content_work_ancestors", recursive=True)
    edges = _work_edges(owner_email=owner)
    ancestors = ancestors.union(
        sa.select(edges.c.parent).join(ancestors, ancestors.c.node == edges.c.child)
    )
    nodes = sa.select(ancestors.c.node)
    return list(
        db.scalars(
            sa.select(Agent.agent_id).where(
                Agent.owner_email == owner,
                ~Agent.agent_id.startswith("system:"),
                sa.or_(
                    sa.exists().where(
                        Conversation.user_email == owner,
                        Conversation.agent_id == Agent.agent_id,
                        (sa.literal("c:") + Conversation.conversation_id).in_(nodes),
                    ),
                    sa.exists().where(
                        Task.created_by == owner,
                        sa.or_(
                            Task.agent_id == Agent.agent_id,
                            Task.created_by_agent_id == Agent.agent_id,
                        ),
                        (sa.literal("t:") + Task.task_id).in_(nodes),
                    ),
                ),
            )
        )
    )


def initialize_content_owner(db: DBSession, row: ArtifactRecordRow | DeliverableRow) -> None:
    """Resolve omitted or transient ownership before first publication."""
    if not row.owner_email:
        return
    owner = row.owner_email
    producer = db.get(Session, row.session_id) if row.session_id else None
    if producer is not None and producer.user_email != owner:
        return
    if not row.owner_agent_id or row.owner_agent_id.startswith("system:"):
        task = (
            db.scalar(
                sa.select(Task)
                .join(StepRun, StepRun.task_id == Task.task_id)
                .where(StepRun.session_id == row.session_id, Task.created_by == owner)
            )
            if row.session_id
            else None
        )
        conversation = db.get(Conversation, row.conversation_id) if row.conversation_id else None
        candidates = [
            task.agent_id if task else None,
            producer.agent_id if producer else None,
            conversation.agent_id if conversation and conversation.user_email == owner else None,
        ]
        cursor, seen = producer, set()
        while cursor is not None and cursor.session_id not in seen:
            seen.add(cursor.session_id)
            candidates.append(cursor.agent_id)
            cursor = db.get(Session, cursor.parent_session_id) if cursor.parent_session_id else None
            if cursor is not None and cursor.user_email != owner:
                break
        row.owner_agent_id = next(
            (item for item in candidates if item and not item.startswith("system:")), None
        )


def register_content_entitlements(db: DBSession, row: ArtifactRecordRow | DeliverableRow) -> None:
    """Snapshot validated read provenance without changing initialized ownership."""
    if not row.owner_email:
        return
    owner = row.owner_email
    ref = row.artifact_id if isinstance(row, ArtifactRecordRow) else row.deliverable_id
    predecessor = (
        row.supersedes_artifact_id
        if isinstance(row, ArtifactRecordRow)
        else row.supersedes_deliverable_id
    )
    if not predecessor and row.lineage_id and row.lineage_id != ref:
        predecessor = row.lineage_id
    if predecessor:
        # Lineage grants belong to the original owner, independently of whether
        # the producer's cross-user project context can supply read provenance.
        for recipient, permission in db.execute(
            sa.select(ArtifactGrantRow.agent_id, ArtifactGrantRow.permission).where(
                ArtifactGrantRow.owner_email == owner,
                ArtifactGrantRow.content_ref == predecessor,
                ArtifactGrantRow.source == "explicit",
            )
        ):
            add_grant(db, owner, ref, recipient, "explicit", permission)
    producer = db.get(Session, row.session_id) if row.session_id else None
    if producer is not None and producer.user_email != owner:
        return
    if producer is not None:
        for ancestor in session_ancestors(db, producer):
            add_grant(
                db,
                owner,
                ref,
                f"scope:{ancestor.artifact_scope_id or ancestor.session_id}",
                "output",
            )
    if row.owner_agent_id:
        add_grant(db, owner, ref, row.owner_agent_id, "output")
    db.flush()
    for agent_id in content_recipients(db, row):
        if agent_id == row.owner_agent_id:
            continue
        add_grant(db, owner, ref, agent_id, "output")


def inherit_session_content(db: DBSession, target: Session) -> None:
    """Copy retained source context for explicit retry/follow-up/fork, not parent storage."""
    if not target.source_session_id:
        return
    source = db.get(Session, target.source_session_id)
    if (
        source is None
        or source.user_email != target.user_email
        or source.agent_id != target.agent_id
    ):
        return
    source_scope = f"scope:{source.artifact_scope_id or source.session_id}"
    target_scope = f"scope:{target.artifact_scope_id or target.session_id}"
    for grant in list(
        db.scalars(
            sa.select(ArtifactGrantRow).where(
                ArtifactGrantRow.owner_email == target.user_email,
                ArtifactGrantRow.agent_id == source_scope,
            )
        )
    ):
        add_grant(db, target.user_email, grant.content_ref, target_scope, "input")
