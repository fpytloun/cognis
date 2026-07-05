"""Strict public schemas for the Chat v2 sync contract."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cognis.models.artifact import AttachmentRef

SchemaVersion = Literal[2]
ChatMode = Literal["default", "plan", "build"]
TimelineItemStatus = Literal[
    "pending",
    "running",
    "waiting",
    "complete",
    "failed",
    "cancelled",
    "compacted",
    "skipped",
]
ChatResetReason = Literal[
    "cursor_invalid",
    "cursor_expired",
    "projection_version_changed",
    "lineage_changed",
    "history_compacted",
    "range_too_large",
    "server_restart_lost_runtime",
    "unsupported_cursor",
]


class StrictModel(BaseModel):
    """Base model for v2 public contracts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


def _ensure_canonical_timeline_items(items: list[TimelineItem]) -> None:
    """Ensure canonical snapshot/sync containers cannot carry volatile items."""

    for item in items:
        if not item.stable:
            raise ValueError("canonical timeline items must have stable=true")


class SourceRef(StrictModel):
    """Reference to a canonical source event used to project a timeline item."""

    store: str
    session_id: str
    seq: int = Field(ge=0)
    event_id: str | None = None
    event_type: str


class FileDiffRef(StrictModel):
    """Minimal file-diff reference rendered by tool and file-diff cards."""

    path: str
    diff: str


class ThinkingBlock(StrictModel):
    """One rendered thinking block."""

    id: str
    title: str | None = None
    content: str
    status: Literal["running", "complete", "failed"] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class QuestionSpec(StrictModel):
    """Structured question rendered in a question-set card."""

    id: str
    question: str
    header: str | None = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    multiple: bool = False
    allow_custom: bool = False
    required: bool = True


class TimelineItemBase(StrictModel):
    """Common fields for all timeline items."""

    id: str
    kind: str
    sort_key: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    status: TimelineItemStatus | None = None
    stable: bool = True


class MessageTimelineItem(TimelineItemBase):
    kind: Literal["message"] = "message"
    role: Literal["user", "assistant", "system"]
    content: str
    message_id: str
    client_message_id: str | None = None
    client_txn_id: str | None = None
    turn_id: str | None = None
    assistant_phase_index: int | None = Field(default=None, ge=0)
    turn_cycle_index: int | None = Field(default=None, ge=0)
    notice_id: str | None = None
    notice_kind: str | None = None
    notice_scope: str | None = None
    follow_up_conversation_id: str | None = None
    follow_up_session_id: str | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list)
    partial: bool = False
    chat_mode: ChatMode | None = None
    chat_mode_source: str | None = None


class ThinkingTimelineItem(TimelineItemBase):
    kind: Literal["thinking"] = "thinking"
    message_id: str | None = None
    turn_id: str | None = None
    assistant_phase_index: int | None = Field(default=None, ge=0)
    turn_cycle_index: int | None = Field(default=None, ge=0)
    blocks: list[ThinkingBlock] = Field(default_factory=list)
    active_title: str | None = None


class ToolCallTimelineItem(TimelineItemBase):
    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    tool_name: str
    display_name: str | None = None
    turn_id: str | None = None
    assistant_phase_index: int | None = Field(default=None, ge=0)
    turn_cycle_index: int | None = Field(default=None, ge=0)
    # Structured, named tool arguments (path/command/pattern/query/title/...).
    # The client renders concise per-tool subtitles and rich bodies from these;
    # ``arguments_preview`` remains the raw-payload fallback string.
    arguments: dict[str, Any] | None = None
    arguments_preview: str | None = None
    result_preview: str | None = None
    streamed_output: str | None = None
    is_error: bool = False
    duration_ms: int | None = Field(default=None, ge=0)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    file_diffs: list[FileDiffRef] = Field(default_factory=list)
    output_size: int | None = Field(default=None, ge=0)
    truncated: bool = False
    has_full_output: bool = False
    recovery_call_id: str | None = None
    tool_output_artifact_id: str | None = None
    evaluation: dict[str, Any] | None = None
    # Live tool progress (apply_patch streaming, etc). Runtime-overlay only.
    progress_phase: str | None = None
    progress_input_chars: int | None = Field(default=None, ge=0)
    progress_input_lines: int | None = Field(default=None, ge=0)
    progress_complete: bool | None = None
    # Delegation details folded onto the delegate tool call so it renders as a
    # single rich, auto-expanding tool call (title/progress/todos) rather than a
    # separate delegation card. Populated only for delegate/fork tool calls.
    delegation: dict[str, Any] | None = None


class DelegationTimelineItem(TimelineItemBase):
    kind: Literal["delegation"] = "delegation"
    child_session_id: str
    turn_id: str | None = None
    assistant_phase_index: int | None = Field(default=None, ge=0)
    turn_cycle_index: int | None = Field(default=None, ge=0)
    agent_id: str | None = None
    used_agent_id: str | None = None
    title: str | None = None
    summary: str | None = None
    status: TimelineItemStatus
    result_summary: str | None = None
    result_anchors: dict[str, str] | None = None
    todos: list[dict[str, Any]] | None = None
    tool_call_count: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    last_tool: str | None = None


class ManagedConversationTimelineItem(TimelineItemBase):
    kind: Literal["managed_conversation"] = "managed_conversation"
    managed_conversation_id: str
    agent_id: str
    title: str | None = None
    status: TimelineItemStatus
    result_summary: str | None = None


class TaskTimelineItem(TimelineItemBase):
    kind: Literal["task"] = "task"
    task_id: str
    title: str
    workflow_id: str | None = None
    workflow_step: str | None = None
    status: TimelineItemStatus
    result_summary: str | None = None
    deliverable_ids: list[str] = Field(default_factory=list)


class QuestionSetTimelineItem(TimelineItemBase):
    kind: Literal["question_set"] = "question_set"
    request_id: str
    title: str | None = None
    questions: list[QuestionSpec] = Field(default_factory=list)
    status: Literal["waiting", "complete", "cancelled"]


class AuthChallengeTimelineItem(TimelineItemBase):
    kind: Literal["auth_challenge"] = "auth_challenge"
    challenge_id: str
    challenge_kind: str
    label: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    status: Literal["waiting", "complete", "cancelled", "failed"]


class CredentialRequestTimelineItem(TimelineItemBase):
    kind: Literal["credential_request"] = "credential_request"
    credential_request_id: str
    credential_id: str
    credential_kind: str
    label: str
    description: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    status: Literal["waiting", "complete", "cancelled", "failed"]


class TodoStateTimelineItem(TimelineItemBase):
    kind: Literal["todo_state"] = "todo_state"
    todos: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactTimelineItem(TimelineItemBase):
    kind: Literal["artifact"] = "artifact"
    artifact_id: str
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    title: str | None = None


class FileDiffTimelineItem(TimelineItemBase):
    kind: Literal["file_diff"] = "file_diff"
    file_diffs: list[FileDiffRef] = Field(default_factory=list)
    title: str | None = None


class NoticeTimelineItem(TimelineItemBase):
    kind: Literal["notice"] = "notice"
    level: Literal["info", "warning"]
    title: str
    message: str | None = None


class CompactionTimelineItem(TimelineItemBase):
    kind: Literal["compaction"] = "compaction"
    status: Literal["running", "compacted", "failed", "skipped"] = "compacted"
    session_id: str | None = None
    previous_session_id: str | None = None
    summary_preview: str
    summary: str | None = None
    method: str
    turns_compacted: int = Field(default=0, ge=0)
    trigger: str | None = None
    reason: str | None = None
    previous_usage_percentage: float | None = None
    effective_usage_percentage: float | None = None
    hard_pressure_exceeded: bool = False
    used_timeout_fallback: bool = False


class ErrorTimelineItem(TimelineItemBase):
    kind: Literal["error"] = "error"
    level: Literal["error"] = "error"
    title: str
    message: str | None = None
    error_code: str | None = None
    recoverable: bool = False


TimelineItem = Annotated[
    MessageTimelineItem
    | ThinkingTimelineItem
    | ToolCallTimelineItem
    | DelegationTimelineItem
    | ManagedConversationTimelineItem
    | TaskTimelineItem
    | QuestionSetTimelineItem
    | AuthChallengeTimelineItem
    | CredentialRequestTimelineItem
    | TodoStateTimelineItem
    | ArtifactTimelineItem
    | FileDiffTimelineItem
    | NoticeTimelineItem
    | CompactionTimelineItem
    | ErrorTimelineItem,
    Field(discriminator="kind"),
]


class TurnCycleState(StrictModel):
    turn_id: str
    turn_cycle_index: int
    lifecycle_status: Literal["open", "complete"] = "complete"
    has_tool_activity: bool = False


class TimelineWindow(StrictModel):
    items: list[TimelineItem] = Field(default_factory=list)
    cycle_states: list[TurnCycleState] = Field(default_factory=list)
    has_more_before: bool = False
    before_cursor: str | None = None

    @model_validator(mode="after")
    def _validate_canonical_items(self) -> TimelineWindow:
        _ensure_canonical_timeline_items(self.items)
        return self


class RuntimeActiveTurn(StrictModel):
    turn_id: str
    session_id: str
    status: Literal["starting", "running", "waiting", "cancelling"]
    chat_mode: ChatMode | None = None
    chat_mode_source: str | None = None
    started_at: str | None = None
    updated_at: str | None = None


class RuntimeOverlaySnapshot(StrictModel):
    runtime_epoch: str
    runtime_revision: int = Field(ge=0)
    generated_at: str
    has_active_turn: bool
    active_turn: RuntimeActiveTurn | None = None
    volatile_items: list[TimelineItem] = Field(default_factory=list)
    cycle_states: list[TurnCycleState] = Field(default_factory=list)
    context_usage: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_active_turn_consistency(self) -> RuntimeOverlaySnapshot:
        if not self.has_active_turn and self.active_turn is not None:
            raise ValueError("active_turn must be null when has_active_turn is false")
        for item in self.volatile_items:
            if item.stable:
                raise ValueError("runtime volatile_items must have stable=false")
        return self


class ConversationSummary(StrictModel):
    conversation_id: str
    title: str | None = None
    agent_id: str
    agent_profile_id: str | None = None
    project_id: str | None = None
    status: str = "active"
    active_session_id: str | None = None
    last_message_at: str | None = None
    last_read_at: str | None = None


class QueueMessage(StrictModel):
    queue_id: str
    client_message_id: str | None = None
    client_txn_id: str | None = None
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    position: int = Field(ge=0)
    created_at: str | None = None
    updated_at: str | None = None


class QueueState(StrictModel):
    messages: list[QueueMessage] = Field(default_factory=list)
    queued_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_count(self) -> QueueState:
        if self.queued_count != len(self.messages):
            raise ValueError("queued_count must match messages length")
        return self


class ConversationStateView(StrictModel):
    state_version: int = Field(ge=0)
    snapshot_generated_at: str
    capabilities: list[str] = Field(default_factory=list)
    active_turn: dict[str, Any] = Field(default_factory=dict)
    pending: dict[str, Any] = Field(default_factory=dict)
    active_session: dict[str, Any] = Field(default_factory=dict)
    task: dict[str, Any] | None = None


class ChatSnapshot(StrictModel):
    schema_version: SchemaVersion = 2
    projection_version: str
    conversation: ConversationSummary
    timeline: TimelineWindow
    state: ConversationStateView
    queue: QueueState
    runtime: RuntimeOverlaySnapshot
    cursor: str
    server_time: str


class UpsertTimelineItemOp(StrictModel):
    op: Literal["upsert_item"] = "upsert_item"
    item: TimelineItem

    @model_validator(mode="after")
    def _validate_canonical_item(self) -> UpsertTimelineItemOp:
        _ensure_canonical_timeline_items([self.item])
        return self


class RemoveTimelineItemOp(StrictModel):
    op: Literal["remove_item"] = "remove_item"
    id: str
    reason: str | None = None


class ReplaceConversationOp(StrictModel):
    op: Literal["replace_conversation"] = "replace_conversation"
    conversation: ConversationSummary


class ReplaceStateOp(StrictModel):
    op: Literal["replace_state"] = "replace_state"
    state: ConversationStateView


class ReplaceQueueOp(StrictModel):
    op: Literal["replace_queue"] = "replace_queue"
    queue: QueueState


class ResetOp(StrictModel):
    op: Literal["reset"] = "reset"
    reason: ChatResetReason


ChatViewOp = Annotated[
    UpsertTimelineItemOp
    | RemoveTimelineItemOp
    | ReplaceConversationOp
    | ReplaceStateOp
    | ReplaceQueueOp
    | ResetOp,
    Field(discriminator="op"),
]


class ChatSyncResponse(StrictModel):
    schema_version: SchemaVersion = 2
    projection_version: str
    conversation_id: str
    cursor_before: str
    cursor_after: str
    ops: list[ChatViewOp] = Field(default_factory=list)
    cycle_states: list[TurnCycleState] = Field(default_factory=list)
    runtime: RuntimeOverlaySnapshot | None = None
    reset_required: bool = False
    reset_reason: ChatResetReason | None = None
    has_more: bool = False
    server_time: str

    @model_validator(mode="after")
    def _validate_reset(self) -> ChatSyncResponse:
        if self.reset_required and self.reset_reason is None:
            raise ValueError("reset_reason is required when reset_required is true")
        return self


class ChatRealtimeFrame(StrictModel):
    type: Literal["chat_v2_frame"] = "chat_v2_frame"
    schema_version: SchemaVersion = 2
    projection_version: str
    conversation_id: str
    cursor_before: str
    cursor_after: str
    ops: list[ChatViewOp] = Field(default_factory=list)
    cycle_states: list[TurnCycleState] = Field(default_factory=list)
    runtime: RuntimeOverlaySnapshot | None = None
    server_time: str


class SendMessageV2Request(StrictModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(default="", max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    chat_mode: ChatMode | None = None

    @model_validator(mode="after")
    def _validate_content_or_attachment(self) -> SendMessageV2Request:
        if not self.content.strip() and not self.attachments:
            raise ValueError("content or attachments are required")
        return self


class SendMessageV2Response(StrictModel):
    status: Literal["accepted", "queued", "duplicate"]
    client_txn_id: str
    client_message_id: str
    conversation_id: str
    message_id: str | None = None
    queue_id: str | None = None
    cursor: str | None = None
    server_time: str


class ControlMutationV2Request(StrictModel):
    client_txn_id: str = Field(min_length=1, max_length=128)


class QueueUpdateV2Request(StrictModel):
    client_txn_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=100_000)


class QueueMutationResponse(StrictModel):
    conversation_id: str
    client_txn_id: str
    status: Literal["deleted", "updated", "duplicate"]
    queue: QueueState
    cursor: str | None = None
    runtime: RuntimeOverlaySnapshot | None = None
    server_time: str


class CancelTurnV2Response(StrictModel):
    conversation_id: str
    client_txn_id: str
    status: Literal["cancelled", "idle", "duplicate"]
    runtime: RuntimeOverlaySnapshot | None = None
    server_time: str


class TimelineBackfillResponse(StrictModel):
    schema_version: SchemaVersion = 2
    projection_version: str
    conversation_id: str
    items: list[TimelineItem] = Field(default_factory=list)
    cycle_states: list[TurnCycleState] = Field(default_factory=list)
    has_more_before: bool = False
    before_cursor: str | None = None
    server_time: str

    @model_validator(mode="after")
    def _validate_canonical_items(self) -> TimelineBackfillResponse:
        _ensure_canonical_timeline_items(self.items)
        return self


class ChatClientTransaction(StrictModel):
    conversation_id: str
    principal_id: str
    client_txn_id: str
    client_message_id: str
    request_hash: str
    status: Literal["accepted", "queued", "duplicate", "failed"]
    queue_id: str | None = None
    message_id: str | None = None
    source_ref: SourceRef | None = None
    created_at: str
    updated_at: str
