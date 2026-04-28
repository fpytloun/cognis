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
    check_agent_access,
    check_project_access,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_resource_owner,
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
    TaskCommentCreateRequest,
    TaskCommentResponse,
    TaskCommentUpdateRequest,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskRerunResponse,
    TaskResponse,
    TaskUpdateRequest,
    WorkflowRunResponse,
)
from cognis.api.serializers import (
    deliverable_to_response,
    dependency_to_response,
    step_run_to_response,
    task_comment_to_response,
    task_detail_to_response,
    task_to_response,
)
from cognis.core.management import (
    resolve_task_pause_action,
    respond_task_input,
    task_pending_pause_response,
    task_workflow_run_response,
)
from cognis.core.workflow_management import (
    delete_materialized_workflow,
    get_attached_skill_workflow_source,
    materialize_skill_workflow,
)
from cognis.models.task import TaskDelivery, TaskModel
from cognis.models.workflow import CompletionDeliveryPolicy, WorkflowState
from cognis.store.models import Task
from cognis.store.queries import (
    add_task_dependency,
    create_task_comment,
    get_agent,
    get_conversation,
    get_project,
    get_step_run,
    get_task,
    get_task_comment,
    get_task_dependencies,
    list_deliverables_for_step_run,
    list_step_run_history,
    list_step_runs_for_task,
    list_task_comments,
    remove_task_dependency,
    update_task_comment,
)

_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
_DELIVERY_MODES: frozenset[str] = frozenset(
    {
        "same_conversation",
        "specific_conversation",
        "latest_active_for_agent",
        "preferred_channel",
        "silent",
    }
)

router = APIRouter(tags=["tasks"])


def _validate_delivery_mode_applicability(
    *,
    delivery_mode: str,
    source_type: str,
    source_ref: str | None,
) -> None:
    if delivery_mode not in _DELIVERY_MODES:
        raise api_exception(
            400,
            "validation_error",
            f"Unsupported delivery mode: {delivery_mode}",
        )
    if delivery_mode == "same_conversation" and not (
        source_type in {"chat", "agent"} and source_ref is not None
    ):
        raise api_exception(
            400,
            "validation_error",
            "same_conversation delivery requires a chat source conversation",
        )


async def _resolve_completion_delivery(
    request: Request,
    *,
    workflow_id: str | None,
    owner_email: str,
    project_id: str | None,
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
            workflow_id, owner_email=owner_email, project_id=project_id
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
    project_id: str | None = None,
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
        if project_id is not None:
            query = query.where(Task.project_id == project_id)
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
    if payload.workflow_id is not None and payload.skill_id is not None:
        raise api_exception(
            400,
            "validation_error",
            "Specify either workflow_id or skill_id, not both",
        )
    if payload.source_type == "chat" and payload.source_ref is None:
        raise api_exception(
            400,
            "validation_error",
            "chat-sourced tasks require source_ref conversation id",
        )
    delivery_mode = payload.delivery_mode
    if "delivery_mode" not in payload.model_fields_set and payload.source_type == "chat":
        delivery_mode = "same_conversation"
    if delivery_mode == "specific_conversation" and payload.delivery_target is None:
        raise api_exception(
            400,
            "validation_error",
            "specific_conversation delivery requires delivery_target",
        )
    _validate_delivery_mode_applicability(
        delivery_mode=delivery_mode,
        source_type=payload.source_type,
        source_ref=payload.source_ref,
    )
    await _validate_agent_access(request, payload.agent_id)
    if payload.source_type in {"chat", "agent"} and payload.source_ref is not None:
        await _validate_conversation_access(request, payload.source_ref)
    if payload.delivery_target is not None:
        await _validate_conversation_access(request, payload.delivery_target)
    project_id = await _resolve_task_project_id(
        request, payload.project_id, payload.source_type, payload.source_ref
    )
    await _validate_workflow_access(request, payload.workflow_id, project_id=project_id)
    _validate_execution_paths(payload.workspace_root, payload.working_directory)
    resolved_workflow_id = payload.workflow_id
    created_workflow_id: str | None = None
    if payload.skill_id is not None:
        agent = await request.app.state.agent_registry.get(payload.agent_id, owner_email=user.email)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        try:
            source = await get_attached_skill_workflow_source(
                session_factory=request.app.state.session_factory,
                owner_email=agent.owner_email,
                agent=agent,
                skill_id=payload.skill_id,
            )
            created_workflow = await materialize_skill_workflow(
                session_factory=request.app.state.session_factory,
                owner_email=user.email,
                skill_id=payload.skill_id,
                lifecycle="ephemeral",
                composition_source="manual",
                composition_intent=payload.description or payload.title,
                source=source,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if message == "Skill not found" else 400
            raise api_exception(status_code, "validation_error", message) from exc
        resolved_workflow_id = created_workflow.workflow_id
        created_workflow_id = created_workflow.workflow_id
    queue = request.app.state.task_queue
    delivery = TaskDelivery(mode=delivery_mode, target=payload.delivery_target)
    completion_delivery = await _resolve_completion_delivery(
        request,
        workflow_id=resolved_workflow_id,
        owner_email=user.email,
        project_id=project_id,
        completion_mode_family=payload.completion_mode_family,
        allow_silent_completion=payload.allow_silent_completion,
    )
    try:
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
                workflow_id=resolved_workflow_id,
                project_id=project_id,
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
                workflow_id=resolved_workflow_id,
                project_id=project_id,
                workspace_root=payload.workspace_root,
                working_directory=payload.working_directory,
                status=payload.status,
            )
    except Exception:
        if created_workflow_id is not None:
            await delete_materialized_workflow(
                session_factory=request.app.state.session_factory,
                workflow_id=created_workflow_id,
            )
        raise
    return task_to_response(task)


@router.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
async def task_detail(request: Request, task_id: str) -> TaskDetailResponse:
    task = await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        dep_rows = await get_task_dependencies(session, task_id)
        step_rows = await list_step_runs_for_task(session, task_id)
        deliverables_by_step_run = {
            row.step_run_id: [
                deliverable_to_response(item)
                for item in await list_deliverables_for_step_run(session, row.step_run_id)
            ]
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
    if payload.workflow_id is not None and payload.skill_id is not None:
        raise api_exception(
            400,
            "validation_error",
            "Specify either workflow_id or skill_id, not both",
        )
    resolved_workflow_id = payload.workflow_id
    created_workflow_id: str | None = None
    if payload.skill_id is not None:
        agent_id = payload.agent_id or existing_row.agent_id
        agent = await request.app.state.agent_registry.get(
            agent_id,
            owner_email=existing_row.created_by,
        )
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        try:
            source = await get_attached_skill_workflow_source(
                session_factory=request.app.state.session_factory,
                owner_email=agent.owner_email,
                agent=agent,
                skill_id=payload.skill_id,
            )
            created_workflow = await materialize_skill_workflow(
                session_factory=request.app.state.session_factory,
                owner_email=existing_row.created_by,
                skill_id=payload.skill_id,
                lifecycle="ephemeral",
                composition_source="manual",
                composition_intent=payload.description or payload.title or existing_row.title,
                source=source,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if message == "Skill not found" else 400
            raise api_exception(status_code, "validation_error", message) from exc
        resolved_workflow_id = created_workflow.workflow_id
        created_workflow_id = created_workflow.workflow_id
    if payload.project_id is not None:
        await _validate_project_access(request, payload.project_id)
    effective_project_id = (
        payload.project_id if payload.project_id is not None else getattr(existing_row, "project_id", None)
    )
    effective_workflow_id = resolved_workflow_id or getattr(existing_row, "workflow_id", None)
    if effective_workflow_id is not None:
        await _validate_workflow_access(
            request,
            effective_workflow_id,
            owner_email=existing_row.created_by,
            project_id=effective_project_id,
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
    _validate_delivery_mode_applicability(
        delivery_mode=effective_delivery_mode,
        source_type=existing_row.source_type,
        source_ref=existing_row.source_ref,
    )
    if payload.delivery_target is not None:
        await _validate_conversation_access(request, payload.delivery_target)
    if (
        effective_delivery_mode == "same_conversation"
        and existing_row.source_type == "agent"
        and existing_row.source_ref is not None
    ):
        await _validate_conversation_access(request, existing_row.source_ref)
    CompletionDeliveryPolicy(
        completion_mode_family=effective_completion_mode_family,
        allow_silent_completion=effective_allow_silent_completion,
    )
    try:
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
            updates.pop("skill_id", None)
            if payload.skill_id is not None:
                updates["workflow_id"] = resolved_workflow_id
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
    except Exception:
        if created_workflow_id is not None:
            await delete_materialized_workflow(
                session_factory=request.app.state.session_factory,
                workflow_id=created_workflow_id,
            )
        raise
    return task_to_response(_row_to_task(row))


@router.get("/api/v1/tasks/{task_id}/comments", response_model=list[TaskCommentResponse])
async def task_comments(request: Request, task_id: str) -> list[TaskCommentResponse]:
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        rows = await list_task_comments(session, task_id)
    return [task_comment_to_response(row) for row in rows]


@router.post(
    "/api/v1/tasks/{task_id}/comments", response_model=TaskCommentResponse, status_code=201
)
async def task_comment_create(
    request: Request,
    task_id: str,
    payload: TaskCommentCreateRequest,
) -> TaskCommentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    task = await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        row = await create_task_comment(
            session,
            task_id=task_id,
            author_email=user.email,
            body=payload.body,
            intent=payload.intent,
            noop=payload.noop,
            target_step=payload.target_step,
            attempt_number=task.attempt_number,
            metadata=payload.metadata,
        )
        await session.commit()
        await session.refresh(row)
    applied = False
    if payload.intent == "answer_pause":
        applied = await _apply_answer_pause_comment(request, task, row.comment_id, payload)
    elif payload.intent == "request_revision":
        applied = await _apply_request_revision_comment(request, task, row.comment_id, payload)
    if applied:
        async with request.app.state.session_factory() as session:
            updated = await get_task_comment(session, row.comment_id)
        if updated is not None:
            row = updated
    return task_comment_to_response(row)


@router.patch("/api/v1/tasks/{task_id}/comments/{comment_id}", response_model=TaskCommentResponse)
async def task_comment_update(
    request: Request,
    task_id: str,
    comment_id: str,
    payload: TaskCommentUpdateRequest,
) -> TaskCommentResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        existing = await get_task_comment(session, comment_id)
        if existing is None or existing.task_id != task_id:
            raise api_exception(404, "not_found", "Task comment not found")
        row = await update_task_comment(
            session, comment_id, **payload.model_dump(exclude_unset=True)
        )
        await session.commit()
        if row is None:
            raise api_exception(404, "not_found", "Task comment not found")
        await session.refresh(row)
    return task_comment_to_response(row)


@router.delete("/api/v1/tasks/{task_id}", response_model=TaskActionResponse)
async def task_delete(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    await _require_task(request, task_id)
    task = await request.app.state.task_queue.cancel_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/submit", response_model=TaskActionResponse)
async def task_submit(request: Request, task_id: str) -> TaskActionResponse:
    forbid_mutation_for_viewer(request)
    task = await _require_task(request, task_id)
    await _validate_agent_access(request, task.agent_id)
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
    task = await _require_task(request, task_id)
    await _validate_agent_access(request, task.agent_id)
    task = await request.app.state.task_queue.resume_task(task_id)
    return TaskActionResponse(ok=True, task_id=task_id, status=str(task.status))


@router.post("/api/v1/tasks/{task_id}/rerun", response_model=TaskRerunResponse)
async def task_rerun(request: Request, task_id: str) -> TaskRerunResponse:
    forbid_mutation_for_viewer(request)
    task = await _require_task(request, task_id)
    await _validate_agent_access(request, task.agent_id)
    result = await request.app.state.task_queue.rerun_task(task_id)
    return TaskRerunResponse(
        ok=True,
        source_task_id=task_id,
        task_id=result.task.task_id,
        status=str(result.task.status),
        created_new=result.created_new,
    )


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
    agent_ids: set[str] = set()
    async with request.app.state.session_factory() as session:
        for task_id in payload.task_ids:
            row = await get_task(session, task_id)
            if row is None:
                continue
            require_resource_owner(request, row.created_by)
            agent_ids.add(row.agent_id)
    for agent_id in agent_ids:
        await _validate_agent_access(request, agent_id)
    result = await request.app.state.task_queue.batch_submit(payload.task_ids)
    return BatchSubmitResponse(**result)


@router.get("/api/v1/tasks/{task_id}/steps", response_model=list[StepRunResponse])
async def task_steps(request: Request, task_id: str) -> list[StepRunResponse]:
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        rows = await list_step_runs_for_task(session, task_id)
        deliverables_by_step_run = {
            row.step_run_id: [
                deliverable_to_response(item)
                for item in await list_deliverables_for_step_run(session, row.step_run_id)
            ]
            for row in rows
        }
    return [
        step_run_to_response(row, deliverables=deliverables_by_step_run.get(row.step_run_id, []))
        for row in rows
    ]


@router.get(
    "/api/v1/tasks/{task_id}/steps/{step_name}/history", response_model=list[StepRunResponse]
)
async def task_step_history(
    request: Request, task_id: str, step_name: str
) -> list[StepRunResponse]:
    await _require_task(request, task_id)
    async with request.app.state.session_factory() as session:
        rows = await list_step_run_history(session, task_id, step_name)
        deliverables_by_step_run = {
            row.step_run_id: [
                deliverable_to_response(item)
                for item in await list_deliverables_for_step_run(session, row.step_run_id)
            ]
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
    await check_agent_access(request, row, required="use")


async def _validate_workflow_access(
    request: Request,
    workflow_id: str | None,
    *,
    owner_email: str | None = None,
    project_id: str | None = None,
) -> None:
    if workflow_id is None:
        return
    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=owner_email or user.email, project_id=project_id
    )
    if workflow is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if workflow.owner_email is not None:
        require_resource_owner(request, workflow.owner_email)


async def _validate_conversation_access(request: Request, conversation_id: str | None) -> None:
    if conversation_id is None:
        return
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)


async def _validate_project_access(request: Request, project_id: str | None) -> None:
    if project_id is None:
        return
    async with request.app.state.session_factory() as session:
        row = await get_project(session, project_id)
    if row is None or row.status != "active":
        raise api_exception(404, "not_found", "Project not found")
    await check_project_access(request, row, required="use")


async def _resolve_task_project_id(
    request: Request,
    requested_project_id: str | None,
    source_type: str,
    source_ref: str | None,
) -> str | None:
    if requested_project_id is not None:
        await _validate_project_access(request, requested_project_id)
        return requested_project_id
    if source_type != "chat" or source_ref is None:
        return None
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, source_ref)
    project_id = getattr(row, "project_id", None) if row is not None else None
    if project_id is not None:
        await _validate_project_access(request, project_id)
    return project_id


async def _apply_answer_pause_comment(
    request: Request,
    task: TaskModel,
    comment_id: str,
    payload: TaskCommentCreateRequest,
) -> bool:
    state = task.workflow_state
    if state is None or state.pending_pause_type is None:
        return False
    try:
        if state.pending_pause_type == "gate":
            action = str(payload.metadata.get("action") or "continue")
            await resolve_task_pause_action(
                task=task,
                requested_action=action,
                note=payload.body,
                pause_waiter=request.app.state.pause_waiter,
                notification_service=getattr(request.app.state, "notification_service", None),
                task_queue=request.app.state.task_queue,
                session_factory=request.app.state.session_factory,
                user_email=task.created_by,
            )
        elif state.pending_pause_type == "step_input":
            await respond_task_input(
                task=task,
                response=payload.body,
                pause_waiter=request.app.state.pause_waiter,
                notification_service=getattr(request.app.state, "notification_service", None),
                task_queue=request.app.state.task_queue,
                session_factory=request.app.state.session_factory,
                user_email=task.created_by,
            )
        else:
            return False
    except (RuntimeError, ValueError):
        return False
    async with request.app.state.session_factory() as session:
        await update_task_comment(session, comment_id, applied=True)
        await session.commit()
    return True


async def _apply_request_revision_comment(
    request: Request,
    task: TaskModel,
    comment_id: str,
    payload: TaskCommentCreateRequest,
) -> bool:
    try:
        await request.app.state.task_queue.request_revision(
            task.task_id,
            target_step=payload.target_step,
            instruction=payload.body,
        )
    except (RuntimeError, ValueError):
        return False
    async with request.app.state.session_factory() as session:
        await update_task_comment(session, comment_id, applied=True)
        await session.commit()
    return True


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
        project_id=getattr(row, "project_id", None),
        attempt_number=getattr(row, "attempt_number", 1),
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
