"""Add declarative local-model desired state.

Revision ID: 087_local_model_foundation
Revises: 086_channel_delivery_attachments
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "087_local_model_foundation"
down_revision: str | Sequence[str] | None = "086_channel_delivery_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    expected_tables = {
        "local_model_deployments",
        "local_model_operations",
        "local_model_target_statuses",
    }
    present_tables = expected_tables.intersection(sa.inspect(op.get_bind()).get_table_names())
    if present_tables == expected_tables:
        # Startup bootstrap creates new tables from current ORM metadata. This
        # keeps a later Alembic stamp/upgrade safe for that supported path.
        return
    if present_tables:
        raise RuntimeError("partial local-model schema exists; refusing an unsafe migration retry")

    op.create_table(
        "local_model_deployments",
        sa.Column("deployment_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("runtime_type", sa.String(), server_default="ollama", nullable=False),
        sa.Column("requested_ref", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("runtime_name", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=True),
        sa.Column("revision", sa.String(), nullable=True),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("desired_state", sa.String(), server_default="present", nullable=False),
        sa.Column("update_policy", sa.String(), server_default="if_changed", nullable=False),
        sa.Column("prune_policy", sa.String(), server_default="retain", nullable=False),
        sa.Column("max_parallel", sa.Integer(), server_default="1", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column(
            "capacity_override_acknowledged",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("capacity_assessment_generation", sa.Integer(), nullable=True),
        sa.Column("reconcile_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "runtime_type = 'ollama'",
            name="ck_local_model_deployment_runtime",
        ),
        sa.CheckConstraint(
            "source IN ('ollama', 'huggingface')",
            name="ck_local_model_deployment_source",
        ),
        sa.CheckConstraint(
            "desired_state IN ('present', 'absent')",
            name="ck_local_model_deployment_desired_state",
        ),
        sa.CheckConstraint(
            "update_policy IN ('if_changed', 'always', 'manual')",
            name="ck_local_model_deployment_update_policy",
        ),
        sa.CheckConstraint(
            "prune_policy IN ('retain', 'delete')",
            name="ck_local_model_deployment_prune_policy",
        ),
        sa.CheckConstraint(
            "max_parallel > 0",
            name="ck_local_model_deployment_max_parallel",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_local_model_deployment_generation",
        ),
        sa.CheckConstraint(
            "capacity_assessment_generation IS NULL OR capacity_assessment_generation >= 0",
            name="ck_local_model_deployment_capacity_generation",
        ),
        sa.ForeignKeyConstraint(
            ["owner_email"],
            ["users.email"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["llm_providers.provider_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("deployment_id"),
    )
    op.create_index(
        "ix_local_model_deployments_owner_updated",
        "local_model_deployments",
        ["owner_email", "updated_at"],
    )
    op.create_index(
        "ix_local_model_deployments_provider",
        "local_model_deployments",
        ["provider_id"],
    )
    op.create_index(
        "ix_local_model_deployments_reconcile_requested",
        "local_model_deployments",
        ["reconcile_requested_at"],
    )

    op.create_table(
        "local_model_operations",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("deployment_id", sa.String(), nullable=False),
        sa.Column("executor_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="queued", nullable=False),
        sa.Column("progress_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('pull', 'delete')",
            name="ck_local_model_operation_action",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'cancel_requested', 'succeeded', "
            "'failed', 'cancelled', 'interrupted')",
            name="ck_local_model_operation_state",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_local_model_operation_generation",
        ),
        sa.CheckConstraint(
            "progress_seq >= 0",
            name="ck_local_model_operation_progress_seq",
        ),
        sa.CheckConstraint(
            "progress_bytes >= 0",
            name="ck_local_model_operation_progress_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["local_model_deployments.deployment_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["executor_id"],
            ["executors.executor_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint(
            "deployment_id",
            "idempotency_key",
            name="uq_local_model_operation_idempotency",
        ),
    )
    op.create_index(
        "ix_local_model_operations_deployment_state",
        "local_model_operations",
        ["deployment_id", "state", "created_at"],
    )
    op.create_index(
        "ix_local_model_operations_executor_state",
        "local_model_operations",
        ["executor_id", "state", "created_at"],
    )

    op.create_table(
        "local_model_target_statuses",
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("deployment_id", sa.String(), nullable=False),
        sa.Column("executor_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("observed_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("observed_digest", sa.String(), nullable=True),
        sa.Column("observed_size_bytes", sa.Integer(), nullable=True),
        sa.Column("current_operation_id", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reconcile_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'reconciling', 'ready', 'absent', 'blocked', 'error')",
            name="ck_local_model_target_state",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_local_model_target_generation",
        ),
        sa.CheckConstraint(
            "observed_generation >= 0",
            name="ck_local_model_target_observed_generation",
        ),
        sa.CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_local_model_target_observed_size",
        ),
        sa.ForeignKeyConstraint(
            ["current_operation_id"],
            ["local_model_operations.operation_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["local_model_deployments.deployment_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["executor_id"],
            ["executors.executor_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("target_id"),
        sa.UniqueConstraint(
            "deployment_id",
            "executor_id",
            name="uq_local_model_target_deployment_executor",
        ),
    )
    op.create_index(
        "ix_local_model_targets_deployment_state",
        "local_model_target_statuses",
        ["deployment_id", "state"],
    )
    op.create_index(
        "ix_local_model_targets_executor_state",
        "local_model_target_statuses",
        ["executor_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_local_model_targets_executor_state",
        table_name="local_model_target_statuses",
    )
    op.drop_index(
        "ix_local_model_targets_deployment_state",
        table_name="local_model_target_statuses",
    )
    op.drop_table("local_model_target_statuses")
    op.drop_index(
        "ix_local_model_operations_executor_state",
        table_name="local_model_operations",
    )
    op.drop_index(
        "ix_local_model_operations_deployment_state",
        table_name="local_model_operations",
    )
    op.drop_table("local_model_operations")
    op.drop_index(
        "ix_local_model_deployments_reconcile_requested",
        table_name="local_model_deployments",
    )
    op.drop_index(
        "ix_local_model_deployments_provider",
        table_name="local_model_deployments",
    )
    op.drop_index(
        "ix_local_model_deployments_owner_updated",
        table_name="local_model_deployments",
    )
    op.drop_table("local_model_deployments")
