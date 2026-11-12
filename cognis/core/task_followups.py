"""Task-owned delivery through the existing durable followup and workflow boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.followups import parse_follow_up_metadata
from cognis.core.task_execution import assert_task_execution_fence
from cognis.store.models import (
    Conversation,
    FollowUpDedupeRow,
    FollowUpIntentRow,
    Session,
    StepRun,
    Task,
)


@dataclass(frozen=True)
class TaskFollowUpOwner:
    """Immutable execution identity, not permission to resume a paused task."""

    task_id: str
    attempt_number: int
    step_run_id: str

    def payload(self) -> dict[str, Any]:
        """Return metadata stored in the existing durable intent."""
        return asdict(self)


async def task_follow_up_owner(
    db: AsyncSession,
    conversation_id: str,
    origin_session_id: str | None,
    origin_turn_id: str | None = None,
) -> TaskFollowUpOwner | None:
    """Resolve persisted task lineage, including rotated and delegated sessions."""
    if origin_session_id is None:
        conversation = await db.get(Conversation, conversation_id)
        origin_session_id = conversation.active_session_id if conversation else None
    if not origin_session_id:
        return None
    origin = (
        await db.execute(
            select(Session).where(
                or_(
                    Session.session_id == origin_session_id,
                    Session.intaris_session_id == origin_session_id,
                ),
                Session.conversation_id == conversation_id,
            )
        )
    ).scalar_one_or_none()
    if origin is None:
        return None
    lineage: set[str] = set()
    pending = [origin.session_id]
    while pending:
        session_id = pending.pop()
        if session_id in lineage:
            continue
        lineage.add(session_id)
        row = await db.get(Session, session_id)
        if row is None:
            continue
        steps = list(
            (await db.execute(select(StepRun).where(StepRun.session_id == session_id))).scalars()
        )
        step = next((s for s in steps if s.step_run_id == origin_turn_id), None)
        if step is None and origin_turn_id is None and len(steps) == 1:
            step = steps[0]
        if step is None:
            task_call = (row.delegation_metadata or {}).get("task_call") or {}
            step_id = task_call.get("step_run_id") or task_call.get("turn_id")
            if isinstance(step_id, str):
                step = await db.get(StepRun, step_id)
                if step is not None and step.conversation_id != conversation_id:
                    step = None
        if step is not None:
            return TaskFollowUpOwner(step.task_id, step.attempt_number, step.step_run_id)
        pending.extend(
            sid
            for sid in (row.parent_session_id, row.previous_session_id)
            if isinstance(sid, str) and sid not in lineage
        )
    # A StepRun may now point at a newer compacted session. Walk its retained
    # previous-session links, never treating a shared conversation as ownership.
    retained_steps = (
        await db.execute(select(StepRun).where(StepRun.conversation_id == conversation_id))
    ).scalars()
    matches = []
    for step in retained_steps:
        if origin_turn_id is not None and step.step_run_id != origin_turn_id:
            continue
        retained_session_id = step.session_id
        seen: set[str] = set()
        while retained_session_id and retained_session_id not in seen:
            if retained_session_id in lineage:
                matches.append(
                    TaskFollowUpOwner(step.task_id, step.attempt_number, step.step_run_id)
                )
                break
            seen.add(retained_session_id)
            row = await db.get(Session, retained_session_id)
            retained_session_id = row.previous_session_id if row else None
    return matches[0] if len(matches) == 1 else None


async def pending_task_follow_ups(
    db: AsyncSession,
    *,
    task_id: str,
    attempt_number: int,
    step_run_id: str,
    consumer_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read workflow-owned intents without claiming a competing direct turn."""
    await assert_task_execution_fence(db)
    step = await db.get(StepRun, step_run_id)
    task = await db.get(Task, task_id)
    if (
        step is None
        or task is None
        or step.task_id != task_id
        or step.attempt_number != attempt_number
        or task.attempt_number != attempt_number
        or step.superseded_by_step_run_id is not None
    ):
        return []
    consumer_session_id = consumer_session_id or step.session_id
    consumer = await db.get(Session, consumer_session_id) if consumer_session_id else None
    lineage: set[str] = set()
    cursor = consumer
    while cursor is not None and cursor.session_id not in lineage:
        lineage.update((cursor.session_id, cursor.intaris_session_id or cursor.session_id))
        cursor = (
            await db.get(Session, cursor.previous_session_id)
            if cursor.previous_session_id
            else None
        )
    owner = TaskFollowUpOwner(task_id, attempt_number, step_run_id)
    rows = (
        await db.execute(
            select(FollowUpIntentRow)
            .where(
                FollowUpIntentRow.conversation_id == step.conversation_id,
                FollowUpIntentRow.status == "pending",
            )
            .order_by(FollowUpIntentRow.created_at, FollowUpIntentRow.intent_id)
        )
    ).scalars()
    items = []
    for row in rows:
        payload = row.event_payload
        if payload.get("task_owner") != owner.payload():
            continue
        actual = await task_follow_up_owner(
            db,
            row.conversation_id,
            payload.get("origin_session_id"),
            payload.get("origin_turn_id"),
        )
        if actual != owner:
            continue
        origin_id = payload.get("origin_session_id") or step.session_id
        if origin_id not in lineage:
            source = await db.scalar(
                select(Session).where(
                    or_(Session.session_id == origin_id, Session.intaris_session_id == origin_id)
                )
            )
            seen: set[str] = set()
            while source is not None and source.session_id not in seen:
                seen.add(source.session_id)
                successor = await db.scalar(
                    select(Session).where(
                        Session.previous_session_id == source.session_id,
                        Session.conversation_id == source.conversation_id,
                        Session.user_email == source.user_email,
                    )
                )
                if successor is None:
                    break
                source = successor
            while source is not None and source.status in {
                "completed",
                "failed",
                "cancelled",
                "terminated",
            }:
                if source.parent_session_id in lineage:
                    break
                source = (
                    await db.get(Session, source.parent_session_id)
                    if source.parent_session_id
                    else None
                )
            else:
                continue
        items.append(
            {
                "content": "",
                "system_initiated": True,
                "source": "task_follow_up",
                "follow_up": parse_follow_up_metadata(payload["follow_up"]),
                "durable_request_id": row.follow_up_id,
                "follow_up_id": row.follow_up_id,
                "conversation_id": row.conversation_id,
            }
        )
    return items


async def acknowledge_task_follow_up(db: AsyncSession, item: dict[str, Any]) -> None:
    """Retire the intent only after its canonical boundary append is confirmed."""
    await assert_task_execution_fence(db)
    identity = {
        "conversation_id": item["conversation_id"],
        "follow_up_id": item["follow_up_id"],
    }
    for model, status in ((FollowUpIntentRow, "submitted"), (FollowUpDedupeRow, "handled")):
        await db.execute(
            update(model)
            .where(
                model.conversation_id == identity["conversation_id"],
                model.follow_up_id == identity["follow_up_id"],
            )
            .values(status=status, lease_owner=None, lease_expires_at=None)
        )
