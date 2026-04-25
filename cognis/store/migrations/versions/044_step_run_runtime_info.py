"""Add runtime_info to step runs.

Revision ID: 044_step_run_runtime_info
Revises: 043_agent_grants
Create Date: 2026-04-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "044_step_run_runtime_info"
down_revision = "043_agent_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("step_runs") as batch:
        batch.add_column(sa.Column("runtime_info", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("step_runs") as batch:
        batch.drop_column("runtime_info")
