"""Persist MCP OAuth refresh lifecycle state.

Revision ID: 081_mcp_oauth_refresh_lifecycle
Revises: 080_managed_turn_correlation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "081_mcp_oauth_refresh_lifecycle"
down_revision: str | Sequence[str] | None = "080_managed_turn_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("mcp_oauth_tokens")
    }
    with op.batch_alter_table("mcp_oauth_tokens") as batch_op:
        if "refresh_failure_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "refresh_failure_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "next_refresh_attempt_at" not in columns:
            batch_op.add_column(
                sa.Column("next_refresh_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
        if "last_refresh_error_code" not in columns:
            batch_op.add_column(sa.Column("last_refresh_error_code", sa.String(), nullable=True))
        if "last_refresh_error_description" not in columns:
            batch_op.add_column(
                sa.Column("last_refresh_error_description", sa.Text(), nullable=True)
            )
        if "last_refresh_error_at" not in columns:
            batch_op.add_column(
                sa.Column("last_refresh_error_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("mcp_oauth_tokens")}
    if "ix_mcp_oauth_tokens_refresh_due" not in indexes:
        op.create_index(
            "ix_mcp_oauth_tokens_refresh_due",
            "mcp_oauth_tokens",
            ["status", "next_refresh_attempt_at", "expires_at"],
        )


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("mcp_oauth_tokens")}
    if "ix_mcp_oauth_tokens_refresh_due" in indexes:
        op.drop_index("ix_mcp_oauth_tokens_refresh_due", table_name="mcp_oauth_tokens")
    with op.batch_alter_table("mcp_oauth_tokens") as batch_op:
        batch_op.drop_column("last_refresh_error_at")
        batch_op.drop_column("last_refresh_error_description")
        batch_op.drop_column("last_refresh_error_code")
        batch_op.drop_column("next_refresh_attempt_at")
        batch_op.drop_column("refresh_failure_count")
