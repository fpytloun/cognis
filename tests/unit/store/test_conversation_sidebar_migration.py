from __future__ import annotations

import importlib
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATION = "cognis.store.migrations.versions.072_conversation_sidebar_indexes"


def _run_migration(engine: Engine, direction: str) -> None:
    module = importlib.import_module(MIGRATION)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        original_op = module.op
        module.op = Operations(context)
        try:
            getattr(module, direction)()
        finally:
            module.op = original_op


def _create_minimal_conversations_table(engine: Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "conversations",
        metadata,
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("context_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO conversations "
                "(conversation_id, user_email, agent_id, context_type, status, created_at) "
                "VALUES ('conv_1', 'user@example.com', 'agent-1', 'web', 'active', CURRENT_TIMESTAMP)"
            )
        )


def _assert_sidebar_index_migration_up_down(engine: Engine) -> None:
    _create_minimal_conversations_table(engine)

    _run_migration(engine, "upgrade")

    inspector = sa.inspect(engine)
    index_names = {index["name"] for index in inspector.get_indexes("conversations")}
    assert "ix_conversations_owner_activity" in index_names
    assert "ix_conversations_owner_agent_context" in index_names
    with engine.connect() as connection:
        null_count = connection.execute(
            sa.text("SELECT COUNT(*) FROM conversations WHERE last_message_at IS NULL")
        ).scalar_one()
    assert null_count == 0

    _run_migration(engine, "downgrade")

    inspector = sa.inspect(engine)
    index_names = {index["name"] for index in inspector.get_indexes("conversations")}
    assert "ix_conversations_owner_activity" not in index_names
    assert "ix_conversations_owner_agent_context" not in index_names


def test_conversation_sidebar_index_migration_up_down() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        _assert_sidebar_index_migration_up_down(engine)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_conversation_sidebar_index_migration_up_down_postgresql(
    postgres_migration_engine: AsyncEngine,
) -> None:
    async with postgres_migration_engine.connect() as connection:
        await connection.run_sync(lambda bind: _assert_sidebar_index_migration_up_down(bind.engine))


def test_conversation_sidebar_index_migration_tolerates_bootstrap_created_indexes() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        _create_minimal_conversations_table(engine)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE INDEX ix_conversations_owner_activity "
                    "ON conversations (user_email, status, last_message_at, created_at)"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE INDEX ix_conversations_owner_agent_context "
                    "ON conversations (user_email, status, agent_id, context_type)"
                )
            )

        _run_migration(engine, "upgrade")

        inspector = sa.inspect(engine)
        index_names = {index["name"] for index in inspector.get_indexes("conversations")}
        assert "ix_conversations_owner_activity" in index_names
        assert "ix_conversations_owner_agent_context" in index_names
    finally:
        engine.dispose()


def test_postgresql_sidebar_index_migration_validation_available() -> None:
    if os.getenv("COGNIS_TEST_POSTGRES_URL"):
        return
    pytest.skip("COGNIS_TEST_POSTGRES_URL is not set; PostgreSQL migration validation unavailable")
