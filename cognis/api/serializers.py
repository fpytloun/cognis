"""Serialization helpers for API DTOs."""

from __future__ import annotations

from typing import Any

from cognis.api.models import (
    AgentResponse,
    ConversationContextModel,
    ConversationResponse,
    DependencyResponse,
    EscalationResponse,
    LLMProviderResponse,
    MCPServerResponse,
    MessageEventResponse,
    PendingPauseResponse,
    ProviderTestResultResponse,
    SecretResponse,
    SessionResponse,
    SettingResponse,
    StepRunResponse,
    TaskDetailResponse,
    TaskResponse,
    ToolResponse,
    WorkflowResponse,
    WorkflowRunResponse,
)
from cognis.models.task import TaskModel
from cognis.models.workflow import Workflow


def conversation_to_response(row: Any) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=row.conversation_id,
        user_email=row.user_email,
        agent_id=row.agent_id,
        title=row.title,
        context=ConversationContextModel(
            type=row.context_type if hasattr(row, "context_type") else row.context.type,
            ref=row.context_ref if hasattr(row, "context_ref") else row.context.ref,
            platform_data=(row.context_data or {})
            if hasattr(row, "context_data")
            else row.context.platform_data,
            memory_labels=(row.memory_labels or {})
            if hasattr(row, "memory_labels")
            else row.context.memory_labels,
        ),
        root_session_id=getattr(row, "root_session_id", None),
        status=row.status,
        last_message_at=getattr(row, "last_message_at", None),
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def session_to_response(row: Any) -> SessionResponse:
    return SessionResponse(
        session_id=row.session_id,
        conversation_id=row.conversation_id,
        parent_session_id=row.parent_session_id,
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=row.delegation_mode,
        delegation_task=row.delegation_task,
        status=row.status,
        intaris_session_id=row.intaris_session_id,
        mnemory_session_id=row.mnemory_session_id,
        started_at=row.started_at,
        idle_since=row.idle_since,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        updated_at=row.updated_at,
    )


def agent_to_response(row: Any) -> AgentResponse:
    return AgentResponse(
        agent_id=row.agent_id,
        owner_email=row.owner_email,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        system_prompt=row.system_prompt,
        personality=row.personality,
        skills=row.skills,
        tools=row.tools,
        permissions=row.permissions,
        llm_config=row.llm_config,
        execution=row.execution,
        avatar_url=row.avatar_url,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def setting_to_response(row: Any) -> SettingResponse:
    return SettingResponse(
        key=row.key,
        value=row.value,
        category=row.category,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def llm_provider_to_response(row: Any) -> LLMProviderResponse:
    config = dict(row.config or {})
    last_test = getattr(row, "last_test", None)
    return LLMProviderResponse(
        provider_id=row.provider_id,
        display_name=row.display_name,
        location=row.location,
        backend=row.backend,
        config=config,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        models=list(config.get("models", [])) if isinstance(config.get("models", []), list) else [],
        last_test=ProviderTestResultResponse.model_validate(last_test) if last_test else None,
    )


def task_to_response(task: TaskModel) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        title=task.title,
        description=task.description,
        status=str(task.status),
        priority=task.priority,
        created_by=task.created_by,
        agent_id=task.agent_id,
        source_type=task.source_type,
        source_ref=task.source_ref,
        delivery=task.delivery,
        workflow_id=task.workflow_id,
        workflow_state=task.workflow_state,
        queue_name=task.queue_name,
        scheduled_for=task.scheduled_for,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        result_summary=task.result_summary,
        result_data=task.result_data,
    )


def task_detail_to_response(
    task: TaskModel,
    *,
    dependencies: list[DependencyResponse],
    step_runs: list[StepRunResponse],
    pending_pause: PendingPauseResponse | None,
    workflow_run: WorkflowRunResponse | None,
) -> TaskDetailResponse:
    return TaskDetailResponse(
        **task_to_response(task).model_dump(),
        dependencies=dependencies,
        step_runs=step_runs,
        pending_pause=pending_pause,
        workflow_run=workflow_run,
    )


def dependency_to_response(row: Any) -> DependencyResponse:
    return DependencyResponse(task_id=row.task_id, depends_on=row.depends_on, required=row.required)


def step_run_to_response(row: Any) -> StepRunResponse:
    return StepRunResponse(
        step_run_id=row.step_run_id,
        task_id=row.task_id,
        step_name=row.step_name,
        step_type=row.step_type,
        status=row.status,
        attempt=row.attempt,
        agent_id=row.agent_id,
        session_id=row.session_id,
        intaris_session_id=row.intaris_session_id,
        output=row.output,
        evaluation=row.evaluation,
        todos=row.todos,
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=getattr(row, "updated_at", None),
    )


def workflow_to_response(row: Any) -> WorkflowResponse:
    definition = row.definition if hasattr(row, "definition") else row.model_dump(mode="json")
    if isinstance(row, Workflow):
        return WorkflowResponse(**row.model_dump(mode="json"))
    return WorkflowResponse(
        workflow_id=row.workflow_id,
        name=row.name,
        description=row.description or "",
        version=row.version,
        criteria=str(definition.get("criteria", "")),
        tags=list(definition.get("tags", [])),
        interaction=dict(definition.get("interaction", {})),
        defaults=dict(definition.get("defaults", {})),
        steps=list(definition.get("steps", [])),
        is_system=row.is_system,
        owner_email=row.owner_email,
    )


def workflow_run_to_response(
    task: TaskModel,
    *,
    current_step_name: str | None,
    pending_pause: PendingPauseResponse | None,
) -> WorkflowRunResponse:
    return WorkflowRunResponse(
        task_id=task.task_id,
        workflow_id=task.workflow_id,
        workflow_state=task.workflow_state,
        current_step_name=current_step_name,
        pending_pause=pending_pause,
    )


def secret_to_response(row: dict[str, Any]) -> SecretResponse:
    return SecretResponse(
        name=str(row.get("name", "")),
        scope=str(row.get("scope", "user")),
        agent_id=row.get("agent_id"),
        description=row.get("description"),
    )


def tool_to_response(row: Any) -> ToolResponse:
    return ToolResponse(
        name=row.name,
        description=row.description,
        category=row.category,
        read_only=row.read_only,
        source=row.source.model_dump(mode="json")
        if hasattr(row.source, "model_dump")
        else row.source,
        timeout_seconds=row.timeout_seconds,
        non_bypassable=row.non_bypassable,
    )


def pending_pause_to_response(pause: Any | None) -> PendingPauseResponse | None:
    if pause is None:
        return None
    return PendingPauseResponse(
        pause_id=pause.pause_id,
        pause_type=pause.pause_type,
        task_id=pause.task_id,
        step_name=pause.step_name,
        step_run_id=pause.step_run_id,
        session_id=pause.session_id,
        question=pause.question,
        options=pause.options,
        context=pause.context,
    )


def event_to_response(row: dict[str, Any]) -> MessageEventResponse:
    return MessageEventResponse(
        seq=row.get("seq"),
        type=str(row.get("type", "")),
        data=dict(row.get("data", {})),
        timestamp=row.get("ts"),
    )


def escalation_to_response(row: Any) -> EscalationResponse:
    return EscalationResponse(
        call_id=row.call_id,
        session_id=row.session_id,
        tool_name=row.tool_name,
        decision=row.decision,
        resolved=row.resolved,
        reasoning=row.reasoning,
        risk=row.risk,
    )


def mcp_server_to_response(
    name: str, server_type: str, details: dict[str, Any]
) -> MCPServerResponse:
    return MCPServerResponse(name=name, type=server_type, details=details)
