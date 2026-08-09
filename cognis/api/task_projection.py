"""Backend-owned lightweight projection for the workflow task cockpit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.models import (
    PendingPauseResponse,
    TaskProgressProjection,
    TaskProgressTodo,
    TaskProgressWorkItem,
    TaskWorkflowProjection,
    WorkflowPhaseProjection,
    WorkflowStepProjection,
)
from cognis.models.task import TaskModel
from cognis.models.workflow import Workflow, WorkflowPhaseDefinition
from cognis.store.models import ManagedConversationLink, Session
from cognis.store.queries import (
    list_conversation_todos_by_conversation,
    list_session_todos_by_session,
)

_TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
_SUCCESS_STEP_STATUSES = {"approved", "completed", "skipped"}
_ACTIVE_STEP_STATUSES = {"running", "evaluating"}
_PROGRESS_WORK_ITEM_LIMIT = 50
_PROGRESS_TODO_LIMIT = 100
_PROGRESS_ITEM_TODO_LIMIT = 30
_PROGRESS_ACTIVE_STATUSES = {"active", "queued", "running"}


async def build_task_workflow_projection(
    task: TaskModel,
    *,
    workflow_registry: Any,
    step_runs: list[Any],
    pending_pause: PendingPauseResponse | None,
) -> TaskWorkflowProjection | None:
    """Project canonical workflow phases and latest step attempts without heavy payloads."""

    workflow = await _resolve_workflow(task, workflow_registry)
    if workflow is None:
        return None

    state = task.workflow_state
    current_step_name = _current_step_name(workflow, state)
    pause_step_name = _pause_step_name(pending_pause, state, current_step_name)
    if str(task.status) in _TERMINAL_TASK_STATUSES:
        pause_step_name = None
    attempts_by_step = _attempts_by_step(step_runs)
    routing_skips = state.routing_skips if state is not None else {}
    exhaustion_skips = set(state.skipped_steps if state is not None else [])
    if str(task.status) in {"failed", "cancelled"} and current_step_name is None:
        current_step_name = _terminal_step_name(
            workflow,
            attempts_by_step=attempts_by_step,
            exhaustion_skips=exhaustion_skips,
        )

    phases = (
        workflow.presentation.phases
        if workflow.presentation is not None
        else [
            WorkflowPhaseDefinition(
                id="workflow",
                title=workflow.name or "Workflow",
                description=workflow.description,
                step_names=[step.name for step in workflow.steps],
            )
        ]
    )
    steps_by_name = {step.name: step for step in workflow.steps}
    phase_projections: list[WorkflowPhaseProjection] = []
    current_phase_id: str | None = None

    for phase in phases:
        projected_steps: list[WorkflowStepProjection] = []
        for step_name in phase.step_names:
            step = steps_by_name[step_name]
            attempts = attempts_by_step.get(step_name, [])
            latest = _latest_attempt(attempts)
            skip_reason = routing_skips.get(step_name)
            if skip_reason is None and step_name in exhaustion_skips:
                skip_reason = "exhausted"
            projected = _project_step(
                task=task,
                step=step,
                attempts=attempts,
                latest=latest,
                current_step_name=current_step_name,
                pause_step_name=pause_step_name,
                pause_type=pending_pause.pause_type if pending_pause is not None else None,
                skip_reason=skip_reason,
                default_max_attempts=workflow.defaults.max_attempts,
            )
            projected_steps.append(projected)

        phase_status = _phase_status(
            task_status=str(task.status),
            steps=projected_steps,
            current_step_name=current_step_name,
            pause_step_name=pause_step_name,
        )
        if any(step.name == (pause_step_name or current_step_name) for step in projected_steps):
            current_phase_id = phase.id
        phase_projections.append(
            WorkflowPhaseProjection(
                id=phase.id,
                title=phase.title,
                description=phase.description,
                status=phase_status,
                steps=projected_steps,
            )
        )

    return TaskWorkflowProjection(
        workflow_id=workflow.workflow_id,
        workflow_version=(
            state.effective_workflow_version
            if state is not None and state.effective_workflow_version is not None
            else workflow.version
        ),
        workflow_digest=state.effective_workflow_digest if state is not None else None,
        current_phase_id=current_phase_id,
        current_step_name=pause_step_name or current_step_name,
        phases=phase_projections,
    )


async def build_task_progress_projection(
    db_session: AsyncSession,
    *,
    owner_email: str,
    step_runs: list[Any],
) -> TaskProgressProjection:
    """Project task-owned live work without inferring ownership from source conversations."""

    live_runs = [
        row for row in step_runs if getattr(row, "superseded_by_step_run_id", None) is None
    ]
    session_owners = {
        str(row.session_id): (str(row.step_name), str(row.step_run_id))
        for row in live_runs
        if getattr(row, "session_id", None)
    }
    ordered_runs = sorted(
        live_runs,
        key=lambda row: (
            getattr(row, "updated_at", None) or datetime.min.replace(tzinfo=UTC),
            str(getattr(row, "step_run_id", "")),
        ),
        reverse=True,
    )
    todos: list[TaskProgressTodo] = []
    truncated = (
        sum(_valid_todo_count(getattr(row, "todos", None)) for row in ordered_runs)
        > _PROGRESS_TODO_LIMIT
    )
    for row in ordered_runs:
        for item in _normalized_todos(getattr(row, "todos", None)):
            if len(todos) >= _PROGRESS_TODO_LIMIT:
                truncated = True
                break
            todos.append(item)
        if len(todos) >= _PROGRESS_TODO_LIMIT:
            break

    if not session_owners:
        return TaskProgressProjection(todos=todos, truncated=truncated)

    owner_session_ids = sorted(session_owners)
    child_result = await db_session.execute(
        select(Session)
        .where(
            Session.user_email == owner_email,
            Session.parent_session_id.in_(owner_session_ids),
        )
        .order_by(Session.updated_at.desc(), Session.session_id.asc())
        .limit(_PROGRESS_WORK_ITEM_LIMIT + 1)
    )
    children = list(child_result.scalars().all())
    managed_result = await db_session.execute(
        select(ManagedConversationLink)
        .where(
            ManagedConversationLink.user_email == owner_email,
            ManagedConversationLink.controller_session_id.in_(owner_session_ids),
        )
        .order_by(
            ManagedConversationLink.updated_at.desc(),
            ManagedConversationLink.link_id.asc(),
        )
        .limit(_PROGRESS_WORK_ITEM_LIMIT + 1)
    )
    managed = list(managed_result.scalars().all())

    child_todos = await list_session_todos_by_session(
        db_session, [row.session_id for row in children]
    )
    managed_todos = await list_conversation_todos_by_conversation(
        db_session, [row.target_conversation_id for row in managed]
    )
    work_items: list[TaskProgressWorkItem] = []
    for row in children:
        step_name, step_run_id = session_owners[str(row.parent_session_id)]
        row_todos = child_todos.get(row.session_id)
        truncated = truncated or _todos_exceed_limit(row_todos, _PROGRESS_ITEM_TODO_LIMIT)
        work_items.append(
            TaskProgressWorkItem(
                kind="delegated_session",
                work_id=row.session_id,
                step_name=step_name,
                step_run_id=step_run_id,
                title=_bounded_text(row.delegation_task, limit=300),
                agent_id=row.agent_id,
                status=str(row.status),
                result_summary=_bounded_text(row.result_summary),
                error=_bounded_text(row.completion_reason if str(row.status) == "failed" else None),
                todos=_normalized_todos(row_todos, limit=_PROGRESS_ITEM_TODO_LIMIT),
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                started_at=row.started_at,
                updated_at=row.updated_at,
            )
        )
    for row in managed:
        step_name, step_run_id = session_owners[str(row.controller_session_id)]
        turn_state = str(row.turn_state)
        conversation_state = str(row.conversation_state)
        status = conversation_state if conversation_state in {"completed", "closed"} else turn_state
        row_todos = managed_todos.get(row.target_conversation_id)
        truncated = truncated or _todos_exceed_limit(row_todos, _PROGRESS_ITEM_TODO_LIMIT)
        work_items.append(
            TaskProgressWorkItem(
                kind="managed_conversation",
                work_id=row.link_id,
                step_name=step_name,
                step_run_id=step_run_id,
                title=_bounded_text(row.title, limit=300),
                agent_id=row.target_agent_id,
                status=status,
                result_summary=_bounded_text(row.last_result_summary),
                error=_bounded_text(row.last_error),
                todos=_normalized_todos(
                    row_todos,
                    limit=_PROGRESS_ITEM_TODO_LIMIT,
                ),
                conversation_id=row.target_conversation_id,
                session_id=row.target_session_id,
                started_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    work_items.sort(
        key=lambda item: (
            item.updated_at or datetime.min.replace(tzinfo=UTC),
            item.work_id,
        ),
        reverse=True,
    )
    if len(work_items) > _PROGRESS_WORK_ITEM_LIMIT:
        truncated = True
        work_items = work_items[:_PROGRESS_WORK_ITEM_LIMIT]
    active_count = sum(item.status in _PROGRESS_ACTIVE_STATUSES for item in work_items)
    completed_count = sum(item.status in {"completed", "closed"} for item in work_items)
    return TaskProgressProjection(
        todos=todos,
        work_items=work_items,
        active_count=active_count,
        completed_count=completed_count,
        truncated=truncated,
    )


def _normalized_todos(value: Any, *, limit: int | None = None) -> list[TaskProgressTodo]:
    if not isinstance(value, list):
        return []
    result: list[TaskProgressTodo] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip() or not isinstance(status, str):
            continue
        result.append(
            TaskProgressTodo(
                content=_bounded_text(content.strip(), limit=500) or "", status=status[:32]
            )
        )
        if limit is not None and len(result) >= limit:
            break
    return result


def _todos_exceed_limit(value: Any, limit: int) -> bool:
    return _valid_todo_count(value) > limit


def _valid_todo_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    valid_count = 0
    for item in value:
        if not isinstance(item, dict):
            continue
        if (
            isinstance(item.get("content"), str)
            and item["content"].strip()
            and isinstance(item.get("status"), str)
        ):
            valid_count += 1
    return valid_count


async def _resolve_workflow(task: TaskModel, workflow_registry: Any) -> Workflow | None:
    if task.workflow_id is None:
        return None
    state = task.workflow_state
    if state is not None and state.effective_workflow_definition is not None:
        return Workflow.model_validate(state.effective_workflow_definition)
    return await workflow_registry.get(
        task.workflow_id,
        owner_email=task.created_by,
        include_disabled=True,
        project_id=task.project_id,
    )


def _attempts_by_step(step_runs: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in step_runs:
        result.setdefault(str(row.step_name), []).append(row)
    return result


def _latest_attempt(attempts: list[Any]) -> Any | None:
    if not attempts:
        return None
    live = [row for row in attempts if getattr(row, "superseded_by_step_run_id", None) is None]
    candidates = live or attempts
    return max(
        candidates,
        key=lambda row: (
            int(getattr(row, "attempt_number", 1) or 1),
            int(getattr(row, "attempt", 1) or 1),
            getattr(row, "updated_at", None) or datetime.min.replace(tzinfo=UTC),
            str(getattr(row, "step_run_id", "")),
        ),
    )


def _current_step_name(workflow: Workflow, state: Any) -> str | None:
    if state is None or not workflow.steps:
        return None
    index = int(state.current_step_index)
    if 0 <= index < len(workflow.steps):
        return workflow.steps[index].name
    return None


def _terminal_step_name(
    workflow: Workflow,
    *,
    attempts_by_step: dict[str, list[Any]],
    exhaustion_skips: set[str],
) -> str | None:
    for step in reversed(workflow.steps):
        latest = _latest_attempt(attempts_by_step.get(step.name, []))
        if str(getattr(latest, "status", "")) in {"failed", "rejected"}:
            return step.name
    for step in reversed(workflow.steps):
        if step.name in exhaustion_skips:
            return step.name
    return None


def _pause_step_name(
    pending_pause: PendingPauseResponse | None,
    state: Any,
    current_step_name: str | None,
) -> str | None:
    if pending_pause is not None and pending_pause.step_name:
        return pending_pause.step_name
    payload = state.pending_pause_payload if state is not None else None
    if isinstance(payload, dict):
        for key in ("step_name", "target_step"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    if state is not None and (
        state.pending_pause_type is not None or state.current_step_status == "paused"
    ):
        return current_step_name
    return None


def _project_step(
    *,
    task: TaskModel,
    step: Any,
    attempts: list[Any],
    latest: Any | None,
    current_step_name: str | None,
    pause_step_name: str | None,
    pause_type: str | None,
    skip_reason: str | None,
    default_max_attempts: int,
) -> WorkflowStepProjection:
    status = _step_status(
        task_status=str(task.status),
        step_name=step.name,
        latest=latest,
        current_step_name=current_step_name,
        pause_step_name=pause_step_name,
        skipped=skip_reason is not None,
    )
    output = getattr(latest, "output", None) if latest is not None else None
    summary = (
        output.get("summary")
        if isinstance(output, dict)
        else getattr(latest, "output_summary", None)
    )
    error = (
        output.get("error") if isinstance(output, dict) else getattr(latest, "output_error", None)
    )
    started_at = getattr(latest, "started_at", None) if latest is not None else None
    completed_at = getattr(latest, "completed_at", None) if latest is not None else None
    step_run_id = str(latest.step_run_id) if latest is not None else None
    has_logs = bool(
        latest is not None
        and (getattr(latest, "session_id", None) or getattr(latest, "intaris_session_id", None))
    )
    has_deliverable = bool(latest is not None and getattr(latest, "deliverable_id", None))
    max_attempts = 1
    if step.type == "run":
        max_attempts = (
            step.completion.max_attempts
            if getattr(step, "completion", None) is not None
            else default_max_attempts
        )
    metadata = _bounded_runtime_metadata(
        getattr(latest, "runtime_info", None) or _projected_runtime_metadata(latest)
    )
    has_output = bool(
        latest is not None and (output is not None or getattr(latest, "has_output", False))
    )
    return WorkflowStepProjection(
        name=step.name,
        type=step.type,
        status=status,
        attempt_count=len(attempts),
        max_attempts=max_attempts,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=_duration_seconds(started_at, completed_at),
        action_required=step.name == pause_step_name,
        pause_type=pause_type if step.name == pause_step_name else None,
        summary=_bounded_text(summary),
        error=_bounded_text(error),
        has_output=has_output,
        has_logs=has_logs,
        has_deliverable=has_deliverable,
        skip_reason=skip_reason,
        step_run_id=step_run_id,
        output_url=f"/api/v1/step-runs/{step_run_id}" if has_output else None,
        logs_url=f"/api/v1/chat/v2/task-steps/{step_run_id}/snapshot" if has_logs else None,
        deliverables_url=(
            f"/api/v1/tasks/{task.task_id}/steps/{step.name}/history" if has_deliverable else None
        ),
        metadata=metadata,
    )


def _step_status(
    *,
    task_status: str,
    step_name: str,
    latest: Any | None,
    current_step_name: str | None,
    pause_step_name: str | None,
    skipped: bool,
) -> str:
    if step_name == pause_step_name:
        return "waiting"
    raw_status = str(getattr(latest, "status", "pending"))
    if raw_status == "skipped":
        return "skipped"
    if skipped:
        if task_status == "failed" and step_name == current_step_name:
            return "failed"
        return "skipped"
    if task_status == "failed" and step_name == current_step_name:
        return "failed"
    if task_status == "cancelled" and step_name == current_step_name:
        return "cancelled"
    if (
        task_status == "running"
        and step_name == current_step_name
        and raw_status
        in {
            "pending",
            "failed",
            "rejected",
            "superseded",
        }
    ):
        return "running"
    if raw_status in _ACTIVE_STEP_STATUSES:
        return raw_status
    if raw_status in _SUCCESS_STEP_STATUSES:
        return "completed"
    if raw_status in {"failed", "rejected"}:
        return "failed"
    if raw_status == "cancelled":
        return "cancelled"
    if raw_status == "paused":
        return "waiting"
    if task_status == "running" and step_name == current_step_name:
        return "running"
    return "pending"


def _phase_status(
    *,
    task_status: str,
    steps: list[WorkflowStepProjection],
    current_step_name: str | None,
    pause_step_name: str | None,
) -> str:
    names = {step.name for step in steps}
    if pause_step_name in names or any(step.status == "waiting" for step in steps):
        return "waiting"
    if task_status == "failed" and (
        current_step_name in names or any(step.status == "failed" for step in steps)
    ):
        return "failed"
    if task_status == "cancelled" and (
        current_step_name in names or any(step.status == "cancelled" for step in steps)
    ):
        return "cancelled"
    if any(step.status in _ACTIVE_STEP_STATUSES for step in steps):
        return "active"
    if steps and all(step.status in {"completed", "skipped"} for step in steps):
        return "completed"
    return "pending"


def _duration_seconds(started_at: Any, completed_at: Any) -> float | None:
    if started_at is None:
        return None
    end = completed_at or datetime.now(UTC)
    try:
        return max(0.0, (end - started_at).total_seconds())
    except (TypeError, ValueError):
        return None


def _bounded_runtime_metadata(runtime_info: Any) -> dict[str, Any]:
    if not isinstance(runtime_info, dict):
        return {}
    allowed = (
        "execution_kind",
        "deterministic_substate",
        "recovery_state",
        "tool_name",
        "selected_branch",
        "selected_target",
        "condition",
        "render",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        value = runtime_info.get(key)
        if key in {"condition", "render"} and isinstance(value, dict):
            result[key] = _bounded_evidence_record(value)
        elif isinstance(value, str):
            result[key] = value[:256]
        elif isinstance(value, (int, float, bool)):
            result[key] = value
    return result


def _projected_runtime_metadata(latest: Any | None) -> dict[str, Any]:
    if latest is None:
        return {}
    return {
        key: value
        for key in (
            "execution_kind",
            "deterministic_substate",
            "recovery_state",
            "tool_name",
            "selected_branch",
            "selected_target",
            "condition",
            "render",
        )
        if (value := getattr(latest, key, None)) is not None
    }


def _bounded_evidence_record(value: dict[str, Any]) -> dict[str, Any]:
    """Allow only workflow-renderer's already-redacted, bounded audit shape."""

    result: dict[str, Any] = {}
    for key in ("template_digest", "rendered_digest", "truncated"):
        item = value.get(key)
        if isinstance(item, str):
            result[key] = item[:128]
        elif isinstance(item, bool):
            result[key] = item
    rendered = value.get("rendered")
    if isinstance(rendered, (str, int, float, bool)) or rendered is None:
        result["rendered"] = rendered[:500] if isinstance(rendered, str) else rendered
    elif isinstance(rendered, dict):
        keys = sorted(str(key)[:128] for key in rendered)[:50]
        result["rendered_keys"] = keys
        result["redacted_keys"] = sorted(
            str(key)[:128] for key, item in rendered.items() if item == "[redacted]"
        )[:50]
    return result


def _bounded_text(value: Any, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
