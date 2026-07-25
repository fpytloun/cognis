"""Add bounded managed-conversation lineage.

Revision ID: 077_managed_conversation_lineage
Revises: 076_repair_legacy_deliverable_content_nullable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "077_managed_conversation_lineage"
down_revision = "076_repair_legacy_deliverable_content_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("managed_conversation_links")}
    indexes = {index["name"] for index in inspector.get_indexes("managed_conversation_links")}
    foreign_keys = {
        foreign_key["name"]
        for foreign_key in inspector.get_foreign_keys("managed_conversation_links")
    }

    with op.batch_alter_table("managed_conversation_links") as batch_op:
        if "parent_link_id" not in columns:
            batch_op.add_column(sa.Column("parent_link_id", sa.String(), nullable=True))
        if "root_link_id" not in columns:
            batch_op.add_column(sa.Column("root_link_id", sa.String(), nullable=True))
        if "depth" not in columns:
            batch_op.add_column(
                sa.Column("depth", sa.Integer(), nullable=False, server_default="1")
            )
        if "fk_managed_links_parent_link" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_managed_links_parent_link",
                "managed_conversation_links",
                ["parent_link_id"],
                ["link_id"],
            )
        if "fk_managed_links_root_link" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_managed_links_root_link",
                "managed_conversation_links",
                ["root_link_id"],
                ["link_id"],
            )
        if "ix_managed_conversation_links_parent_link" not in indexes:
            batch_op.create_index("ix_managed_conversation_links_parent_link", ["parent_link_id"])
        if "ix_managed_conversation_links_root_depth" not in indexes:
            batch_op.create_index(
                "ix_managed_conversation_links_root_depth", ["root_link_id", "depth"]
            )
    op.execute(
        sa.text(
            "UPDATE managed_conversation_links "
            "SET root_link_id = link_id, depth = 1 "
            "WHERE root_link_id IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("managed_conversation_links") as batch_op:
        batch_op.drop_index("ix_managed_conversation_links_root_depth")
        batch_op.drop_index("ix_managed_conversation_links_parent_link")
        batch_op.drop_constraint("fk_managed_links_root_link", type_="foreignkey")
        batch_op.drop_constraint("fk_managed_links_parent_link", type_="foreignkey")
        batch_op.drop_column("depth")
        batch_op.drop_column("root_link_id")
        batch_op.drop_column("parent_link_id")
