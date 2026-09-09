from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]

EXPECTED_INDEXES = {
    "sessions": {
        "ix_sessions_owner_conversation_activity_scope_session": (
            "user_email",
            "conversation_id",
            "activity_scope_id",
            "session_id",
        ),
    },
    "step_runs": {
        "ix_step_runs_task_step_run": ("task_id", "step_run_id"),
        "ix_step_runs_session_status": ("session_id", "status"),
    },
}


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


def _migrate(sync_connection: Any, revision: str, *, downgrade: bool = False) -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", _url())
    config.config_file_name = None
    config.attributes["connection"] = sync_connection
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def _indexes(sync_connection: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    inspector = sa.inspect(sync_connection)
    return {
        table: {
            str(index["name"]): tuple(index["column_names"])
            for index in inspector.get_indexes(table)
        }
        for table in EXPECTED_INDEXES
    }


@pytest.mark.asyncio
async def test_postgresql_work_load_indexes_accept_manual_indexes_and_downgrade() -> None:
    url = _url()
    schema_name = f"cognis_work_load_indexes_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate, "127_work_record_categories")
            await connection.execute(
                sa.text(
                    "CREATE INDEX "
                    "ix_sessions_owner_conversation_activity_scope_session "
                    "ON sessions "
                    "(user_email, conversation_id, activity_scope_id, session_id)"
                )
            )
            await connection.execute(
                sa.text(
                    "CREATE INDEX ix_step_runs_task_step_run "
                    "ON step_runs (task_id DESC, step_run_id)"
                )
            )
            await connection.commit()

            with pytest.raises(
                RuntimeError,
                match="Conflicting PostgreSQL index definition",
            ):
                await connection.run_sync(_migrate, "128_work_load_indexes")

            await connection.execute(sa.text("DROP INDEX ix_step_runs_task_step_run"))
            await connection.commit()

            await connection.run_sync(_migrate, "128_work_load_indexes")
            upgraded = await connection.run_sync(_indexes)
            for table, expected in EXPECTED_INDEXES.items():
                for name, columns in expected.items():
                    assert upgraded[table][name] == columns

            await connection.run_sync(
                _migrate,
                "127_work_record_categories",
                downgrade=True,
            )
            downgraded = await connection.run_sync(_indexes)
            for table, expected in EXPECTED_INDEXES.items():
                assert expected.keys().isdisjoint(downgraded[table])
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgresql_work_load_indexes_recover_exact_invalid_index() -> None:
    url = _url()
    schema_name = f"cognis_work_load_invalid_index_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate, "127_work_record_categories")
            await connection.execute(
                sa.text(
                    "CREATE INDEX ix_step_runs_session_status ON step_runs (session_id, status)"
                )
            )
            await connection.commit()

            try:
                await connection.execute(
                    sa.text(
                        """
                        UPDATE pg_index
                        SET indisvalid = FALSE
                        WHERE indexrelid = to_regclass(
                            'ix_step_runs_session_status'
                        )
                        """
                    )
                )
                await connection.commit()
            except DBAPIError as exc:
                await connection.rollback()
                if getattr(exc.orig, "sqlstate", None) == "42501":
                    pytest.skip("PostgreSQL role cannot mark an isolated test index invalid")
                raise

            await connection.run_sync(_migrate, "128_work_load_indexes")
            index_state = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT index_metadata.indisvalid
                        FROM pg_index AS index_metadata
                        WHERE index_metadata.indexrelid = to_regclass(
                            'ix_step_runs_session_status'
                        )
                        """
                    )
                )
            ).scalar_one()
            assert index_state is True
            upgraded = await connection.run_sync(_indexes)
            assert upgraded["step_runs"]["ix_step_runs_session_status"] == (
                "session_id",
                "status",
            )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgresql_activity_list_index_and_query_plan() -> None:
    url = _url()
    schema_name = f"cognis_work_activity_list_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate, "134_work_activity_list_index")
            indexes = await connection.run_sync(_indexes)
            assert indexes["sessions"]["ix_sessions_owner_activity_scope_updated"] == (
                "user_email",
                "activity_scope_id",
                "updated_at",
                "session_id",
            )
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO users (email, name, role, is_active, created_at, updated_at)
                    VALUES ('owner@example.com', 'Owner', 'user', TRUE, now(), now())
                    """
                )
            )
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO agents (
                        agent_id, owner_email, name, agent_type, is_system, hidden,
                        status, created_at, updated_at
                    )
                    VALUES (
                        'agent', 'owner@example.com', 'Agent', 'primary', FALSE, FALSE,
                        'active', now(), now()
                    )
                    """
                )
            )
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO conversations (
                        conversation_id, user_email, agent_id, title_source,
                        context_type, status,
                        active_executor_generation, created_at, updated_at
                    )
                    VALUES (
                        'conversation', 'owner@example.com', 'agent', 'unset',
                        'web', 'active', 0, now(), now()
                    )
                    """
                )
            )
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO sessions (
                        session_id, activity_scope_id, conversation_id, user_email,
                        agent_id, delegation_metadata, status, started_at, updated_at
                    )
                    SELECT
                        'session-' || value,
                        'session-' || value,
                        'conversation',
                        'owner@example.com',
                        'agent',
                        '{}'::jsonb,
                        'completed',
                        now() - (value || ' seconds')::interval,
                        now() - (value || ' seconds')::interval
                    FROM generate_series(1, 2000) AS value
                    """
                )
            )
            await connection.commit()
            await connection.execute(sa.text("SET enable_seqscan = off"))
            plan_rows = (
                await connection.execute(
                    sa.text(
                        """
                        EXPLAIN (COSTS OFF)
                        WITH activity_members AS (
                            SELECT
                                session_id,
                                activity_scope_id,
                                updated_at,
                                max(updated_at) OVER (
                                    PARTITION BY activity_scope_id
                                ) AS last_activity_at,
                                row_number() OVER (
                                    PARTITION BY activity_scope_id
                                    ORDER BY updated_at DESC, session_id DESC
                                ) AS latest_rank
                            FROM sessions
                            WHERE user_email = 'owner@example.com'
                        )
                        SELECT members.activity_scope_id, members.last_activity_at
                        FROM activity_members AS members
                        JOIN sessions AS root
                            ON root.session_id = members.activity_scope_id
                            AND root.activity_scope_id = members.activity_scope_id
                            AND root.user_email = 'owner@example.com'
                        WHERE members.latest_rank = 1
                        ORDER BY
                            members.last_activity_at DESC,
                            members.activity_scope_id DESC
                        LIMIT 21
                        """
                    )
                )
            ).scalars()
            plan = "\n".join(str(row) for row in plan_rows)
            assert "ix_sessions_owner_activity_scope_updated" in plan
            assert "work_records" not in plan
            assert "work_generation_summary_snapshots" not in plan
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True], ids=["conflicting", "invalid"])
async def test_postgresql_activity_list_migration_repairs_existing_index(
    invalid: bool,
) -> None:
    url = _url()
    schema_name = f"cognis_work_activity_repair_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate, "133_work_generation_snapshots")
            columns = (
                "user_email, activity_scope_id, updated_at, session_id"
                if invalid
                else "user_email, session_id"
            )
            await connection.execute(
                sa.text(
                    f"CREATE INDEX ix_sessions_owner_activity_scope_updated ON sessions ({columns})"
                )
            )
            await connection.commit()
            if invalid:
                try:
                    await connection.execute(
                        sa.text(
                            """
                            UPDATE pg_index
                            SET indisvalid = FALSE
                            WHERE indexrelid = to_regclass(
                                'ix_sessions_owner_activity_scope_updated'
                            )
                            """
                        )
                    )
                    await connection.commit()
                except DBAPIError as exc:
                    await connection.rollback()
                    if getattr(exc.orig, "sqlstate", None) == "42501":
                        pytest.skip("PostgreSQL role cannot mark an isolated test index invalid")
                    raise

            await connection.run_sync(_migrate, "134_work_activity_list_index")
            indexes = await connection.run_sync(_indexes)
            assert indexes["sessions"]["ix_sessions_owner_activity_scope_updated"] == (
                "user_email",
                "activity_scope_id",
                "updated_at",
                "session_id",
            )
            valid = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT index_metadata.indisvalid
                        FROM pg_index AS index_metadata
                        WHERE index_metadata.indexrelid = to_regclass(
                            'ix_sessions_owner_activity_scope_updated'
                        )
                        """
                    )
                )
            ).scalar_one()
            assert valid is True
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
