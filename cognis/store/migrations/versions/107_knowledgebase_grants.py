"""Add direct user knowledgebase grants."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "107_knowledgebase_grants"
down_revision: str | Sequence[str] | None = "106_kb_index_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "knowledgebase_grants" not in inspector.get_table_names():
        op.create_table(
            "knowledgebase_grants",
            sa.Column("grant_id", sa.String(), primary_key=True),
            sa.Column(
                "knowledgebase_id",
                sa.String(),
                sa.ForeignKey("knowledgebases.knowledgebase_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "grantee_user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False
            ),
            sa.Column("permission", sa.String(), nullable=False, server_default="view"),
            sa.Column("granted_by", sa.String(), sa.ForeignKey("users.email"), nullable=False),
            sa.Column(
                "granted_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
            sa.Column("note", sa.Text()),
            sa.CheckConstraint("permission = 'view'", name="ck_knowledgebase_grants_permission"),
        )
    existing = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("knowledgebase_grants")
    }
    if "uq_knowledgebase_grants_active_user" not in existing:
        op.create_index(
            "uq_knowledgebase_grants_active_user",
            "knowledgebase_grants",
            ["knowledgebase_id", "grantee_user_email"],
            unique=True,
            sqlite_where=sa.text("revoked_at IS NULL"),
            postgresql_where=sa.text("revoked_at IS NULL"),
        )
    if "ix_knowledgebase_grants_grantee" not in existing:
        op.create_index(
            "ix_knowledgebase_grants_grantee", "knowledgebase_grants", ["grantee_user_email"]
        )
    if "ix_knowledgebase_grants_kb" not in existing:
        op.create_index("ix_knowledgebase_grants_kb", "knowledgebase_grants", ["knowledgebase_id"])


def downgrade() -> None:
    op.drop_table("knowledgebase_grants")
