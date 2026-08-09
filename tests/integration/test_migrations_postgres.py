from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import run_schema_bootstrap, seed_system_agents
from cognis.store.database import create_session_factory
from cognis.store.queries import (
    create_agent,
    create_user,
    list_agents,
    list_secondary_bindings,
    set_secondary_bindings,
)
from tests.schema_parity import assert_schema_matches_metadata

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


def _run_upgrade(sync_connection: Any, revision: str = "head") -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", _url())
    config.config_file_name = None
    config.attributes["connection"] = sync_connection
    command.upgrade(config, revision)


def _run_downgrade(sync_connection: Any, revision: str) -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", _url())
    config.config_file_name = None
    config.attributes["connection"] = sync_connection
    command.downgrade(config, revision)


def _table_names(sync_connection: Any) -> list[str]:
    return sa.inspect(sync_connection).get_table_names()


def _version_column_length(sync_connection: Any) -> int | None:
    version_column = next(
        column
        for column in sa.inspect(sync_connection).get_columns("alembic_version")
        if column["name"] == "version_num"
    )
    return version_column["type"].length


def _assert_schema_matches_metadata(sync_connection: Any) -> None:
    assert_schema_matches_metadata(sync_connection)


async def _assert_agent_queries_work(engine: Any) -> None:
    factory = create_session_factory(engine)
    async with factory() as session:
        await seed_system_agents(session)
        await create_user(session, "owner@example.com", "Owner", "hash")
        await create_agent(
            session,
            agent_id="primary",
            owner_email="owner@example.com",
            name="Primary",
        )
        await create_agent(
            session,
            agent_id="secondary",
            owner_email="owner@example.com",
            name="Secondary",
            agent_type="secondary",
        )
        await set_secondary_bindings(session, "primary", ["secondary"])
        await session.commit()

        agents = await list_agents(session, "owner@example.com")
        assert {agent.agent_id for agent in agents} == {"primary", "secondary"}
        assert all(agent.is_system is False and agent.hidden is False for agent in agents)
        assert await list_secondary_bindings(session, "primary") == ["secondary"]


@pytest.mark.asyncio
async def test_fresh_postgresql_upgrade_to_head() -> None:
    url = _url()
    schema_name = f"cognis_migration_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_upgrade)
            tables = set(await connection.run_sync(_table_names))
            version_length = await connection.run_sync(_version_column_length)
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            await connection.run_sync(_assert_schema_matches_metadata)
        assert {"users", "workflows", "tasks", "schedules", "managed_conversation_links"} <= tables
        config = Config("cognis/store/migrations/alembic.ini")
        assert revision == ScriptDirectory.from_config(config).get_current_head()
        assert version_length == 255
        await _assert_agent_queries_work(engine)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgresql_bootstrap_at_113_upgrades_and_downgrades() -> None:
    url = _url()
    schema_name = f"cognis_migration_bootstrap_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(
                _run_upgrade,
                "113_managed_channel_lifecycle",
            )
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            indexes = await connection.run_sync(
                lambda sync_connection: {
                    index["name"]: index
                    for index in sa.inspect(sync_connection).get_indexes(
                        "managed_conversation_signals"
                    )
                }
            )
            assert bool(indexes["uq_managed_signal_source_turn"]["unique"]) is True
            assert bool(indexes["uq_managed_signal_resume_request"]["unique"]) is True
            await connection.run_sync(_run_upgrade, "117_group_context")
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == "117_group_context"
        async with engine.connect() as connection:
            await connection.run_sync(
                _run_downgrade,
                "113_managed_channel_lifecycle",
            )
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            reflected = await connection.run_sync(
                lambda sync_connection: {
                    "columns": {
                        column["name"]
                        for column in sa.inspect(sync_connection).get_columns(
                            "managed_conversation_signals"
                        )
                    },
                    "unique_names": {
                        str(item["name"])
                        for item in (
                            sa.inspect(sync_connection).get_unique_constraints(
                                "managed_conversation_signals"
                            )
                            + sa.inspect(sync_connection).get_indexes(
                                "managed_conversation_signals"
                            )
                        )
                    },
                }
            )
        assert revision == "113_managed_channel_lifecycle"
        assert "source_turn_id" not in reflected["columns"]
        assert "resume_request_id" not in reflected["columns"]
        assert "uq_managed_signal_source_turn" not in reflected["unique_names"]
        assert "uq_managed_signal_resume_request" not in reflected["unique_names"]
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgresql_revision_036_with_legacy_version_column_upgrades_to_head() -> None:
    url = _url()
    schema_name = f"cognis_migration_legacy_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_upgrade, "036_workflow_deliverables")
            created_at = datetime(2026, 7, 26, tzinfo=UTC)
            await connection.execute(
                sa.text(
                    "INSERT INTO users "
                    "(email, name, password_hash, role, created_at, updated_at) "
                    "VALUES ('legacy@example.com', 'Legacy', 'hash', 'user', "
                    ":created_at, NULL)"
                ),
                {"created_at": created_at},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(agent_id, owner_email, name, status, created_at, updated_at) "
                    "VALUES ('legacy-agent', 'legacy@example.com', 'Legacy', "
                    "'active', :created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO artifacts "
                    "(artifact_id, namespace, object_id, filename, purpose, kind, "
                    "mime_type, size_bytes, status, created_at) "
                    "VALUES ('artifact-1', 'owner', 'object-1', 'file.txt', "
                    "'chat_input', 'file', 'text/plain', 1, 'temporary', "
                    ":created_at)"
                ),
                {"created_at": created_at},
            )
            await connection.commit()
            await connection.execute(
                sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(32)")
            )
            assert await connection.run_sync(_version_column_length) == 32

            await connection.run_sync(_run_upgrade)
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            version_length = await connection.run_sync(_version_column_length)
            await connection.run_sync(_assert_schema_matches_metadata)
            legacy_agent = (
                await connection.execute(
                    sa.text(
                        "SELECT agent_type, is_system, hidden "
                        "FROM agents WHERE agent_id = 'legacy-agent'"
                    )
                )
            ).one()
            assert tuple(legacy_agent) == ("primary", False, False)

        config = Config("cognis/store/migrations/alembic.ini")
        assert revision == ScriptDirectory.from_config(config).get_current_head()
        assert version_length == 255
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
