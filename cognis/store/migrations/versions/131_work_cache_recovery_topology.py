"""Add durable Work cache topology recovery state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "131_work_cache_recovery_topology"
down_revision: str | Sequence[str] | None = "130_work_reconstructable_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GENERATION_STATES = (
    "state IN ('missing','recovering','evidence_ready','ready','stale','failed',"
    "'source_unavailable')"
)


def upgrade() -> None:
    columns = {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("work_scope_states")
    }
    checks = {
        str(item["name"]): str(item.get("sqltext") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints("work_scope_states")
        if item.get("name")
    }
    with op.batch_alter_table("work_scope_states") as batch:
        if "recovery_priority" not in columns:
            batch.add_column(
                sa.Column(
                    "recovery_priority",
                    sa.Integer(),
                    server_default="0",
                    nullable=False,
                )
            )
        cache_check = checks.get("ck_work_scope_states_cache_nonnegative", "")
        if "recovery_priority" not in cache_check:
            if cache_check:
                batch.drop_constraint("ck_work_scope_states_cache_nonnegative", type_="check")
            batch.create_check_constraint(
                "ck_work_scope_states_cache_nonnegative",
                "retry_count >= 0 AND lease_fence >= 0 "
                "AND reconciliation_count >= 0 AND recovery_priority >= 0 "
                "AND expected_generation_count >= 0 AND expected_stream_count >= 0 "
                "AND expected_projection_count >= 0 AND expected_record_count >= 0 "
                "AND expected_current_file_count >= 0",
            )

    generation_checks = {
        str(item["name"]): str(item.get("sqltext") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints("work_scope_generations")
        if item.get("name")
    }
    if "evidence_ready" not in generation_checks.get("ck_work_scope_generations_state", ""):
        with op.batch_alter_table("work_scope_generations") as batch:
            if "ck_work_scope_generations_state" in generation_checks:
                batch.drop_constraint("ck_work_scope_generations_state", type_="check")
            batch.create_check_constraint("ck_work_scope_generations_state", GENERATION_STATES)

    if "work_scope_generation_frontier" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "work_scope_generation_frontier",
            sa.Column("scope_key", sa.String(), nullable=False),
            sa.Column("cache_epoch", sa.String(), nullable=False),
            sa.Column("entity_kind", sa.String(), nullable=False),
            sa.Column("entity_id", sa.String(), nullable=False),
            sa.Column("state", sa.String(), server_default="pending", nullable=False),
            sa.Column("parent_session_id", sa.String(), nullable=True),
            sa.Column("edge_kind", sa.String(), nullable=False),
            sa.Column("discovered_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("cursor_payload", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "state IN ('pending','visited')",
                name="ck_work_scope_generation_frontier_state",
            ),
            sa.CheckConstraint(
                "discovered_ordinal >= 0",
                name="ck_work_scope_generation_frontier_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["scope_key", "cache_epoch"],
                ["work_scope_generations.scope_key", "work_scope_generations.cache_epoch"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("scope_key", "cache_epoch", "entity_kind", "entity_id"),
        )
    indexes = {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes("work_scope_generation_frontier")
        if item.get("name")
    }
    if "ix_work_scope_generation_frontier_pending" not in indexes:
        op.create_index(
            "ix_work_scope_generation_frontier_pending",
            "work_scope_generation_frontier",
            ["scope_key", "cache_epoch", "state", "discovered_ordinal"],
        )


def downgrade() -> None:
    op.drop_table("work_scope_generation_frontier")
    with op.batch_alter_table("work_scope_generations") as batch:
        batch.drop_constraint("ck_work_scope_generations_state", type_="check")
        batch.create_check_constraint(
            "ck_work_scope_generations_state",
            "state IN ('missing','recovering','ready','stale','failed','source_unavailable')",
        )
    with op.batch_alter_table("work_scope_states") as batch:
        batch.drop_constraint("ck_work_scope_states_cache_nonnegative", type_="check")
        batch.create_check_constraint(
            "ck_work_scope_states_cache_nonnegative",
            "retry_count >= 0 AND lease_fence >= 0 AND reconciliation_count >= 0 "
            "AND expected_generation_count >= 0 AND expected_stream_count >= 0 "
            "AND expected_projection_count >= 0 AND expected_record_count >= 0 "
            "AND expected_current_file_count >= 0",
        )
        batch.drop_column("recovery_priority")
