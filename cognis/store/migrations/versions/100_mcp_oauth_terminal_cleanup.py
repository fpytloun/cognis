"""Add MCP OAuth terminal cleanup markers.

Revision ID: 100_mcp_oauth_terminal_cleanup
Revises: 099_controller_instances
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "100_mcp_oauth_terminal_cleanup"
down_revision: str | None = "099_controller_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_oauth_transactions",
        sa.Column(
            "terminal_cleanup_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "mcp_oauth_transactions",
        sa.Column(
            "terminal_notification_resolved_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "mcp_oauth_transactions",
        sa.Column(
            "terminal_reconfigure_applied_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
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


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_oauth_transactions_terminal_cleanup",
        table_name="mcp_oauth_transactions",
    )
    op.drop_column("mcp_oauth_transactions", "terminal_reconfigure_applied_at")
    op.drop_column("mcp_oauth_transactions", "terminal_notification_resolved_at")
    op.drop_column("mcp_oauth_transactions", "terminal_cleanup_required")
