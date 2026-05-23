"""Initial Cognis schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("email", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("personality", sa.JSON(), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("tools", sa.JSON(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=True),
        sa.Column("llm_config", sa.JSON(), nullable=True),
        sa.Column("execution", sa.JSON(), nullable=True),
        sa.Column("avatar_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("context_type", sa.String(), nullable=False),
        sa.Column("context_ref", sa.String(), nullable=True),
        sa.Column("context_data", sa.JSON(), nullable=True),
        sa.Column("memory_labels", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("root_session_id", sa.String(), nullable=True),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_session_id", sa.String(), sa.ForeignKey("sessions.session_id"), nullable=True
        ),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("delegation_mode", sa.String(), nullable=True),
        sa.Column("delegation_task", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("intaris_session_id", sa.String(), nullable=True),
        sa.Column("mnemory_session_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("result_content", sa.Text(), nullable=True),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), sa.ForeignKey("users.email"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "llm_providers",
        sa.Column("provider_id", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "model_routing",
        sa.Column("task_type", sa.String(), primary_key=True),
        sa.Column(
            "provider_id", sa.String(), sa.ForeignKey("llm_providers.provider_id"), nullable=True
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "secrets",
        sa.Column("secret_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="user"),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("user_email", "name", "scope", "agent_id", name="uq_secret"),
    )
    op.create_table(
        "audit_log",
        sa.Column("log_id", sa.String(), primary_key=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("secrets")
    op.drop_table("model_routing")
    op.drop_table("llm_providers")
    op.drop_table("settings")
    op.drop_table("sessions")
    op.drop_table("conversations")
    op.drop_table("agents")
    op.drop_table("api_keys")
    op.drop_table("users")
