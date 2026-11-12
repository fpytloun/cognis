"""Run against SQLite and an explicitly supplied disposable local PostgreSQL."""

import importlib
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.video_schema import ensure_video_error_message


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def schema_engine(request, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/video-schema.db"
    schema = None
    options = {}
    if request.param == "postgresql":
        url = os.environ.get("COGNIS_VIDEO_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Requires a disposable local PostgreSQL via COGNIS_VIDEO_TEST_POSTGRES_URL")
        parsed = sa.engine.make_url(url)
        assert parsed.host == "127.0.0.1" and parsed.database == "video_diagnostics_test"
        schema = "video_test_" + uuid.uuid4().hex
        options["connect_args"] = {"server_settings": {"search_path": schema}}
    engine = create_async_engine(url, **options)
    async with engine.begin() as connection:
        if schema:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.dialect.default_schema_name = schema
        await connection.execute(
            sa.text(
                "CREATE TABLE video_jobs (job_id VARCHAR PRIMARY KEY, error_code VARCHAR, "
                "cost_status VARCHAR NOT NULL)"
            )
        )
        await connection.execute(
            sa.text(
                "INSERT INTO video_jobs VALUES "
                "('existing-video-job', 'provider_http_400', 'not_submitted')"
            )
        )
    try:
        yield engine
    finally:
        if schema:
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def assert_preserved(connection):
    row = (
        await connection.execute(
            sa.text(
                "SELECT error_code, cost_status, provider_error_message FROM video_jobs "
                "WHERE job_id='existing-video-job'"
            )
        )
    ).one()
    assert tuple(row) == ("provider_http_400", "not_submitted", None)
    columns = await connection.run_sync(
        lambda conn: {col["name"]: col for col in sa.inspect(conn).get_columns("video_jobs")}
    )
    assert columns["provider_error_message"]["nullable"] is True
    assert columns["provider_error_message"]["type"].length == 512


@pytest.mark.asyncio
async def test_additive_helper_repeats_without_touching_existing_accounting(schema_engine):
    async with schema_engine.begin() as connection:
        for _ in range(2):
            await connection.run_sync(ensure_video_error_message)
            await assert_preserved(connection)


@pytest.mark.asyncio
async def test_formal_upgrade_downgrade_and_bootstrap_share_the_column(schema_engine):
    migration = importlib.import_module("cognis.store.migrations.versions.172_video_error_message")

    def migrate(connection, operation):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, operation)()

    async with schema_engine.begin() as connection:
        for _ in range(2):
            await connection.run_sync(lambda conn: migrate(conn, "upgrade"))
            await connection.run_sync(lambda conn: migrate(conn, "upgrade"))
            await assert_preserved(connection)
            await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
            await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
        await connection.run_sync(ensure_video_error_message)
        await assert_preserved(connection)


@pytest.mark.asyncio
async def test_registered_startup_bootstrap_is_idempotent(schema_engine):
    for _ in range(2):
        await run_schema_bootstrap(schema_engine)
        async with schema_engine.begin() as connection:
            await assert_preserved(connection)


@pytest.mark.asyncio
async def test_resequenced_upgrade_preserves_published_audio_schema_and_data(schema_engine):
    migrations = Path(__file__).resolve().parents[2] / "cognis/store/migrations"
    script = ScriptDirectory(str(migrations))
    assert script.get_revision("172_video_error_message").down_revision == "171_audio_jobs"

    def migrate(connection, operation, revision):
        config = Config()
        config.set_main_option("script_location", str(migrations))
        config.attributes["connection"] = connection
        getattr(command, operation)(config, revision)

    def audio_schema(connection):
        return [
            {**column, "type": str(column["type"])}
            for column in sa.inspect(connection).get_columns("audio_jobs")
        ]

    async with schema_engine.connect() as connection:
        await connection.run_sync(migrate, "stamp", "170_palette_destinations")
        await connection.run_sync(migrate, "upgrade", "171_audio_jobs")
        await connection.execute(
            sa.text(
                "INSERT INTO audio_jobs "
                "(job_id, owner_email, deliverable_id, idempotency_key, mode, target_minutes, "
                "status, part_artifact_ids, total_parts, created_at, updated_at) VALUES "
                "('existing-audio-job', 'schema-owner@test.local', 'existing-deliverable', "
                "'existing-audio-request', 'podcast', 1, 'completed', '[]', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.commit()
        audio_columns = await connection.run_sync(audio_schema)
        for operation, revision in (
            ("upgrade", "172_video_error_message"),
            ("downgrade", "171_audio_jobs"),
            ("upgrade", "172_video_error_message"),
        ):
            await connection.run_sync(migrate, operation, revision)
            assert await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                revision
            )
            assert (
                await connection.scalar(
                    sa.text("SELECT status FROM audio_jobs WHERE job_id='existing-audio-job'")
                )
                == "completed"
            )
            assert await connection.run_sync(audio_schema) == audio_columns
            if operation == "upgrade":
                await assert_preserved(connection)

    for _ in range(2):
        await run_schema_bootstrap(schema_engine)
        async with schema_engine.connect() as connection:
            await assert_preserved(connection)
            assert (
                await connection.scalar(
                    sa.text("SELECT status FROM audio_jobs WHERE job_id='existing-audio-job'")
                )
                == "completed"
            )
