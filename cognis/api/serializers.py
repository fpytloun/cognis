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
    ProjectGrantResponse,
    ProjectResponse,
    ProjectSourceResponse,
    ProviderTestResultResponse,
    SecretResponse,
    SessionResponse,
    SettingResponse,
    StepRunResponse,
    TaskCommentResponse,
    TaskDetailResponse,
    TaskResponse,
    ToolResponse,
    WorkflowResponse,
    WorkflowRunResponse,
)
from cognis.logging import get_logger
from cognis.models.conversation_state import ConversationStateEnvelope
from cognis.models.task import TaskModel
from cognis.models.tool import stable_tool_id
from cognis.models.workflow import Workflow
from cognis.providers.llm.reasoning import enrich_model_entry

logger = get_logger(__name__)


def _session_result_anchors(content: str | None) -> list[dict[str, Any]]:
    if not content:
        return []
    lines = content.splitlines()
    anchors: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_no, line in enumerate(lines, start=1):
        if not line.startswith("[[") or not line.endswith("]]"):
            continue
        anchor = line[2:-2]
        if not anchor.startswith("message:"):
            continue
        if current is not None:
            current["end_line"] = line_no - 1
            anchors.append(current)
        current = {
            "anchor": anchor,
            "label": f"Assistant message {anchor.split(':', 1)[1]}",
            "kind": "section",
            "start_line": line_no,
            "end_line": line_no,
        }
    if current is not None:
        current["end_line"] = len(lines)
        anchors.append(current)
    return anchors


def _session_result_sections(
    content: str | None,
    anchors: list[dict[str, Any]],
    *,
    max_chars: int = 12_000,
) -> list[dict[str, Any]]:
    if not content or not anchors:
        return []
    lines = content.splitlines()
    sections: list[dict[str, Any]] = []
    used_chars = 0
    for anchor in anchors:
        start = anchor.get("start_line")
        end = anchor.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        text = "\n".join(lines[max(start - 1, 0) : min(end, len(lines))])
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        truncated = len(text) > remaining
        if truncated:
            text = text[:remaining].rstrip() + "\n[section truncated]"
        used_chars += len(text)
        sections.append({**anchor, "content": text, "truncated": truncated})
        if truncated:
            break
    return sections


def conversation_to_response(
    row: Any,
    *,
    has_active_turn: bool = False,
    active_session: Any | None = None,
    active_turn_state: dict[str, Any] | None = None,
    pending_notification_types: list[str] | None = None,
    conversation_state: ConversationStateEnvelope | None = None,
    managed_link: Any | None = None,
) -> ConversationResponse:
    last_message_at = getattr(row, "last_message_at", None)
    last_read_at = getattr(row, "last_read_at", None)
    platform_data = (
        (row.context_data or {}) if hasattr(row, "context_data") else row.context.platform_data
    )
    has_unread = last_message_at is not None and (
        last_read_at is None or last_message_at > last_read_at
    )
    return ConversationResponse(
        conversation_id=row.conversation_id,
        user_email=row.user_email,
        agent_id=row.agent_id,
        project_id=getattr(row, "project_id", None),
        title=row.title,
        title_source=getattr(row, "title_source", "unset"),
        context=ConversationContextModel(
            type=row.context_type if hasattr(row, "context_type") else row.context.type,
            ref=row.context_ref if hasattr(row, "context_ref") else row.context.ref,
            platform_data=platform_data,
            memory_labels=(row.memory_labels or {})
            if hasattr(row, "memory_labels")
            else row.context.memory_labels,
        ),
        active_session_id=getattr(row, "active_session_id", None),
        active_executor_id=getattr(row, "active_executor_id", None),
        active_executor_assigned_at=getattr(row, "active_executor_assigned_at", None),
        active_executor_expires_at=getattr(row, "active_executor_expires_at", None),
        active_executor_source=getattr(row, "active_executor_source", None),
        active_session_status=getattr(active_session, "status", None),
        active_session_completion_reason=getattr(active_session, "completion_reason", None),
        active_turn_chat_mode=(active_turn_state or {}).get("chat_mode"),
        active_turn_chat_mode_source=(active_turn_state or {}).get("chat_mode_source"),
        pending_notification_types=pending_notification_types or [],
        starred_at=getattr(row, "starred_at", None),
        status=row.status,
        last_message_at=last_message_at,
        last_read_at=last_read_at,
        has_unread=has_unread,
        has_active_turn=has_active_turn,
        managed_agent=(
            {
                "channel": "agent_work",
                "link_id": getattr(managed_link, "link_id", None) or platform_data.get("link_id"),
                "controller_agent_id": platform_data.get("controller_agent_id"),
                "controller_conversation_id": platform_data.get("controller_conversation_id"),
                "controller_session_id": platform_data.get("controller_session_id"),
                "target_agent_id": platform_data.get("target_agent_id"),
                "conversation_state": getattr(managed_link, "conversation_state", None),
                "turn_state": getattr(managed_link, "turn_state", None),
                "last_result_summary": getattr(managed_link, "last_result_summary", None),
                "last_error": getattr(managed_link, "last_error", None),
            }
            if platform_data.get("kind") in {"agent_work", "managed_agent_conversation"}
            else None
        ),
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
        conversation_state=conversation_state,
    )


def session_to_response(row: Any, *, include_result_content: bool = False) -> SessionResponse:
    result_content = getattr(row, "result_content", None) if include_result_content else None
    result_anchors = _session_result_anchors(result_content)
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
        result_content=result_content,
        result_anchors=result_anchors if include_result_content else None,
        result_sections=_session_result_sections(result_content, result_anchors)
        if include_result_content
        else None,
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
        is_shared_with_me=bool(getattr(row, "is_shared_with_me", False)),
        shared_by_email=getattr(row, "shared_by_email", None),
        granted_permission=getattr(row, "granted_permission", None),
        executor_scope=getattr(row, "executor_scope", None),
        is_readonly_for_caller=bool(getattr(row, "is_readonly_for_caller", False)),
        sync_metadata=sync_metadata if sync_metadata else None,
        status=row.status,
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def project_source_to_response(row: Any) -> ProjectSourceResponse:
    return ProjectSourceResponse(
        source_id=row.source_id,
        project_id=row.project_id,
        name=row.name,
        local_path=row.local_path,
        remote_url=row.remote_url,
        default_branch=row.default_branch,
        credential_ref=row.credential_ref,
        instructions=row.instructions,
        metadata=getattr(row, "metadata_json", None) or {},
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def project_grant_to_response(row: Any) -> ProjectGrantResponse:
    return ProjectGrantResponse(
        grant_id=row.grant_id,
        project_id=row.project_id,
        grantee_type=row.grantee_type,
        grantee_user_email=row.grantee_user_email,
        grantee_group_id=row.grantee_group_id,
        permission=row.permission,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
        note=row.note,
    )


def project_to_response(
    row: Any,
    *,
    sources: list[Any] | None = None,
    workflow_ids: list[str] | None = None,
    active_schedule_count: int = 0,
    grants: list[Any] | None = None,
) -> ProjectResponse:
    return ProjectResponse(
        project_id=row.project_id,
        owner_email=row.owner_email,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        default_workflow_id=row.default_workflow_id,
        avatar_image_id=getattr(row, "avatar_image_id", None),
        avatar_url=(
            f"/api/v1/images/{row.avatar_image_id}"
            if getattr(row, "avatar_image_id", None)
            else getattr(row, "avatar_url", None)
        ),
        metadata=getattr(row, "metadata_json", None) or {},
        status=row.status,
        sources=[project_source_to_response(source) for source in sources or []],
        workflow_ids=workflow_ids or [],
        active_schedule_count=active_schedule_count,
        grants=[project_grant_to_response(grant) for grant in grants or []],
        is_shared_with_me=bool(getattr(row, "is_shared_with_me", False)),
        shared_by_email=getattr(row, "shared_by_email", None),
        granted_permission=getattr(row, "granted_permission", None),
        is_readonly_for_caller=bool(getattr(row, "is_readonly_for_caller", False)),
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
        owner_email=getattr(row, "owner_email", None),
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
        created_by_agent_id=task.created_by_agent_id,
        source_type=task.source_type,
        source_ref=task.source_ref,
        delivery=task.delivery,
        completion_mode_family=task.completion_delivery.completion_mode_family,
        allow_silent_completion=task.completion_delivery.allow_silent_completion,
        interaction_mode_override=task.interaction_mode_override,
        session_policy=task.session_policy,
        workflow_id=task.workflow_id,
        project_id=task.project_id,
        attempt_number=task.attempt_number,
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


def task_comment_to_response(row: Any) -> TaskCommentResponse:
    return TaskCommentResponse(
        comment_id=row.comment_id,
        task_id=row.task_id,
        author_email=row.author_email,
        body=row.body,
        intent=row.intent,
        noop=row.noop,
        target_step=row.target_step,
        confidence=row.confidence,
        applied=row.applied,
        attempt_number=row.attempt_number,
        metadata=getattr(row, "metadata_json", None) or {},
        created_at=getattr(row, "created_at", None),
        updated_at=getattr(row, "updated_at", None),
    )


def deliverable_to_response(row: Any) -> DeliverableResponse:
    return DeliverableResponse(
        deliverable_id=row.deliverable_id,
        step_run_id=row.step_run_id,
        version=row.version,
        attempt_number=getattr(row, "attempt_number", 1),
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
    accumulated_duration_seconds: float | None = None,
) -> StepRunResponse:
    duration_seconds = _duration_seconds(row.started_at, row.completed_at)
    return StepRunResponse(
        step_run_id=row.step_run_id,
        task_id=row.task_id,
        step_name=row.step_name,
        step_type=row.step_type,
        status=row.status,
        attempt=row.attempt,
        attempt_number=getattr(row, "attempt_number", 1),
        superseded_by_step_run_id=getattr(row, "superseded_by_step_run_id", None),
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
        runtime_info=_coerce_dict_or_none(getattr(row, "runtime_info", None)),
        deliverables=deliverables or [],
        todos=_coerce_list_of_dicts(getattr(row, "todos", None)),
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=getattr(row, "updated_at", None),
        duration_seconds=duration_seconds,
        latest_attempt_duration_seconds=duration_seconds,
        accumulated_duration_seconds=(
            accumulated_duration_seconds
            if accumulated_duration_seconds is not None
            else duration_seconds
        ),
    )


def _duration_seconds(started_at: Any, completed_at: Any) -> float | None:
    if started_at is None or completed_at is None:
        return None
    try:
        return max(0.0, (completed_at - started_at).total_seconds())
    except Exception:
        return None


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
        project_id=task.project_id,
        attempt_number=task.attempt_number,
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
        tool_id=stable_tool_id(row),
        name=row.name,
        description=row.description,
        parameters=row.parameters if isinstance(row.parameters, dict) else {},
        category=row.category,
        profile_group=getattr(row, "profile_group", None),
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
        question=getattr(pause, "question", None),
        questions=getattr(pause, "questions", None),
        options=getattr(pause, "options", None),
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
