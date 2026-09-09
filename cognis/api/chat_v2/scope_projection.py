from __future__ import annotations

from typing import Any

from cognis.api.chat_v2.schemas import TimelineScope


def project_session_scope(*, root_session: Any, current_session: Any) -> TimelineScope:
    """Project the canonical session scope used by Work and Chat v2."""

    return TimelineScope(
        key=f"session:{root_session.session_id}",
        kind="session",
        conversation_id=root_session.conversation_id,
        session_id=root_session.session_id,
        parent_session_id=root_session.parent_session_id,
        label=root_session.delegation_task or root_session.agent_id,
        status=current_session.status,
    )


def project_task_step_scope(
    *,
    step_run: Any,
    session_row: Any | None,
    conversation_id: str | None,
) -> TimelineScope:
    """Project the canonical task-step scope used by Work and Chat v2."""

    return TimelineScope(
        key=f"task_step:{step_run.step_run_id}",
        kind="task_step",
        conversation_id=conversation_id,
        session_id=step_run.session_id,
        task_id=step_run.task_id,
        step_run_id=step_run.step_run_id,
        parent_session_id=session_row.parent_session_id if session_row is not None else None,
        label=f"{step_run.step_name} (attempt {step_run.attempt_number})",
        status=step_run.status,
        missing_stream=session_row is None,
    )
