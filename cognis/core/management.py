"""Shared management helpers for agent tools and API routes."""

from __future__ import annotations

import uuid
from typing import Any

from cognis.api.models import PendingPauseResponse, WorkflowRunResponse
from cognis.api.serializers import (
    pending_pause_to_response,
    workflow_run_to_response,
    workflow_to_response,
)
from cognis.core.agent_loop import PauseResolution, PendingPause
from cognis.core.notifications import NotificationService
from cognis.core.question_sets import (
    normalize_context,
    normalize_questions,
    validate_reply_for_questions,
)
from cognis.models.task import TaskModel
from cognis.models.workflow import WorkflowState
from cognis.store.queries import get_task, get_workflow, update_task_workflow_state

_ACTIVE_WORKFLOW_TASK_STATUSES = {"queued", "ready", "running", "paused"}


def task_pending_pause_response(pause_waiter: Any, task: TaskModel) -> PendingPauseResponse | None:
    """Return the live or recovered pending pause for a task."""

    live_pause = pause_waiter.find_pending(task_id=task.task_id)
    if live_pause is not None:
        return pending_pause_to_response(live_pause)
    if task.workflow_state is None or task.workflow_state.pending_pause_type is None:
        return None

    payload = task.workflow_state.pending_pause_payload or {}
    recovered_pause = PendingPause(
        pause_id=str(payload.get("pause_id", f"recovered_{uuid.uuid4().hex[:8]}")),
        pause_type=task.workflow_state.pending_pause_type or "unknown",
        task_id=task.task_id,
        step_name=payload.get("step_name"),
        step_run_id=payload.get("step_run_id"),
        session_id=payload.get("session_id"),
        question=payload.get("message") or payload.get("question"),
        options=payload.get("options") if isinstance(payload.get("options"), list) else None,
        questions=normalize_questions(payload.get("questions"))
        if payload.get("questions") is not None
        else None,
        context=normalize_context(payload.get("context")),
    )
    return pending_pause_to_response(recovered_pause)


async def task_workflow_run_response(
    task: TaskModel,
    *,
    workflow_registry: Any,
    pending_pause: PendingPauseResponse | None,
) -> WorkflowRunResponse | None:
    """Return current workflow-run state for a task."""

    if task.workflow_state is None:
        return None
    current_step_name: str | None = None
    if task.workflow_id:
        try:
            workflow = await workflow_registry.get(
                task.workflow_id,
                owner_email=task.created_by,
                project_id=task.project_id,
            )
        except TypeError:
            workflow = await workflow_registry.get(task.workflow_id)
        if workflow is not None and task.workflow_state.current_step_index < len(workflow.steps):
            current_step_name = workflow.steps[task.workflow_state.current_step_index].name
    return workflow_run_to_response(
        task,
        current_step_name=current_step_name,
        pending_pause=pending_pause,
    )


def should_store_operator_instruction(action: str, note: str) -> bool:
    """Return whether a resolved gate action should persist a one-shot instruction."""

    if not note:
        return False
    return action == "continue" or action.startswith("revise(")


async def update_operator_instruction(
    session_factory: Any,
    *,
    task_id: str,
    workflow_state: WorkflowState | None,
    instruction: str,
) -> None:
    """Persist a one-shot operator instruction on the task workflow state."""

    if workflow_state is None:
        return
    workflow_state.last_operator_instruction = instruction
    async with session_factory() as session:
        await update_task_workflow_state(session, task_id, workflow_state.model_dump(mode="json"))
        await session.commit()


def _pause_offers_action(pause: PendingPauseResponse, action: str) -> bool:
    options = pause.options or []
    return any(isinstance(option, dict) and option.get("action") == action for option in options)


def _pause_retry_action(pause: PendingPauseResponse) -> str | None:
    options = pause.options or []
    for option in options:
        if not isinstance(option, dict):
            continue
        action = option.get("action")
        if isinstance(action, str) and action.startswith("revise(") and action.endswith(")"):
            return action
    return None


async def resolve_task_pause_action(
    *,
    task: TaskModel,
    requested_action: str,
    note: str,
    pause_waiter: Any,
    notification_service: NotificationService | None,
    task_queue: Any,
    session_factory: Any,
    user_email: str,
) -> dict[str, Any]:
    """Resolve a paused task gate using the shared API/tool semantics."""

    pause = task_pending_pause_response(pause_waiter, task)
    if pause is None or pause.pause_type != "gate":
        raise ValueError("No pending gate for task")

    if requested_action == "retry":
        decision = _pause_retry_action(pause)
        if decision is None:
            raise ValueError("This paused gate does not offer a retry action.")
    else:
        if not _pause_offers_action(pause, requested_action):
            raise ValueError(f"This paused gate does not offer a {requested_action} action.")
        decision = requested_action

    resolved = False
    if notification_service is not None:
        notif = await notification_service.find_by_task(
            task.task_id, notification_type="gate", status="pending"
        )
        if notif is not None:
            resolved = await notification_service.resolve(
                notif.notification_id,
                decision,
                {"note": note},
                user_email=user_email,
            )
            if not resolved:
                raise RuntimeError("Gate has already been resolved")

    if not resolved:
        raw_pause = pause_waiter.find_pending(task_id=task.task_id, pause_type="gate")
        if raw_pause is None:
            raise ValueError("No pending gate for task")
        ok = pause_waiter.resolve(
            raw_pause.pause_id,
            PauseResolution(decision=decision, data={"note": note}),
        )
        if not ok:
            raise RuntimeError("Pause has already been resolved")

    if should_store_operator_instruction(decision, note):
        await update_operator_instruction(
            session_factory,
            task_id=task.task_id,
            workflow_state=task.workflow_state,
            instruction=note,
        )

    if decision == "cancel":
        return {
            "status": "cancelled",
            "task_status": "cancelled",
            "message": "Gate resolved with 'cancel'.",
            "note_applied": False,
        }
    return {
        "status": "retrying" if requested_action == "retry" else "continuing",
        "task_status": str(task.status),
        "message": (
            f"Retrying: resolved gate with '{decision}'."
            if requested_action == "retry"
            else "Gate resolved with 'continue'."
        ),
        "note_applied": bool(note),
    }


async def respond_task_input(
    *,
    task: TaskModel,
    reply: dict[str, Any],
    pause_waiter: Any,
    notification_service: NotificationService | None,
    task_queue: Any,
    session_factory: Any,
    user_email: str,
) -> dict[str, Any]:
    """Answer a paused step question and resume the task when needed."""

    pending_pause = task_pending_pause_response(pause_waiter, task)
    if pending_pause is None or pending_pause.pause_type not in {"step_input", "step_question"}:
        raise ValueError("No pending step question for task")
    questions = pending_pause.questions or []
    normalized_reply = validate_reply_for_questions(reply, questions)

    resolved = False
    if notification_service is not None:
        notif = await notification_service.find_by_task(
            task.task_id,
            notification_type="step_question",
            status="pending",
        )
        if notif is not None:
            resolved = await notification_service.resolve(
                notif.notification_id,
                "continue",
                normalized_reply,
                user_email=user_email,
            )
            if not resolved:
                raise RuntimeError("Step question has already been resolved")

    if not resolved:
        raw_pause = pause_waiter.find_pending(task_id=task.task_id, pause_type="step_input")
        if raw_pause is None:
            raise ValueError("No pending step question for task")
        ok = pause_waiter.resolve(
            raw_pause.pause_id,
            PauseResolution(decision="continue", data=normalized_reply),
        )
        if not ok:
            raise RuntimeError("Pause has already been resolved")

    if not task_queue.has_active_run(task.task_id):
        async with session_factory() as session:
            row = await get_task(session, task.task_id)
            if row is not None and row.workflow_state:
                state = WorkflowState.model_validate(row.workflow_state)
                if state.pending_pause_type == "step_input":
                    payload = dict(state.pending_pause_payload or {})
                    payload["answers"] = normalized_reply["answers"]
                    payload["mode"] = normalized_reply["mode"]
                    state.pending_pause_payload = payload
                    await update_task_workflow_state(
                        session, task.task_id, state.model_dump(mode="json")
                    )
                    await session.commit()
        resumed_task = await task_queue.resume_task(task.task_id)
        return {"status": str(resumed_task.status)}
    return {"status": str(task.status)}


async def count_active_task_references_for_workflow(
    session_factory: Any,
    workflow_id: str,
) -> int:
    """Count non-terminal task references that should block workflow mutation."""

    from sqlalchemy import func, select

    from cognis.store.models import Task

    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.workflow_id == workflow_id)
            .where(Task.status.in_(_ACTIVE_WORKFLOW_TASK_STATUSES))
        )
        return int(result.scalar_one())


def validate_workflow_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a workflow definition using the shared registry rules."""

    from cognis.core.workflow_registry import _validate_workflow
    from cognis.models.workflow import Workflow

    normalized = dict(definition)
    restore_missing_id = normalized.get("workflow_id") in {None, ""}
    if restore_missing_id:
        normalized["workflow_id"] = "wf_validation"
    workflow = Workflow.model_validate(normalized)
    _validate_workflow(workflow)
    payload = workflow.model_dump(mode="json", exclude_none=True)
    if restore_missing_id:
        payload.pop("workflow_id", None)
    return payload


async def get_scoped_workflow_row(
    session_factory: Any,
    workflow_id: str,
    *,
    owner_email: str,
) -> Any | None:
    """Return a workflow row if it is visible to the user."""

    async with session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            return None
        if row.is_system or row.owner_email == owner_email:
            return row
        return None


def workflow_row_to_summary(row: Any) -> dict[str, Any]:
    """Convert a workflow row/model into a summary payload."""

    response = workflow_to_response(row)
    return {
        "workflow_id": response.workflow_id,
        "name": response.name,
        "description": response.description,
        "version": response.version,
        "is_system": response.is_system,
        "owner_email": response.owner_email,
        "lifecycle": response.lifecycle,
        "archived_at": response.archived_at.isoformat() if response.archived_at else None,
        "tags": response.tags,
    }
