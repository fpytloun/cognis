"""Content-free per-agent skill usage counters."""

import sqlalchemy as sa
from alembic import op

revision = "188_agent_skill_usage"
down_revision = "187_usage_api_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("agent_skill_usage"):
        return
    op.create_table(
        "agent_skill_usage",
        sa.Column(
            "agent_id",
            sa.String(),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "skill_id",
            sa.String(),
            sa.ForeignKey("skills.skill_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("load_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("search_load_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_loaded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_search_hit_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_skill_usage")
