"""FastAPI request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

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
    refresh_token: str


class LogoutRequest(BaseModel):
    token: str | None = None
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    token: str
    refresh_token: str | None = None
    expires_in: int
    user: dict[str, Any]


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

    backend: str = "direct"
    tavily_configured: bool = False
    brave_configured: bool = False
    available_backends: list[str] = Field(default_factory=lambda: ["direct"])


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
    root_session_id: str | None = None
    status: str
    last_message_at: datetime | None = None
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
    avatar_url: str | None = None
    status: str | None = None


class AgentCreateRequest(AgentRequestBase):
    agent_id: str | None = None  # optional: auto-generated from name
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
    avatar_url: str | None = None
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
    avatar_url: str | None = None
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


class ModelRoutingResponse(BaseModel):
    default: str | None = None
    classifier: str | None = None
    compaction: str | None = None
    simple_inline: str | None = None
    items: dict[str, str] = Field(default_factory=dict)


class ModelRoutingUpdateRequest(BaseModel):
    default: str | None = None
    classifier: str | None = None
    compaction: str | None = None
    simple_inline: str | None = None
    items: dict[str, str] = Field(default_factory=dict)


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
    delivery_mode: str = "same_conversation"
    delivery_target: str | None = None
    source_type: str = "api"
    source_ref: str | None = None
    status: str = "draft"


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    expected_output: str | None = None
    priority: int | None = None
    agent_id: str | None = None
    workflow_id: str | None = None
    delivery_mode: str | None = None
    delivery_target: str | None = None


class TaskActionResponse(BaseModel):
    ok: bool
    task_id: str
    status: str


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
    workflow_id: str | None = None
    workflow_state: WorkflowState | None = None
    queue_name: str = "default"
    scheduled_for: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    result_data: dict[str, Any] | None = None


class WorkflowRunResponse(BaseModel):
    task_id: str
    workflow_id: str | None = None
    workflow_state: WorkflowState | None = None
    current_step_name: str | None = None
    pending_pause: PendingPauseResponse | None = None


class StepRunResponse(BaseModel):
    step_run_id: str
    task_id: str
    step_name: str
    step_type: str
    status: str
    attempt: int = 1
    agent_id: str
    session_id: str | None = None
    intaris_session_id: str | None = None
    output: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    todos: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class TaskDetailResponse(TaskResponse):
    dependencies: list[DependencyResponse] = Field(default_factory=list)
    step_runs: list[StepRunResponse] = Field(default_factory=list)
    workflow_run: WorkflowRunResponse | None = None
    pending_pause: PendingPauseResponse | None = None


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


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    version: int | None = None
    criteria: str | None = None
    tags: list[str] | None = None
    interaction: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None


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


class ToolResponse(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    category: str = "general"
    read_only: bool = False
    source: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    non_bypassable: bool = False


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


class SkillResponse(BaseModel):
    skill_id: str
    name: str
    description: str | None = None
    instructions: str
    tools: list[dict[str, Any]] | None = None
    prompt_templates: dict[str, Any] | None = None
    tags: list[str] | None = None
    auto_load: bool = False
    source: str = "db"
    owner_email: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillCreateRequest(BaseModel):
    name: str
    description: str | None = None
    instructions: str
    tools: list[dict[str, Any]] | None = None
    prompt_templates: dict[str, Any] | None = None
    tags: list[str] | None = None
    auto_load: bool = False


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    prompt_templates: dict[str, Any] | None = None
    tags: list[str] | None = None
    auto_load: bool | None = None


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
    is_default: bool = False
    owner_email: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExecutorCreateRequest(BaseModel):
    name: str
    executor_type: str = "in_process"
    labels: dict[str, Any] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    enabled_tool_groups: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class ExecutorUpdateRequest(BaseModel):
    name: str | None = None
    labels: dict[str, Any] | None = None
    enabled_tools: list[str] | None = None
    enabled_tool_groups: list[str] | None = None
    config: dict[str, Any] | None = None
    status: str | None = None
    is_default: bool | None = None


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
