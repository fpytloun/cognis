import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def _config(database_path: Path) -> Config:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def test_schedule_fire_timezone_migration_is_linear_head() -> None:
    script = ScriptDirectory.from_config(_config(Path("/tmp/unused-cognis.db")))

    assert script.get_heads() == ["149_signal_destination_policy"]
    revision = script.get_revision("146_schedule_fire_timezone")
    assert revision is not None
    assert revision.down_revision == "145_notification_attention_state"


def test_schedule_fire_timezone_migration_matches_orm(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule-fire-timezone.db"
    command.upgrade(_config(database_path), "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"]: column for column in sa.inspect(engine).get_columns("schedule_fires")
        }
        assert columns["schedule_timezone"]["nullable"] is True
        assert columns["schedule_timezone"]["default"] is None
        with engine.connect() as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            }
        assert "trg_schedule_fire_timezone_snapshot" in trigger_names
    finally:
        engine.dispose()


def test_upgrade_from_145_backfills_existing_non_utc_fire(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule-fire-backfill.db"
    config = _config(database_path)
    command.upgrade(config, "145_notification_attention_state")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    now = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedules (
                        schedule_id, name, schedule_type, timezone, agent_id,
                        task_template, enabled, max_concurrent_runs, delete_after_run,
                        retry_failed_tasks, fail_paused_task_on_next_fire,
                        completion_mode_family, allow_silent_completion,
                        created_by, created_at, updated_at, consecutive_errors
                    ) VALUES (
                        'schedule-prague', 'Prague', 'cron', 'Europe/Prague', 'agent',
                        '{}', 1, 1, 0, 0, 1, 'default', 0,
                        'owner@example.com', :now, :now, 0
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedule_fires (
                        fire_id, schedule_id, fire_kind, scheduled_fire_at,
                        status, attempt_count, created_at, updated_at
                    ) VALUES (
                        'fire-prague', 'schedule-prague', 'recurring', :now,
                        'claimed', 1, :now, :now
                    )
                    """
                ),
                {"now": now},
            )
        engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(f"sqlite:///{database_path}")
        with engine.connect() as connection:
            timezone = connection.scalar(
                sa.text(
                    "SELECT schedule_timezone FROM schedule_fires WHERE fire_id = 'fire-prague'"
                )
            )
        assert timezone == "Europe/Prague"
    finally:
        engine.dispose()


def test_old_writer_insert_snapshots_timezone_and_downgrade_removes_trigger(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "schedule-fire-rolling.db"
    config = _config(database_path)
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    now = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedules (
                        schedule_id, name, schedule_type, timezone, agent_id,
                        task_template, enabled, max_concurrent_runs, delete_after_run,
                        retry_failed_tasks, fail_paused_task_on_next_fire,
                        completion_mode_family, allow_silent_completion,
                        created_by, created_at, updated_at, consecutive_errors
                    ) VALUES (
                        'schedule-rolling', 'Rolling', 'cron', 'Europe/Prague', 'agent',
                        '{}', 1, 1, 0, 0, 1, 'default', 0,
                        'owner@example.com', :now, :now, 0
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedule_fires (
                        fire_id, schedule_id, fire_kind, scheduled_fire_at,
                        status, attempt_count, created_at, updated_at
                    ) VALUES (
                        'fire-rolling', 'schedule-rolling', 'recurring', :now,
                        'claimed', 1, :now, :now
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "UPDATE schedules SET timezone = 'UTC' WHERE schedule_id = 'schedule-rolling'"
                )
            )
            timezone = connection.scalar(
                sa.text(
                    "SELECT schedule_timezone FROM schedule_fires WHERE fire_id = 'fire-rolling'"
                )
            )
        assert timezone == "Europe/Prague"
    finally:
        engine.dispose()

    command.downgrade(config, "145_notification_attention_state")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            triggers = connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' "
                    "AND name = 'trg_schedule_fire_timezone_snapshot'"
                )
            ).all()
        assert triggers == []
        assert "schedule_timezone" not in {
            column["name"] for column in sa.inspect(engine).get_columns("schedule_fires")
        }
    finally:
        engine.dispose()


def test_migration_defines_postgresql_insertion_trigger() -> None:
    migration = Path("cognis/store/migrations/versions/146_schedule_fire_timezone.py").read_text()

    assert 'bind.dialect.name == "postgresql"' in migration
    assert "BEFORE INSERT ON schedule_fires" in migration
    assert "NEW.schedule_timezone := COALESCE" in migration


def test_postgresql_old_writer_snapshot_and_downgrade_when_available() -> None:
    database_url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    engine = sa.create_engine(database_url)
    if sa.inspect(engine).get_table_names():
        engine.dispose()
        pytest.fail("COGNIS_TEST_POSTGRES_URL must reference an empty isolated database")
    now = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO users (
                        email, role, is_active, created_at, updated_at, auth_version
                    ) VALUES (
                        'owner@example.com', 'user', true, :now, :now, 0
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO agents (
                        agent_id, owner_email, name, agent_type, is_system,
                        hidden, status, created_at, updated_at
                    ) VALUES (
                        'agent', 'owner@example.com', 'Agent', 'primary', false,
                        false, 'active', :now, :now
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedules (
                        schedule_id, name, schedule_type, timezone, agent_id,
                        task_template, enabled, max_concurrent_runs, delete_after_run,
                        retry_failed_tasks, fail_paused_task_on_next_fire,
                        completion_mode_family, allow_silent_completion,
                        created_by, created_at, updated_at, consecutive_errors
                    ) VALUES (
                        'schedule-pg-rolling', 'Rolling', 'cron', 'Europe/Prague', 'agent',
                        '{}', true, 1, false, false, true, 'default', false,
                        'owner@example.com', :now, :now, 0
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO schedule_fires (
                        fire_id, schedule_id, fire_kind, scheduled_fire_at,
                        status, attempt_count, created_at, updated_at
                    ) VALUES (
                        'fire-pg-rolling', 'schedule-pg-rolling', 'recurring', :now,
                        'claimed', 1, :now, :now
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "UPDATE schedules SET timezone = 'UTC' "
                    "WHERE schedule_id = 'schedule-pg-rolling'"
                )
            )
            timezone = connection.scalar(
                sa.text(
                    "SELECT schedule_timezone FROM schedule_fires WHERE fire_id = 'fire-pg-rolling'"
                )
            )
        assert timezone == "Europe/Prague"

        command.downgrade(config, "145_notification_attention_state")
        assert "schedule_timezone" not in {
            column["name"] for column in sa.inspect(engine).get_columns("schedule_fires")
        }
        with engine.connect() as connection:
            trigger_count = connection.scalar(
                sa.text(
                    """
                    SELECT count(*) FROM pg_trigger
                    WHERE tgname = 'trg_schedule_fire_timezone_snapshot'
                    AND NOT tgisinternal
                    """
                )
            )
        assert trigger_count == 0
    finally:
        engine.dispose()
        command.downgrade(config, "base")
