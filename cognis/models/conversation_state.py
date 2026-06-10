"""Chat-facing conversation state projection models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ConversationKind = Literal["normal", "task", "task_step", "external_channel"]


class ConversationStateOffsets(BaseModel):
    active_session_id: str | None = None
    active_session_last_seq: int | None = None
    task_state_revision: str | None = None
    step_state_revision: str | None = None


class ConversationActiveTurnState(BaseModel):
    has_active_turn: bool = False
    chat_mode: str | None = None
    chat_mode_source: str | None = None


class ConversationActiveSessionState(BaseModel):
    session_id: str | None = None
    status: str | None = None
    completion_reason: str | None = None


class ConversationTodoItem(BaseModel):
    content: str
    status: str = "pending"
    priority: str | None = None


class ConversationStepState(BaseModel):
    step_run_id: str
    step_name: str | None = None
    status: str
    conversation_id: str | None = None
    session_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    todos: list[ConversationTodoItem] = Field(default_factory=list)


class ConversationTaskState(BaseModel):
    task_id: str
    title: str | None = None
    status: str
    current_step: ConversationStepState | None = None
    relevant_step: ConversationStepState | None = None


class ConversationPendingSummary(BaseModel):
    notification_id: str
    notification_type: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    question: str | None = None
    label: str | None = None
    message: str | None = None
    options: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ConversationPendingState(BaseModel):
    notification_types: list[str] = Field(default_factory=list)
    pending_input: ConversationPendingSummary | None = None
    credential_request: ConversationPendingSummary | None = None
    auth_challenge: ConversationPendingSummary | None = None
    escalation: ConversationPendingSummary | None = None


class ConversationStateEnvelope(BaseModel):
    conversation_id: str
    conversation_kind: ConversationKind = "normal"
    linked_task_id: str | None = None
    linked_step_run_id: str | None = None
    state_version: int = 1
    snapshot_generated_at: datetime
    capabilities: list[str] = Field(default_factory=list)
    offsets: ConversationStateOffsets = Field(default_factory=ConversationStateOffsets)
    active_turn: ConversationActiveTurnState = Field(default_factory=ConversationActiveTurnState)
    active_session: ConversationActiveSessionState = Field(
        default_factory=ConversationActiveSessionState
    )
    task: ConversationTaskState | None = None
    pending: ConversationPendingState = Field(default_factory=ConversationPendingState)


class ConversationStateDeltaSource(BaseModel):
    kind: str
    task_id: str | None = None
    step_run_id: str | None = None


class ConversationStateDelta(BaseModel):
    conversation_id: str
    delta_id: str
    state_version: int = 1
    snapshot_required: bool = False
    changed_paths: list[str] = Field(default_factory=list)
    replace: dict[str, Any] = Field(default_factory=dict)
    source: ConversationStateDeltaSource
