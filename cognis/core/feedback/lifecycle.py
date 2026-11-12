"""Project only the execution identified by an admission or explicit child link."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models import feedback as dto
from cognis.store.feedback_models import FeedbackWorkLinkRow
from cognis.store.models import DirectTurnRequestRow, Session, Task


def task_work(status: str) -> dto.FeedbackWorkState:
    """Translate a retained task-attempt state, never a different current attempt."""
    states: dict[str, dto.FeedbackWorkState] = {
        "draft": "queued",
        "submitted": "queued",
        "pending": "queued",
        "queued": "queued",
        "ready": "queued",
        "running": "working",
        "paused": "waiting_for_input",
        "done": "completed",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    return states.get(status, "unavailable")


def turn_work(status: str) -> dto.FeedbackWorkState:
    """Translate the exact durable request rather than conversation activity."""
    states: dict[str, dto.FeedbackWorkState] = {
        "queued": "queued",
        "claimed": "queued",
        "running": "working",
        "recoverable": "waiting_for_input",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    return states.get(status, "unavailable")


async def child_work(db: AsyncSession, link: FeedbackWorkLinkRow) -> dto.ChildWorkResponse:
    """Read a child's exact linked turn/attempt, preserving superseded terminal evidence."""
    work: dto.FeedbackWorkState = "unavailable"
    if link.kind == "child_conversation" and link.turn_id:
        request = await db.scalar(
            select(DirectTurnRequestRow).where(
                DirectTurnRequestRow.conversation_id == link.reference_id,
                DirectTurnRequestRow.turn_id == link.turn_id,
            )
        )
        if request:
            work = turn_work(request.status)
    elif link.kind == "child_task":
        if link.state:
            work = task_work(link.state)
        else:
            task = await db.get(Task, link.reference_id)
            if task and task.attempt_number == link.attempt_number:
                work = task_work(task.status)
    elif link.kind == "child_session":
        session = await db.get(Session, link.reference_id)
        if session and session.parent_session_id and session.delegation_mode:
            states: dict[str, dto.FeedbackWorkState] = {
                "active": "working",
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
            }
            work = states.get(session.status, "unavailable")
    return dto.ChildWorkResponse(
        work_link_id=link.link_id,
        submission_id=link.submission_id,
        conversation_id=link.reference_id if link.kind == "child_conversation" else None,
        task_id=link.reference_id if link.kind == "child_task" else None,
        session_id=link.reference_id if link.kind == "child_session" else None,
        attempt_number=link.attempt_number,
        turn_id=link.turn_id,
        work=work,
    )
