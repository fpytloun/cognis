"""Add MCP OAuth storage.

Revision ID: 060_mcp_oauth
Revises: 059_task_session_policy
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "060_mcp_oauth"
down_revision = "059_task_session_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("auth_config", sa.JSON(), nullable=True))
    op.create_table(
        "mcp_oauth_tokens",
        sa.Column("token_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("mcp_server_id", sa.String(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("token_type", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_refresh_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.server_id"]),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"]),
        sa.PrimaryKeyConstraint("token_id"),
        sa.UniqueConstraint(
            "user_email",
            "mcp_server_id",
            "issuer",
            "resource_key",
            name="uq_mcp_oauth_token_scope",
        ),
    )
    op.create_index(
        "ix_mcp_oauth_tokens_user_server",
        "mcp_oauth_tokens",
        ["user_email", "mcp_server_id"],
    )
    op.create_table(
        "mcp_oauth_transactions",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("mcp_server_id", sa.String(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("authorization_server", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.String(), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("step_name", sa.String(), nullable=True),
        sa.Column("step_run_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("notification_id", sa.String(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.server_id"]),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"]),
        sa.PrimaryKeyConstraint("transaction_id"),
    )
    op.create_index(
        "ix_mcp_oauth_transactions_user_server",
        "mcp_oauth_transactions",
        ["user_email", "mcp_server_id"],
    )
    op.create_index(
        "ix_mcp_oauth_transactions_status_expiry",
        "mcp_oauth_transactions",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_transactions_status_expiry", table_name="mcp_oauth_transactions")
    op.drop_index("ix_mcp_oauth_transactions_user_server", table_name="mcp_oauth_transactions")
    op.drop_table("mcp_oauth_transactions")
    op.drop_index("ix_mcp_oauth_tokens_user_server", table_name="mcp_oauth_tokens")
    op.drop_table("mcp_oauth_tokens")
    op.drop_column("mcp_servers", "auth_config")
