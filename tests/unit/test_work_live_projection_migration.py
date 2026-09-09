from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.store import queries
from cognis.store.database import create_engine, create_session_factory

OBSOLETE = {
    "work_scope_states",
    "work_scope_streams",
    "work_scope_generations",
    "work_scope_generation_streams",
    "work_scope_generation_frontier",
    "work_generation_summary_snapshots",
    "work_generation_current_file_snapshots",
    "work_invalidation_outbox",
}
RETAINED = {
    "work_live_revisions",
    "work_session_projections",
    "work_records",
    "work_record_files",
    "work_current_files",
}


@pytest.mark.asyncio
async def test_fresh_bootstrap_contains_only_live_work_projection_tables(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
    finally:
        await engine.dispose()
    assert tables >= RETAINED
    assert OBSOLETE.isdisjoint(tables)


@pytest.mark.asyncio
async def test_populated_cutover_drops_only_generation_tables(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    migration = importlib.import_module("cognis.store.migrations.versions.137_work_live_projection")
    try:
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        async with factory() as db:
            await queries.create_user(
                db,
                email="owner@example.com",
                name="Owner",
                password_hash="x",
                role="user",
            )
            await queries.create_agent(
                db,
                agent_id="agent",
                owner_email="owner@example.com",
                name="Agent",
            )
            conversation = await queries.create_conversation(
                db,
                conversation_id="conversation",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="web",
            )
            await queries.create_session(
                db,
                session_id="session",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent",
                intaris_session_id="source",
            )
            await queries.create_session(
                db,
                session_id="historical",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent",
                intaris_session_id="historical-source",
            )
            await db.commit()
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM work_session_projections WHERE session_id = 'historical'")
            )
            await connection.execute(
                text(
                    "UPDATE work_session_projections "
                    "SET target_seq = 17, covered_through_seq = 11, "
                    "state = 'repair', command_count = 7 "
                    "WHERE session_id = 'session'"
                )
            )
            for table_name in OBSOLETE:
                await connection.execute(
                    text(
                        f'CREATE TABLE IF NOT EXISTS "{table_name}" '
                        "(id VARCHAR PRIMARY KEY, marker VARCHAR)"
                    )
                )
                await connection.execute(
                    text(f"INSERT INTO \"{table_name}\" (id, marker) VALUES ('row', 'obsolete')")
                )

            def upgrade(sync_connection: object) -> None:
                context = MigrationContext.configure(sync_connection)
                migration.op = Operations(context)
                migration.upgrade()

            await connection.run_sync(upgrade)
            tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
            retained_count = await connection.scalar(
                text("SELECT count(*) FROM work_session_projections")
            )
            seeded = (
                await connection.execute(
                    text(
                        "SELECT projection_id, owner_email, source_session_id, "
                        "state, target_seq, covered_through_seq, command_count, "
                        "next_head_check_at "
                        "FROM work_session_projections "
                        "WHERE session_id = 'historical' "
                        "AND materializer_version = 'work-v7'"
                    )
                )
            ).one()
            preserved = (
                await connection.execute(
                    text(
                        "SELECT target_seq, covered_through_seq, state, command_count "
                        "FROM work_session_projections WHERE session_id = 'session' "
                        "AND materializer_version = 'work-v8'"
                    )
                )
            ).one()
            await connection.run_sync(upgrade)
            idempotent_count = await connection.scalar(
                text("SELECT count(*) FROM work_session_projections")
            )
    finally:
        await engine.dispose()
    assert OBSOLETE.isdisjoint(tables)
    assert tables >= RETAINED
    assert retained_count == 3
    assert idempotent_count == 3
    assert seeded[:7] == (
        "wsp_seed_historical",
        "owner@example.com",
        "historical-source",
        "repair",
        0,
        0,
        0,
    )
    assert seeded.next_head_check_at is not None
    assert preserved == (17, 11, "repair", 7)


@pytest.mark.asyncio
async def test_v8_repair_preserves_legacy_v7_projection(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'v8-repair.db'}")
    migration = importlib.import_module(
        "cognis.store.migrations.versions.144_work_v8_projection_repair"
    )
    try:
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        async with factory() as db:
            await queries.create_user(
                db,
                email="owner@example.com",
                name="Owner",
                password_hash="x",
                role="user",
            )
            await queries.create_agent(
                db,
                agent_id="agent",
                owner_email="owner@example.com",
                name="Agent",
            )
            conversation = await queries.create_conversation(
                db,
                conversation_id="conversation",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="web",
            )
            await queries.create_session(
                db,
                session_id="session",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent",
                intaris_session_id="source",
            )
            await db.commit()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE work_session_projections "
                    "SET projection_id = 'wsp_seed_session', materializer_version = 'work-v7' "
                    "WHERE session_id = 'session'"
                )
            )

            def upgrade(sync_connection: object) -> None:
                context = MigrationContext.configure(sync_connection)
                migration.op = Operations(context)
                migration.upgrade()

            await connection.run_sync(upgrade)
            await connection.run_sync(upgrade)
            rows = (
                await connection.execute(
                    text(
                        "SELECT projection_id, materializer_version, state "
                        "FROM work_session_projections WHERE session_id = 'session' "
                        "ORDER BY materializer_version"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    assert rows == [
        ("wsp_seed_session", "work-v7", "caught_up"),
        ("wsp_seed_v8_session", "work-v8", "repair"),
    ]
