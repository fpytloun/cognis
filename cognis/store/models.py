"""SQLAlchemy ORM models for Cognis DB.

Cognis DB stores only system state and session metadata. Session content
(messages, tool calls, events) is stored in Intaris. Intaris-derived state
(event sequences, compaction summaries, intention) is NOT stored here —
it lives in the session cache layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
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
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String, nullable=True)


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


class BrowserSession(Base):
    """Opaque browser session for the bundled web UI."""

    __tablename__ = "browser_sessions"
    __table_args__ = (
        Index("ix_browser_sessions_user_email", "user_email"),
        Index("ix_browser_sessions_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_browser_sessions_token_hash"),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
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
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)  # deprecated
    avatar_image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Type system
    agent_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="primary", server_default="primary"
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AgentSecondaryBinding(Base):
    """Junction table for primary-to-secondary agent bindings."""

    __tablename__ = "agent_secondary_bindings"

    primary_agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    secondary_agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )


class AgentGrantRow(Base):
    """User-to-user sharing grant for an agent."""

    __tablename__ = "agent_grants"
    __table_args__ = (
        Index(
            "uq_agent_grants_active_user",
            "agent_id",
            "grantee_user_email",
            unique=True,
            sqlite_where=text("grantee_type = 'user' AND revoked_at IS NULL"),
            postgresql_where=text("grantee_type = 'user' AND revoked_at IS NULL"),
        ),
        Index("ix_agent_grants_grantee_user", "grantee_user_email"),
        Index("ix_agent_grants_agent", "agent_id"),
    )

    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.agent_id", ondelete="CASCADE"), nullable=False
    )
    grantee_type: Mapped[str] = mapped_column(String, nullable=False, default="user")
    grantee_user_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email"), nullable=True
    )
    grantee_group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    permission: Mapped[str] = mapped_column(String, nullable=False, default="use")
    executor_scope: Mapped[str] = mapped_column(String, nullable=False, default="owner_executor")
    granted_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    grantee_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ProjectRow(Base):
    """First-class project context owned by a user."""

    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_owner_email", "owner_email"),)

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ProjectSourceRow(Base):
    """Repository/source hint associated with a project."""

    __tablename__ = "project_sources"
    __table_args__ = (
        Index("ix_project_sources_project_id", "project_id"),
        Index("ix_project_sources_local_path", "local_path"),
    )

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ProjectWorkflowRow(Base):
    """Project-to-workflow eligibility binding."""

    __tablename__ = "project_workflows"

    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True
    )
    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ProjectGrantRow(Base):
    """User-to-user sharing grant for a project."""

    __tablename__ = "project_grants"
    __table_args__ = (
        Index(
            "uq_project_grants_active_user",
            "project_id",
            "grantee_user_email",
            unique=True,
            sqlite_where=text("grantee_type = 'user' AND revoked_at IS NULL"),
            postgresql_where=text("grantee_type = 'user' AND revoked_at IS NULL"),
        ),
        Index("ix_project_grants_grantee_user", "grantee_user_email"),
        Index("ix_project_grants_project", "project_id"),
    )

    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    grantee_type: Mapped[str] = mapped_column(String, nullable=False, default="user")
    grantee_user_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email"), nullable=True
    )
    grantee_group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    permission: Mapped[str] = mapped_column(String, nullable=False, default="use")
    granted_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemAgentOverride(Base):
    """Per-user runtime tuning overlay for shipped system agents."""

    __tablename__ = "system_agent_overrides"
    __table_args__ = (
        UniqueConstraint("owner_email", "agent_id", name="uq_system_agent_overrides_owner_agent"),
        Index("ix_system_agent_overrides_owner", "owner_email"),
    )

    override_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    llm_config_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    skills_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    title_source: Mapped[str] = mapped_column(String, nullable=False, default="unset")
    context_type: Mapped[str] = mapped_column(String, nullable=False)
    context_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    context_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    memory_labels: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    active_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_executor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
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
    previous_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    delegation_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    delegation_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    completion_reason: Mapped[str | None] = mapped_column(String, nullable=True)
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


class CredentialRow(Base):
    """Encrypted credential records for agent-facing authentication."""

    __tablename__ = "credentials"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    credential_id: Mapped[str] = mapped_column(String, nullable=False)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="user")
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_email", "credential_id", name="uq_credentials_user_id"),
        Index("ix_credentials_user_label", "user_email", "label"),
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
    expected_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="api")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String, nullable=False, default="same_conversation")
    delivery_target: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_mode_family: Mapped[str] = mapped_column(
        String, nullable=False, default="default", server_default="default"
    )
    allow_silent_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    interaction_mode_override: Mapped[str | None] = mapped_column(String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    workspace_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    working_directory: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    queue_name: Mapped[str] = mapped_column(String, nullable=False, default="default")
    scheduled_for: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Stage 36: task-level active executor pin. Initialised on the first
    # step that resolves a runtime; carried forward to every step
    # conversation so all steps of a task run on the same executor unless
    # the agent explicitly switches.
    active_executor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    applied_completion_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_completion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    lifecycle: Mapped[str] = mapped_column(
        String, nullable=False, default="persistent", server_default="persistent"
    )
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SystemWorkflowOverride(Base):
    """Per-user runtime tuning overlay for shipped system workflows."""

    __tablename__ = "system_workflow_overrides"
    __table_args__ = (
        UniqueConstraint(
            "owner_email", "workflow_id", name="uq_system_workflow_overrides_owner_workflow"
        ),
        Index("ix_system_workflow_overrides_owner", "owner_email"),
    )

    override_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    step_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StepRun(Base):
    """Current execution state for one workflow step within a task run."""

    __tablename__ = "step_runs"

    step_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.task_id"), nullable=False)
    step_name: Mapped[str] = mapped_column(String, nullable=False)
    step_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    superseded_by_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    workspace_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    working_directory: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    intaris_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deliverable_id: Mapped[str | None] = mapped_column(String, nullable=True)
    require_deliverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    todos: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    runtime_info: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class DeliverableRow(Base):
    """Typed user-facing artifact written by a workflow step."""

    __tablename__ = "deliverables"
    __table_args__ = (
        UniqueConstraint("step_run_id", "version", name="uq_deliverables_step_run_version"),
        Index("ix_deliverables_step_run", "step_run_id"),
        Index("ix_deliverables_status", "status"),
    )

    deliverable_id: Mapped[str] = mapped_column(String, primary_key=True)
    step_run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("step_runs.step_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False, default="markdown")
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="buffered")
    evaluator_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Schedule(Base):
    """Cron/interval/one-shot task factory.

    Schedules evaluate on a timer and create Tasks when they fire.
    The task_template JSON blob is used as kwargs to TaskQueue.submit().
    """

    __tablename__ = "schedules"

    schedule_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_type: Mapped[str] = mapped_column(String, nullable=False, default="cron")
    cron_expr: Mapped[str | None] = mapped_column(String, nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    one_shot_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="UTC")
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    skill_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_template: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delete_after_run: Mapped[bool] = mapped_column(nullable=False, default=False)
    completion_mode_family: Mapped[str] = mapped_column(
        String, nullable=False, default="default", server_default="default"
    )
    allow_silent_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    interaction_mode_override: Mapped[str | None] = mapped_column(
        String, nullable=True, default="none", server_default="none"
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (Index("ix_schedules_enabled_next_fire", "enabled", "next_fire_at"),)


class TaskCommentRow(Base):
    """Human-authored task comment with explicit intent."""

    __tablename__ = "task_comments"
    __table_args__ = (Index("ix_task_comments_task", "task_id"),)

    comment_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    author_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(
        String, nullable=False, default="record_only", server_default="record_only"
    )
    noop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    target_step: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ExecutorRow(Base):
    """Executor configurations with tool assignment."""

    __tablename__ = "executors"

    executor_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    executor_type: Mapped[str] = mapped_column(String, nullable=False, default="in_process")
    labels: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    enabled_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    enabled_tool_groups: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    desired_config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_tools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    runtime_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    runtime_state: Mapped[str] = mapped_column(String, nullable=False, default="offline")
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_default: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="0")
    owner_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class MCPServerRow(Base):
    """Global MCP server definitions assigned to executors."""

    __tablename__ = "mcp_servers"

    server_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    transport: Mapped[str] = mapped_column(String, nullable=False, default="stdio")
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    args: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    env: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    headers: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (UniqueConstraint("name", "owner_email", name="uq_mcp_server_name_owner"),)


class ToolClassificationRow(Base):
    """Persisted tool classification state and retry metadata."""

    __tablename__ = "tool_classifications"

    classification_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=True
    )
    tool_id: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    tool_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    capabilities: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    classification_source: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("scope_key", "tool_id", name="uq_tool_classifications_scope_tool"),
        Index("ix_tool_classifications_status_next_retry", "status", "next_retry_at"),
    )


class ToolClassificationOverrideRow(Base):
    """Manual tool classification overrides with highest precedence."""

    __tablename__ = "tool_classification_overrides"

    override_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=True
    )
    tool_id: Mapped[str] = mapped_column(String, nullable=False)
    profile_group: Mapped[str] = mapped_column(String, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "scope_key", "tool_id", name="uq_tool_classification_overrides_scope_tool"
        ),
    )


class SkillRow(Base):
    """DB-managed skill definitions (logical skill record).

    The ``skills`` table is the logical skill record.  Versioned content
    (instructions, tools, templates, assets) lives in ``skill_versions``.
    The ``current_version_id`` points to the active published version.

    Legacy fields (instructions, tools, prompt_templates) are kept for
    backward compatibility with existing data but new content should be
    stored via ``SkillVersionRow``.
    """

    __tablename__ = "skills"

    skill_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    # Historically persisted as dict, now canonical list[dict] matching
    # SkillVersionRow. Read path must defensively coerce legacy dict rows.
    tools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    linked_tool_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    prompt_templates: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    auto_load: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="0")
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=false())
    source: Mapped[str] = mapped_column(String, nullable=False, default="db")
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_email: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.email"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SkillVersionRow(Base):
    """Immutable skill version record.

    Each version captures a snapshot of instructions, tool definitions,
    prompt templates, import provenance, and an asset manifest.  The
    ``content_hash`` is a SHA-256 of the canonical content for
    deduplication and integrity verification.
    """

    __tablename__ = "skill_versions"

    version_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_id: Mapped[str] = mapped_column(
        String, ForeignKey("skills.skill_id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    linked_tool_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    prompt_templates: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    secret_placeholders: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    decomposition_source_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Import provenance
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_url: Mapped[str | None] = mapped_column(String, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    import_checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    import_format: Mapped[str | None] = mapped_column(String, nullable=True)
    # Asset manifest (list of {filename, asset_id, content_hash, size_bytes, content_type})
    asset_manifest: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("skill_id", "version_number", name="uq_skill_version_number"),
        Index("ix_skill_versions_skill_id", "skill_id"),
    )


class SkillAssetRow(Base):
    """Skill asset linked to artifact store.

    Each asset belongs to a specific skill version and references an
    object in the Cognis artifact store.  Assets are staged to executor
    temp storage only when needed for active skill tools.
    """

    __tablename__ = "skill_assets"

    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("skill_versions.version_id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    artifact_namespace: Mapped[str] = mapped_column(String, nullable=False, default="skills")
    artifact_object_id: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(
        String, nullable=False, default="application/octet-stream"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_skill_assets_version_id", "skill_version_id"),)


class NotificationRow(Base):
    """Persistent notifications for escalations, gates, and step questions.

    Notifications are the durable record of user-facing prompts that
    require resolution (approve/deny, gate continue, step input).  The
    in-memory PauseWaiter provides the async synchronization; this table
    provides durability across restarts and a unified query surface.
    """

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String, primary_key=True)
    notification_type: Mapped[str] = mapped_column(String, nullable=False)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    step_name: Mapped[str | None] = mapped_column(String, nullable=True)
    step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_notifications_user_status", "user_email", "status"),
        Index("ix_notifications_conv_status", "conversation_id", "status"),
    )


class PushSubscriptionRow(Base):
    """Browser Web Push subscription for PWA notifications."""

    __tablename__ = "push_subscriptions"

    subscription_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (Index("ix_push_subscriptions_user_enabled", "user_email", "enabled"),)


class TtsCacheRow(Base):
    """Cached text-to-speech artifact metadata.

    Cognis caches synthesized audio in the artifact store under namespace
    ``tts``. This row tracks the (message, voice, model) tuple that maps to
    a cached artifact so subsequent speaker-button clicks can serve from
    cache without re-synthesizing. Pruned by TTL (``tts.cache_ttl_days``).
    """

    __tablename__ = "tts_cache"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    voice: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_tts_cache_created_at", "created_at"),)


class RememberQueueRow(Base):
    """Durable queue for deferred Mnemory remember work.

    The queue is controller-owned and stores only metadata plus the serialized
    remember payload required to retry the call after restart.
    """

    __tablename__ = "remember_queue"

    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_remember_queue_status_due", "status", "next_retry_at"),
        Index("ix_remember_queue_session", "session_id"),
    )


class FollowUpDedupeRow(Base):
    """Durable dedupe state for follow-up turn requests.

    This replaces the previous in-memory-only suppression so duplicate follow-up
    ids remain suppressed across restarts and across controller replicas.
    """

    __tablename__ = "follow_up_dedupe"

    dedupe_key: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    follow_up_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "follow_up_id", name="uq_follow_up_dedupe_pair"),
        Index("ix_follow_up_dedupe_expires", "expires_at"),
        Index("ix_follow_up_dedupe_conversation", "conversation_id"),
    )


class ChannelDeliveryOutboxRow(Base):
    """Durable outbox for background/system channel follow-up sends.

    Stores only metadata and deterministic fallback text. Assistant/user
    content is never persisted here.
    """

    __tablename__ = "channel_delivery_outbox"

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    fallback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_channel_delivery_status_due", "status", "next_attempt_at"),
        Index("ix_channel_delivery_conversation_created", "conversation_id", "created_at"),
        Index("ix_channel_delivery_source", "source_type", "source_id"),
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


class ChannelAccountRow(Base):
    """Channel account configurations.

    Each row represents a configured connection to an external messaging
    platform (Signal, WhatsApp, Telegram, etc.).  Credentials are stored
    via SecretsProvider and referenced by name in ``credential_refs``.
    """

    __tablename__ = "channel_accounts"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credential_refs: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    # Routing
    default_conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    allow_new_conversations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    preferred_for_task_delivery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Adapter location
    adapter_location: Mapped[str] = mapped_column(
        String, nullable=False, default="controller", server_default="controller"
    )
    executor_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("executors.executor_id", ondelete="SET NULL"), nullable=True
    )
    # Access control
    allowed_senders: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    dm_policy: Mapped[str] = mapped_column(String, nullable=False, default="pairing")
    group_policy: Mapped[str] = mapped_column(String, nullable=False, default="pairing")
    webhook_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_channel_accounts_type", "channel_type"),
        Index("ix_channel_accounts_user", "user_email"),
    )


class ChannelContact(Base):
    """Maps external platform senders to Cognis users.

    Resolves the identity gap between platform-specific sender IDs
    (phone numbers, Discord IDs, etc.) and Cognis user emails.
    """

    __tablename__ = "channel_contacts"

    contact_id: Mapped[str] = mapped_column(String, primary_key=True)
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("channel_type", "sender_id", name="uq_channel_contact"),
        Index("ix_channel_contacts_lookup", "channel_type", "sender_id"),
    )


class ChannelPairingRequest(Base):
    """Short-lived pairing challenges for external channel senders."""

    __tablename__ = "channel_pairing_requests"

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_name: Mapped[str | None] = mapped_column(String, nullable=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("code", name="uq_channel_pairing_code"),
        Index("ix_channel_pairing_owner_status", "owner_email", "status"),
        Index("ix_channel_pairing_sender_status", "channel_type", "sender_id", "status"),
    )


class ArtifactRecordRow(Base):
    """Metadata and lifecycle state for stored artifacts."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    object_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(String, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_role: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False, default="chat_input")
    kind: Mapped[str] = mapped_column(String, nullable=False, default="file")
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="temporary")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_artifacts_owner_status", "owner_email", "status"),
        Index("ix_artifacts_conversation", "conversation_id"),
        Index("ix_artifacts_expiry", "expires_at"),
    )
