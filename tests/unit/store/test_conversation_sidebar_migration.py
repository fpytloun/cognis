from __future__ import annotations

import importlib
import os
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine

MIGRATION = "cognis.store.migrations.versions.072_conversation_sidebar_indexes"


def _engine_urls() -> Iterator[tuple[str, str]]:
    yield "sqlite", "sqlite:///:memory:"
    postgres_url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if postgres_url:
        yield "postgresql", postgres_url


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


@pytest.mark.parametrize("dialect,url", list(_engine_urls()))
def test_conversation_sidebar_index_migration_up_down(dialect: str, url: str) -> None:
    engine = sa.create_engine(url)
    try:
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
    finally:
        engine.dispose()


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
