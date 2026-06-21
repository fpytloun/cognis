"""FastAPI request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from cognis.core.question_sets import normalize_questions, normalize_reply
from cognis.models.artifact import AttachmentRef
from cognis.models.conversation_state import ConversationStateEnvelope
from cognis.models.task import TaskDelivery
from cognis.models.workflow import SessionPolicy, WorkflowState

InteractionModeOverride = Literal["none", "explicit_gates", "step_requests"]


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
    ui_url: str


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
    password: str | None = Field(default=None, min_length=8)


class ProfileUpdateRequest(BaseModel):
    name: str | None = None


class ProviderTestResultResponse(BaseModel):
    ok: bool
    model_resolved: str | None = None
    model_sent: str | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    error_detail: str | None = None
    tested_at: datetime | None = None
    executor_routed: bool | None = None
    executor_id: str | None = None
    executor_backend: str | None = None


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
    browser_fetch_session_idle_seconds: int = 60
    browser_fetch_wait_timeout_seconds: int = 30
    browser_fetch_navigation_timeout_seconds: int = 60
    browser_fetch_wait_until: str = "domcontentloaded"
    browser_fetch_network_idle_after_dom_seconds: int = 3
    browser_fetch_headed_fallback_enabled: bool = False
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
    agent_profile_id: str | None = None
    context_type: str = "web"
    scope: Literal["latest", "agent_direct"] = "latest"


class ConversationOpenRequest(BaseModel):
    """Resolve the best conversation to open for the chat landing page."""

    agent_id: str
    agent_profile_id: str | None = None
    context_type: str = "web"
    candidate_conversation_ids: list[str] = Field(default_factory=list)


class ConversationCreateRequest(BaseModel):
    agent_id: str
    agent_profile_id: str | None = None
    title: str | None = None
    project_id: str | None = None
    context: ConversationContextModel = Field(default_factory=ConversationContextModel)


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    project_id: str | None = None
    archived: bool | None = None
    starred_at: datetime | None = None


class ConversationResponse(BaseModel):
    conversation_id: str
    user_email: str
    agent_id: str
    agent_profile_id: str | None = None
    project_id: str | None = None
    title: str | None = None
    title_source: str = "unset"
    context: ConversationContextModel
    active_session_id: str | None = None
    active_executor_id: str | None = None
    active_executor_assigned_at: datetime | None = None
    active_executor_expires_at: datetime | None = None
    active_executor_source: str | None = None
    active_session_status: str | None = None
    active_session_completion_reason: str | None = None
    active_turn_chat_mode: str | None = None
    active_turn_chat_mode_source: str | None = None
    pending_notification_types: list[str] = Field(default_factory=list)
    starred_at: datetime | None = None
    status: str
    last_message_at: datetime | None = None
    last_read_at: datetime | None = None
    has_unread: bool = False
    has_active_turn: bool = False
    managed_agent: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    conversation_state: ConversationStateEnvelope | None = None


class ManagedConversationActionRequest(BaseModel):
    message: str | None = None
    reason: str | None = None
    instruction: str | None = None
    wait: bool = False


class ManagedConversationActionResponse(BaseModel):
    status: str
    conversation_id: str
    managed_agent: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class ConversationTitleSuggestionResponse(BaseModel):
    title: str | None = None
    source: str = "intaris"
    generated_at: str | None = None
    available: bool = False
    reason: str | None = None


class MessageEventResponse(BaseModel):
    seq: int | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None


class ActiveStreamSnapshotResponse(BaseModel):
    conversation_id: str
    session_id: str
    message_id: str
    turn_id: str | None = None
    content: str
    chunk_count: int = 0
    content_offset: int = 0
    updated_at: str | None = None


class ActiveToolOutputSnapshotResponse(BaseModel):
    conversation_id: str
    session_id: str
    call_id: str
    tool_name: str
    turn_id: str | None = None
    status: str = "running"
    result: str = ""
    stream: str | None = None
    is_error: bool = False
    chunk_count: int = 0
    content_offset: int = 0
    output_size: int = 0
    truncated: bool = False
    agent_visible_truncated: bool = False
    transport_truncated: bool = False
    has_full_output: bool = False
    recovery_call_id: str | None = None
    tool_output_artifact_id: str | None = None
    anchors_available: bool = False
    anchor_count: int = 0
    updated_at: str | None = None


class ToolOutputChunkResponse(BaseModel):
    index: int
    offset: int
    stream: str | None = None
    text: str


class ToolOutputPageResponse(BaseModel):
    conversation_id: str
    session_id: str | None = None
    call_id: str
    status: str
    source: str
    content: str
    chunks: list[ToolOutputChunkResponse] = Field(default_factory=list)
    offset: int = 0
    limit: int = 200
    next_offset: int | None = None
    prev_offset: int | None = None
    has_more_before: bool = False
    has_more_after: bool = False
    output_size: int = 0
    total_lines: int | None = None
    recoverable: bool = False
    truncated: bool = False
    spool_truncated: bool = False


class ActiveThinkingBlockResponse(BaseModel):
    block_id: str
    title: str
    content: str
    source: str = "summary"
    complete: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    provider_block_index: int | None = None


class ActiveThinkingSnapshotResponse(BaseModel):
    session_id: str
    message_id: str
    turn_id: str | None = None
    blocks: list[ActiveThinkingBlockResponse] = Field(default_factory=list)
    updated_at: str | None = None


class MessageHistoryResponse(BaseModel):
    items: list[MessageEventResponse]
    last_seq: int = 0
    has_more: bool = False
    older_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for loading older conversation history before this page.",
    )
    has_active_turn: bool = Field(
        default=False,
        description="Whether the controller currently has user-visible work running for this conversation.",
    )
    active_streams: list[ActiveStreamSnapshotResponse] = Field(default_factory=list)
    active_tool_outputs: list[ActiveToolOutputSnapshotResponse] = Field(default_factory=list)
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
    state_snapshot: ConversationStateEnvelope | None = Field(
        default=None,
        description="Authoritative backend-projected conversation state at history load time.",
    )


class TimelineProjectionResponse(MessageHistoryResponse):
    timeline_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Backend-projected canonical timeline items for initial/refresh rendering.",
    )


class ProjectSourceCreateRequest(BaseModel):
    name: str
    local_path: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None
    credential_ref: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectSourceUpdateRequest(BaseModel):
    name: str | None = None
    local_path: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None
    credential_ref: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] | None = None


class ProjectSourceResponse(BaseModel):
    source_id: str
    project_id: str
    name: str
    local_path: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None
    credential_ref: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectGrantCreateRequest(BaseModel):
    grantee_type: Literal["user", "group"] = "user"
    grantee_user_email: str | None = None
    grantee_group_id: str | None = None
    permission: Literal["use"] = "use"
    note: str | None = None


class ProjectGrantResponse(BaseModel):
    grant_id: str
    project_id: str
    grantee_type: str = "user"
    grantee_user_email: str | None = None
    grantee_group_id: str | None = None
    permission: str = "use"
    granted_by: str
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    note: str | None = None


class ProjectCreateRequest(BaseModel):
    name: str
    description: str | None = None
    instructions: str | None = None
    default_workflow_id: str | None = None
    avatar_image_id: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    default_workflow_id: str | None = None
    avatar_image_id: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] | None = None
    status: str | None = None


class ProjectResponse(BaseModel):
    project_id: str
    owner_email: str
    name: str
    description: str | None = None
    instructions: str | None = None
    default_workflow_id: str | None = None
    avatar_image_id: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    sources: list[ProjectSourceResponse] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    active_schedule_count: int = 0
    grants: list[ProjectGrantResponse] = Field(default_factory=list)
    is_shared_with_me: bool = False
    shared_by_email: str | None = None
    granted_permission: str | None = None
    is_readonly_for_caller: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectAvatarGenerateResponse(BaseModel):
    avatar_image_id: str
    avatar_url: str


class SessionResponse(BaseModel):
    session_id: str
    conversation_id: str
    parent_session_id: str | None = None
    previous_session_id: str | None = None
    user_email: str
    agent_id: str
    agent_profile_id: str | None = None
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
    result_content: str | None = None
    result_anchors: list[dict[str, Any]] | None = None
    result_sections: list[dict[str, Any]] | None = None
    updated_at: datetime | None = None


class SessionEventsResponse(BaseModel):
    session_id: str
    items: list[MessageEventResponse]
    last_seq: int = 0
    has_more: bool = False
    active_thinking: list[ActiveThinkingSnapshotResponse] = Field(default_factory=list)


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
    agent_profiles: dict[str, Any] | None = None
    default_agent_profile_id: str | None = None
    execution: dict[str, Any] | None = None
    avatar_image_id: str | None = None
    status: str | None = None

    @field_validator("execution")
    @classmethod
    def _validate_execution_default_chat_mode(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return value
        raw_mode = value.get("default_chat_mode")
        if raw_mode is not None and raw_mode not in {"default", "plan", "build"}:
            raise ValueError("execution.default_chat_mode must be one of: default, plan, build")
        return value


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
    agent_profiles: dict[str, Any] | None = None
    default_agent_profile_id: str | None = None
    execution: dict[str, Any] | None = None
    avatar_image_id: str | None = None
    status: str | None = None

    @field_validator("execution")
    @classmethod
    def _validate_execution_default_chat_mode(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return value
        raw_mode = value.get("default_chat_mode")
        if raw_mode is not None and raw_mode not in {"default", "plan", "build"}:
            raise ValueError("execution.default_chat_mode must be one of: default, plan, build")
        return value


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
    agent_profiles: dict[str, Any] | None = None
    default_agent_profile_id: str | None = None
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


class AgentDirectChatResponse(BaseModel):
    agent: AgentResponse
    conversation: ConversationResponse


class SidebarProjectionResponse(BaseModel):
    agents: list[AgentResponse] = Field(default_factory=list)
    agent_direct_chats: list[AgentDirectChatResponse] = Field(default_factory=list)
    conversations: CursorPage[ConversationResponse]
    context_types: list[str] = Field(default_factory=list)


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
    grantee_overrides: dict[str, Any] | None = None


class AgentGrantOverrideUpdateRequest(BaseModel):
    execution: dict[str, Any] | None = None


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
    owner_scope: str | None = None
    config: dict[str, Any] | None = None
    status: str | None = None


class LLMProviderResponse(BaseModel):
    provider_id: str
    display_name: str
    location: str
    backend: str
    owner_email: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    last_test: ProviderTestResultResponse | None = None


class LLMProviderOAuthStatusResponse(BaseModel):
    provider_id: str
    status: str
    verification_url: str | None = None
    user_code: str | None = None
    interval: int | None = None
    expires_at: float | None = None


class CodexUsageWindowResponse(BaseModel):
    used_percent: float
    window_duration_mins: int | None = None
    resets_at: str | None = None
    reset_after_seconds: int | None = None


class CodexUsageCreditsResponse(BaseModel):
    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | int | float | None = None


class CodexUsageAdditionalLimitResponse(BaseModel):
    limit_id: str | None = None
    limit_name: str | None = None
    primary: CodexUsageWindowResponse | None = None
    secondary: CodexUsageWindowResponse | None = None
    allowed: bool | None = None
    limit_reached: bool | None = None


class CodexUsageResponse(BaseModel):
    provider_id: str
    ok: bool = True
    source: str = "chatgpt_codex_usage"
    usage_url: str | None = None
    fetched_at: str | None = None
    plan_type: str | None = None
    primary: CodexUsageWindowResponse | None = None
    secondary: CodexUsageWindowResponse | None = None
    credits: CodexUsageCreditsResponse | None = None
    rate_limit_reached_type: str | None = None
    allowed: bool | None = None
    limit_reached: bool | None = None
    additional_rate_limits: list[CodexUsageAdditionalLimitResponse] = Field(default_factory=list)


class ModelRoutingEntry(BaseModel):
    model: str | None = None
    reasoning_effort: str | None = None


class ModelRoutingResponse(BaseModel):
    default: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    classifier: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    compaction: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    evaluator: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    speech_to_text: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    text_to_speech: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    image_generation: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    attachment_analysis: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    embedding: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)


class ModelRoutingUpdateRequest(BaseModel):
    default: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    classifier: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    compaction: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    evaluator: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    speech_to_text: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    text_to_speech: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    image_generation: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    attachment_analysis: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)
    embedding: ModelRoutingEntry = Field(default_factory=ModelRoutingEntry)


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


class QuestionSetOption(BaseModel):
    id: str
    label: str
    description: str | None = None


class QuestionSetQuestion(BaseModel):
    id: str
    question: str
    header: str | None = None
    options: list[QuestionSetOption] = Field(default_factory=list)
    multiple: bool = False
    allow_custom: bool = True
    required: bool = True


class QuestionSetAnswer(BaseModel):
    question_id: str
    selected_option_ids: list[str] = Field(default_factory=list)
    custom_answer: str | None = None


class QuestionSetReply(BaseModel):
    answers: list[QuestionSetAnswer] = Field(default_factory=list)
    mode: Literal["structured", "plain_text"] = "structured"

    @model_validator(mode="before")
    @classmethod
    def _normalize_reply(cls, value: Any) -> Any:
        return normalize_reply(value)


class PendingPauseResponse(BaseModel):
    model_config = {"extra": "forbid"}

    pause_id: str
    pause_type: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    question: str | None = None
    questions: list[QuestionSetQuestion] | None = None
    options: list[dict[str, Any]] | None = None
    context: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_questions(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("questions") is not None:
            normalized = dict(value)
            normalized["questions"] = normalize_questions(normalized.get("questions"))
            return normalized
        return value

    @model_validator(mode="after")
    def _reject_step_question_legacy_fields(self) -> PendingPauseResponse:
        if self.pause_type in {"step_input", "step_question"} and self.question is not None:
            raise ValueError("step question pauses must use questions")
        return self


class TaskCreateRequest(BaseModel):
    agent_id: str
    agent_profile_id: str | None = None
    title: str
    description: str = ""
    expected_output: str | None = None
    priority: int = 0
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    delivery_mode: str = "preferred_channel"
    delivery_target: str | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None
    interaction_mode_override: InteractionModeOverride | None = None
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
    source_type: str = "api"
    source_ref: str | None = None
    status: str = "draft"
    draft: bool | None = None
    start_immediately: bool | None = None
    workspace_root: str | None = None
    working_directory: str | None = None
    created_by_agent_id: str | None = None

    @field_validator("agent_profile_id", "workflow_id", "project_id", "skill_id", mode="before")
    @classmethod
    def _empty_optional_ids_are_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("status")
    @classmethod
    def _validate_create_status(cls, value: str) -> str:
        status = str(value or "draft").strip().lower()
        if status not in {"draft", "queued", "ready"}:
            raise ValueError("Task create status must be one of: draft, queued, ready")
        return status


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    expected_output: str | None = None
    priority: int | None = None
    agent_id: str | None = None
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    delivery_mode: str | None = None
    delivery_target: str | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None
    interaction_mode_override: InteractionModeOverride | None = None
    session_policy: SessionPolicy | None = None
    workspace_root: str | None = None
    working_directory: str | None = None

    @field_validator(
        "agent_id", "agent_profile_id", "workflow_id", "project_id", "skill_id", mode="before"
    )
    @classmethod
    def _empty_optional_ids_are_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class TaskActionResponse(BaseModel):
    ok: bool
    task_id: str
    status: str


class TaskChatResponse(BaseModel):
    conversation_id: str
    session_id: str


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


class TaskCommentCreateRequest(BaseModel):
    body: str
    intent: str = "record_only"
    noop: bool = True
    target_step: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCommentUpdateRequest(BaseModel):
    body: str | None = None
    intent: str | None = None
    noop: bool | None = None
    target_step: str | None = None
    applied: bool | None = None
    metadata: dict[str, Any] | None = None


class TaskCommentResponse(BaseModel):
    comment_id: str
    task_id: str
    author_email: str
    body: str
    intent: str = "record_only"
    noop: bool = True
    target_step: str | None = None
    confidence: float | None = None
    applied: bool = False
    attempt_number: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskResponse(BaseModel):
    task_id: str
    title: str
    description: str = ""
    expected_output: str | None = None
    status: str
    priority: int = 0
    created_by: str
    agent_id: str
    agent_profile_id: str | None = None
    created_by_agent_id: str | None = None
    source_type: str
    source_ref: str | None = None
    delivery: TaskDelivery = Field(default_factory=TaskDelivery)
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False
    interaction_mode_override: InteractionModeOverride | None = None
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
    workflow_id: str | None = None
    project_id: str | None = None
    attempt_number: int = 1
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
    project_id: str | None = None
    attempt_number: int = 1
    workflow_state: WorkflowState | None = None
    current_step_name: str | None = None
    pending_pause: PendingPauseResponse | None = None


class DeliverableResponse(BaseModel):
    deliverable_id: str
    step_run_id: str
    version: int
    attempt_number: int = 1
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
    attempt_number: int = 1
    superseded_by_step_run_id: str | None = None
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
    duration_seconds: float | None = None
    accumulated_duration_seconds: float | None = None
    latest_attempt_duration_seconds: float | None = None


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
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    max_concurrent_runs: int = 1
    delete_after_run: bool = False
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False
    interaction_mode_override: InteractionModeOverride | None = "none"
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)

    @field_validator("agent_profile_id", "workflow_id", "project_id", "skill_id", mode="before")
    @classmethod
    def _empty_optional_ids_are_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_schedule_type(self) -> CreateScheduleRequest:
        self.task_template.pop("created_by_agent_id", None)
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
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] | None = None
    enabled: bool | None = None
    max_concurrent_runs: int | None = None
    delete_after_run: bool | None = None
    completion_mode_family: str | None = None
    allow_silent_completion: bool | None = None
    interaction_mode_override: InteractionModeOverride | None = None
    session_policy: SessionPolicy | None = None

    @field_validator("agent_profile_id", "workflow_id", "project_id", "skill_id", mode="before")
    @classmethod
    def _empty_optional_ids_are_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _strip_reserved_task_template_fields(self) -> UpdateScheduleRequest:
        if self.task_template is not None:
            self.task_template.pop("created_by_agent_id", None)
        return self


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
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    max_concurrent_runs: int = 1
    delete_after_run: bool = False
    completion_mode_family: str = "default"
    allow_silent_completion: bool = False
    interaction_mode_override: InteractionModeOverride | None = "none"
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
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
    is_expired: bool = False
    expiration_grace_until: datetime | None = None


class ScheduleTriggerResponse(ScheduleResponse):
    task_id: str | None = None


class GateResponseRequest(BaseModel):
    step_name: str | None = None
    action: str
    feedback: str | None = None


class StepResponseRequest(BaseModel):
    step_name: str | None = None
    answers: list[QuestionSetAnswer]
    mode: Literal["structured", "plain_text"] = "structured"

    @model_validator(mode="before")
    @classmethod
    def _normalize_reply(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        try:
            normalized = normalize_reply(value)
        except ValueError as exc:
            raise PydanticCustomError("invalid_question_reply", str(exc)) from exc
        if value.get("step_name") is not None:
            normalized["step_name"] = value.get("step_name")
        return normalized


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
    aliases: list[dict[str, Any]] = Field(default_factory=list)
    canonical_name: str | None = None
    primary_name: str | None = None
    configurable: bool = True
    surfaces: dict[str, str] = Field(default_factory=dict)


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
    aliases: list[dict[str, Any]] = Field(default_factory=list)
    canonical_name: str | None = None
    primary_name: str | None = None
    configurable: bool = True
    surfaces: dict[str, str] = Field(default_factory=dict)
    # Stage 36: list of assigned executor ids that observe this tool. Empty
    # for non-executor tools. Helps UIs render a per-executor matrix.
    available_on: list[str] = Field(default_factory=list)


class EffectiveToolsStateResponse(BaseModel):
    tools: list[EffectiveToolItemResponse] = Field(default_factory=list)
    connected: bool = False
    observed_at: datetime | None = None
    stale_after: datetime | None = None


class EffectiveToolsExecutorResponse(BaseModel):
    executor_id: str | None = None
    executor_type: str | None = None
    selection_source: str = "unresolved"
    # Stage 36 multi-executor agents — these fields are populated when the
    # executor is one of several assigned to the agent. ``is_active`` marks
    # the conversation's currently-active executor; ``state`` carries the
    # ExecutorAvailability value (usable, degraded, offline, blocked,
    # reconfiguring, policy_denied, not_found, unauthorized).
    is_primary: bool | None = None
    is_active: bool | None = None
    state: str | None = None
    description: str | None = None


class EffectiveToolsResponse(BaseModel):
    # Back-compat: ``executor`` is the active (or primary) executor that
    # the existing UI displays. Stage 36 adds ``executors`` for the full
    # assigned pool.
    executor: EffectiveToolsExecutorResponse
    executors: list[EffectiveToolsExecutorResponse] = Field(default_factory=list)
    configured_state: EffectiveToolsStateResponse
    live_state: EffectiveToolsStateResponse
    warnings: list[str] = Field(default_factory=list)


class EffectiveToolsPreviewRequest(BaseModel):
    tools: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    skills: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    agent_type: Literal["primary", "secondary"] = "primary"


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
    agent_id: str | None = None


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
    agent_id: str | None = None


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
    expires_in: int | None = None


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
    auth_config: dict[str, Any] = Field(default_factory=dict)
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
    auth_config: dict[str, Any] | None = None
    timeout_seconds: int = 30
    description: str | None = None
    shared: bool = False

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerCreateRequest:
        from cognis.models.tool import MCPServerConfig

        MCPServerConfig(
            name=self.name,
            transport=self.transport,
            command=self.command,
            url=self.url,
            args=self.args,
            env=self.env,
            headers=self.headers,
            auth_config=self.auth_config,
            timeout_seconds=self.timeout_seconds,
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
    auth_config: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    description: str | None = None
    status: str | None = None
    shared: bool | None = None


class SendMessageRequest(BaseModel):
    """Request body for POST /conversations/{id}/messages."""

    content: str = Field(default="", max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    client_message_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_not_empty(self) -> SendMessageRequest:
        if not self.content.strip() and not self.attachments:
            raise ValueError("content or attachments are required")
        return self


class SendMessageResponse(BaseModel):
    """Response for fire-and-forget message submission (202 Accepted)."""

    status: Literal["accepted", "queued"] = "accepted"


class QueuedMessageResponse(BaseModel):
    queue_id: str
    client_message_id: str | None = None
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    position: int


class QueuedMessagesResponse(BaseModel):
    messages: list[QueuedMessageResponse]
    queued_count: int


class UpdateQueuedMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def _validate_not_blank(self) -> UpdateQueuedMessageRequest:
        if not self.content.strip():
            raise ValueError("content is required")
        return self


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


# ---------------------------------------------------------------------------
# Voice mode (TTS / STT)
# ---------------------------------------------------------------------------


class TtsSynthesizeRequest(BaseModel):
    """Request body for ``POST /api/v1/tts/synthesize``.

    ``message_id`` enables artifact caching keyed on
    ``(message_id, voice, model)``. ``agent_id`` lets the server resolve
    the per-agent voice override. ``voice`` overrides everything.
    """

    text: str
    message_id: str | None = None
    agent_id: str | None = None
    voice: str | None = None
    format: str = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    low_latency: bool = False


class TtsSynthesizeResponse(BaseModel):
    audio_url: str
    content_type: str
    duration_seconds: float | None = None
    voice: str
    model: str
    cached: bool = False


class SttTranscribeResponse(BaseModel):
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    model: str


# ---------------------------------------------------------------------------
# Voice mode WebSocket frames
# ---------------------------------------------------------------------------


class WebSocketEnableTts(BaseModel):
    type: str = "enable_tts"
    voice: str | None = None


class WebSocketDisableTts(BaseModel):
    type: str = "disable_tts"


class WebSocketTtsSentenceReady(BaseModel):
    type: str = "tts_sentence_ready"
    conversation_id: str
    message_id: str
    sentence_index: int
    text: str
