"""Task and step-run routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
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
    DeliverableResponse,
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
    deliverable_to_response,
    dependency_to_response,
    step_run_to_response,
    task_detail_to_response,
    task_to_response,
)
from cognis.core.management import (
    resolve_task_pause_action,
    respond_task_input,
    task_pending_pause_response,
    task_workflow_run_response,
)
from cognis.models.task import TaskDelivery, TaskModel
from cognis.models.workflow import CompletionDeliveryPolicy, WorkflowState
from cognis.store.models import Task
from cognis.store.queries import (
    add_task_dependency,
    get_agent,
    get_conversation,
    get_step_run,
    get_task,
    get_task_dependencies,
    list_deliverables_for_step_run,
    list_step_runs_for_task,
    remove_task_dependency,
)

_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

router = APIRouter(tags=["tasks"])


async def _resolve_completion_delivery(
    request: Request,
    *,
    workflow_id: str | None,
    owner_email: str,
    completion_mode_family: str | None,
    allow_silent_completion: bool | None,
) -> CompletionDeliveryPolicy:
    if completion_mode_family is not None or allow_silent_completion is not None:
        return CompletionDeliveryPolicy(
            completion_mode_family=completion_mode_family or "default",
            allow_silent_completion=bool(allow_silent_completion),
        )

    if workflow_id is not None:
        workflow = await request.app.state.workflow_registry.get(
            workflow_id, owner_email=owner_email
        )
        if workflow is not None:
            defaults = getattr(workflow, "defaults", None)
            delivery_defaults = (
                defaults.get("delivery")
                if isinstance(defaults, dict)
                else getattr(defaults, "delivery", None)
            )
            if delivery_defaults is not None:
                return CompletionDeliveryPolicy.model_validate(
                    delivery_defaults.model_dump(mode="json")
                    if hasattr(delivery_defaults, "model_dump")
                    else delivery_defaults
                )

    return CompletionDeliveryPolicy()


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
    _validate_execution_paths(payload.workspace_root, payload.working_directory)
    queue = request.app.state.task_queue
    delivery = TaskDelivery(mode=payload.delivery_mode, target=payload.delivery_target)
    completion_delivery = await _resolve_completion_delivery(
        request,
        workflow_id=payload.workflow_id,
        owner_email=user.email,
        completion_mode_family=payload.completion_mode_family,
        allow_silent_completion=payload.allow_silent_completion,
    )
    if payload.status == "draft":
        task = await queue.create_draft(
            created_by=user.email,
            agent_id=payload.agent_id,
            title=payload.title,
            description=payload.description,
            expected_output=payload.expected_output,
            priority=payload.priority,
            delivery=delivery,
            completion_delivery=completion_delivery,
            workflow_id=payload.workflow_id,
            workspace_root=payload.workspace_root,
            working_directory=payload.working_directory,
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
            completion_delivery=completion_delivery,
            workflow_id=payload.workflow_id,
            workspace_root=payload.workspace_root,
            working_directory=payload.working_directory,
            status=payload.status,
        )
    return task_to_response(task)


@router.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
async def task_detail(request: Request, task_id: str) -> TaskDetailResponse:
    task = await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        dep_rows = await get_task_dependencies(session, task_id)
        step_rows = await list_step_runs_for_task(session, task_id)
        deliverables_by_step_run = {
            row.step_run_id: [deliverable_to_response(item) for item in await list_deliverables_for_step_run(session, row.step_run_id)]
            for row in step_rows
        }
    pending_pause = _task_pending_pause(request, task)
    workflow_run = await _build_workflow_run_response(request, task, pending_pause)
    return task_detail_to_response(
        task,
        dependencies=[dependency_to_response(row) for row in dep_rows],
        step_runs=[
            step_run_to_response(
                row,
                deliverables=deliverables_by_step_run.get(row.step_run_id, []),
            )
            for row in step_rows
        ],
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
        await _validate_workflow_access(
            request, payload.workflow_id, owner_email=existing_row.created_by
        )
    effective_delivery_mode = payload.delivery_mode or existing_row.delivery_mode
    effective_delivery_target = payload.delivery_target or existing_row.delivery_target
    effective_completion_mode_family = payload.completion_mode_family or getattr(
        existing_row, "completion_mode_family", "default"
    )
    effective_allow_silent_completion = (
        payload.allow_silent_completion
        if payload.allow_silent_completion is not None
        else bool(getattr(existing_row, "allow_silent_completion", False))
    )
    effective_workspace_root = payload.workspace_root or getattr(
        existing_row, "workspace_root", None
    )
    effective_working_directory = payload.working_directory or getattr(
        existing_row, "working_directory", None
    )
    if payload.working_directory is not None and effective_workspace_root is None:
        effective_workspace_root = payload.working_directory
    if payload.workspace_root is not None and effective_working_directory is None:
        effective_working_directory = payload.workspace_root
    _validate_execution_paths(effective_workspace_root, effective_working_directory)
    if effective_delivery_mode == "specific_conversation" and effective_delivery_target is None:
        raise api_exception(
            400,
            "validation_error",
            "specific_conversation delivery requires delivery_target",
        )
    if payload.delivery_target is not None:
        await _validate_conversation_access(request, payload.delivery_target)
    CompletionDeliveryPolicy(
        completion_mode_family=effective_completion_mode_family,
        allow_silent_completion=effective_allow_silent_completion,
    )
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
        if "completion_mode_family" in updates:
            row.completion_mode_family = updates.pop("completion_mode_family")
        if "allow_silent_completion" in updates:
            row.allow_silent_completion = updates.pop("allow_silent_completion")
        if (
            payload.working_directory is not None
            and payload.workspace_root is None
            and row.workspace_root is None
        ):
            row.workspace_root = payload.working_directory
        if (
            payload.workspace_root is not None
            and payload.working_directory is None
            and row.working_directory is None
        ):
            row.working_directory = payload.workspace_root
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
    note = (payload.feedback or "").strip()
    try:
        result = await resolve_task_pause_action(
            task=task,
            requested_action=payload.action,
            note=note,
            pause_waiter=request.app.state.pause_waiter,
            notification_service=getattr(request.app.state, "notification_service", None),
            task_queue=request.app.state.task_queue,
            session_factory=request.app.state.session_factory,
            user_email=task.created_by,
        )
    except ValueError as exc:
        message = str(exc)
        if message == "No pending gate for task":
            resolved_pause = request.app.state.pause_waiter.find_pending(
                task_id=task_id,
                step_name=payload.step_name,
                pause_type="gate",
                include_resolved=True,
            )
            if resolved_pause is not None:
                raise api_exception(409, "conflict", "Pause has already been resolved") from exc
            raise api_exception(404, "not_found", message) from exc
        raise api_exception(409, "conflict", message) from exc
    except RuntimeError as exc:
        raise api_exception(409, "conflict", str(exc)) from exc
    return TaskActionResponse(ok=True, task_id=task_id, status=result["task_status"])


@router.post("/api/v1/tasks/{task_id}/step-response", response_model=TaskActionResponse)
async def step_response(
    request: Request,
    task_id: str,
    payload: StepResponseRequest,
) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    task = await _require_task(request, task_id)

    try:
        result = await respond_task_input(
            task=task,
            response=payload.response,
            pause_waiter=request.app.state.pause_waiter,
            notification_service=getattr(request.app.state, "notification_service", None),
            task_queue=request.app.state.task_queue,
            session_factory=request.app.state.session_factory,
            user_email=task.created_by,
        )
    except ValueError as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    except RuntimeError as exc:
        raise api_exception(409, "conflict", str(exc)) from exc
    return TaskActionResponse(ok=True, task_id=task_id, status=result["status"])


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
        deliverables_by_step_run = {
            row.step_run_id: [deliverable_to_response(item) for item in await list_deliverables_for_step_run(session, row.step_run_id)]
            for row in rows
        }
    return [
        step_run_to_response(row, deliverables=deliverables_by_step_run.get(row.step_run_id, []))
        for row in rows
    ]


@router.get("/api/v1/step-runs/{step_run_id}", response_model=StepRunResponse)
async def step_run_detail(request: Request, step_run_id: str) -> StepRunResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_step_run(session, step_run_id)
        if row is None:
            raise api_exception(404, "not_found", "Step run not found")
        task_row = await get_task(session, row.task_id)
        deliverables = [
            deliverable_to_response(item)
            for item in await list_deliverables_for_step_run(session, row.step_run_id)
        ]
    if task_row is None or task_row.created_by != user.email:
        raise api_exception(404, "not_found", "Step run not found")
    return step_run_to_response(row, deliverables=deliverables)


@router.get(
    "/api/v1/step-runs/{step_run_id}/deliverables",
    response_model=list[DeliverableResponse],
)
async def step_run_deliverables(request: Request, step_run_id: str) -> list[DeliverableResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_step_run(session, step_run_id)
        if row is None:
            raise api_exception(404, "not_found", "Step run not found")
        task_row = await get_task(session, row.task_id)
        if task_row is None or task_row.created_by != user.email:
            raise api_exception(404, "not_found", "Step run not found")
        rows = await list_deliverables_for_step_run(session, step_run_id)
    return [deliverable_to_response(row) for row in rows]


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
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_task(session, task_id)
    if row is None:
        raise api_exception(404, "not_found", "Task not found")
    if row.created_by != user.email:
        raise api_exception(404, "not_found", "Task not found")
    return _row_to_task(row)


async def _validate_agent_access(request: Request, agent_id: str | None) -> None:
    if agent_id is None:
        return
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, row.owner_email)


async def _validate_workflow_access(
    request: Request, workflow_id: str | None, *, owner_email: str | None = None
) -> None:
    if workflow_id is None:
        return
    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=owner_email or user.email
    )
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


async def _build_workflow_run_response(
    request: Request,
    task: TaskModel,
    pending_pause: Any,
) -> WorkflowRunResponse | None:
    return await task_workflow_run_response(
        task,
        workflow_registry=request.app.state.workflow_registry,
        pending_pause=pending_pause,
    )


def _task_pending_pause(request: Request, task: TaskModel) -> Any:
    return task_pending_pause_response(request.app.state.pause_waiter, task)


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
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family=getattr(row, "completion_mode_family", "default"),
            allow_silent_completion=bool(getattr(row, "allow_silent_completion", False)),
        ),
        workflow_id=row.workflow_id,
        workspace_root=getattr(row, "workspace_root", None),
        working_directory=getattr(row, "working_directory", None),
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
        applied_completion_mode=getattr(row, "applied_completion_mode", None),
        applied_completion_reason=getattr(row, "applied_completion_reason", None),
    )


def _validate_execution_paths(workspace_root: str | None, working_directory: str | None) -> None:
    if not workspace_root or not working_directory:
        return

    root = Path(os.path.realpath(os.path.expanduser(workspace_root)))
    cwd = Path(os.path.realpath(os.path.expanduser(working_directory)))
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise api_exception(
            400,
            "validation_error",
            "working_directory must be equal to or inside workspace_root",
        ) from exc
