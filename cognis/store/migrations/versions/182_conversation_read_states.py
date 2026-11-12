"""Personal read markers for project-shared conversations."""

import sqlalchemy as sa
from alembic import op

revision = "182_conversation_read_states"
down_revision = "181_merge_document_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("conversation_read_states"):
        return
    op.create_table(
        "conversation_read_states",
        sa.Column(
            "user_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_read_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_read_states")
