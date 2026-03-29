"""SQLAlchemy ORM models for Cognis DB.

Cognis DB stores only system state and session metadata. Session content
(messages, tool calls, events) is stored in Intaris. Intaris-derived state
(event sequences, compaction summaries, intention) is NOT stored here —
it lives in the session cache layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, TIMESTAMP, ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    type_annotation_map = {
        dict: JSON,
        list: JSON,
    }


class User(Base):
    """User accounts. Email is the primary identifier everywhere."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ApiKey(Base):
    """API keys for programmatic access."""

    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class Agent(Base):
    """Agent definitions."""

    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    skills: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tools: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    permissions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    llm_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sync_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Conversation(Base):
    """Conversation metadata. Session content is in Intaris."""

    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    context_type: Mapped[str] = mapped_column(String, nullable=False)
    context_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    context_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    memory_labels: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    root_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Session(Base):
    """Session metadata. Session content is in Intaris.

    NOTE: intention, last_event_seq, last_compaction_summary, and
    last_compaction_seq are NOT stored here. They live in the session
    cache (in-memory / Redis). See 01-architecture.md.
    """

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.conversation_id"), nullable=False
    )
    parent_session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.session_id"), nullable=True
    )
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    delegation_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    delegation_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    intaris_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mnemory_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    idle_since: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Setting(Base):
    """System settings stored in DB (replaces config file for app-level config)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.email"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class LLMProvider(Base):
    """LLM provider configurations."""

    __tablename__ = "llm_providers"

    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    backend: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ModelRouting(Base):
    """Model routing policy — which model for which task type."""

    __tablename__ = "model_routing"

    task_type: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("llm_providers.provider_id"), nullable=True
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Secret(Base):
    """Encrypted secrets (AES-256-GCM)."""

    __tablename__ = "secrets"

    secret_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="user")
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_email", "name", "scope", "agent_id", name="uq_secret"),
    )


class Task(Base):
    """Durable work items with queue semantics and workflow state.

    Tasks own workflow execution state directly via the workflow_state
    JSONB column. There is no separate workflow_runs table in MVP.
    """

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="api")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String, nullable=False, default="same_conversation")
    delivery_target: Mapped[str | None] = mapped_column(String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    workflow_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    queue_name: Mapped[str] = mapped_column(String, nullable=False, default="default")
    scheduled_for: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class TaskDependency(Base):
    """DAG edges between tasks."""

    __tablename__ = "task_dependencies"

    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.task_id"), primary_key=True)
    depends_on: Mapped[str] = mapped_column(String, ForeignKey("tasks.task_id"), primary_key=True)
    required: Mapped[bool] = mapped_column(nullable=False, default=True)


class WorkflowRow(Base):
    """Portable workflow templates stored in DB."""

    __tablename__ = "workflows"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)
    owner_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StepRun(Base):
    """Individual step execution attempts within a workflow run."""

    __tablename__ = "step_runs"

    step_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.task_id"), nullable=False)
    step_name: Mapped[str] = mapped_column(String, nullable=False)
    step_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    intaris_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    todos: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Schedule(Base):
    """Cron-like task factory."""

    __tablename__ = "schedules"

    schedule_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_template: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AuditLog(Base):
    """System-level audit events (NOT session content)."""

    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    user_email: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
