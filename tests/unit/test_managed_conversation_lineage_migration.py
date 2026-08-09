import asyncio
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine


@contextmanager
def _preserve_logging_state() -> Iterator[None]:
    """Isolate Alembic's fileConfig changes from the surrounding test process."""

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


def test_lineage_migration_backfills_legacy_links() -> None:
    migration = (
        Path(__file__).parents[2]
        / "cognis/store/migrations/versions/077_managed_conversation_lineage.py"
    ).read_text()

    assert 'sa.Column("parent_link_id", sa.String(), nullable=True)' in migration
    assert 'sa.Column("root_link_id", sa.String(), nullable=True)' in migration
    assert 'sa.Column("depth", sa.Integer(), nullable=False, server_default="1")' in migration
    assert "SET root_link_id = link_id, depth = 1" in migration
    assert "fk_managed_links_parent_link" in migration
    assert "fk_managed_links_root_link" in migration
    assert "ix_managed_conversation_links_root_depth" in migration


def test_migration_graph_has_single_linear_head() -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["120_schedule_fire_kinds"]
    revisions = list(script.walk_revisions("base", "120_schedule_fire_kinds"))
    assert [revision.revision for revision in revisions[:17]] == [
        "120_schedule_fire_kinds",
        "119_work_scope_revisions",
        "118_channel_delivery_receipts",
        "117_group_context",
        "116_managed_resume_prepared",
        "115_managed_channel_resume",
        "114_managed_channel_fences",
        "113_managed_channel_lifecycle",
        "112_channel_observed_targets",
        "111_managed_channel_foundation",
        "110_conversation_lineage",
        "109_task_control_conversation",
        "108_kb_active_metadata",
        "107_knowledgebase_grants",
        "106_kb_index_lifecycle",
        "105_managed_join_handoffs",
        "104_channel_direct_turn_delivery",
    ]
    assert all(len(revision.revision) <= 32 for revision in revisions[:16])


def test_artifact_source_migration_backfills_legacy_identity() -> None:
    migration = (
        Path(__file__).parents[2]
        / "cognis/store/migrations/versions/094_artifact_tool_source_id.py"
    ).read_text()

    assert 'artifacts.c.purpose == "tool_artifact"' in migration
    assert "source_tool_call_id=artifacts.c.conversation_id" in migration
    assert "source_anchor=artifacts.c.session_id" in migration
    assert 'artifacts.c.purpose == "tool_output"' in migration
    assert "source_tool_call_id = filename_text[:-4].strip()" in migration
    assert "source_tool_call_id=source_tool_call_id" in migration


def test_revision_chain_fits_postgresql_alembic_version() -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    script = ScriptDirectory.from_config(config)
    migration_context = MigrationContext.configure(dialect_name="postgresql")
    version_table = migration_context.impl.version_table_impl(
        version_table="alembic_version",
        version_table_schema=None,
        version_table_pk=True,
    )
    version_column = version_table.c.version_num
    chain = [
        "079_delegate_lineage",
        "080_managed_turn_correlation",
        "081_mcp_oauth_refresh_lifecycle",
        "082_sys_agent_profile_overrides",
        "083_follow_up_leases",
        "084_channel_delivery_progress",
        "085_channel_delivery_inflight",
        "086_channel_delivery_attachments",
        "087_local_model_foundation",
        "088_local_model_runtime",
        "089_local_model_capacity_bigint",
        "090_local_model_provider_domain",
        "091_channel_default_profile",
        "092_local_model_byte_bigint",
        "093_canonical_chart_payloads",
        "094_artifact_tool_source_id",
        "095_channel_delivery_dlv_id",
        "096_coordination_leases",
        "097_direct_turn_requests",
        "098_schedule_fires",
        "099_controller_instances",
        "100_mcp_oauth_terminal_cleanup",
        "101_mcp_oauth_cleanup_dispatch",
    ]

    assert version_column.type.length == 32
    for index, revision_id in enumerate(chain):
        revision = script.get_revision(revision_id)
        assert revision is not None
        assert revision.down_revision == (chain[index - 1] if index else "078_follow_up_intents")
        assert len(revision_id) <= version_column.type.length
        statement = (
            version_table.insert()
            .values(version_num=revision_id)
            .compile(
                dialect=migration_context.dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        assert revision_id in str(statement)


def test_lineage_migration_upgrade_downgrade_sqlite(tmp_path: Path) -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{tmp_path / 'lineage-migration.db'}",
    )
    with _preserve_logging_state():
        command.upgrade(config, "094_artifact_tool_source_id")
        command.downgrade(config, "076_repair_legacy_deliverable_content_nullable")
        command.upgrade(config, "094_artifact_tool_source_id")


def test_managed_join_handoff_migration_upgrades_104_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "managed-join-handoffs.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")

    with _preserve_logging_state():
        command.upgrade(config, "104_channel_direct_turn_delivery")
        command.upgrade(config, "105_managed_join_handoffs")
        command.upgrade(config, "105_managed_join_handoffs")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        inspector = sa.inspect(sync_engine)
        columns = {column["name"] for column in inspector.get_columns("managed_conversation_links")}
        indexes = {index["name"] for index in inspector.get_indexes("managed_conversation_links")}
        assert {
            "handoff_state",
            "handoff_target_turn_id",
            "handoff_controller_session_id",
            "handoff_controller_turn_id",
            "handoff_tool_call_id",
        }.issubset(columns)
        assert "ix_managed_conversation_links_handoff_owner" in indexes
    finally:
        sync_engine.dispose()


def test_artifact_source_identity_094_upgrades_093_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-source-093.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    with _preserve_logging_state():
        command.upgrade(config, "093_canonical_chart_payloads")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, namespace, object_id, filename, owner_email,
                        purpose, kind, mime_type, size_bytes, status,
                        conversation_id, session_id, created_at
                    ) VALUES
                        ('legacy_artifact', 'attachments', 'legacy_artifact',
                         'image.jpg', 'user@example.com', 'tool_artifact',
                         'image', 'image/jpeg', 10, 'attached',
                         'call-artifact', 'media:1', CURRENT_TIMESTAMP),
                        ('legacy_output', 'tool-outputs', 'legacy_output',
                         'call-output.txt', 'user@example.com', 'tool_output',
                         'file', 'text/plain', 10, 'temporary',
                         'conversation-1', 'session-1', CURRENT_TIMESTAMP),
                        ('malformed_output', 'tool-outputs', 'malformed_output',
                         '.txt', 'user@example.com', 'tool_output',
                         'file', 'text/plain', 10, 'temporary',
                         'conversation-1', 'session-1', CURRENT_TIMESTAMP)
                    """
                )
            )
    finally:
        sync_engine.dispose()

    with _preserve_logging_state():
        command.upgrade(config, "094_artifact_tool_source_id")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.connect() as connection:
            rows = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    sa.text(
                        "SELECT artifact_id, source_tool_call_id, source_anchor "
                        "FROM artifacts WHERE artifact_id IN "
                        "('legacy_artifact', 'legacy_output', 'malformed_output')"
                    )
                )
            }
        indexes = {index["name"] for index in sa.inspect(sync_engine).get_indexes("artifacts")}
        assert rows["legacy_artifact"] == ("call-artifact", "media:1")
        assert rows["legacy_output"] == ("call-output", None)
        assert rows["malformed_output"] == (None, None)
        assert "ix_artifacts_tool_source" in indexes
    finally:
        sync_engine.dispose()


def test_lineage_migration_accepts_bootstrap_upgraded_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "bootstrap-then-migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_engine(database_url)
    asyncio.run(run_schema_bootstrap(engine))
    asyncio.run(engine.dispose())

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    with sync_engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO artifacts (
                    artifact_id, namespace, object_id, filename, owner_email,
                    purpose, kind, mime_type, size_bytes, status,
                    conversation_id, session_id, source_tool_call_id,
                    source_anchor, created_at, updated_at
                ) VALUES
                    ('bootstrap_legacy', 'attachments', 'bootstrap_legacy',
                     'image.jpg', 'user@example.com', 'tool_artifact',
                     'image', 'image/jpeg', 10, 'attached',
                     'legacy-call', 'legacy-anchor', NULL, NULL,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('bootstrap_preserved', 'attachments', 'bootstrap_preserved',
                     'image.jpg', 'user@example.com', 'tool_artifact',
                     'image', 'image/jpeg', 10, 'attached',
                     'legacy-call-2', 'legacy-anchor-2',
                     'current-call', 'current-anchor',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
    sync_engine.dispose()
    engine = create_engine(database_url)
    asyncio.run(run_schema_bootstrap(engine))
    asyncio.run(engine.dispose())

    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    with _preserve_logging_state():
        command.stamp(config, "076_repair_legacy_deliverable_content_nullable")
        command.upgrade(config, "094_artifact_tool_source_id")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.connect() as connection:
            rows = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    sa.text(
                        "SELECT artifact_id, source_tool_call_id, source_anchor "
                        "FROM artifacts WHERE artifact_id IN "
                        "('bootstrap_legacy', 'bootstrap_preserved')"
                    )
                )
            }
        assert rows["bootstrap_legacy"] == ("legacy-call", "legacy-anchor")
        assert rows["bootstrap_preserved"] == ("current-call", "current-anchor")
        assert "ix_artifacts_tool_source" in {
            index["name"] for index in sa.inspect(sync_engine).get_indexes("artifacts")
        }
    finally:
        sync_engine.dispose()


def test_legacy_long_profile_revision_is_normalized_before_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-profile-revision.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    with _preserve_logging_state():
        command.upgrade(config, "081_mcp_oauth_refresh_lifecycle")

        sync_engine = sa.create_engine(f"sqlite:///{database_path}")
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE alembic_version SET version_num = '082_system_agent_profile_overrides'"
                )
            )
        sync_engine.dispose()

        command.upgrade(config, "094_artifact_tool_source_id")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    with sync_engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        columns = {
            row["name"] for row in sa.inspect(connection).get_columns("system_agent_overrides")
        }
    sync_engine.dispose()

    assert version == "094_artifact_tool_source_id"
    assert {
        "agent_profiles_override",
        "default_agent_profile_id_override",
    } <= columns


@pytest.mark.parametrize(
    "legacy_revision",
    [
        "083_local_model_foundation",
        "084_local_model_runtime",
        "085_local_model_capacity_bigint",
    ],
)
def test_legacy_local_model_revision_is_normalized_before_upgrade(
    tmp_path: Path,
    legacy_revision: str,
) -> None:
    database_path = tmp_path / f"{legacy_revision}.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    with _preserve_logging_state():
        command.upgrade(config, "082_sys_agent_profile_overrides")
        engine = create_engine(database_url)
        asyncio.run(run_schema_bootstrap(engine))
        asyncio.run(engine.dispose())

        sync_engine = sa.create_engine(f"sqlite:///{database_path}")
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :legacy_revision"),
                {"legacy_revision": legacy_revision},
            )
        sync_engine.dispose()

        command.upgrade(config, "094_artifact_tool_source_id")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    with sync_engine.connect() as connection:
        version = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    sync_engine.dispose()

    assert version == "094_artifact_tool_source_id"


def test_artifact_source_identity_094_postgresql_when_available() -> None:
    database_url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip(
            "COGNIS_TEST_POSTGRES_URL is not set; PostgreSQL artifact migration unavailable"
        )
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    artifacts = sa.Table(
        "artifacts",
        metadata,
        sa.Column("artifact_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
    )
    test_schema_created = False
    try:
        existing_tables = set(sa.inspect(engine).get_table_names())
        if existing_tables:
            pytest.fail(
                "COGNIS_TEST_POSTGRES_URL must reference an empty isolated database; "
                f"found tables {sorted(existing_tables)}"
            )
        metadata.create_all(engine)
        test_schema_created = True
        with engine.begin() as connection:
            connection.execute(
                artifacts.insert(),
                [
                    {
                        "artifact_id": "legacy_artifact",
                        "owner_email": "user@example.com",
                        "filename": "image.jpg",
                        "purpose": "tool_artifact",
                        "conversation_id": "call-artifact",
                        "session_id": "media:1",
                    },
                    {
                        "artifact_id": "legacy_output",
                        "owner_email": "user@example.com",
                        "filename": "call-output.txt",
                        "purpose": "tool_output",
                        "conversation_id": "conversation-1",
                        "session_id": "session-1",
                    },
                ],
            )
        async_database_url = database_url
        if async_database_url.startswith("postgresql+psycopg://"):
            async_database_url = async_database_url.replace(
                "postgresql+psycopg://", "postgresql+asyncpg://", 1
            )
        elif async_database_url.startswith("postgresql://"):
            async_database_url = async_database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        async_engine = create_engine(async_database_url)
        asyncio.run(run_schema_bootstrap(async_engine))
        asyncio.run(run_schema_bootstrap(async_engine))
        asyncio.run(async_engine.dispose())
        reflected = sa.Table("artifacts", sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = {
                row.artifact_id: (row.source_tool_call_id, row.source_anchor)
                for row in connection.execute(sa.select(reflected))
            }
        assert rows["legacy_artifact"] == ("call-artifact", "media:1")
        assert rows["legacy_output"] == ("call-output", None)
        assert "ix_artifacts_tool_source" in {
            index["name"] for index in sa.inspect(engine).get_indexes("artifacts")
        }
    finally:
        if test_schema_created:
            table_names = sa.inspect(engine).get_table_names()
            quote = engine.dialect.identifier_preparer.quote
            with engine.begin() as connection:
                for table_name in table_names:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {quote(table_name)} CASCADE"))
        engine.dispose()
