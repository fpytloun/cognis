"""Domain models for conversations and sessions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SessionStatus(StrEnum):
    """Session lifecycle states."""

    ACTIVE = "active"
    IDLE = "idle"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionTransition(StrEnum):
    """Operational semantics for a session successor."""

    COMPACT = "compact"
    RENEW = "renew"
    RESET = "reset"


TERMINAL_STATES = frozenset(
    {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.TERMINATED,
    }
)

BLOCKED_STATES = TERMINAL_STATES | frozenset({SessionStatus.SUSPENDED})


class ConversationContext(BaseModel):
    """Conversation context metadata."""

    type: str
    ref: str | None = None
    platform_data: dict[str, Any] = Field(default_factory=dict)
    memory_labels: dict[str, str] = Field(default_factory=dict)


class ConversationLineage(BaseModel):
    """Trusted typed input for one canonical conversation lineage edge."""

    kind: Literal["conversation", "task", "task_step"]
    source_conversation_id: str
    source_session_id: str
    task_id: str | None = None
    step_run_id: str | None = None

    @model_validator(mode="after")
    def validate_exact_edge(self) -> ConversationLineage:
        if self.kind == "conversation":
            if self.task_id is not None or self.step_run_id is not None:
                raise ValueError("Conversation lineage cannot reference a task or step")
        elif not self.task_id or not self.step_run_id:
            raise ValueError("Task lineage requires task and step references")
        return self


class ConversationModel(BaseModel):
    """Conversation metadata stored in Cognis DB."""

    conversation_id: str
    user_email: str
    agent_id: str
    agent_profile_id: str | None = None
    title: str | None = None
    title_source: str = "unset"
    context: ConversationContext
    project_id: str | None = None
    active_session_id: str | None = None
    active_executor_id: str | None = None
    active_executor_assigned_at: datetime | None = None
    active_executor_expires_at: datetime | None = None
    active_executor_source: str | None = None
    starred_at: datetime | None = None
    status: str = "active"
    last_message_at: datetime | None = None
    last_read_at: datetime | None = None
    has_unread: bool = False
    has_active_turn: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionModel(BaseModel):
    """Session metadata stored in Cognis DB."""

    session_id: str
    activity_scope_id: str = ""
    conversation_id: str
    parent_session_id: str | None = None
    previous_session_id: str | None = None
    source_session_id: str | None = None
    user_email: str
    agent_id: str
    agent_profile_id: str | None = None
    channel_default_agent_profile_id: str | None = Field(default=None, exclude=True)
    delegation_mode: str | None = None
    delegation_task: str | None = None
    delegation_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = SessionStatus.ACTIVE
    completion_reason: str | None = None
    intaris_session_id: str | None = None
    mnemory_session_id: str | None = None
    started_at: datetime | None = None
    idle_since: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    result_content: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def default_activity_scope(self) -> SessionModel:
        if not self.activity_scope_id:
            self.activity_scope_id = self.session_id
        return self


class SessionEvent(BaseModel):
    """Session event payload sent to Intaris."""

    type: str
    data: dict[str, Any]


def with_session_event_turn_id(event: SessionEvent, turn_id: str | None) -> SessionEvent:
    """Return a copy of ``event`` with a normalized ``turn_id`` field."""

    data = dict(event.data)
    data.setdefault("turn_id", turn_id)
    return event.model_copy(update={"data": data})


def with_session_events_turn_id(
    events: Sequence[SessionEvent],
    turn_id: str | None,
) -> list[SessionEvent]:
    """Normalize ``turn_id`` on a batch of persisted session events."""

    return [with_session_event_turn_id(event, turn_id) for event in events]


class EventAppendResult(BaseModel):
    """Intaris event append response."""

    ok: bool
    count: int
    first_seq: int
    last_seq: int


class EventReadResult(BaseModel):
    """Intaris event read response."""

    events: list[dict[str, Any]]
    last_seq: int
    has_more: bool
    missing_stream_fallback_used: bool = False


class IntarisSession(BaseModel):
    """Intaris session state."""

    session_id: str
    user_id: str
    agent_id: str
    title: str | None = None
    intention: str | None = None
    details: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    status: str
    total_calls: int = 0
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    parent_session_id: str | None = None
    created_at: str
    updated_at: str


class IntarisSessionSummaryRecord(BaseModel):
    """One Intaris-generated session summary record."""

    id: str
    session_id: str
    window_start: str
    window_end: str
    trigger: str
    summary_type: str = "window"
    summary: str
    tools_used: list[str] | None = None
    intent_alignment: str
    risk_indicators: list[dict[str, Any]] | None = None
    call_count: int
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    created_at: str


class IntarisAgentSummaryRecord(BaseModel):
    """One agent-reported session summary record stored in Intaris."""

    id: str
    session_id: str
    summary: str
    created_at: str


class IntarisSessionSummaries(BaseModel):
    """Combined Intaris summary response for a session."""

    intaris_summaries: list[IntarisSessionSummaryRecord] = Field(default_factory=list)
    agent_summaries: list[IntarisAgentSummaryRecord] = Field(default_factory=list)


class ReasoningReportResult(BaseModel):
    """Optional enriched result from Intaris reasoning submission."""

    ok: bool = True
    call_id: str = ""
    intention: str | None = None
    title: str | None = None
    updated_at: str | None = None
