"""Add owner scope to LLM providers and routing.

Revision ID: 056_llm_provider_owner
Revises: 055_conversation_starred_at
Create Date: 2026-01-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "056_llm_provider_owner"
down_revision = "055_conversation_starred_at"
branch_labels = None
depends_on = None

SYSTEM_OWNER = "system@cognis.local"


def _sqlite_upgrade_model_routing_primary_key() -> None:
    # SQLite cannot drop and recreate primary keys in-place, so rebuild the
    # table explicitly while preserving existing route rows.
    op.create_table(
        "model_routing_new",
        sa.Column("route_id", sa.String(), primary_key=True),
        sa.Column("task_type", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False, server_default=SYSTEM_OWNER),
        sa.Column(
            "provider_id", sa.String(), sa.ForeignKey("llm_providers.provider_id"), nullable=True
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_email", "task_type", name="uq_model_routing_owner_task"),
    )
    op.execute(
        """
        INSERT INTO model_routing_new (route_id, task_type, owner_email, provider_id, model, config, updated_at)
        SELECT route_id, task_type, owner_email, provider_id, model, config, updated_at
        FROM model_routing
        """
    )
    op.drop_table("model_routing")
    op.rename_table("model_routing_new", "model_routing")


def _sqlite_downgrade_model_routing_primary_key() -> None:
    # Owner-scoped rows collapse back to the legacy task_type primary key.
    # Prefer shared/system rows when multiple owners define the same route.
    op.create_table(
        "model_routing_old",
        sa.Column("task_type", sa.String(), primary_key=True),
        sa.Column(
            "provider_id", sa.String(), sa.ForeignKey("llm_providers.provider_id"), nullable=True
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.execute(
        f"""
        INSERT OR IGNORE INTO model_routing_old (task_type, provider_id, model, config, updated_at)
        SELECT task_type, provider_id, model, config, updated_at
        FROM model_routing
        ORDER BY CASE WHEN owner_email = '{SYSTEM_OWNER}' THEN 0 ELSE 1 END, task_type
        """
    )
    op.drop_table("model_routing")
    op.rename_table("model_routing_old", "model_routing")


def upgrade() -> None:
    with op.batch_alter_table("llm_providers") as batch:
        batch.add_column(
            sa.Column("owner_email", sa.String(), nullable=False, server_default=SYSTEM_OWNER)
        )
    with op.batch_alter_table("model_routing") as batch:
        batch.add_column(sa.Column("route_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("owner_email", sa.String(), nullable=False, server_default=SYSTEM_OWNER)
        )
    op.execute("UPDATE model_routing SET route_id = 'route_' || task_type WHERE route_id IS NULL")
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_upgrade_model_routing_primary_key()
    else:
        with op.batch_alter_table("model_routing") as batch:
            batch.alter_column("route_id", nullable=False)
            batch.drop_constraint("model_routing_pkey", type_="primary")
            batch.create_primary_key("model_routing_pkey", ["route_id"])
            batch.create_unique_constraint(
                "uq_model_routing_owner_task", ["owner_email", "task_type"]
            )
    op.create_index("ix_llm_providers_owner_email", "llm_providers", ["owner_email"])
    op.create_index("ix_model_routing_owner_email", "model_routing", ["owner_email"])
    op.create_index(
        "ix_llm_providers_owner_provider", "llm_providers", ["owner_email", "provider_id"]
    )
    op.create_index(
        "ix_llm_providers_owner_default", "llm_providers", ["owner_email", "is_default"]
    )
    op.create_index("ix_model_routing_owner_task", "model_routing", ["owner_email", "task_type"])
    op.create_table(
        "llm_provider_auth_sessions",
        sa.Column("setup_id", sa.String(), primary_key=True),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("executor_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False),
        sa.Column("credential_version_before", sa.Integer(), nullable=True),
        sa.Column("status_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_llm_provider_auth_owner_provider",
        "llm_provider_auth_sessions",
        ["owner_email", "provider_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_provider_auth_owner_provider", table_name="llm_provider_auth_sessions")
    op.drop_table("llm_provider_auth_sessions")
    op.drop_index("ix_model_routing_owner_task", table_name="model_routing")
    op.drop_index("ix_llm_providers_owner_default", table_name="llm_providers")
    op.drop_index("ix_llm_providers_owner_provider", table_name="llm_providers")
    op.drop_index("ix_model_routing_owner_email", table_name="model_routing")
    op.drop_index("ix_llm_providers_owner_email", table_name="llm_providers")
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_downgrade_model_routing_primary_key()
    else:
        with op.batch_alter_table("model_routing") as batch:
            batch.drop_constraint("uq_model_routing_owner_task", type_="unique")
            batch.drop_constraint("model_routing_pkey", type_="primary")
            batch.create_primary_key("model_routing_pkey", ["task_type"])
            batch.drop_column("route_id")
            batch.drop_column("owner_email")
    with op.batch_alter_table("llm_providers") as batch:
        batch.drop_column("owner_email")
