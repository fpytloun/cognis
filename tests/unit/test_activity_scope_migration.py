from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine


@contextmanager
def _preserve_logging_state() -> Iterator[None]:
    logger_dict = dict(logging.Logger.manager.loggerDict)
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    state = {
        logger: (
            logger.disabled,
            logger.level,
            list(logger.handlers),
            logger.propagate,
        )
        for logger in loggers
    }
    try:
        yield
    finally:
        for logger, (disabled, level, handlers, propagate) in state.items():
            logger.disabled = disabled
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        logging.Logger.manager.loggerDict.clear()
        logging.Logger.manager.loggerDict.update(logger_dict)


def test_activity_scope_migration_backfills_rotation_and_delegate_chains(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "activity-scopes.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    with _preserve_logging_state():
        command.upgrade(config, "123_direct_turn_retry_schedule")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sessions (
                    session_id, conversation_id, parent_session_id, previous_session_id,
                    user_email, agent_id, delegation_metadata, status, completion_reason,
                    started_at, completed_at, updated_at
                ) VALUES
                    ('root', 'conv', NULL, NULL, 'user@example.com', 'agent', '{}',
                     'completed', 'compacted', '2026-01-01', '2026-01-02', '2026-01-02'),
                    ('compact', 'conv', NULL, 'root', 'user@example.com', 'agent', '{}',
                     'completed', 'user_reset', '2026-01-02', '2026-01-03', '2026-01-03'),
                    ('reset', 'conv', NULL, 'compact', 'user@example.com', 'agent', '{}',
                     'active', NULL, '2026-01-03', NULL, '2026-01-03'),
                    ('child', 'conv', 'reset', NULL, 'user@example.com', 'agent', '{}',
                     'active', NULL, '2026-01-06', NULL, '2026-01-06'),
                    ('old-child', 'conv', 'root', NULL, 'user@example.com', 'agent', '{}',
                     'completed', NULL, '2026-01-02', '2026-01-04', '2026-01-04')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    task_id, title, status, priority, created_by, agent_id,
                    source_type, source_ref, delivery_mode, completion_mode_family,
                    allow_silent_completion, created_at, updated_at
                ) VALUES
                    ('task-old', 'Old task', 'completed', 0, 'user@example.com', 'agent',
                     'chat', 'conv', 'same_conversation', 'default', 0,
                     '2026-01-01 12:00:00', '2026-01-01 12:00:00'),
                    ('task-reset', 'Reset task', 'completed', 0, 'user@example.com', 'agent',
                     'chat', 'conv', 'same_conversation', 'default', 0,
                     '2026-01-05 12:00:00', '2026-01-05 12:00:00'),
                    ('task-overlap', 'Overlap task', 'completed', 0, 'user@example.com', 'agent',
                     'chat', 'conv', 'same_conversation', 'default', 0,
                     '2026-01-03 12:00:00', '2026-01-03 12:00:00')
                """
            )
        )
    engine.dispose()

    with _preserve_logging_state():
        command.upgrade(config, "125_task_source_session")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            scopes = dict(
                connection.execute(text("SELECT session_id, activity_scope_id FROM sessions")).all()
            )
        assert scopes == {
            "root": "root",
            "compact": "root",
            "reset": "reset",
            "child": "reset",
            "old-child": "root",
        }
        with engine.connect() as connection:
            task_origins = dict(
                connection.execute(
                    text("SELECT task_id, source_session_id FROM tasks ORDER BY task_id")
                ).all()
            )
        assert task_origins == {
            "task-old": "root",
            "task-overlap": None,
            "task-reset": "reset",
        }
        column = next(
            item
            for item in inspect(engine).get_columns("sessions")
            if item["name"] == "activity_scope_id"
        )
        assert column["nullable"] is False
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_activity_scope_bootstrap_is_idempotent_and_non_nullable(tmp_path: Path) -> None:
    database_path = tmp_path / "activity-scope-bootstrap.db"
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    await run_schema_bootstrap(async_engine)
    await run_schema_bootstrap(async_engine)
    await async_engine.dispose()

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        column = next(
            item
            for item in inspect(engine).get_columns("sessions")
            if item["name"] == "activity_scope_id"
        )
        assert column["nullable"] is False
    finally:
        engine.dispose()


def test_activity_scope_bootstrap_enforces_pre_124_null_rejection_twice(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "activity-scope-pre-124.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    with _preserve_logging_state():
        command.upgrade(config, "123_direct_turn_retry_schedule")

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(async_engine.dispose())

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with (
            engine.begin() as connection,
            pytest.raises(Exception, match="activity_scope_id"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, conversation_id, user_email, agent_id,
                        delegation_metadata, status, started_at, updated_at,
                        activity_scope_id
                    ) VALUES (
                        'invalid-null-scope', 'conv', 'user@example.com', 'agent',
                        '{}', 'active', '2026-01-05', '2026-01-05', NULL
                    )
                    """
                )
            )
    finally:
        engine.dispose()


def test_task_source_session_bootstrap_leaves_overlapping_candidates_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-source-session-bootstrap.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    with _preserve_logging_state():
        command.upgrade(config, "124_session_activity_scope")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sessions (
                    session_id, activity_scope_id, conversation_id, parent_session_id,
                    previous_session_id, user_email, agent_id, delegation_metadata,
                    status, completion_reason, started_at, completed_at, updated_at
                ) VALUES
                    ('old-root', 'old-root', 'conv', NULL, NULL, 'user@example.com', 'agent',
                     '{}', 'completed', 'user_reset', '2026-01-01', '2026-01-03',
                     '2026-01-03'),
                    ('old-child', 'old-root', 'conv', 'old-root', NULL, 'user@example.com',
                     'agent', '{}', 'completed', NULL, '2026-01-02', '2026-01-04',
                     '2026-01-04'),
                    ('new-root', 'new-root', 'conv', NULL, 'old-root', 'user@example.com',
                     'agent', '{}', 'active', NULL, '2026-01-03', NULL, '2026-01-03')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    task_id, title, status, priority, created_by, agent_id,
                    source_type, source_ref, delivery_mode, completion_mode_family,
                    allow_silent_completion, created_at, updated_at
                ) VALUES
                    ('task-overlap', 'Overlap task', 'completed', 0, 'user@example.com',
                     'agent', 'chat', 'conv', 'same_conversation', 'default', 0,
                     '2026-01-03 12:00:00', '2026-01-03 12:00:00'),
                    ('task-unique', 'Unique task', 'completed', 0, 'user@example.com',
                     'agent', 'chat', 'conv', 'same_conversation', 'default', 0,
                     '2026-01-05 12:00:00', '2026-01-05 12:00:00')
                """
            )
        )
    engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(async_engine.dispose())

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            task_origins = dict(
                connection.execute(
                    text("SELECT task_id, source_session_id FROM tasks ORDER BY task_id")
                ).all()
            )
        assert task_origins == {"task-overlap": None, "task-unique": "new-root"}
    finally:
        engine.dispose()
