from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from cognis.store.models import (
    LocalModelDeployment,
    LocalModelOperation,
    LocalModelTargetStatus,
)

_BYTE_COUNTER_MIGRATION = "cognis.store.migrations.versions.092_local_model_byte_bigint"


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


def test_local_model_migration_upgrade_and_downgrade_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "local-model-migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    with _preserve_logging_state():
        command.upgrade(config, "head")

    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(sync_engine)
        assert {
            "local_model_deployments",
            "local_model_target_statuses",
            "local_model_operations",
        }.issubset(inspector.get_table_names())
        deployment_foreign_keys = {
            (tuple(foreign_key["constrained_columns"]), foreign_key["options"].get("ondelete"))
            for foreign_key in inspector.get_foreign_keys("local_model_deployments")
        }
        assert (("provider_id",), "SET NULL") in deployment_foreign_keys
        capacity_column = next(
            column
            for column in inspector.get_columns("local_model_deployments")
            if column["name"] == "capacity_assessment_generation"
        )
        assert isinstance(capacity_column["type"], BigInteger)
        assert {
            index["name"] for index in inspector.get_indexes("local_model_target_statuses")
        } >= {
            "ix_local_model_targets_deployment_state",
            "ix_local_model_targets_executor_state",
        }
        operation_columns = {
            column["name"]: column for column in inspector.get_columns("local_model_operations")
        }
        assert "post_pull_provider_upsert" in operation_columns
        assert isinstance(operation_columns["progress_bytes"]["type"], BigInteger)
        assert operation_columns["progress_bytes"]["nullable"] is False
        assert operation_columns["progress_bytes"]["default"] is not None
        target_columns = {
            column["name"]: column
            for column in inspector.get_columns("local_model_target_statuses")
        }
        assert isinstance(target_columns["observed_size_bytes"]["type"], BigInteger)
        assert target_columns["observed_size_bytes"]["nullable"] is True
        operation_checks = {
            check["name"] for check in inspector.get_check_constraints("local_model_operations")
        }
        target_checks = {
            check["name"]
            for check in inspector.get_check_constraints("local_model_target_statuses")
        }
        assert "ck_local_model_operation_progress_bytes" in operation_checks
        assert "ck_local_model_target_observed_size" in target_checks
        provider_columns = {column["name"] for column in inspector.get_columns("llm_providers")}
        assert "managed_local_key" in provider_columns
        assert {index["name"] for index in inspector.get_indexes("llm_providers")} >= {
            "uq_llm_providers_managed_local_key"
        }
    finally:
        sync_engine.dispose()

    with _preserve_logging_state():
        command.downgrade(config, "079_delegate_lineage")

    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        assert "local_model_deployments" not in inspect(sync_engine).get_table_names()
    finally:
        sync_engine.dispose()

    with _preserve_logging_state():
        command.upgrade(config, "head")


def test_local_model_migration_is_the_single_head() -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["120_schedule_fire_kinds"]
    assert script.get_revision("120_schedule_fire_kinds").down_revision == (
        "119_work_scope_revisions"
    )
    assert script.get_revision("119_work_scope_revisions").down_revision == (
        "118_channel_delivery_receipts"
    )
    assert script.get_revision("118_channel_delivery_receipts").down_revision == (
        "117_group_context"
    )
    assert script.get_revision("117_group_context").down_revision == ("116_managed_resume_prepared")
    assert script.get_revision("116_managed_resume_prepared").down_revision == (
        "115_managed_channel_resume"
    )
    assert script.get_revision("115_managed_channel_resume").down_revision == (
        "114_managed_channel_fences"
    )
    assert script.get_revision("114_managed_channel_fences").down_revision == (
        "113_managed_channel_lifecycle"
    )
    assert script.get_revision("113_managed_channel_lifecycle").down_revision == (
        "112_channel_observed_targets"
    )
    assert script.get_revision("112_channel_observed_targets").down_revision == (
        "111_managed_channel_foundation"
    )
    assert script.get_revision("111_managed_channel_foundation").down_revision == (
        "110_conversation_lineage"
    )
    assert script.get_revision("110_conversation_lineage").down_revision == (
        "109_task_control_conversation"
    )
    assert script.get_revision("109_task_control_conversation").down_revision == (
        "108_kb_active_metadata"
    )
    assert script.get_revision("094_artifact_tool_source_id").down_revision == (
        "093_canonical_chart_payloads"
    )
    assert script.get_revision("093_canonical_chart_payloads").down_revision == (
        "092_local_model_byte_bigint"
    )
    assert script.get_revision("092_local_model_byte_bigint").down_revision == (
        "091_channel_default_profile"
    )
    assert script.get_revision("091_channel_default_profile").down_revision == (
        "090_local_model_provider_domain"
    )
    assert script.get_revision("090_local_model_provider_domain").down_revision == (
        "089_local_model_capacity_bigint"
    )
    assert script.get_revision("089_local_model_capacity_bigint").down_revision == (
        "088_local_model_runtime"
    )
    assert script.get_revision("088_local_model_runtime").down_revision == (
        "087_local_model_foundation"
    )
    assert script.get_revision("087_local_model_foundation").down_revision == (
        "086_channel_delivery_attachments"
    )
    assert all(len(revision) <= 32 for revision in script.get_heads())


def test_local_model_capacity_generation_compiles_as_postgresql_bigint() -> None:
    ddl = str(CreateTable(LocalModelDeployment.__table__).compile(dialect=postgresql.dialect()))

    assert "capacity_assessment_generation BIGINT" in ddl


def test_local_model_byte_counters_compile_as_postgresql_bigint() -> None:
    operation_ddl = str(
        CreateTable(LocalModelOperation.__table__).compile(dialect=postgresql.dialect())
    )
    target_ddl = str(
        CreateTable(LocalModelTargetStatus.__table__).compile(dialect=postgresql.dialect())
    )

    assert "progress_bytes BIGINT" in operation_ddl
    assert "observed_size_bytes BIGINT" in target_ddl


def test_local_model_byte_counter_upgrade_from_previous_sqlite_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "local-model-byte-upgrade.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")

    with _preserve_logging_state():
        command.upgrade(config, "091_channel_default_profile")
    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(sync_engine)
        operation_type = next(
            column["type"]
            for column in inspector.get_columns("local_model_operations")
            if column["name"] == "progress_bytes"
        )
        target_type = next(
            column["type"]
            for column in inspector.get_columns("local_model_target_statuses")
            if column["name"] == "observed_size_bytes"
        )
        assert not isinstance(operation_type, BigInteger)
        assert not isinstance(target_type, BigInteger)
    finally:
        sync_engine.dispose()

    with _preserve_logging_state():
        command.upgrade(config, "head")
        command.upgrade(config, "head")
    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(sync_engine)
        assert isinstance(
            next(
                column["type"]
                for column in inspector.get_columns("local_model_operations")
                if column["name"] == "progress_bytes"
            ),
            BigInteger,
        )
        assert isinstance(
            next(
                column["type"]
                for column in inspector.get_columns("local_model_target_statuses")
                if column["name"] == "observed_size_bytes"
            ),
            BigInteger,
        )
    finally:
        sync_engine.dispose()


def test_local_model_byte_counter_downgrade_refuses_oversized_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-model-byte-downgrade.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    with _preserve_logging_state():
        command.upgrade(config, "head")

    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO local_model_target_statuses ("
                    "target_id, deployment_id, executor_id, generation, observed_generation, "
                    "state, observed_size_bytes, created_at, updated_at"
                    ") VALUES ("
                    "'oversized', 'missing-deployment', 'missing-executor', 1, 1, "
                    "'ready', 5629109111, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        sync_engine.dispose()

    with (
        _preserve_logging_state(),
        pytest.raises(
            RuntimeError,
            match="stored values exceed signed int32 maximum",
        ),
    ):
        command.downgrade(config, "091_channel_default_profile")
    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(sync_engine)
        assert isinstance(
            next(
                column["type"]
                for column in inspector.get_columns("local_model_operations")
                if column["name"] == "progress_bytes"
            ),
            BigInteger,
        )
        assert isinstance(
            next(
                column["type"]
                for column in inspector.get_columns("local_model_target_statuses")
                if column["name"] == "observed_size_bytes"
            ),
            BigInteger,
        )
    finally:
        sync_engine.dispose()


def test_local_model_byte_counter_postgresql_type_migration() -> None:
    database_url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip(
            "COGNIS_TEST_POSTGRES_URL is not set; PostgreSQL byte-counter validation unavailable"
        )
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    operations = sa.Table(
        "local_model_operations",
        metadata,
        sa.Column(
            "progress_bytes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "progress_bytes >= 0",
            name="ck_local_model_operation_progress_bytes",
        ),
    )
    targets = sa.Table(
        "local_model_target_statuses",
        metadata,
        sa.Column("observed_size_bytes", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_local_model_target_observed_size",
        ),
    )
    module = importlib.import_module(_BYTE_COUNTER_MIGRATION)
    tables_created = False
    try:
        existing_tables = set(inspect(engine).get_table_names())
        conflicting_tables = {operations.name, targets.name}.intersection(existing_tables)
        if conflicting_tables:
            pytest.fail(
                "COGNIS_TEST_POSTGRES_URL must reference an isolated database without "
                f"local-model tables; found {sorted(conflicting_tables)}"
            )
        metadata.create_all(engine)
        tables_created = True
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            original_op = module.op
            module.op = Operations(context)
            try:
                module.upgrade()
                module.upgrade()
            finally:
                module.op = original_op
        inspector = inspect(engine)
        operation_column = inspector.get_columns(operations.name)[0]
        target_column = inspector.get_columns(targets.name)[0]
        assert isinstance(operation_column["type"], BigInteger)
        assert operation_column["nullable"] is False
        assert operation_column["default"] is not None
        assert isinstance(target_column["type"], BigInteger)
        assert target_column["nullable"] is True
    finally:
        if tables_created:
            metadata.drop_all(engine)
        engine.dispose()
