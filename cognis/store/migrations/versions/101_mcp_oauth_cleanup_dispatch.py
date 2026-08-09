"""Add MCP OAuth cleanup completion marker.

Revision ID: 101_mcp_oauth_cleanup_dispatch
Revises: 100_mcp_oauth_terminal_cleanup
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "101_mcp_oauth_cleanup_dispatch"
down_revision: str | None = "100_mcp_oauth_terminal_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_oauth_transactions",
        sa.Column(
            "terminal_reconfigure_completed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.drop_index(
        "ix_mcp_oauth_transactions_terminal_cleanup",
        table_name="mcp_oauth_transactions",
    )
    op.create_index(
        "ix_mcp_oauth_transactions_terminal_cleanup",
        "mcp_oauth_transactions",
        [
            "status",
            "terminal_cleanup_required",
            "terminal_notification_resolved_at",
            "terminal_reconfigure_applied_at",
            "terminal_reconfigure_completed_at",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_oauth_transactions_terminal_cleanup",
        table_name="mcp_oauth_transactions",
    )
    op.create_index(
        "ix_mcp_oauth_transactions_terminal_cleanup",
        "mcp_oauth_transactions",
        [
            "status",
            "terminal_cleanup_required",
            "terminal_notification_resolved_at",
            "terminal_reconfigure_applied_at",
        ],
    )
    op.drop_column(
        "mcp_oauth_transactions",
        "terminal_reconfigure_completed_at",
    )
