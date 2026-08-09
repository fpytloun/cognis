"""SQLAlchemy ORM models for Cognis DB.

Cognis DB stores only system state and session metadata. Session content
(messages, tool calls, events) is stored in Intaris. Intaris-derived state
(event sequences, compaction summaries, intention) is NOT stored here —
it lives in the session cache layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _session_activity_scope_default(context: Any) -> str:
    return str(context.get_current_parameters()["session_id"])


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    type_annotation_map = {
        dict: JSON,
        list: JSON,
    }


class CoordinationLeaseRow(Base):
    """Database-authoritative lease with a monotonically increasing fence."""

    __tablename__ = "coordination_leases"

    resource_key: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (Index("ix_coordination_leases_expires", "lease_expires_at"),)


class ControllerInstanceRow(Base):
    """Minimal DB-authoritative controller boot-incarnation directory."""

    __tablename__ = "controller_instances"

    owner_id: Mapped[str] = mapped_column(String, primary_key=True)
    controller_id: Mapped[str] = mapped_column(String, nullable=False)
    incarnation_id: Mapped[str] = mapped_column(String, nullable=False)
    internal_url: Mapped[str | None] = mapped_column(String, nullable=True)
    lifecycle_state: Mapped[str] = mapped_column(String, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('starting', 'ready', 'draining', 'stopped')",
            name="ck_controller_instances_lifecycle_state",
        ),
        Index("ix_controller_instances_controller", "controller_id"),
        Index("ix_controller_instances_expires", "expires_at"),
    )


class DirectTurnRequestRow(Base):
    """Durable admission and fenced ownership state for one direct turn request."""

    __tablename__ = "direct_turn_requests"

    admission_order: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    request_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, default=lambda: f"dtr_{uuid.uuid4().hex}"
    )
    turn_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, default=lambda: f"turn_{uuid.uuid4().hex[:12]}"
    )
    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_scope: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    admission_hash: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    owner_controller_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_incarnation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    absorbed_by_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'queued', 'claimed', 'running', 'absorbing', 'recoverable', "
            "'completed', 'failed', 'cancelled', 'absorbed', 'ambiguous'"
            ")",
            name="ck_direct_turn_requests_status",
        ),
        UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_direct_turn_requests_idempotency",
        ),
        Index(
            "ix_direct_turn_requests_fifo",
            "conversation_id",
            "status",
            "admission_order",
        ),
        Index(
            "ix_direct_turn_requests_status_due",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_direct_turn_requests_owner",
            "owner_controller_id",
            "owner_incarnation_id",
            "status",
        ),
    )


class User(Base):
    """User accounts. Email is the primary identifier everywhere."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
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


class UserUiState(Base):
    """Per-user UI state persisted by the web application."""

    __tablename__ = "user_ui_state"
    __table_args__ = (Index("ix_user_ui_state_user_email", "user_email"),)

    user_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
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
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    agent_profiles: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    default_agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sync_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)  # deprecated
    avatar_image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Type system
    agent_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="primary", server_default="primary"
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
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
        Boolean, nullable=False, default=False, server_default=false()
    )
    llm_config_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    skills_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tools_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    permissions_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    agent_profiles_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    default_agent_profile_id_override: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Conversation(Base):
    """Conversation metadata. Session content is in Intaris."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "ix_conversations_owner_activity",
            "user_email",
            "status",
            "last_message_at",
            "created_at",
        ),
        Index(
            "ix_conversations_owner_agent_context",
            "user_email",
            "status",
            "agent_id",
            "context_type",
        ),
        Index(
            "ix_conversations_owner_fork_conversation",
            "user_email",
            "fork_source_conversation_id",
            "conversation_id",
        ),
        Index(
            "ix_conversations_owner_fork_session",
            "user_email",
            "fork_source_session_id",
            "conversation_id",
        ),
        Index(
            "ix_conversations_owner_lineage_task",
            "user_email",
            "lineage_task_id",
            "conversation_id",
        ),
        Index(
            "ix_conversations_owner_lineage_step",
            "user_email",
            "lineage_step_run_id",
            "conversation_id",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    title_source: Mapped[str] = mapped_column(String, nullable=False, default="unset")
    context_type: Mapped[str] = mapped_column(String, nullable=False)
    context_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    context_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    lineage_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    fork_source_conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fork_source_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lineage_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lineage_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    memory_labels: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    active_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_executor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_executor_assigned_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    active_executor_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    active_executor_source: Mapped[str | None] = mapped_column(String, nullable=True)
    active_executor_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    active_executor_unavailable_since: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    starred_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
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


class ChatClientTransactionRow(Base):
    """Durable idempotency ledger for Chat v2 client mutations.

    The row is a control-plane ledger only. Canonical conversation content remains
    in the configured session event store; route handlers store only request
    identity, request hash, and small mutation results here.
    """

    __tablename__ = "chat_client_transactions"

    transaction_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"chat_txn_{uuid.uuid4().hex}"
    )
    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        nullable=False,
    )
    client_txn_id: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "principal_id",
            "client_txn_id",
            "operation",
            name="uq_chat_client_transactions_key",
        ),
        Index("ix_chat_client_transactions_conversation", "conversation_id"),
        Index("ix_chat_client_transactions_principal", "principal_id"),
        Index("ix_chat_client_transactions_updated", "updated_at"),
    )


class Session(Base):
    """Session metadata. Session content is in Intaris.

    NOTE: intention, last_event_seq, last_compaction_summary, and
    last_compaction_seq are NOT stored here. They live in the session
    cache (in-memory / Redis). See 01-architecture.md.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_owner_source_session", "user_email", "source_session_id"),
        Index("ix_sessions_owner_parent_session", "user_email", "parent_session_id", "session_id"),
        Index(
            "ix_sessions_owner_previous_session",
            "user_email",
            "previous_session_id",
            "session_id",
        ),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    activity_scope_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=_session_activity_scope_default,
    )
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.conversation_id"), nullable=False
    )
    parent_session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.session_id"), nullable=True
    )
    previous_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    delegation_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    delegation_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
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
    result_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class WorkScopeState(Base):
    """Rebuildable control-plane revision state for one authorized Work root.

    Canonical Work evidence remains in Intaris. This row stores only monotonic
    invalidation counters and the fingerprint of the derived lineage graph.
    """

    __tablename__ = "work_scope_states"
    __table_args__ = (
        Index("ix_work_scope_states_owner_root", "user_email", "scope_kind", "root_id"),
    )

    scope_key: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        nullable=False,
    )
    scope_kind: Mapped[str] = mapped_column(String, nullable=False)
    root_id: Mapped[str] = mapped_column(String, nullable=False)
    graph_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    work_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    graph_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class WorkRecordRow(Base):
    """Bounded, rebuildable Work evidence projected from one Intaris stream."""

    __tablename__ = "work_records"
    __table_args__ = (
        UniqueConstraint(
            "owner_email",
            "source_store",
            "source_session_id",
            "source_seq",
            "item_ordinal",
            "materializer_version",
            name="uq_work_records_source_item_version",
        ),
        Index(
            "ix_work_records_owner_session_version_order",
            "owner_email",
            "session_id",
            "materializer_version",
            "occurred_at",
            "source_seq",
            "item_ordinal",
            "work_record_id",
        ),
        Index(
            "ix_work_records_owner_version_newest",
            "owner_email",
            "materializer_version",
            "is_evidence",
            "occurred_at",
            "session_id",
            "source_seq",
            "item_ordinal",
            "work_record_id",
        ),
        Index(
            "ix_work_records_owner_category_order",
            "owner_email",
            "materializer_version",
            "is_evidence",
            "category",
            "occurred_at",
            "session_id",
            "source_seq",
            "item_ordinal",
            "work_record_id",
        ),
        Index(
            "ix_work_records_owner_category_entity",
            "owner_email",
            "materializer_version",
            "category",
            "entity_id",
        ),
        Index(
            "ix_work_records_pairing",
            "owner_email",
            "session_id",
            "materializer_version",
            "call_id",
            "pairing_key",
        ),
    )

    work_record_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    materializer_version: Mapped[str] = mapped_column(String, nullable=False)
    source_store: Mapped[str] = mapped_column(
        String, nullable=False, default="intaris", server_default="intaris"
    )
    source_session_id: Mapped[str] = mapped_column(String, nullable=False)
    source_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_item_id: Mapped[str] = mapped_column(String, nullable=False)
    item_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    record_type: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    file_path_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_evidence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    pairing_key: Mapped[str | None] = mapped_column(String, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    timeline_item: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    materialized_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )


class WorkRecordFileRow(Base):
    """Normalized file metadata for one bounded Work record."""

    __tablename__ = "work_record_files"
    __table_args__ = (
        UniqueConstraint(
            "work_record_id",
            "file_ordinal",
            name="uq_work_record_files_record_ordinal",
        ),
        Index(
            "ix_work_record_files_record_order",
            "work_record_id",
            "file_ordinal",
        ),
        Index(
            "ix_work_record_files_path",
            "path_id",
            "work_record_id",
        ),
    )

    work_record_file_id: Mapped[str] = mapped_column(String, primary_key=True)
    work_record_id: Mapped[str] = mapped_column(
        ForeignKey("work_records.work_record_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    path_id: Mapped[str] = mapped_column(String, nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkSessionProjectionRow(Base):
    """Contiguous materialization state for one Cognis session and version."""

    __tablename__ = "work_session_projections"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "materializer_version",
            name="uq_work_session_projections_session_version",
        ),
        CheckConstraint(
            "state IN ('pending','materializing','caught_up','repair','failed')",
            name="ck_work_session_projections_state",
        ),
        CheckConstraint(
            "target_seq >= 0 AND covered_through_seq >= 0 AND retry_count >= 0 "
            "AND lease_fence >= 0",
            name="ck_work_session_projections_nonnegative",
        ),
        Index(
            "ix_work_session_projections_queue",
            "materializer_version",
            "state",
            "priority",
            "next_retry_at",
        ),
        Index(
            "ix_work_session_projections_owner_state",
            "owner_email",
            "materializer_version",
            "state",
        ),
        Index("ix_work_session_projections_lease", "lease_expires_at", "lease_fence"),
    )

    projection_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    source_session_id: Mapped[str] = mapped_column(String, nullable=False)
    materializer_version: Mapped[str] = mapped_column(String, nullable=False)
    target_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    covered_through_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    lease_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )
    materialized_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    head_checked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class WorkScopeStream(Base):
    """Reference-only membership and high-water state for one Work stream."""

    __tablename__ = "work_scope_streams"
    __table_args__ = (
        Index(
            "ix_work_scope_streams_event_stream",
            "event_store_id",
            "event_store_session_id",
            "scope_key",
        ),
        Index("ix_work_scope_streams_session", "session_id", "scope_key"),
    )

    scope_key: Mapped[str] = mapped_column(
        String,
        ForeignKey("work_scope_states.scope_key", ondelete="CASCADE"),
        primary_key=True,
    )
    session_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_store_id: Mapped[str] = mapped_column(String, nullable=False, default="intaris")
    event_store_session_id: Mapped[str] = mapped_column(String, nullable=False)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ConversationTodo(Base):
    """Authoritative conversation-scoped TODO state.

    Cognis may create or rotate backing Intaris sessions under one chat
    conversation. This table is the OpenCode-style long-lived TODO state for
    the user-visible conversation.
    """

    __tablename__ = "conversation_todos"
    __table_args__ = (Index("ix_conversation_todos_conversation", "conversation_id"),)

    conversation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SessionTodo(Base):
    """Backing-session TODO state.

    Session content still lives in Intaris; TODOs are small controller-owned
    runtime state. ConversationTodo is authoritative for user-visible chat
    state; this table mirrors it for backing-session lineage and audit.
    """

    __tablename__ = "session_todos"
    __table_args__ = (Index("ix_session_todos_session", "session_id"),)

    session_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ManagedConversationLink(Base):
    """Control-plane link for an agent-managed normal conversation.

    The target conversation/session remain normal root Intaris/Cognis sessions.
    This row records supervisory ownership and durable turn state; it must not
    be confused with ``Session.parent_session_id`` delegation lineage.
    """

    __tablename__ = "managed_conversation_links"
    __table_args__ = (
        UniqueConstraint(
            "target_conversation_id",
            name="uq_managed_conversation_links_target_conversation",
        ),
        Index(
            "ix_managed_conversation_links_controller_conversation",
            "controller_conversation_id",
        ),
        Index(
            "ix_managed_conversation_links_owner_controller_session",
            "user_email",
            "controller_session_id",
            "link_id",
        ),
        Index("ix_managed_conversation_links_user_state", "user_email", "conversation_state"),
        Index(
            "ix_managed_conversation_links_user_kind_state",
            "user_email",
            "kind",
            "conversation_state",
        ),
        Index("ix_managed_conversation_links_target_agent", "target_agent_id"),
        Index("ix_managed_conversation_links_parent_link", "parent_link_id"),
        Index("ix_managed_conversation_links_root_depth", "root_link_id", "depth"),
        Index(
            "ix_managed_conversation_links_handoff_owner",
            "handoff_state",
            "handoff_controller_session_id",
            "handoff_controller_turn_id",
        ),
    )

    link_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=lambda: f"mconv_{uuid.uuid4().hex[:24]}",
    )
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    controller_agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.agent_id"), nullable=False
    )
    controller_conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.conversation_id"), nullable=False
    )
    controller_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_link_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("managed_conversation_links.link_id"), nullable=True
    )
    root_link_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("managed_conversation_links.link_id"), nullable=True
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    target_agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.agent_id"), nullable=False
    )
    target_agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.conversation_id"), nullable=False
    )
    target_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="agent", server_default="agent"
    )
    completion_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="turn", server_default="turn"
    )
    owner_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    creation_policy_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conversation_state: Mapped[str] = mapped_column(
        String, nullable=False, default="open", server_default="open"
    )
    turn_state: Mapped[str] = mapped_column(
        String, nullable=False, default="idle", server_default="idle"
    )
    active_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notify_on_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    last_result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    handoff_state: Mapped[str | None] = mapped_column(String, nullable=True)
    handoff_target_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    handoff_controller_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    handoff_controller_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    handoff_tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    control_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ManagedConversationSignal(Base):
    """Durable explicit or lifecycle signal from a managed child."""

    __tablename__ = "managed_conversation_signals"
    __table_args__ = (
        Index("ix_managed_conversation_signals_link_state", "link_id", "state"),
        Index("ix_managed_conversation_signals_owner", "link_id", "owner_epoch"),
        UniqueConstraint(
            "link_id",
            "owner_epoch",
            "source_turn_id",
            "kind",
            name="uq_managed_signal_source_turn",
        ),
        UniqueConstraint(
            "resume_request_id",
            name="uq_managed_signal_resume_request",
        ),
    )

    signal_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"mcsig_{uuid.uuid4().hex[:24]}"
    )
    link_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("managed_conversation_links.link_id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_prepared_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    resume_admitted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    resume_terminal_status: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="explicit", server_default="explicit"
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    wait: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    memory_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ManagedChannelBinding(Base):
    """Thin external-channel route bound to a managed conversation link."""

    __tablename__ = "managed_channel_bindings"
    __table_args__ = (
        UniqueConstraint("link_id", name="uq_managed_channel_bindings_link"),
        UniqueConstraint("active_route_key", name="uq_managed_channel_bindings_active_route"),
        Index("ix_managed_channel_bindings_user_state", "user_email", "state"),
        Index("ix_managed_channel_bindings_expires", "expires_at"),
    )

    binding_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"mcb_{uuid.uuid4().hex[:24]}"
    )
    link_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("managed_conversation_links.link_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_key: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    active_route_key: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="provisioning", server_default="provisioning"
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    safety_guidance: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    explicit_tool_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    terminal_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_lease_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_lease_owner_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class ChannelInboundLedgerRow(Base):
    """Normalized durable inbound identity used by managed-channel consumers."""

    __tablename__ = "channel_inbound_ledger"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "chat_id",
            "thread_key",
            "message_id",
            name="uq_channel_inbound_ledger_message",
        ),
        Index(
            "ix_channel_inbound_ledger_context",
            "account_id",
            "chat_id",
            "thread_key",
            "ordering_key",
            "message_id",
        ),
        Index("ix_channel_inbound_ledger_binding_state", "binding_id", "disposition"),
        Index("ix_channel_inbound_ledger_retention", "binding_id", "retain_until"),
    )

    inbound_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"chin_{uuid.uuid4().hex[:24]}"
    )
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    binding_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("managed_channel_bindings.binding_id", ondelete="SET NULL"),
        nullable=True,
    )
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_key: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    message_id: Mapped[str] = mapped_column(String, nullable=False)
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ordering_key: Mapped[str] = mapped_column(String, nullable=False)
    ordering_source: Mapped[str] = mapped_column(
        String, nullable=False, default="observed", server_default="observed"
    )
    retain_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_bot_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_primary_input: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    disposition: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    platform_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ChannelContextConsumptionRow(Base):
    """Reservation or committed use of one inbound row by one conversation."""

    __tablename__ = "channel_context_consumptions"
    __table_args__ = (
        UniqueConstraint(
            "consumer_conversation_id",
            "inbound_id",
            name="uq_channel_context_consumptions_consumer_inbound",
        ),
        Index("ix_channel_context_consumptions_reservation", "state", "reserved_until"),
        Index(
            "ix_channel_context_consumptions_admission",
            "admitted_turn_id",
            "state",
        ),
    )

    consumption_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"chctx_{uuid.uuid4().hex[:24]}"
    )
    consumer_conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    inbound_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_inbound_ledger.inbound_id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="reserved", server_default="reserved"
    )
    usage: Mapped[str] = mapped_column(
        String, nullable=False, default="context", server_default="context"
    )
    trigger_inbound_id: Mapped[str | None] = mapped_column(String, nullable=True)
    admitted_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reservation_token: Mapped[str] = mapped_column(String, nullable=False)
    reserved_until: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    committed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


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
    __table_args__ = (
        Index("ix_llm_providers_owner_provider", "owner_email", "provider_id"),
        Index("ix_llm_providers_owner_default", "owner_email", "is_default"),
        UniqueConstraint(
            "managed_local_key",
            name="uq_llm_providers_managed_local_key",
        ),
    )

    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    backend: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str] = mapped_column(
        String, nullable=False, default="system@cognis.local", server_default="system@cognis.local"
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    managed_local_key: Mapped[str | None] = mapped_column(String, nullable=True)
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
    __table_args__ = (
        UniqueConstraint("owner_email", "task_type", name="uq_model_routing_owner_task"),
        Index("ix_model_routing_owner_task", "owner_email", "task_type"),
    )

    route_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: f"route_{uuid.uuid4().hex[:12]}"
    )
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str] = mapped_column(
        String, nullable=False, default="system@cognis.local", server_default="system@cognis.local"
    )
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


class LLMProviderAuthSession(Base):
    """Durable executor-routed provider auth/setup session state."""

    __tablename__ = "llm_provider_auth_sessions"
    __table_args__ = (Index("ix_llm_provider_auth_owner_provider", "owner_email", "provider_id"),)

    setup_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    owner_email: Mapped[str] = mapped_column(String, nullable=False)
    actor_email: Mapped[str] = mapped_column(String, nullable=False)
    executor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="created")
    credential_id: Mapped[str] = mapped_column(String, nullable=False)
    credential_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class Task(Base):
    """Durable work items with queue semantics and workflow state.

    Tasks own workflow execution state directly via the workflow_state
    JSONB column. There is no separate workflow_runs table in MVP.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_owner_updated", "created_by", "updated_at", "task_id"),
        Index("ix_tasks_owner_status_updated", "created_by", "status", "updated_at", "task_id"),
        Index("ix_tasks_owner_agent_updated", "created_by", "agent_id", "updated_at", "task_id"),
        Index(
            "ix_tasks_owner_project_updated",
            "created_by",
            "project_id",
            "updated_at",
            "task_id",
        ),
        Index(
            "ix_tasks_owner_workflow_updated",
            "created_by",
            "workflow_id",
            "updated_at",
            "task_id",
        ),
        Index(
            "ux_tasks_control_conversation_id",
            "control_conversation_id",
            unique=True,
        ),
        Index(
            "ix_tasks_owner_source_ref",
            "created_by",
            "source_ref",
            "task_id",
        ),
        Index(
            "ix_tasks_owner_source_session",
            "created_by",
            "source_session_id",
            "task_id",
        ),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="api")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_mode: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="preferred_channel",
        server_default="preferred_channel",
    )
    delivery_target: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_mode_family: Mapped[str] = mapped_column(
        String, nullable=False, default="default", server_default="default"
    )
    allow_silent_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    interaction_mode_override: Mapped[str | None] = mapped_column(String, nullable=True)
    session_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    control_conversation_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    control_conversation_claimed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
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
    active_executor_assigned_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    active_executor_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    active_executor_source: Mapped[str | None] = mapped_column(String, nullable=True)
    active_executor_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    active_executor_unavailable_since: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
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


class ExecutorPinTransitionRow(Base):
    """Durable CAS ledger for an executor pin generation transition."""

    __tablename__ = "executor_pin_transitions"
    transition_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_executor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    new_executor_id: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state_caveat: Mapped[str] = mapped_column(Text, nullable=False)
    notice_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    notice_appended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_id", "generation", name="uq_executor_pin_transition_generation"
        ),
        Index("ix_executor_pin_transitions_notice_pending", "notice_appended_at"),
    )


class ExecutorPinNoticeOutboxRow(Base):
    """Recovery queue for a notice whose canonical history append failed."""

    __tablename__ = "executor_pin_notice_outbox"
    outbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    transition_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("executor_pin_transitions.transition_id", ondelete="CASCADE"),
        unique=True,
    )
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    intaris_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_email: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_executor_pin_notice_outbox_pending", "delivered_at"),)


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
        Boolean, nullable=False, default=False, server_default=false()
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
    agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
    __allow_unmapped__ = True
    __table_args__ = (
        UniqueConstraint("step_run_id", "version", name="uq_deliverables_step_run_version"),
        UniqueConstraint(
            "conversation_id",
            "session_id",
            "turn_id",
            "version",
            name="uq_deliverables_conversation_scope_version",
        ),
        Index("ix_deliverables_step_run", "step_run_id"),
        Index("ix_deliverables_conversation_scope", "conversation_id", "session_id", "turn_id"),
        Index("ix_deliverables_status", "status"),
    )

    deliverable_id: Mapped[str] = mapped_column(String, primary_key=True)
    step_run_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("step_runs.step_run_id", ondelete="CASCADE"),
        nullable=True,
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=True,
    )
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    storage_namespace: Mapped[str] = mapped_column(
        String, nullable=False, default="deliverables", server_default="deliverables"
    )
    storage_object_id: Mapped[str] = mapped_column(String, nullable=False)
    content_key: Mapped[str] = mapped_column(
        String, nullable=False, default="content.md", server_default="content.md"
    )
    content_mime: Mapped[str] = mapped_column(String, nullable=False)
    content_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False, default="markdown")
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    rich_key: Mapped[str | None] = mapped_column(String, nullable=True)
    rich_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rich_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    outputs_key: Mapped[str | None] = mapped_column(String, nullable=True)
    outputs_mime: Mapped[str | None] = mapped_column(String, nullable=True)
    outputs_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outputs_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    render_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    export_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    html_cache_key: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_cache_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="buffered")
    evaluator_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    _content_cache: str | None = None
    _outputs_cache: dict[str, Any] | None = None
    _rich_payload_cache: dict[str, Any] | None = None

    @property
    def content(self) -> str:
        return self._content_cache or ""

    @content.setter
    def content(self, value: str | None) -> None:
        self._content_cache = value or ""

    @property
    def outputs(self) -> dict[str, Any] | None:
        return self._outputs_cache

    @outputs.setter
    def outputs(self, value: dict[str, Any] | None) -> None:
        self._outputs_cache = value or {}

    @property
    def rich_payload(self) -> dict[str, Any] | None:
        return self._rich_payload_cache

    @rich_payload.setter
    def rich_payload(self, value: dict[str, Any] | None) -> None:
        self._rich_payload_cache = value


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
    agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
        Boolean, nullable=False, default=False, server_default=false()
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


class ScheduleFireRow(Base):
    """Durable identity and reconciliation state for one logical schedule fire."""

    __tablename__ = "schedule_fires"

    fire_id: Mapped[str] = mapped_column(String, primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        String, ForeignKey("schedules.schedule_id", ondelete="CASCADE"), nullable=False
    )
    fire_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="recurring", server_default="recurring"
    )
    scheduled_fire_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="claimed")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "fire_kind",
            "scheduled_fire_at",
            name="uq_schedule_fires_logical_fire",
        ),
        Index("ix_schedule_fires_reconcile", "status", "updated_at"),
        Index("ix_schedule_fires_task", "task_id"),
        CheckConstraint(
            "status IN ('claimed', 'dispatched', 'skipped', 'failed')",
            name="ck_schedule_fires_status",
        ),
        CheckConstraint(
            "fire_kind IN ('recurring', 'manual')",
            name="ck_schedule_fires_fire_kind",
        ),
    )


class ScheduleCatchupStateRow(Base):
    """Durable cluster-wide startup catch-up budget."""

    __tablename__ = "schedule_catchup_state"

    catchup_id: Mapped[str] = mapped_column(String, primary_key=True)
    cutoff_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    remaining_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_schedule_catchup_state_status",
        ),
    )


class ChannelAccountOperationRow(Base):
    """Durable in-flight operation count for channel ownership drain."""

    __tablename__ = "channel_account_operations"

    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channel_accounts.account_id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


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
    noop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    target_step: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
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
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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


class LocalModelDeployment(Base):
    """Declarative desired state for one local model across concrete executors."""

    __tablename__ = "local_model_deployments"
    __table_args__ = (
        CheckConstraint("runtime_type = 'ollama'", name="ck_local_model_deployment_runtime"),
        CheckConstraint(
            "source IN ('ollama', 'huggingface')",
            name="ck_local_model_deployment_source",
        ),
        CheckConstraint(
            "desired_state IN ('present', 'absent')",
            name="ck_local_model_deployment_desired_state",
        ),
        CheckConstraint(
            "update_policy IN ('if_changed', 'always', 'manual')",
            name="ck_local_model_deployment_update_policy",
        ),
        CheckConstraint(
            "prune_policy IN ('retain', 'delete')",
            name="ck_local_model_deployment_prune_policy",
        ),
        CheckConstraint("max_parallel > 0", name="ck_local_model_deployment_max_parallel"),
        CheckConstraint("generation > 0", name="ck_local_model_deployment_generation"),
        CheckConstraint(
            "capacity_assessment_generation IS NULL OR capacity_assessment_generation >= 0",
            name="ck_local_model_deployment_capacity_generation",
        ),
        Index(
            "ix_local_model_deployments_owner_updated",
            "owner_email",
            "updated_at",
        ),
        Index(
            "ix_local_model_deployments_provider",
            "provider_id",
        ),
        Index(
            "ix_local_model_deployments_reconcile_requested",
            "reconcile_requested_at",
        ),
    )

    deployment_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_type: Mapped[str] = mapped_column(
        String, nullable=False, default="ollama", server_default="ollama"
    )
    requested_ref: Mapped[str] = mapped_column(String, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    runtime_name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    digest: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
    selector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String, nullable=False, default="present", server_default="present"
    )
    update_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="if_changed", server_default="if_changed"
    )
    prune_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="retain", server_default="retain"
    )
    max_parallel: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    provider_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("llm_providers.provider_id", ondelete="SET NULL"),
        nullable=True,
    )
    capacity_override_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    capacity_assessment_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reconcile_requested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class LocalModelOperation(Base):
    """Durable future executor operation for a local-model target."""

    __tablename__ = "local_model_operations"
    __table_args__ = (
        CheckConstraint(
            "action IN ('pull', 'delete')",
            name="ck_local_model_operation_action",
        ),
        CheckConstraint(
            "state IN "
            "('queued', 'running', 'cancel_requested', 'succeeded', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_local_model_operation_state",
        ),
        CheckConstraint("generation > 0", name="ck_local_model_operation_generation"),
        CheckConstraint("progress_seq >= 0", name="ck_local_model_operation_progress_seq"),
        CheckConstraint(
            "progress_bytes >= 0",
            name="ck_local_model_operation_progress_bytes",
        ),
        UniqueConstraint(
            "deployment_id",
            "idempotency_key",
            name="uq_local_model_operation_idempotency",
        ),
        Index(
            "ix_local_model_operations_deployment_state",
            "deployment_id",
            "state",
            "created_at",
        ),
        Index(
            "ix_local_model_operations_executor_state",
            "executor_id",
            "state",
            "created_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    deployment_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("local_model_deployments.deployment_id", ondelete="CASCADE"),
        nullable=False,
    )
    executor_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("executors.executor_id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default="queued"
    )
    progress_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    progress_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    post_pull_provider_upsert: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    sanitized_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class LocalModelTargetStatus(Base):
    """Observed reconciliation state for one deployment/executor pair."""

    __tablename__ = "local_model_target_statuses"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'reconciling', 'ready', 'absent', 'blocked', 'error')",
            name="ck_local_model_target_state",
        ),
        CheckConstraint("generation > 0", name="ck_local_model_target_generation"),
        CheckConstraint(
            "observed_generation >= 0",
            name="ck_local_model_target_observed_generation",
        ),
        CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_local_model_target_observed_size",
        ),
        UniqueConstraint(
            "deployment_id",
            "executor_id",
            name="uq_local_model_target_deployment_executor",
        ),
        Index(
            "ix_local_model_targets_deployment_state",
            "deployment_id",
            "state",
        ),
        Index(
            "ix_local_model_targets_executor_state",
            "executor_id",
            "state",
        ),
    )

    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    deployment_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("local_model_deployments.deployment_id", ondelete="CASCADE"),
        nullable=False,
    )
    executor_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("executors.executor_id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    observed_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_operation_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("local_model_operations.operation_id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconcile_requested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    reconcile_started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
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
    auth_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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


class MCPOAuthTokenRow(Base):
    """Encrypted per-user OAuth tokens for HTTP MCP servers."""

    __tablename__ = "mcp_oauth_tokens"

    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    mcp_server_id: Mapped[str] = mapped_column(
        String, ForeignKey("mcp_servers.server_id"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    token_type: Mapped[str] = mapped_column(String, nullable=False, default="Bearer")
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_refresh_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    refresh_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_refresh_attempt_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_refresh_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_refresh_error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_refresh_error_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "user_email",
            "mcp_server_id",
            "issuer",
            "resource_key",
            name="uq_mcp_oauth_token_scope",
        ),
        Index("ix_mcp_oauth_tokens_user_server", "user_email", "mcp_server_id"),
        Index(
            "ix_mcp_oauth_tokens_refresh_due",
            "status",
            "next_refresh_attempt_at",
            "expires_at",
        ),
    )


class MCPOAuthTransactionRow(Base):
    """Pending OAuth authorization transactions with PKCE state."""

    __tablename__ = "mcp_oauth_transactions"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    mcp_server_id: Mapped[str] = mapped_column(
        String, ForeignKey("mcp_servers.server_id"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_server: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    state_hash: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    step_name: Mapped[str | None] = mapped_column(String, nullable=True)
    step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notification_id: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_cleanup_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    terminal_notification_resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    terminal_reconfigure_applied_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    terminal_reconfigure_completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_mcp_oauth_transactions_user_server", "user_email", "mcp_server_id"),
        Index("ix_mcp_oauth_transactions_status_expiry", "status", "expires_at"),
        Index(
            "ix_mcp_oauth_transactions_terminal_cleanup",
            "status",
            "terminal_cleanup_required",
            "terminal_notification_resolved_at",
            "terminal_reconfigure_applied_at",
            "terminal_reconfigure_completed_at",
        ),
    )


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
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "follow_up_id", name="uq_follow_up_dedupe_pair"),
        Index("ix_follow_up_dedupe_expires", "expires_at"),
        Index("ix_follow_up_dedupe_lease", "status", "lease_expires_at"),
        Index("ix_follow_up_dedupe_conversation", "conversation_id"),
    )


class FollowUpIntentRow(Base):
    """Durable idempotent intent for a follow-up turn."""

    __tablename__ = "follow_up_intents"

    intent_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    follow_up_id: Mapped[str] = mapped_column(String, nullable=False)
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
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
        UniqueConstraint(
            "conversation_id",
            "follow_up_id",
            name="uq_follow_up_intents_pair",
        ),
        Index("ix_follow_up_intents_status_updated", "status", "updated_at"),
        Index("ix_follow_up_intents_lease", "status", "lease_expires_at"),
    )


class ChannelDeliveryOutboxRow(Base):
    """Durable outbox for background/system channel follow-up sends.

    Stores delivery metadata and retry-safe channel payloads. User-authored
    conversation input is never persisted here.
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
    reply_to_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direct_turn_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direct_turn_fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    managed_binding_id: Mapped[str | None] = mapped_column(String, nullable=True)
    managed_binding_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    managed_owner_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    fallback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    projected_chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projection_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    inflight_chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inflight_idempotent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attachments_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    delivery_receipts_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    first_delivered_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    deliverable_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
        Index(
            "ix_channel_delivery_managed_fence",
            "managed_binding_id",
            "managed_binding_version",
            "managed_owner_epoch",
        ),
    )


class ChannelDeliveryReceiptRow(Base):
    """One externally acknowledged outbox chunk."""

    __tablename__ = "channel_delivery_receipts"
    __table_args__ = (
        Index("ix_channel_delivery_receipts_sent", "sent_at", "delivery_id", "chunk_index"),
    )

    delivery_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channel_delivery_outbox.delivery_id", ondelete="CASCADE"),
        primary_key=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    external_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attachments_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
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
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.agent_id"), nullable=False)
    default_agent_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credential_refs: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    # Routing
    default_conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    allow_new_conversations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    preferred_for_task_delivery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
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
        Boolean, nullable=False, default=False, server_default=false()
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


class ChannelObservedTargetRow(Base):
    """Controller-observed channel destination available to outbound tools."""

    __tablename__ = "channel_observed_targets"

    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", name="uq_channel_observed_target"),
        Index("ix_channel_observed_targets_owner", "user_email", "last_observed_at"),
    )


class ChannelRecipientIntentRow(Base):
    """Durable explicit-recipient resolution and delivery intent."""

    __tablename__ = "channel_recipient_intents"
    __table_args__ = (
        UniqueConstraint("user_email", "intent_id", name="uq_channel_recipient_intent_owner"),
        Index("ix_channel_recipient_intents_state", "resolution_state", "updated_at"),
        Index("ix_channel_recipient_intents_route", "account_id", "provisional_route_key"),
    )

    intent_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, ForeignKey("users.email", ondelete="CASCADE"))
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("channel_accounts.account_id", ondelete="CASCADE")
    )
    channel_type: Mapped[str] = mapped_column(String, nullable=False)
    address_kind: Mapped[str] = mapped_column(String, nullable=False)
    normalized_address: Mapped[str] = mapped_column(String, nullable=False)
    chat_kind: Mapped[str] = mapped_column(String, nullable=False)
    allow_resolution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_creation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provisional_route_key: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    conversation_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    idempotency_key: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    idempotency_scope: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    authorized_artifacts_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )
    resolution_lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    resolution_state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    side_effect_certainty: Mapped[str] = mapped_column(
        String, nullable=False, default="none", server_default="none"
    )
    resolved_route_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    safe_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
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
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_anchor: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_artifacts_owner_status", "owner_email", "status"),
        Index("ix_artifacts_conversation", "conversation_id"),
        Index("ix_artifacts_expiry", "expires_at"),
        Index("ix_artifacts_tool_source", "owner_email", "source_tool_call_id", "source_anchor"),
    )


class KnowledgebaseRow(Base):
    """User-owned artifact-backed knowledgebase."""

    __tablename__ = "knowledgebases"

    knowledgebase_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    metadata_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_knowledgebases_owner_status", "owner_email", "status"),
        Index("ix_knowledgebases_owner_name", "owner_email", "name"),
    )


class KnowledgebaseGrantRow(Base):
    """Direct user read/query grant for a knowledgebase."""

    __tablename__ = "knowledgebase_grants"
    __table_args__ = (
        Index(
            "uq_knowledgebase_grants_active_user",
            "knowledgebase_id",
            "grantee_user_email",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_knowledgebase_grants_grantee", "grantee_user_email"),
        Index("ix_knowledgebase_grants_kb", "knowledgebase_id"),
        CheckConstraint("permission = 'view'", name="ck_knowledgebase_grants_permission"),
    )

    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledgebase_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledgebases.knowledgebase_id", ondelete="CASCADE"), nullable=False
    )
    grantee_user_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email"), nullable=False
    )
    permission: Mapped[str] = mapped_column(
        String, nullable=False, default="view", server_default="view"
    )
    granted_by: Mapped[str] = mapped_column(String, ForeignKey("users.email"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class KnowledgebaseArtifactRow(Base):
    """Artifact attachment and indexing status for a knowledgebase."""

    __tablename__ = "knowledgebase_artifacts"

    kb_artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledgebase_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledgebases.knowledgebase_id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_source_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    active_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    desired_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    source_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    active_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "active_metadata", JSON, nullable=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vector_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attached_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    indexed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_kb_artifacts_kb_status", "knowledgebase_id", "status"),
        Index("ix_kb_artifacts_artifact", "artifact_id"),
        Index("ix_kb_artifacts_pending_artifact", "pending_artifact_id"),
        Index(
            "uq_kb_artifacts_live_source_path",
            "knowledgebase_id",
            "source_path",
            unique=True,
            sqlite_where=text("source_path IS NOT NULL AND status NOT IN ('detached', 'removed')"),
            postgresql_where=text(
                "source_path IS NOT NULL AND status NOT IN ('detached', 'removed')"
            ),
        ),
    )


class KnowledgebaseChunkRow(Base):
    """Disposable indexed chunk derived from a canonical artifact."""

    __tablename__ = "knowledgebase_chunks"

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledgebase_id: Mapped[str] = mapped_column(String, nullable=False)
    kb_artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    vector_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("ix_kb_chunks_kb_artifact", "knowledgebase_id", "artifact_id"),
        Index(
            "ix_kb_chunks_attachment_generation_index",
            "kb_artifact_id",
            "generation",
            "chunk_index",
        ),
    )


class KnowledgebaseIndexJobRow(Base):
    """Persistent background indexing job."""

    __tablename__ = "knowledgebase_index_jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledgebase_id: Mapped[str] = mapped_column(String, nullable=False)
    kb_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chunks_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_kb_jobs_status_queue", "status", "priority", "queued_at"),
        Index("ix_kb_jobs_kb_status", "knowledgebase_id", "status"),
        Index(
            "uq_kb_jobs_live_attachment_generation_type",
            "kb_artifact_id",
            "generation",
            "job_type",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )
