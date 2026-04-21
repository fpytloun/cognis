"""Serialization helpers for API DTOs."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from cognis.api.models import (
    AgentResponse,
    ConversationContextModel,
    ConversationResponse,
    CredentialResponse,
    DeliverableResponse,
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
from cognis.logging import get_logger
from cognis.models.task import TaskModel
from cognis.models.workflow import Workflow
from cognis.providers.llm.reasoning import enrich_model_entry

logger = get_logger(__name__)


def conversation_to_response(row: Any) -> ConversationResponse:
    last_message_at = getattr(row, "last_message_at", None)
    last_read_at = getattr(row, "last_read_at", None)
    has_unread = last_message_at is not None and (
        last_read_at is None or last_message_at > last_read_at
    )
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
        active_session_id=getattr(row, "active_session_id", None),
        status=row.status,
        last_message_at=last_message_at,
        last_read_at=last_read_at,
        has_unread=has_unread,
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def session_to_response(row: Any) -> SessionResponse:
    return SessionResponse(
        session_id=row.session_id,
        conversation_id=row.conversation_id,
        parent_session_id=row.parent_session_id,
        previous_session_id=getattr(row, "previous_session_id", None),
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=row.delegation_mode,
        delegation_task=row.delegation_task,
        status=row.status,
        completion_reason=getattr(row, "completion_reason", None),
        intaris_session_id=row.intaris_session_id,
        mnemory_session_id=row.mnemory_session_id,
        started_at=row.started_at,
        idle_since=row.idle_since,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        updated_at=row.updated_at,
    )


def agent_to_response(row: Any) -> AgentResponse:
    sync_metadata = (
        row.sync_metadata if isinstance(getattr(row, "sync_metadata", None), dict) else {}
    )
    checked_at = sync_metadata.get("personality_sync_checked_at")
    if isinstance(checked_at, str):
        with contextlib.suppress(ValueError):
            checked_at = datetime.fromisoformat(checked_at)
    llm_config = getattr(row, "llm_config", None)
    if hasattr(llm_config, "model_dump"):
        llm_config = llm_config.model_dump(mode="json", exclude_none=True)
    permissions = getattr(row, "permissions", None)
    if hasattr(permissions, "model_dump"):
        permissions = permissions.model_dump(mode="json", exclude_none=True)
    return AgentResponse(
        agent_id=row.agent_id,
        owner_email=row.owner_email,
        name=row.name,
        display_name=getattr(row, "display_name", row.name),
        description=row.description,
        system_prompt=row.system_prompt,
        personality=row.personality,
        skills=row.skills,
        tools=row.tools,
        permissions=permissions,
        llm_config=llm_config,
        execution=getattr(row, "execution", None),
        personality_synced=bool(sync_metadata.get("personality_synced", True)),
        personality_sync_error=(
            str(sync_metadata.get("personality_sync_error"))
            if sync_metadata.get("personality_sync_error")
            else None
        ),
        personality_sync_checked_at=checked_at if isinstance(checked_at, datetime) else None,
        avatar_url=(
            f"/api/v1/images/{row.avatar_image_id}"
            if getattr(row, "avatar_image_id", None)
            else getattr(row, "avatar_url", None)
        ),
        avatar_image_id=getattr(row, "avatar_image_id", None),
        agent_type=getattr(row, "agent_type", "primary"),
        is_system=getattr(row, "is_system", False),
        hidden=getattr(row, "hidden", False),
        editable_fields=list(getattr(row, "editable_fields", []) or []),
        has_overrides=bool(getattr(row, "has_overrides", False)),
        disabled=bool(getattr(row, "disabled", False)),
        disableable=bool(getattr(row, "allow_user_disable", False)),
        sync_metadata=sync_metadata if sync_metadata else None,
        status=row.status,
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
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
    preset = str(config.get("preset", "") or "").lower()
    raw_models = config.get("models", [])
    enriched_models: list[dict[str, Any]] = []
    if isinstance(raw_models, list):
        for entry in raw_models:
            if isinstance(entry, dict):
                enriched_models.append(enrich_model_entry(dict(entry), provider_preset=preset))
            else:
                enriched_models.append(entry)
    return LLMProviderResponse(
        provider_id=row.provider_id,
        display_name=row.display_name,
        location=row.location,
        backend=row.backend,
        config=config,
        is_default=getattr(row, "is_default", False),
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        models=enriched_models,
        last_test=ProviderTestResultResponse.model_validate(last_test) if last_test else None,
    )


def task_to_response(task: TaskModel) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        title=task.title,
        description=task.description,
        expected_output=task.expected_output,
        status=str(task.status),
        priority=task.priority,
        created_by=task.created_by,
        agent_id=task.agent_id,
        source_type=task.source_type,
        source_ref=task.source_ref,
        delivery=task.delivery,
        completion_mode_family=task.completion_delivery.completion_mode_family,
        allow_silent_completion=task.completion_delivery.allow_silent_completion,
        workflow_id=task.workflow_id,
        workspace_root=task.workspace_root,
        working_directory=task.working_directory,
        workflow_state=task.workflow_state,
        queue_name=task.queue_name,
        scheduled_for=task.scheduled_for,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        updated_at=task.updated_at,
        result_summary=task.result_summary,
        result_data=task.result_data,
        applied_completion_mode=task.applied_completion_mode,
        applied_completion_reason=task.applied_completion_reason,
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


def deliverable_to_response(row: Any) -> DeliverableResponse:
    return DeliverableResponse(
        deliverable_id=row.deliverable_id,
        step_run_id=row.step_run_id,
        version=row.version,
        content=row.content,
        format=row.format,
        title=row.title,
        target=row.target,
        outputs=_coerce_dict_or_none(getattr(row, "outputs", None)) or {},
        status=row.status,
        evaluator_feedback=getattr(row, "evaluator_feedback", None),
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def step_run_to_response(
    row: Any,
    *,
    deliverables: list[DeliverableResponse] | None = None,
) -> StepRunResponse:
    return StepRunResponse(
        step_run_id=row.step_run_id,
        task_id=row.task_id,
        step_name=row.step_name,
        step_type=row.step_type,
        status=row.status,
        attempt=row.attempt,
        agent_id=row.agent_id,
        workspace_root=getattr(row, "workspace_root", None),
        working_directory=getattr(row, "working_directory", None),
        conversation_id=getattr(row, "conversation_id", None),
        session_id=row.session_id,
        intaris_session_id=row.intaris_session_id,
        deliverable_id=getattr(row, "deliverable_id", None),
        require_deliverable=getattr(row, "require_deliverable", None),
        output=_coerce_dict_or_none(row.output),
        evaluation=_coerce_dict_or_none(row.evaluation),
        deliverables=deliverables or [],
        todos=_coerce_list_of_dicts(getattr(row, "todos", None)),
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=getattr(row, "updated_at", None),
    )


def _coerce_dict_or_none(value: Any) -> dict[str, Any] | None:
    """Accept dict or return None; log warning for unexpected shapes."""

    if value is None:
        return None
    if isinstance(value, dict):
        return value
    logger.warning(
        "serializer: unexpected non-dict value coerced to None",
        extra={"extra_data": {"actual_type": type(value).__name__}},
    )
    return None


def _coerce_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    """Accept list[dict] canonical form; normalize legacy shapes.

    Guards every API response against producer bugs that might persist
    unexpected shapes (None, dict, stray primitives). Producers in the
    codebase should always persist list[dict], but this helper keeps
    API endpoints from returning 500 for historical or corrupt rows.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    logger.warning(
        "serializer: unexpected non-list value normalized to empty list",
        extra={"extra_data": {"actual_type": type(value).__name__}},
    )
    return []


def workflow_to_response(row: Any) -> WorkflowResponse:
    definition = row.definition if hasattr(row, "definition") else row.model_dump(mode="json")
    if isinstance(row, Workflow):
        payload = row.model_dump(mode="json")
        payload.update(
            editable_fields=list(getattr(row, "editable_fields", []) or []),
            has_overrides=bool(getattr(row, "has_overrides", False)),
            disabled=bool(getattr(row, "disabled", False)),
            disableable=bool(getattr(row, "allow_user_disable", False)),
            override_warnings=list(getattr(row, "override_warnings", []) or []),
        )
        return WorkflowResponse(**payload)
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
        lifecycle=getattr(row, "lifecycle", str(definition.get("lifecycle", "persistent"))),
        archived_at=getattr(row, "archived_at", None),
        lineage=(
            dict(definition.get("lineage", {}))
            if isinstance(definition.get("lineage"), Mapping)
            else None
        ),
        editable_fields=list(getattr(row, "editable_fields", []) or []),
        has_overrides=bool(getattr(row, "has_overrides", False)),
        disabled=bool(getattr(row, "disabled", False)),
        disableable=bool(getattr(row, "allow_user_disable", False)),
        override_warnings=list(getattr(row, "override_warnings", []) or []),
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


def credential_to_response(row: Any) -> CredentialResponse:
    metadata = row.metadata_json if hasattr(row, "metadata_json") else row.metadata
    return CredentialResponse(
        credential_id=row.credential_id,
        kind=row.kind,
        label=row.label,
        metadata=metadata or {},
        field_names=list(getattr(row, "field_names", []) or []),
        scope=row.scope,
        agent_id=row.agent_id,
        description=row.description,
        version=int(row.version or 1),
        status=row.status,
        last_verified_at=row.last_verified_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def tool_to_response(row: Any) -> ToolResponse:
    return ToolResponse(
        name=row.name,
        description=row.description,
        parameters=row.parameters if isinstance(row.parameters, dict) else {},
        category=row.category,
        read_only=row.read_only,
        capabilities=[str(capability) for capability in getattr(row, "capabilities", []) or []],
        classification_status=getattr(row, "classification_status", None),
        classification_source=getattr(row, "classification_source", None),
        classification_confidence=getattr(row, "classification_confidence", None),
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


def event_to_response(row: Mapping[str, Any]) -> MessageEventResponse:
    data = row.get("data", {})
    normalized_data = dict(data) if isinstance(data, Mapping) else {}
    return MessageEventResponse(
        seq=row.get("seq"),
        type=str(row.get("type", "")),
        data=normalized_data,
        timestamp=row.get("ts") or row.get("timestamp"),
    )


def serialize_event_rows(
    rows: list[object],
    *,
    log_label: str,
    log_context: dict[str, Any],
) -> list[MessageEventResponse]:
    responses: list[MessageEventResponse] = []
    skipped_rows = 0
    for row in rows:
        if not isinstance(row, Mapping):
            skipped_rows += 1
            continue
        responses.append(event_to_response(row))
    if skipped_rows:
        logger.warning(
            "Skipped malformed event rows during API serialization",
            extra={"extra_data": {**log_context, "route": log_label, "skipped_rows": skipped_rows}},
        )
    return responses


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
