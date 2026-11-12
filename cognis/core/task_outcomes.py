"""Shared terminal-occurrence identity and bounded task outcome presentation."""

from __future__ import annotations

import hashlib
from datetime import UTC
from typing import TYPE_CHECKING, Any

from cognis.models.task import TaskModel, TaskStatus

if TYPE_CHECKING:
    from cognis.store.models import Task


def task_outcome_key(task: TaskModel | Task) -> str:
    """Deduplicate delivery retries, not subsequent terminal occurrences."""
    finished = task.completed_at
    if finished is not None:
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        finished = finished.astimezone(UTC)
    identity = f"{task.task_id}:{task.attempt_number}:{task.status}:{finished.isoformat() if finished else ''}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def task_outcome_completion_mode(task: TaskModel) -> str:
    """Respect a committed override, otherwise resolve the outcome policy."""
    return task.applied_completion_mode or task.completion_delivery.mode_for_outcome(
        succeeded=task.status == TaskStatus.COMPLETED
    )


def task_outcome_context(task: TaskModel) -> dict[str, Any]:
    """Expose references and recovery state, never an unbounded transcript."""
    data = task.result_data or {}
    context: dict[str, Any] = {
        "task_url": f"/tasks/{task.task_id}",
        "expected_output": (task.expected_output or "")[:1200],
        "attempt_number": task.attempt_number,
        "source_ref": task.source_ref,
        "workflow_id": task.workflow_id,
        "project_id": task.project_id,
    }
    for key in ("final_deliverable_id", "final_format", "error", "failure_reason"):
        value = data.get(key)
        if isinstance(value, str):
            context[key] = value[:2000]
    if task.workflow_state:
        state = task.workflow_state
        context["current_step_index"] = state.current_step_index
        steps = (state.effective_workflow_definition or {}).get("steps", [])
        if isinstance(steps, list) and 0 <= state.current_step_index < len(steps):
            step = steps[state.current_step_index]
            if isinstance(step, dict):
                context["current_step"] = step.get("name")
        context["last_error"] = (state.last_evaluation_feedback or "")[:2000]
    return context


_OUTCOME_TITLE_PREFIX = {"completed": "Completed", "failed": "Failed", "cancelled": "Cancelled"}


def task_outcome_title(task: TaskModel) -> str:
    """Short notification title; the full outcome stays in the notification body."""
    prefix = _OUTCOME_TITLE_PREFIX.get(str(task.status), str(task.status).capitalize())
    return f"{prefix}: {task.title[:120]}"


def task_outcome_reference(task: TaskModel) -> dict[str, Any]:
    """Identify the delivered run without copying its result."""
    return {
        "task_id": task.task_id,
        "title": task.title,
        "status": str(task.status),
        "attempt_number": task.attempt_number,
    }


def format_task_outcome(
    task: TaskModel, *, content: str | None = None, channel: bool = False
) -> str:
    """Render a self-contained notification with a contextual continuation link."""
    context = task_outcome_context(task)
    lines = [f"Task {task.status}: {task.title[:200]}"]
    if task.description:
        lines.append(f"Goal: {task.description[:300]}")
    summary = (task.result_summary or "").strip()[:700]
    body = (content or "").strip()
    if summary and summary != body:
        lines.append(summary)
    if body:
        lines.append(body)
    if not summary and not body:
        lines.append("No result summary was recorded; the outcome needs inspection.")
    error = context.get("last_error") or context.get("failure_reason") or context.get("error")
    if error and error != summary:
        lines.append(f"Reason: {str(error)[:400]}")
    if context.get("current_step") and task.status in {"failed", "cancelled"}:
        lines.append(f"Step: {context['current_step']}")
    if task.status == "failed":
        lines.append("The task did not finish successfully; its outcome may be partial.")
    elif task.status == "cancelled":
        lines.append(
            "Execution stopped. Completed actions may remain; no repair was started by this notification."
        )
    if context.get("final_deliverable_id"):
        lines.append(f"Result: {context['final_deliverable_id']}")
    if not channel:
        lines.append(f"Task details and follow-up: {context['task_url']}")
    return "\n\n".join(lines)
