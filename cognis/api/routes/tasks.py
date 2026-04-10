"""Task and step-run routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.models import (
    BatchSubmitRequest,
    BatchSubmitResponse,
    CursorPage,
    DependencyRequest,
    DependencyResponse,
    GateResponseRequest,
    StepResponseRequest,
    StepRunResponse,
    TaskActionResponse,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskResponse,
    TaskUpdateRequest,
    WorkflowRunResponse,
)
from cognis.api.serializers import (
    dependency_to_response,
    pending_pause_to_response,
    step_run_to_response,
    task_detail_to_response,
    task_to_response,
    workflow_run_to_response,
)
from cognis.core.agent_loop import PauseResolution, PendingPause
from cognis.models.task import TaskDelivery, TaskModel
from cognis.models.workflow import WorkflowState
from cognis.store.models import Task
from cognis.store.queries import (
    add_task_dependency,
    get_agent,
    get_conversation,
    get_step_run,
    get_task,
    get_task_dependencies,
    list_step_runs_for_task,
    remove_task_dependency,
    update_task_workflow_state,
)

_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

router = APIRouter(tags=["tasks"])


@router.get("/api/v1/tasks", response_model=CursorPage[TaskResponse])
async def task_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = None,
    agent_id: str | None = None,
    queue: str | None = None,
    priority: int | None = None,
) -> CursorPage[TaskResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        query = (
            select(Task)
            .where(Task.created_by == user.email)
            .order_by(Task.updated_at.desc(), Task.task_id.asc())
        )
        if status is not None:
            query = query.where(Task.status == status)
        if agent_id is not None:
            query = query.where(Task.agent_id == agent_id)
        if queue is not None:
            query = query.where(Task.queue_name == queue)
        if priority is not None:
            query = query.where(Task.priority == priority)
        rows = list((await session.execute(query)).scalars().all())
    items = [task_to_response(_row_to_task(row)) for row in rows]
    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.task_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


@router.post("/api/v1/tasks", response_model=TaskResponse)
async def task_create(request: Request, payload: TaskCreateRequest) -> TaskResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    if payload.source_type == "chat" and payload.source_ref is None:
        raise api_exception(
            400,
            "validation_error",
            "chat-sourced tasks require source_ref conversation id",
        )
    if payload.delivery_mode == "specific_conversation" and payload.delivery_target is None:
        raise api_exception(
            400,
            "validation_error",
            "specific_conversation delivery requires delivery_target",
        )
    await _validate_agent_access(request, payload.agent_id)
    await _validate_workflow_access(request, payload.workflow_id)
    if payload.source_type == "chat" and payload.source_ref is not None:
        await _validate_conversation_access(request, payload.source_ref)
    if payload.delivery_target is not None:
        await _validate_conversation_access(request, payload.delivery_target)
    queue = request.app.state.task_queue
    delivery = TaskDelivery(mode=payload.delivery_mode, target=payload.delivery_target)
    if payload.status == "draft":
        task = await queue.create_draft(
            created_by=user.email,
            agent_id=payload.agent_id,
            title=payload.title,
            description=payload.description,
            expected_output=payload.expected_output,
            priority=payload.priority,
            delivery=delivery,
            workflow_id=payload.workflow_id,
            source_type=payload.source_type,
            source_ref=payload.source_ref,
        )
    else:
        task = await queue.submit(
            created_by=user.email,
            agent_id=payload.agent_id,
            title=payload.title,
            description=payload.description,
            expected_output=payload.expected_output,
            priority=payload.priority,
            source_type=payload.source_type,
            source_ref=payload.source_ref,
            delivery=delivery,
            workflow_id=payload.workflow_id,
            status=payload.status,
        )
    return task_to_response(task)


@router.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
async def task_detail(request: Request, task_id: str) -> TaskDetailResponse:
    task = await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        dep_rows = await get_task_dependencies(session, task_id)
        step_rows = await list_step_runs_for_task(session, task_id)
    pending_pause = _task_pending_pause(request, task)
    workflow_run = await _build_workflow_run_response(request, task, pending_pause)
    return task_detail_to_response(
        task,
        dependencies=[dependency_to_response(row) for row in dep_rows],
        step_runs=[step_run_to_response(row) for row in step_rows],
        pending_pause=pending_pause,
        workflow_run=workflow_run,
    )


@router.patch("/api/v1/tasks/{task_id}", response_model=TaskResponse)
async def task_update(request: Request, task_id: str, payload: TaskUpdateRequest) -> TaskResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        existing_row = await get_task(session, task_id)
        if existing_row is None:
            raise api_exception(404, "not_found", "Task not found")
    if payload.agent_id is not None:
        await _validate_agent_access(request, payload.agent_id)
    if payload.workflow_id is not None:
        await _validate_workflow_access(request, payload.workflow_id)
    effective_delivery_mode = payload.delivery_mode or existing_row.delivery_mode
    effective_delivery_target = payload.delivery_target or existing_row.delivery_target
    if effective_delivery_mode == "specific_conversation" and effective_delivery_target is None:
        raise api_exception(
            400,
            "validation_error",
            "specific_conversation delivery requires delivery_target",
        )
    if payload.delivery_target is not None:
        await _validate_conversation_access(request, payload.delivery_target)
    async with request.app.state.session_factory() as session:
        row = await get_task(session, task_id)
        if row is None:
            raise api_exception(404, "not_found", "Task not found")
        if row.status in _TERMINAL_STATUSES:
            raise api_exception(
                409,
                "conflict",
                f"Cannot update task in '{row.status}' status.",
            )
        updates = payload.model_dump(exclude_none=True)
        if "delivery_mode" in updates:
            row.delivery_mode = updates.pop("delivery_mode")
        if "delivery_target" in updates:
            row.delivery_target = updates.pop("delivery_target")
        for field_name, value in updates.items():
            setattr(row, field_name, value)
        row.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
    return task_to_response(_row_to_task(row))


@router.delete("/api/v1/tasks/{task_id}", response_model=TaskActionResponse)
async def task_delete(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.cancel_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/submit", response_model=TaskActionResponse)
async def task_submit(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.submit_existing(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/pause", response_model=TaskActionResponse)
async def task_pause(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.pause_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/resume", response_model=TaskActionResponse)
async def task_resume(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.resume_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskActionResponse)
async def task_cancel(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.cancel_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/gate-response", response_model=TaskActionResponse)
async def gate_response(
    request: Request,
    task_id: str,
    payload: GateResponseRequest,
) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    task = await _require_task(request, task_id)
    if payload.feedback:
        await _update_last_feedback(request, task_id, task.workflow_state, payload.feedback)

    # Try the unified notification service first
    svc = getattr(request.app.state, "notification_service", None)
    if svc is not None:
        from cognis.core.notifications import NotificationService

        svc_typed: NotificationService = svc
        notif = await svc_typed.find_by_task(task_id, notification_type="gate", status="pending")
        if notif is not None:
            ok = await svc_typed.resolve(
                notif.notification_id,
                payload.action,
                {"feedback": payload.feedback or ""},
            )
            if ok:
                return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))
            raise api_exception(409, "conflict", "Gate has already been resolved")
        # Check if there's a recently resolved gate (409 vs 404)
        resolved_notif = await svc_typed.find_by_task(
            task_id, notification_type="gate", status="resolved"
        )
        if resolved_notif is not None:
            raise api_exception(409, "conflict", "Gate has already been resolved")

    # Legacy fallback: direct PauseWaiter
    pause = request.app.state.pause_waiter.find_pending(
        task_id=task_id,
        step_name=payload.step_name,
        pause_type="gate",
    )
    if pause is None:
        resolved_pause = request.app.state.pause_waiter.find_pending(
            task_id=task_id,
            step_name=payload.step_name,
            pause_type="gate",
            include_resolved=True,
        )
        if resolved_pause is not None:
            raise api_exception(409, "conflict", "Pause has already been resolved")
        raise api_exception(404, "not_found", "No pending gate for task")
    ok = request.app.state.pause_waiter.resolve(
        pause.pause_id,
        PauseResolution(decision=payload.action, data={"feedback": payload.feedback or ""}),
    )
    if not ok:
        raise api_exception(409, "conflict", "Pause has already been resolved")
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/step-response", response_model=TaskActionResponse)
async def step_response(
    request: Request,
    task_id: str,
    payload: StepResponseRequest,
) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    task = await _require_task(request, task_id)

    # Try the unified notification service first
    svc = getattr(request.app.state, "notification_service", None)
    if svc is not None:
        from cognis.core.notifications import NotificationService

        svc_typed: NotificationService = svc
        notif = await svc_typed.find_by_task(
            task_id, notification_type="step_question", status="pending"
        )
        if notif is not None:
            ok = await svc_typed.resolve(
                notif.notification_id,
                "continue",
                {"response": payload.response},
            )
            if ok:
                if not request.app.state.task_queue.has_active_run(task_id):
                    await _store_recovered_step_input_response(request, task_id, payload.response)
                    resumed_task = await request.app.state.task_queue.resume_task(task_id)
                    return TaskActionResponse(
                        ok=True, task_id=task_id, status=str(resumed_task.status)
                    )
                return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))
            raise api_exception(409, "conflict", "Step question has already been resolved")

    # Legacy fallback: direct PauseWaiter
    pause = request.app.state.pause_waiter.find_pending(
        task_id=task_id,
        step_name=payload.step_name,
        pause_type="step_input",
    )
    if pause is None:
        raise api_exception(404, "not_found", "No pending step question for task")
    ok = request.app.state.pause_waiter.resolve(
        pause.pause_id,
        PauseResolution(decision="continue", data={"response": payload.response}),
    )
    if not ok:
        raise api_exception(409, "conflict", "Pause has already been resolved")
    if not request.app.state.task_queue.has_active_run(task_id):
        await _store_recovered_step_input_response(request, task_id, payload.response)
        request.app.state.pause_waiter.clear(pause.pause_id)
        resumed_task = await request.app.state.task_queue.resume_task(task_id)
        return TaskActionResponse(ok=True, task_id=task_id, status=str(resumed_task.status))
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/batch-submit", response_model=BatchSubmitResponse)
async def task_batch_submit(request: Request, payload: BatchSubmitRequest) -> BatchSubmitResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        for task_id in payload.task_ids:
            row = await get_task(session, task_id)
            if row is None:
                continue
            require_owner_or_admin(request, row.created_by)
    result = await request.app.state.task_queue.batch_submit(payload.task_ids)
    return BatchSubmitResponse(**result)


@router.get("/api/v1/tasks/{task_id}/steps", response_model=list[StepRunResponse])
async def task_steps(request: Request, task_id: str) -> list[StepRunResponse]:
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        rows = await list_step_runs_for_task(session, task_id)
    return [step_run_to_response(row) for row in rows]


@router.get("/api/v1/step-runs/{step_run_id}", response_model=StepRunResponse)
async def step_run_detail(request: Request, step_run_id: str) -> StepRunResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_step_run(session, step_run_id)
        if row is None:
            raise api_exception(404, "not_found", "Step run not found")
        task_row = await get_task(session, row.task_id)
    if task_row is None or (task_row.created_by != user.email and user.role != "admin"):
        raise api_exception(404, "not_found", "Step run not found")
    return step_run_to_response(row)


@router.get("/api/v1/tasks/{task_id}/workflow-run", response_model=WorkflowRunResponse)
async def workflow_run_detail(request: Request, task_id: str) -> WorkflowRunResponse:
    task = await _require_task(request, task_id)
    pending_pause = _task_pending_pause(request, task)
    workflow_run = await _build_workflow_run_response(request, task, pending_pause)
    if workflow_run is None:
        raise api_exception(404, "not_found", "Workflow run not found")
    return workflow_run


@router.post("/api/v1/tasks/{task_id}/dependencies", response_model=DependencyResponse)
async def task_add_dependency(
    request: Request,
    task_id: str,
    payload: DependencyRequest,
) -> DependencyResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    await _require_task(request, payload.depends_on)
    async with request.app.state.session_factory() as session:
        row = await add_task_dependency(
            session,
            task_id,
            payload.depends_on,
            required=payload.required,
        )
        await session.commit()
    return dependency_to_response(row)


@router.delete("/api/v1/tasks/{task_id}/dependencies/{depends_on}", response_model=dict)
async def task_remove_dependency(
    request: Request, task_id: str, depends_on: str
) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    await _require_task(request, depends_on)
    async with request.app.state.session_factory() as session:
        ok = await remove_task_dependency(session, task_id, depends_on)
        await session.commit()
    if not ok:
        raise api_exception(404, "not_found", "Dependency not found")
    return {"ok": True}


async def _require_task(request: Request, task_id: str) -> TaskModel:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_task(session, task_id)
    if row is None:
        raise api_exception(404, "not_found", "Task not found")
    require_owner_or_admin(request, row.created_by)
    return _row_to_task(row)


async def _validate_agent_access(request: Request, agent_id: str | None) -> None:
    if agent_id is None:
        return
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, row.owner_email)


async def _validate_workflow_access(request: Request, workflow_id: str | None) -> None:
    if workflow_id is None:
        return
    workflow = await request.app.state.workflow_registry.get(workflow_id)
    if workflow is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if workflow.owner_email is not None:
        require_owner_or_admin(request, workflow.owner_email)


async def _validate_conversation_access(request: Request, conversation_id: str | None) -> None:
    if conversation_id is None:
        return
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_owner_or_admin(request, row.user_email)


async def _update_last_feedback(
    request: Request,
    task_id: str,
    workflow_state: WorkflowState | None,
    feedback: str,
) -> None:
    if workflow_state is None:
        return
    workflow_state.last_evaluation_feedback = feedback
    async with request.app.state.session_factory() as session:
        await update_task_workflow_state(session, task_id, workflow_state.model_dump(mode="json"))
        await session.commit()


async def _build_workflow_run_response(
    request: Request,
    task: TaskModel,
    pending_pause: Any,
) -> WorkflowRunResponse | None:
    if task.workflow_state is None:
        return None
    current_step_name: str | None = None
    if task.workflow_id:
        workflow = await request.app.state.workflow_registry.get(task.workflow_id)
        if workflow is not None and task.workflow_state.current_step_index < len(workflow.steps):
            current_step_name = workflow.steps[task.workflow_state.current_step_index].name
    return workflow_run_to_response(
        task, current_step_name=current_step_name, pending_pause=pending_pause
    )


def _task_pending_pause(request: Request, task: TaskModel) -> Any:
    live_pause = request.app.state.pause_waiter.find_pending(task_id=task.task_id)
    if live_pause is not None:
        return pending_pause_to_response(live_pause)
    if task.workflow_state is None or task.workflow_state.pending_pause_type is None:
        return None

    payload = task.workflow_state.pending_pause_payload or {}
    recovered_pause = PendingPause(
        pause_id=str(payload.get("pause_id", "recovered")),
        pause_type=task.workflow_state.pending_pause_type or "unknown",
        task_id=task.task_id,
        step_name=payload.get("step_name"),
        step_run_id=payload.get("step_run_id"),
        session_id=payload.get("session_id"),
        question=payload.get("question") or payload.get("message") or payload.get("label"),
        options=payload.get("options"),
        context=payload.get("context"),
    )
    return pending_pause_to_response(recovered_pause)


async def _store_recovered_step_input_response(
    request: Request,
    task_id: str,
    response: str,
) -> None:
    async with request.app.state.session_factory() as session:
        row = await get_task(session, task_id)
        if row is None or not row.workflow_state:
            return
        state = WorkflowState.model_validate(row.workflow_state)
        if state.pending_pause_type != "step_input":
            return
        payload = dict(state.pending_pause_payload or {})
        payload["response"] = response
        state.pending_pause_payload = payload
        await update_task_workflow_state(session, task_id, state.model_dump(mode="json"))
        await session.commit()


def _row_to_task(row: Any) -> TaskModel:
    return TaskModel(
        task_id=row.task_id,
        title=row.title,
        description=row.description or "",
        expected_output=row.expected_output,
        status=row.status,
        priority=row.priority,
        created_by=row.created_by,
        agent_id=row.agent_id,
        source_type=row.source_type,
        source_ref=row.source_ref,
        delivery=TaskDelivery(mode=row.delivery_mode, target=row.delivery_target),
        workflow_id=row.workflow_id,
        workflow_state=WorkflowState.model_validate(row.workflow_state)
        if row.workflow_state
        else None,
        queue_name=row.queue_name,
        scheduled_for=row.scheduled_for,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        result_data=row.result_data,
    )
