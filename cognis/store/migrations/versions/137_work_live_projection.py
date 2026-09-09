"""Remove unreleased Work scope-generation cache tables.

Revision ID: 137_work_live_projection
Revises: 136_totp_mfa
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "137_work_live_projection"
down_revision = "136_totp_mfa"
branch_labels = None
depends_on = None

_DROPPED_TABLES = (
    "work_generation_current_file_snapshots",
    "work_generation_summary_snapshots",
    "work_invalidation_outbox",
    "work_scope_generation_frontier",
    "work_scope_generation_streams",
    "work_scope_streams",
    "work_scope_generations",
    "work_scope_states",
)


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_direct_turn_requests_session_latest "
        "ON direct_turn_requests (user_id, session_id, admission_order)"
    )
    for table_name in _DROPPED_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    if not sa.inspect(op.get_bind()).has_table("work_live_revisions"):
        op.create_table(
            "work_live_revisions",
            sa.Column("owner_email", sa.String(), nullable=False),
            sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "revision >= 0",
                name="ck_work_live_revisions_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["owner_email"],
                ["users.email"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("owner_email"),
        )
    op.execute(
        sa.text(
            """
            INSERT INTO work_session_projections (
                projection_id, owner_email, session_id, source_session_id,
                materializer_version, target_seq, covered_through_seq, state,
                retry_count, lease_fence, priority, created_at, updated_at,
                next_head_check_at, record_count, evidence_record_count,
                mutation_count, command_count, file_count, artifact_count,
                deliverable_count, additions, deletions, omitted_file_count,
                expected_record_count, expected_record_file_count
            )
            SELECT
                'wsp_seed_' || sessions.session_id,
                sessions.user_email,
                sessions.session_id,
                sessions.intaris_session_id,
                'work-v7',
                0, 0, 'repair', 0, 0, 0,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
            FROM sessions
            WHERE
                sessions.intaris_session_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM work_session_projections
                    WHERE
                        work_session_projections.session_id = sessions.session_id
                        AND work_session_projections.materializer_version = 'work-v7'
                )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "137_work_live_projection is a direct destructive cutover; "
        "restore the pre-cutover database backup to roll back"
    )
