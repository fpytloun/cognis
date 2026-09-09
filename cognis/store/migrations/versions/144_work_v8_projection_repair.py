"""Seed current Work projections after the v8 materializer cutover.

Revision ID: 144_work_v8_projection_repair
Revises: 143_schedule_terminal_task_correlation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "144_work_v8_projection_repair"
down_revision = "143_schedule_terminal_task_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
                'wsp_seed_v8_' || sessions.session_id,
                sessions.user_email,
                sessions.session_id,
                sessions.intaris_session_id,
                'work-v8',
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
                        AND work_session_projections.materializer_version = 'work-v8'
                )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM work_session_projections
            WHERE materializer_version = 'work-v8'
              AND projection_id = 'wsp_seed_v8_' || session_id
              AND target_seq = 0
              AND covered_through_seq = 0
              AND state = 'repair'
            """
        )
    )
