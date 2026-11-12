from __future__ import annotations

import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.conversation_reads import (
    list_conversation_read_markers,
    mark_shared_conversation_read,
)
from cognis.store.database import create_session_factory
from cognis.store.models import ConversationReadState
from cognis.store.queries import create_agent, create_conversation, create_user, get_conversation
from tests.schema_parity import (
    _actual_foreign_keys,
    _actual_index_signatures,
    _expected_foreign_keys,
    _expected_index_signatures,
)


def assert_read_schema_matches_metadata(connection) -> None:
    table = ConversationReadState.__table__
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    assert set(columns) == {column.name for column in table.columns}
    for expected in table.columns:
        actual = columns[expected.name]
        assert actual["type"]._type_affinity is expected.type._type_affinity
        assert actual["nullable"] == expected.nullable
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
        column.name for column in table.primary_key.columns
    ]
    assert _actual_foreign_keys(inspector, table.name) == _expected_foreign_keys(table)
    assert _actual_index_signatures(inspector, table.name) == _expected_index_signatures(
        table,
        connection.dialect.name,
    )


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def read_engine(request, tmp_path):
    schema = f"conversation_reads_{uuid.uuid4().hex}"
    admin = None
    if request.param == "postgresql":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema))
        engine = create_async_engine(
            url,
            connect_args={"server_settings": {"search_path": f'"{schema}"'}},
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reads.db'}")
    try:
        await run_schema_bootstrap(engine)
        yield engine
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(sa.schema.DropSchema(schema, cascade=True))
            await admin.dispose()


async def seed(engine: Any) -> Any:
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in ("author@example.com", "reader@example.com", "other@example.com"):
            await create_user(session, email=email, name=email, password_hash="test")
        await create_agent(
            session, agent_id="source", owner_email="author@example.com", name="Source"
        )
        await create_conversation(
            session,
            conversation_id="shared",
            agent_id="source",
            user_email="author@example.com",
            context_type="web",
        )
        await session.commit()
    return factory


@pytest.mark.asyncio
async def test_personal_read_markers_are_monotonic_and_preserve_author(read_engine):
    factory = await seed(read_engine)
    first = datetime(2026, 1, 2, tzinfo=UTC)
    newer = first + timedelta(days=1)
    async with factory() as session:
        await mark_shared_conversation_read(session, "shared", "reader@example.com", read_at=newer)
        await mark_shared_conversation_read(session, "shared", "reader@example.com", read_at=first)
        await mark_shared_conversation_read(session, "shared", "other@example.com", read_at=first)
        await session.commit()
    async with factory() as session:
        assert await list_conversation_read_markers(session, "reader@example.com", ["shared"]) == {
            "shared": newer,
        }
        assert await list_conversation_read_markers(session, "other@example.com", ["shared"]) == {
            "shared": first,
        }
        assert await list_conversation_read_markers(session, "reader@example.com", []) == {}
        assert await list_conversation_read_markers(session, "author@example.com", ["shared"]) == {}
        assert (await get_conversation(session, "shared")).last_read_at is None


@pytest.mark.asyncio
async def test_read_marker_bootstrap_migration_parity_and_preservation(read_engine, monkeypatch):
    factory = await seed(read_engine)
    read_at = datetime(2026, 1, 2, tzinfo=UTC)
    async with factory() as session:
        await mark_shared_conversation_read(
            session, "shared", "reader@example.com", read_at=read_at
        )
        await session.commit()
    await run_schema_bootstrap(read_engine)
    await run_schema_bootstrap(read_engine)
    migration = importlib.import_module(
        "cognis.store.migrations.versions.182_conversation_read_states"
    )

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert_read_schema_matches_metadata(connection)
        assert connection.scalar(sa.select(sa.func.count()).select_from(ConversationReadState)) == 1
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
        assert_read_schema_matches_metadata(connection)
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint("conversation_read_states")["constrained_columns"] == [
            "user_email",
            "conversation_id",
        ]
        assert len(inspector.get_foreign_keys("conversation_read_states")) == 2

    async with factory() as session:
        assert await list_conversation_read_markers(session, "reader@example.com", ["shared"]) == {
            "shared": read_at,
        }
    async with read_engine.begin() as connection:
        await connection.run_sync(exercise)
