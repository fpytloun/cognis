"""Domain models for conversations and sessions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SessionStatus(StrEnum):
    """Session lifecycle states."""

    ACTIVE = "active"
    IDLE = "idle"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


class ConversationModel(BaseModel):
    """Conversation metadata stored in Cognis DB."""

    conversation_id: str
    user_email: str
    agent_id: str
    title: str | None = None
    context: ConversationContext
    active_session_id: str | None = None
    status: str = "active"
    last_message_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionModel(BaseModel):
    """Session metadata stored in Cognis DB."""

    session_id: str
    conversation_id: str
    parent_session_id: str | None = None
    previous_session_id: str | None = None
    user_email: str
    agent_id: str
    delegation_mode: str | None = None
    delegation_task: str | None = None
    status: str = SessionStatus.ACTIVE
    completion_reason: str | None = None
    intaris_session_id: str | None = None
    mnemory_session_id: str | None = None
    started_at: datetime | None = None
    idle_since: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    updated_at: datetime | None = None


class SessionEvent(BaseModel):
    """Session event payload sent to Intaris."""

    type: str
    data: dict[str, Any]


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


class ReasoningReportResult(BaseModel):
    """Optional enriched result from Intaris reasoning submission."""

    ok: bool = True
    call_id: str = ""
    intention: str | None = None
    title: str | None = None
    updated_at: str | None = None
