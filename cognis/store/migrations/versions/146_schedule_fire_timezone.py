"""Snapshot the timezone on each schedule fire.

Revision ID: 146_schedule_fire_timezone
Revises: 145_notification_attention_state
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "146_schedule_fire_timezone"
down_revision = "145_notification_attention_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_fires",
        sa.Column("schedule_timezone", sa.String(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE schedule_fires
            SET schedule_timezone = COALESCE(
                (
                    SELECT schedules.timezone
                    FROM schedules
                    WHERE schedules.schedule_id = schedule_fires.schedule_id
                ),
                'UTC'
            )
            """
        )
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE FUNCTION cognis_schedule_fire_timezone_snapshot()
                RETURNS trigger AS $$
                BEGIN
                    IF NEW.schedule_timezone IS NULL THEN
                        SELECT timezone INTO NEW.schedule_timezone
                        FROM schedules
                        WHERE schedule_id = NEW.schedule_id;
                        NEW.schedule_timezone := COALESCE(NEW.schedule_timezone, 'UTC');
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER trg_schedule_fire_timezone_snapshot
                BEFORE INSERT ON schedule_fires
                FOR EACH ROW
                EXECUTE FUNCTION cognis_schedule_fire_timezone_snapshot()
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                CREATE TRIGGER trg_schedule_fire_timezone_snapshot
                AFTER INSERT ON schedule_fires
                FOR EACH ROW
                WHEN NEW.schedule_timezone IS NULL
                BEGIN
                    UPDATE schedule_fires
                    SET schedule_timezone = COALESCE(
                        (
                            SELECT timezone
                            FROM schedules
                            WHERE schedule_id = NEW.schedule_id
                        ),
                        'UTC'
                    )
                    WHERE fire_id = NEW.fire_id;
                END
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_schedule_fire_timezone_snapshot ON schedule_fires")
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS cognis_schedule_fire_timezone_snapshot()"))
    else:
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_schedule_fire_timezone_snapshot"))
    with op.batch_alter_table("schedule_fires") as batch_op:
        batch_op.drop_column("schedule_timezone")
