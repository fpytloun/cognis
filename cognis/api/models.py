"""FastAPI request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, model_validator
from pydantic_core import PydanticCustomError

from cognis.models.artifact import AttachmentRef
from cognis.models.task import TaskDelivery
from cognis.models.workflow import WorkflowState


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class CursorPage[T](BaseModel):
    items: list[T]
    cursor: str | None = None
    has_more: bool = False


class SetupRequest(BaseModel):
    token: str
    email: EmailStr
    name: str | None = None
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    token: str | None = None
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    token: str
    refresh_token: str | None = None
    expires_in: int
    user: dict[str, Any]


class AuthSessionResponse(BaseModel):
    user: dict[str, Any]
    expires_at: datetime
    token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None


class ExchangeTokenResponse(BaseModel):
    token: str
    target: str
    expires_in: int


class BootstrapStatusResponse(BaseModel):
    setup_available: bool
    setup_complete: bool


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyResponse(BaseModel):
    key_id: str
    name: str
    prefix: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ApiKeyCreateResponse(ApiKeyResponse):
    api_key: str


class UserResponse(BaseModel):
    email: str
    name: str | None = None
    role: str
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None
    disabled_at: datetime | None = None
    disabled_by: str | None = None


class UserCreateRequest(BaseModel):
    email: EmailStr
    name: str | None = None
    password: str = Field(min_length=8)
    role: str = "user"


class UserUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None


class ProfileUpdateRequest(BaseModel):
    name: str | None = None


class ProviderTestResultResponse(BaseModel):
    ok: bool
    model_resolved: str | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    error_detail: str | None = None
    tested_at: datetime | None = None


class LLMProviderTestResponse(ProviderTestResultResponse):
    provider_id: str


class HealthResponse(BaseModel):
    status: str
    providers: dict[str, dict[str, Any]]
    remember_queue: dict[str, Any] | None = None


class SystemDiagnosticsResponse(BaseModel):
    readiness: dict[str, bool] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    agents: dict[str, Any] = Field(default_factory=dict)
    key_fingerprint: str | None = None


class WebConfigStatusResponse(BaseModel):
    """Web backend configuration status."""

    # Legacy single-axis backend, retained for back-compat.
    backend: str = "direct"
    # Split axes: search and fetch are independently configurable.
    search_backend: str = "direct"
    fetch_backend: str = "direct"
    fetch_fallback_browser: bool = True
    tavily_configured: bool = False
    brave_configured: bool = False
    searxng_url: str = ""
    searxng_configured: bool = False
    available_backends: list[str] = Field(default_factory=lambda: ["direct"])
    available_search_backends: list[str] = Field(default_factory=lambda: ["direct"])
    available_fetch_backends: list[str] = Field(default_factory=lambda: ["direct"])


class ConversationContextModel(BaseModel):
    type: str = "web"
    ref: str | None = None
    platform_data: dict[str, Any] = Field(default_factory=dict)
    memory_labels: dict[str, str] = Field(default_factory=dict)


class ConversationResolveRequest(BaseModel):
    """Find-or-create a conversation for a given agent and context type."""

    agent_id: str
    context_type: str = "web"


class ConversationCreateRequest(BaseModel):
    agent_id: str
    title: str | None = None
    context: ConversationContextModel = Field(default_factory=ConversationContextModel)


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    archived: bool | None = None


class ConversationResponse(BaseModel):
    conversation_id: str
    user_email: str
    agent_id: str
    title: str | None = None
    context: ConversationContextModel
    active_session_id: str | None = None
    status: str
    last_message_at: datetime | None = None
    last_read_at: datetime | None = None
    has_unread: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MessageEventResponse(BaseModel):
    seq: int | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None


class MessageHistoryResponse(BaseModel):
    items: list[MessageEventResponse]
    last_seq: int = 0
    has_more: bool = False
    active_session_id: str | None = Field(
        default=None,
        description="Active session identifier for switching the client from lineage bootstrap to active-session replay.",
    )
    active_session_last_seq: int = Field(
        default=0,
        description="Last persisted sequence number in the active session's seq space.",
    )
    history_truncated: bool = Field(
        default=False,
        description="Whether the returned history is incomplete and the client should surface a truncation notice.",
    )
    truncation_reason: str | None = Field(
        default=None,
        description="Machine-readable reason explaining why the history response was truncated.",
    )


class SessionResponse(BaseModel):
    session_id: str
    conversation_id: str
    parent_session_id: str | None = None
    previous_session_id: str | None = None
    user_email: str
    agent_id: str
    delegation_mode: str | None = None
    delegation_task: str | None = None
    status: str
    completion_reason: str | None = None
    intaris_session_id: str | None = None
    mnemory_session_id: str | None = None
    started_at: datetime | None = None
    idle_since: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    updated_at: datetime | None = None


class SessionEventsResponse(BaseModel):
    session_id: str
    items: list[MessageEventResponse]
    last_seq: int = 0
    has_more: bool = False


class SessionCancelResponse(BaseModel):
    ok: bool
    session_id: str


class AgentRequestBase(BaseModel):
    agent_id: str | None = None
    name: str
    display_name: str | None = None  # backward compat alias for name
    description: str | None = None
    system_prompt: str | None = None
    personality: dict[str, Any] | None = None
    skills: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    llm_config: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    avatar_image_id: str | None = None
    status: str | None = None


class AgentCreateRequest(AgentRequestBase):
    agent_id: str | None = None  # optional: auto-generated from name
    agent_type: str = "primary"  # "primary" | "secondary"
    status: str = "draft"


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    display_name: str | None = None  # backward compat alias for name
    description: str | None = None
    system_prompt: str | None = None
    personality: dict[str, Any] | None = None
    skills: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    llm_config: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    avatar_image_id: str | None = None
    status: str | None = None


class AgentResponse(BaseModel):
    agent_id: str
    owner_email: str
    name: str
    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    personality: dict[str, Any] | None = None
    skills: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    llm_config: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    personality_synced: bool = True
    personality_sync_error: str | None = None
    personality_sync_checked_at: datetime | None = None
    avatar_url: str | None = None  # computed from avatar_image_id (or legacy fallback)
    avatar_image_id: str | None = None
    # Type system
    agent_type: str = "primary"
    is_system: bool = False
    hidden: bool = False
    editable_fields: list[str] = Field(default_factory=list)
    has_overrides: bool = False
    disabled: bool = False
    disableable: bool = False
    is_shared_with_me: bool = False
    shared_by_email: str | None = None
    granted_permission: str | None = None
    executor_scope: str | None = None
    is_readonly_for_caller: bool = False
    sync_metadata: dict[str, Any] | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentCardResponse(BaseModel):
    name: str
    description: str | None = None
    url: str
    capabilities: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    authentication: dict[str, Any] = Field(default_factory=dict)


class AgentGrantCreateRequest(BaseModel):
    grantee_email: str
    executor_scope: Literal["owner_executor", "grantee_executor"] = "owner_executor"
    note: str | None = None


class AgentGrantUpdateRequest(BaseModel):
    executor_scope: Literal["owner_executor", "grantee_executor"] | None = None
    note: str | None = None


class AgentGrantResponse(BaseModel):
    grant_id: str
    agent_id: str
    grantee_type: str
    grantee_user_email: str | None = None
    grantee_group_id: str | None = None
    permission: str
    executor_scope: Literal["owner_executor", "grantee_executor"]
    granted_by: str
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    note: str | None = None


class SettingResponse(BaseModel):
    key: str
    value: Any
    category: str
    updated_by: str | None = None
    updated_at: datetime | None = None


class SettingsCategoryResponse(BaseModel):
    category: str
    items: list[SettingResponse]


class SettingUpdateRequest(BaseModel):
    value: Any


class LLMProviderRequest(BaseModel):
    provider_id: str | None = None  # optional: auto-generated from display_name
    display_name: str
    location: str = "controller"
    backend: str = "litellm"
    config: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"


class LLMProviderUpdateRequest(BaseModel):
    display_name: str | None = None
    location: str | None = None
    backend: str | None = None
    config: dict[str, Any] | None = None
    status: str | None = None


class LLMProviderResponse(BaseModel):
    provider_id: str
    display_name: str
    location: str
    backend: str
    config: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    last_test: ProviderTestResultResponse | None = None


class ModelRoutingEntry(BaseModel):
    model: str | None = None
    reasoning_effort: str | None = None


class ModelRoutingResponse(BaseModel):
    default: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    classifier: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    compaction: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    evaluator: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    speech_to_text: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    image_generation: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    attachment_analysis: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)


class ModelRoutingUpdateRequest(BaseModel):
    default: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    classifier: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    compaction: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    evaluator: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    speech_to_text: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    image_generation: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    attachment_analysis: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)


class EnrichModelsRequest(BaseModel):
    """Request body for enriching model IDs with provider metadata."""

    model_ids: list[str] = Field(max_length=50)


class EnrichModelsPreviewRequest(BaseModel):
    """Request body for enriching model IDs without a saved provider."""

    model_ids: list[str] = Field(max_length=50)
    preset: str = ""
    base_url: str = ""
    api_key: str | None = None
    secret_name: str | None = None
    env_var: str | None = None


class PendingPauseResponse(BaseModel):
    pause_id: str
    pause_type: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    question: str | None = None
    options: list[dict[str, Any]] | None = None
    context: dict[str, Any] | None = None


class TaskCreateRequest(BaseModel):
    agent_id: str
    title: str
    description: str = ""
    expected_output: str | None = None
    priority: int = 0
    workflow_id: str | None = None
    skill_id: str | None = None
    delivery_mode: str = "same_conversation"
    delivery_target: str | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None
    source_type: str = "api"
    source_ref: str | None = None
    status: str = "draft"
    workspace_root: str | None = None
    working_directory: str | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    expected_output: str | None = None
    priority: int | None = None
    agent_id: str | None = None
    workflow_id: str | None = None
    skill_id: str | None = None
    delivery_mode: str | None = None
    delivery_target: str | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None
    workspace_root: str | None = None
    working_directory: str | None = None


class TaskActionResponse(BaseModel):
    ok: bool
    task_id: str
    status: str


class TaskRerunResponse(BaseModel):
    ok: bool
    source_task_id: str
    task_id: str
    status: str
    created_new: bool


class BatchSubmitRequest(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


class BatchSubmitItemResponse(BaseModel):
    task_id: str
    status: str
    error: str | None = None


class BatchSubmitResponse(BaseModel):
    results: list[BatchSubmitItemResponse]
    succeeded: int
    failed: int


class DependencyRequest(BaseModel):
    depends_on: str
    required: bool = True


class DependencyResponse(BaseModel):
    task_id: str
    depends_on: str
    required: bool


class TaskResponse(BaseModel):
    task_id: str
    title: str
    description: str = ""
    expected_output: str | None = None
    status: str
    priority: int = 0
    created_by: str
    agent_id: str
    source_type: str
    source_ref: str | None = None
    delivery: TaskDelivery = Field(default_factory=TaskDelivery)
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False
    workflow_id: str | None = None
    workspace_root: str | None = None
    working_directory: str | None = None
    workflow_state: WorkflowState | None = None
    queue_name: str = "default"
    scheduled_for: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    result_summary: str | None = None
    result_data: dict[str, Any] | None = None
    applied_completion_mode: str | None = None
    applied_completion_reason: str | None = None


class WorkflowRunResponse(BaseModel):
    task_id: str
    workflow_id: str | None = None
    workflow_state: WorkflowState | None = None
    current_step_name: str | None = None
    pending_pause: PendingPauseResponse | None = None


class DeliverableResponse(BaseModel):
    deliverable_id: str
    step_run_id: str
    version: int
    content: str
    format: str = "markdown"
    title: str | None = None
    target: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    status: str
    evaluator_feedback: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChannelPairingRequestResponse(BaseModel):
    request_id: str
    owner_email: str
    account_id: str
    account_display_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    channel_type: str
    sender_id: str
    sender_name: str | None = None
    chat_id: str
    chat_name: str | None = None
    code: str
    status: str
    attempts: int = 0
    expires_at: datetime
    created_at: datetime
    completed_at: datetime | None = None


class StepRunResponse(BaseModel):
    step_run_id: str
    task_id: str
    step_name: str
    step_type: str
    status: str
    attempt: int = 1
    agent_id: str
    workspace_root: str | None = None
    working_directory: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    intaris_session_id: str | None = None
    deliverable_id: str | None = None
    require_deliverable: bool | None = None
    output: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    runtime_info: dict[str, Any] | None = None
    deliverables: list[DeliverableResponse] = Field(default_factory=list)
    todos: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class TaskDetailResponse(TaskResponse):
    dependencies: list[DependencyResponse] = Field(default_factory=list)
    step_runs: list[StepRunResponse] = Field(default_factory=list)
    workflow_run: WorkflowRunResponse | None = None
    pending_pause: PendingPauseResponse | None = None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    name: str
    description: str | None = None
    schedule_type: str = "cron"
    cron_expr: str | None = None
    interval_seconds: int | None = None
    one_shot_at: datetime | None = None
    timezone: str = "UTC"
    agent_id: str
    workflow_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    max_concurrent_runs: int = 1
    delete_after_run: bool = False
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False

    @model_validator(mode="after")
    def _validate_schedule_type(self) -> CreateScheduleRequest:
        if self.schedule_type == "cron" and not self.cron_expr:
            raise PydanticCustomError("missing_cron_expr", "cron_expr required for cron schedules")
        if self.schedule_type == "interval" and not self.interval_seconds:
            raise PydanticCustomError(
                "missing_interval", "interval_seconds required for interval schedules"
            )
        if self.schedule_type == "interval" and (self.interval_seconds or 0) < 10:
            raise PydanticCustomError("interval_too_short", "interval_seconds must be at least 10")
        if self.schedule_type == "one_shot" and not self.one_shot_at:
            raise PydanticCustomError(
                "missing_one_shot_at", "one_shot_at required for one_shot schedules"
            )
        return self


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    schedule_type: str | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    one_shot_at: datetime | None = None
    timezone: str | None = None
    agent_id: str | None = None
    workflow_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] | None = None
    enabled: bool | None = None
    max_concurrent_runs: int | None = None
    delete_after_run: bool | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None


class ScheduleResponse(BaseModel):
    schedule_id: str
    name: str
    description: str | None = None
    schedule_type: str
    cron_expr: str | None = None
    interval_seconds: int | None = None
    one_shot_at: datetime | None = None
    timezone: str = "UTC"
    agent_id: str
    workflow_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    max_concurrent_runs: int = 1
    delete_after_run: bool = False
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False
    last_fired_at: datetime | None = None
    next_fire_at: datetime | None = None
    last_run_status: str | None = None
    consecutive_errors: int = 0
    disabled_reason: str | None = None
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Computed fields
    human_schedule: str | None = None


class GateResponseRequest(BaseModel):
    step_name: str | None = None
    action: str
    feedback: str | None = None


class StepResponseRequest(BaseModel):
    step_name: str | None = None
    response: str = ""


class WorkflowRequest(BaseModel):
    workflow_id: str | None = None
    name: str
    description: str = ""
    version: int = 1
    criteria: str = ""
    tags: list[str] = Field(default_factory=list)
    interaction: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle: str = "persistent"
    lineage: dict[str, Any] | None = None


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    version: int | None = None
    criteria: str | None = None
    tags: list[str] | None = None
    interaction: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    lifecycle: str | None = None
    lineage: dict[str, Any] | None = None


class StepProfileResponse(BaseModel):
    profile_id: str
    name: str
    mode: str = "soft"
    config: dict[str, Any] = Field(default_factory=dict)
    has_override: bool = False
    is_custom: bool = False


class StepProfileUpdateRequest(BaseModel):
    name: str | None = None
    mode: str = "soft"
    config: dict[str, Any] = Field(default_factory=dict)


class StepProfileCreateRequest(StepProfileUpdateRequest):
    profile_id: str


class WorkflowResponse(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    version: int = 1
    criteria: str = ""
    tags: list[str] = Field(default_factory=list)
    interaction: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    is_system: bool = False
    owner_email: str | None = None
    lifecycle: str = "persistent"
    archived_at: datetime | None = None
    lineage: dict[str, Any] | None = None
    editable_fields: list[str] = Field(default_factory=list)
    has_overrides: bool = False
    disabled: bool = False
    disableable: bool = False
    override_warnings: list[str] = Field(default_factory=list)


class SecretUpsertRequest(BaseModel):
    name: str
    value: str
    scope: str = "user"
    agent_id: str | None = None
    description: str | None = None


class SecretResponse(BaseModel):
    name: str
    scope: str
    agent_id: str | None = None
    description: str | None = None


class CredentialUpsertRequest(BaseModel):
    credential_id: str
    kind: str
    label: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scope: str = "user"
    agent_id: str | None = None
    description: str | None = None
    expires_at: datetime | None = None


class CredentialResponse(BaseModel):
    credential_id: str
    kind: str
    label: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    field_names: list[str] = Field(default_factory=list)
    scope: str = "user"
    agent_id: str | None = None
    description: str | None = None
    version: int = 1
    status: str = "active"
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ToolResponse(BaseModel):
    tool_id: str | None = None
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    category: str = "general"
    profile_group: str | None = None
    read_only: bool = False
    capabilities: list[str] = Field(default_factory=list)
    classification_status: str | None = None
    classification_source: str | None = None
    classification_confidence: float | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    non_bypassable: bool = False


class EffectiveToolItemResponse(BaseModel):
    tool_id: str
    name: str
    description: str
    category: str = "general"
    profile_group: str | None = None
    read_only: bool = False
    capabilities: list[str] = Field(default_factory=list)
    classification_status: str | None = None
    classification_source: str | None = None
    classification_confidence: float | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    permission: str = "evaluate"
    enabled: bool = True
    disabled_reason: str | None = None
    timeout_seconds: int = 30
    non_bypassable: bool = False


class EffectiveToolsStateResponse(BaseModel):
    tools: list[EffectiveToolItemResponse] = Field(default_factory=list)
    connected: bool = False
    observed_at: datetime | None = None
    stale_after: datetime | None = None


class EffectiveToolsExecutorResponse(BaseModel):
    executor_id: str | None = None
    executor_type: str | None = None
    selection_source: str = "unresolved"


class EffectiveToolsResponse(BaseModel):
    executor: EffectiveToolsExecutorResponse
    configured_state: EffectiveToolsStateResponse
    live_state: EffectiveToolsStateResponse
    warnings: list[str] = Field(default_factory=list)


class EffectiveToolsPreviewRequest(BaseModel):
    tools: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    skills: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None


class ToolClassificationRequeueRequest(BaseModel):
    tool_id: str | None = None
    pending_only: bool = False


class ToolClassificationOverrideRequest(BaseModel):
    tool_id: str
    profile_group: str
    capabilities: list[str] = Field(default_factory=list)


class ToolClassificationActionResponse(BaseModel):
    updated: int = 0
    status: str = "ok"


class MCPServerResponse(BaseModel):
    name: str
    type: str
    details: dict[str, Any] = Field(default_factory=dict)


class MCPServerTestItemResponse(BaseModel):
    name: str
    ok: bool
    tools: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_detail: str | None = None
    duration_ms: int | None = None


class MCPServerTestResponse(BaseModel):
    ok: bool
    items: list[MCPServerTestItemResponse] = Field(default_factory=list)


class SkillAssetResponse(BaseModel):
    filename: str
    asset_id: str
    artifact_namespace: str
    artifact_object_id: str
    content_hash: str
    size_bytes: int
    content_type: str
    url: str | None = None


class SkillAssetInput(BaseModel):
    filename: str
    existing_asset_id: str | None = None
    source_artifact_id: str | None = None
    content: str | None = None
    content_b64: str | None = None
    content_type: str | None = None


class SkillVersionResponse(BaseModel):
    version_id: str
    skill_id: str
    version_number: int
    content_hash: str
    schema_version: int = 1
    instructions: str
    tools: list[dict[str, Any]] | None = None
    linked_tool_ids: list[str] | None = None
    prompt_templates: dict[str, Any] | None = None
    secret_placeholders: list[str] | None = None
    steps: list[dict[str, Any]] | None = None
    decomposition_source_hash: str | None = None
    decomposition_stale: bool = False
    source_url: str | None = None
    resolved_url: str | None = None
    commit_sha: str | None = None
    import_checksum: str | None = None
    imported_at: datetime | None = None
    import_format: str | None = None
    asset_manifest: list[SkillAssetResponse] | None = None
    created_at: datetime | None = None


class SkillResponse(BaseModel):
    skill_id: str
    name: str
    description: str | None = None
    instructions: str
    tools: list[dict[str, Any]] | None = None
    linked_tool_ids: list[str] | None = None
    prompt_templates: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    attach_to_all_agents: bool = False
    auto_load: bool = False  # Deprecated alias
    is_system: bool = False
    source: str = "db"
    current_version_id: str | None = None
    current_version: SkillVersionResponse | None = None
    owner_email: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillCreateRequest(BaseModel):
    name: str
    description: str | None = None
    instructions: str
    tools: list[dict[str, Any]] | None = None
    linked_tool_ids: list[str] | None = None
    prompt_templates: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    decomposition_source_hash: str | None = None
    tags: list[str] | None = None
    attach_to_all_agents: bool | None = None
    auto_load: bool | None = None
    secret_placeholders: list[str] | None = None
    assets: list[SkillAssetInput] | None = None


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    linked_tool_ids: list[str] | None = None
    prompt_templates: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    decomposition_source_hash: str | None = None
    tags: list[str] | None = None
    attach_to_all_agents: bool | None = None
    auto_load: bool | None = None
    secret_placeholders: list[str] | None = None
    assets: list[SkillAssetInput] | None = None


class SkillImportRequest(BaseModel):
    url: str | None = None
    content: str | None = None
    content_b64: str | None = None
    filename: str | None = None
    format: str | None = None
    name: str | None = None
    tags: list[str] | None = None
    linked_tool_ids: list[str] | None = None
    attach_to_all_agents: bool | None = None
    auto_load: bool | None = None


class SkillDecompositionPreviewResponse(BaseModel):
    skill_id: str
    source_hash: str
    rationale: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)


class SkillExportResponse(BaseModel):
    format: str
    content: str | None = None
    content_b64: str | None = None
    content_type: str | None = None
    filename: str
    warnings: list[str] = Field(default_factory=list)


class ExecutorStatusResponse(BaseModel):
    executor_type: str
    status: str
    active_executors: int = 0
    capabilities: dict[str, Any] = Field(default_factory=dict)
    native_tools: list[str] = Field(default_factory=list)


class IntarisMCPServerResponse(BaseModel):
    name: str
    transport: str | None = None
    enabled: bool = True
    tools_count: int = 0
    agent_pattern: str = "*"


class ExecutorConfigResponse(BaseModel):
    executor_id: str
    name: str
    executor_type: str = "in_process"
    labels: dict[str, Any] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    enabled_tool_groups: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    runtime_state: str = "offline"
    desired_config_version: int = 0
    applied_config_version: int = 0
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    last_observed_at: datetime | None = None
    is_default: bool = False
    shared: bool = False
    owner_email: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExecutorCreateRequest(BaseModel):
    executor_id: str | None = None
    name: str
    executor_type: str = "in_process"
    labels: dict[str, Any] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    enabled_tool_groups: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    shared: bool = False


class ExecutorUpdateRequest(BaseModel):
    name: str | None = None
    labels: dict[str, Any] | None = None
    enabled_tools: list[str] | None = None
    enabled_tool_groups: list[str] | None = None
    config: dict[str, Any] | None = None
    status: str | None = None
    is_default: bool | None = None
    shared: bool | None = None


class ExecutorTokenResponse(BaseModel):
    executor_id: str
    token: str
    expires_in: int


# --- MCP Servers ---


class MCPServerConfigResponse(BaseModel):
    server_id: str
    name: str
    transport: str
    command: str | None = None
    url: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30
    description: str | None = None
    shared: bool = False
    owner_email: str
    status: str = "active"
    invalid_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MCPServerCreateRequest(BaseModel):
    server_id: str | None = None
    name: str
    transport: str = "stdio"
    command: str | None = None
    url: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30
    description: str | None = None
    shared: bool = False

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerCreateRequest:
        if self.transport == "stdio":
            if not self.command:
                raise PydanticCustomError(
                    "mcp_stdio_command_required",
                    "command is required for stdio transport",
                )
            if self.headers:
                raise PydanticCustomError(
                    "mcp_stdio_headers_forbidden",
                    "headers are not allowed for stdio transport",
                )
        elif self.transport in ("sse", "streamable_http"):
            if not self.url:
                raise PydanticCustomError(
                    "mcp_url_required",
                    f"url is required for {self.transport} transport",
                )
            if self.env:
                raise PydanticCustomError(
                    "mcp_http_env_forbidden",
                    f"env is not allowed for {self.transport} transport; use headers",
                )
        else:
            raise PydanticCustomError(
                "mcp_transport_invalid",
                f"unsupported MCP transport: {self.transport}",
            )
        return self


class MCPServerUpdateRequest(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    url: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    timeout_seconds: int | None = None
    description: str | None = None
    status: str | None = None
    shared: bool | None = None


class SendMessageRequest(BaseModel):
    """Request body for POST /conversations/{id}/messages."""

    content: str = Field(default="", max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_not_empty(self) -> SendMessageRequest:
        if not self.content.strip() and not self.attachments:
            raise ValueError("content or attachments are required")
        return self


class SendMessageResponse(BaseModel):
    """Response for fire-and-forget message submission (202 Accepted)."""

    status: Literal["accepted", "queued"] = "accepted"


class EscalationResolveRequest(BaseModel):
    decision: str
    note: str | None = None


class EscalationResponse(BaseModel):
    call_id: str
    session_id: str | None = None
    tool_name: str | None = None
    decision: str
    resolved: bool
    reasoning: str | None = None
    risk: str | None = None


class WebSocketAuthenticated(BaseModel):
    type: str = "authenticated"


class WebSocketError(BaseModel):
    type: str = "error"
    code: str
    message: str
    recoverable: bool = False
    error_detail: str | None = None
    detail: dict[str, Any] | None = None


class WebSocketChunkGap(BaseModel):
    type: str = "chunk_gap"
    conversation_id: str
    session_id: str | None = None
    message_id: str
    dropped_count: int
    recoverable: bool = True


class WebSocketPong(BaseModel):
    type: str = "pong"
